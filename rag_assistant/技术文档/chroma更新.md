# ChromaDB 向量库日常更新计划

## 一、现状分析

当前 `vector_store.py` 仅支持全量重建：

```python
build_vector_store(docs)  → delete_collection() → from_documents()  # 全删全建
```

**核心问题**：新增一个文档 = 清空整个库 → 重新解析所有文档 → 重新向量化。没有增量更新能力。

---

## 二、目标能力矩阵

| 操作 | 场景 | 当前 | 计划 |
|------|------|------|------|
| 全量构建 | 首次使用/全部重来 | ✅ 已支持 | 保留 |
| **增量添加** | 新增一个文档，不动已有的 | ❌ | ✅ 新增 |
| **删除文档** | 下架某份过时文件 | ❌ | ✅ 新增 |
| **更新文档** | 文档内容修改后刷新 | ❌ | ✅ 新增（先写新后删旧） |
| **查看清单** | 库里有哪些文档、多少 chunk | ❌ | ✅ 新增（读 db_meta.json） |
| **去重检测** | 同一文件重复上传时跳过 | ❌ | ✅ 新增（路径+哈希双重校验） |
| **状态检查** | 库是否就绪、总数统计 | ⚠️ 仅文件存在判断 | ✅ 增强 |

---

## 三、核心风险与修复（6 项）

以下都是工程落地大概率会踩的坑，其中前两个属于逻辑级隐患，上线前必须修复。

### 风险 1：文档唯一标识有缺陷，存在误删/混同风险 ⚠️ 最严重

**问题**：删除、去重都以纯文件名 `source = os.path.basename(file_path)` 为唯一依据。如果存在不同目录下的同名文件（比如两个项目都有 `报告.docx`），删除其中一个会误删另一个的所有 chunk；同名但内容修改的文件，也会被去重逻辑跳过，导致更新不生效。

**修复**：
- 将 `source` 字段从纯文件名改为**相对路径**（如 `uploaded_docs/项目A/报告.docx`），保证全局唯一，删除、过滤都用相对路径匹配
- 去重逻辑增加**文件内容哈希（MD5）**校验，在 metadata 中新增 `file_hash` 字段
  - 路径相同 + 哈希相同 → 真重复，跳过
  - 路径相同 + 哈希不同 → 内容已更新，自动走更新逻辑
  - 路径不同 → 不同文档，不冲突

**收益**：彻底解决同名文件混同、内容变更检测不到的问题，去重和删除的准确性从"可用"升级为"可靠"。

### 风险 2：更新操作非原子，存在数据丢失风险 ⚠️ 逻辑级

**问题**：`update_document` 的逻辑是「先删旧版本 → 再添加新版本」。如果删除成功后，解析/向量化过程中报错（比如文件损坏、嵌入模型超时、进程中断），该文档就会从库中彻底消失，没有兜底。

**修复**：改为**「先写新 → 后删旧」的安全更新模式**：
1. 解析生成新版本的 chunks，完整写入向量库
2. 确认全部写入成功后，再删除旧版本的 chunks
3. 如果写入失败，不执行删除，旧版本数据完全保留

**说明**：短暂的双版本共存对检索影响极小（只是多了几份旧 chunk），但彻底避免了数据丢失的风险，对于知识库场景完全可接受。

### 风险 3：写入无事务，失败易产生脏数据

**问题**：添加文档时是批量写入 chunk 的。如果中途报错（比如嵌入模型接口超时、程序意外退出），已经写入的部分 chunk 会残留在库里，形成"半份文档"的脏数据，后续检索会出现不完整的片段。

**修复**：增加失败回滚逻辑：
- 写入前记录当前文档的唯一标识（相对路径）
- 捕获到任何异常时，自动调用 `remove_document` 清理该文档已写入的所有 chunk
- 保证"要么全成功，要么全不写"，避免脏数据污染

### 风险 4：ChromaDB 并发写入冲突

**问题**：ChromaDB 的 `PersistentClient` 原生不支持多进程并发写入，会触发文件锁冲突。Streamlit 是多会话架构，如果两个用户同时上传文档，大概率会抛出文件锁异常。

**修复**：
- 引入 `filelock` 轻量库，在所有写入操作（增、删、改）前加全局文件锁，操作完成后释放
- 轻量处理也可以：捕获锁冲突异常，前端提示"知识库正在操作，请稍后再试"，适配单用户为主的场景

### 风险 5：全量扫描的性能隐患

**问题**：`list_documents`、`is_duplicate` 都依赖 `col.get(where=...)` 全量扫描元数据。当 chunk 数量超过 1 万条后，查询延迟会明显上升，每次操作都扫全库效率不高。

**修复**：
- 维护一个独立的轻量元数据文件 `db_meta.json`，记录文档列表、chunk 数量、文件哈希、入库时间等信息：

```json
{
  "documents": {
    "uploaded_docs/报告.docx": {
      "file_hash": "a1b2c3d4e5f6...",
      "chunks": 42,
      "added_at": "2026-07-04T10:30:00",
      "updated_at": "2026-07-04T15:00:00"
    },
    "uploaded_docs/笔记.txt": {
      "file_hash": "f6e5d4c3b2a1...",
      "chunks": 18,
      "added_at": "2026-07-03T09:00:00",
      "updated_at": null
    }
  },
  "total_chunks": 132
}
```

- 增删改操作同步更新这个文件，查询清单、判断重复直接读文件，不用扫向量库，性能提升一个数量级

### 风险 6：历史数据的元数据兼容性

**问题**：现有老 chunk 没有 `file_path`、`added_at`、`file_hash` 这些新字段，在 `list_documents` 统计、按路径过滤时可能出现 `KeyError`。

**修复**：
- 所有读取 metadata 的地方都加默认值兜底，比如 `chunk.metadata.get("file_path", "")`，兼容历史数据
- 提供一键迁移命令 `kb_manager.py migrate`，给老数据补全默认元数据

---

## 四、补充优化（锦上添花，按需实现）

| # | 优化 | 说明 |
|---|------|------|
| 1 | **批量写入提速** | 调用 Chroma 的 `add` 方法时，一次性传入所有 chunk 的 `ids`、`documents`、`metadatas`，批量写入比循环单条写入速度快 3~5 倍 |
| 2 | **对接上游文档缓存** | 和已有的「文档级解析结果缓存」联动，添加文档时先查缓存，命中的话直接用缓存的分块结果，不用重新解析，进一步提升添加速度 |
| 3 | **批量目录添加增强** | `add-dir` 命令支持递归遍历、过滤指定后缀（只处理 pdf/docx/txt）、自动跳过已存在的文件，批量导入更方便 |
| 4 | **软删除可选** | 删除文档时先标记 `is_deleted=true`，不物理删除，后续可恢复；定期做物理清理，适合需要操作留痕的场景 |

---

## 四-B、剩余优化空间（边缘场景增强，5 分）

以下不影响核心功能，但极端场景能提升稳定性，属于锦上添花，可按需实现。

### 1. 稳定 Chunk ID + 幂等写入，进一步杜绝重复

**问题**：当前方案依赖「先判断重复、再写入」的逻辑，极端并发下仍可能出现重复写入。

**优化**：为每个 chunk 生成稳定的唯一 ID，规则为 `文件MD5_分块序号`（例如 `a1b2c3d4_001`、`a1b2c3d4_002`）：

```python
def _make_chunk_id(file_hash: str, index: int) -> str:
    return f"{file_hash}_{index:04d}"

# 写入时使用自定义 ID
col.add(ids=[_make_chunk_id(hash, i) for i in range(len(chunks))], ...)
```

**收益**：
- 实现天然幂等 — 同一份文档重复添加，chunk ID 完全一致，Chroma 会自动覆盖，不会产生重复数据
- 更新文档时，可以精准对比新旧 chunk 的差异，只变更新的部分，不用整份删除重写，小修改场景下效率更高

### 2. 双源数据一致性校验，避免元数据与向量库脱节

**问题**：当前流程是「先操作向量库 → 再更新 meta 文件」，如果向量库操作成功、写 meta 时意外崩溃，会出现向量库有数据、meta 里没记录的不一致状态。

**优化**：增加 `repair` 校验命令：

```python
def repair():
    """遍历 Chroma 中所有 chunk 的元数据，和 db_meta.json 做对比
    1. 自动补全缺失的文档条目（Chroma 有、meta 没有 → 从 Chroma 重建 meta 条目）
    2. 清理无效的残留数据（meta 有、Chroma 没有 → 从 meta 移除已不存在条目）
    3. 校验文档 chunk 数量一致性
    4. 输出修复报告
    """
```

程序启动时做一次轻量校验，不一致则给出提示，保证日常使用中两边数据始终对齐。

### 3. 删除操作增加历史数据兜底

**问题**：如果用户没有执行 `migrate` 命令，老库的 chunk 只有 `source`（纯文件名）字段，没有 `file_path`，直接按相对路径删除会返回 0 条、删不掉。

**优化**：删除逻辑加一层兜底：

```python
def remove_document(file_path: str) -> dict:
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
    col = client.get_collection("langchain")

    # 1. 优先按精准的相对路径匹配
    results = col.get(where={"file_path": file_path})

    # 2. 没匹配到的话，兜底按纯文件名匹配老数据
    if not results['ids']:
        basename = os.path.basename(file_path)
        results = col.get(where={"source": basename})

    ids = results['ids']
    if ids:
        col.delete(ids=ids)
    return {"file_path": file_path, "chunks_removed": len(ids)}
```

兼容未迁移的历史库，不会出现「明明有数据却删不掉」的问题。

### 4. 嵌入模型版本校验，避免维度不匹配报错

**问题**：如果后续更换嵌入模型（如从 384 维换成 768 维），增量添加的向量维度和老库不一致，会直接导致检索报错，但用户很难第一时间定位原因。

**优化**：在 `db_meta.json` 中新增 `embedding_model` 和 `embedding_dim` 字段，首次建库时写入；每次增量添加前校验模型是否一致，不一致则直接提示「嵌入模型已变更，请全量重建知识库」，避免静默出错。

```json
{
  "embedding_model": "paraphrase-multilingual-MiniLM-L12-v2",
  "embedding_dim": 384,
  "documents": { ... },
  "total_chunks": 132
}
```

### 5. 批量目录添加的失败策略可配置

**问题**：`add-dir` 批量导入时，如果中间某个文件解析失败，默认是停止还是继续处理剩余文件，不同场景需求不同。

**优化**：增加参数控制：

```bash
# 默认：遇到错误立即停止，回滚已成功添加的文档，保证批次完整性
python kb_manager.py add-dir uploaded_docs/ --stop-on-error

# 跳过失败文件，继续处理剩余文件（适合大批量导入、容忍少量失败的场景）
python kb_manager.py add-dir uploaded_docs/ --continue-on-error
```

### 6. 操作级快照备份，降低误操作风险

**问题**：`repair` 只能修复数据不一致，没法恢复误删的文档。对于个人/小团队场景，误删是比性能下降更常见的故障。

**优化**：为破坏性操作增加轻量快照备份：

```
执行 remove / update / clear 前
  ↓
自动备份 db_meta.json → db_meta.{timestamp}.bak
记录操作前的 chunk ID 列表 → snapshot_{timestamp}.json
  ↓
执行操作
  ↓
万一误操作 → kb_manager.py rollback 一键恢复
```

```python
def _backup_snapshot(operation: str) -> str:
    """破坏性操作前自动创建快照
    1. 复制 db_meta.json → db_meta.{timestamp}.bak
    2. 记录当前所有 chunk ID → snapshot_{timestamp}.json
    Returns: 快照时间戳（用于 rollback）
    """

def rollback(timestamp: str = None) -> dict:
    """回退到指定快照状态（默认最近一次）
    1. 从 snapshot_{timestamp}.json 读取操作前的 chunk ID 列表
    2. 对比当前 Chroma 中的 chunk ID：
       - 快照有、当前没有 → 从备份的 db_meta.json 中找到对应文档 → 提示重新添加
    3. 恢复 db_meta.json 到快照版本
    Returns: {"restored_documents": int, "lost_chunks": int, "note": str}
    """
```

配套 `kb_manager.py rollback` 命令：
```bash
python kb_manager.py rollback            # 回退到最近一次操作前
python kb_manager.py rollback --list     # 列出所有可用快照
python kb_manager.py rollback 20260704_153000  # 回退到指定快照
```

快照文件保留最近 10 个（可配置），超出自动清理，不占用过多磁盘。

**收益**：误删文档可一键恢复，`repair` 修数据一致性 + `rollback` 修人为失误，两者互补覆盖完整故障面。

### 额外联动优化（与现有架构呼应）

| # | 优化 | 说明 |
|---|------|------|
| 1 | **对接文档解析缓存** | `add_document` 时先查本地的文档解析结果缓存（基于文件哈希），命中则直接复用分块结果，跳过解析、分块步骤，添加速度提升数倍 |
| 2 | **入库时间透传** | `added_at` 字段在检索返回时一并展示，用户能直观看到文档的入库时间，提升可追溯性 |
| 3 | **软删除作为可选开关** | 默认物理删除，配置项 `SOFT_DELETE=true` 开启后改为标记 `is_deleted=true`，检索时自动过滤，适合需要操作留痕、可回滚的场景 |

---

## 五、技术方案

### 5.1 ChromaDB metadata 字段设计

```python
# 每个 chunk 的 metadata
{
    "source": "上传的原始文件名.docx",           # 保留（兼容旧逻辑）
    "file_path": "uploaded_docs/报告.docx",     # *新增：相对路径，唯一标识
    "file_hash": "a1b2c3d4e5f6...",            # *新增：文件 MD5（8KB 分块读取）
    "added_at": "2026-07-04T10:30:00",          # *新增：入库时间
    "page_label": "第1页",
    "h1": "一、概述",
    # ... 其他已有字段
}

# db_meta.json 结构
{
    "embedding_model": "paraphrase-multilingual-MiniLM-L12-v2",  # *新增：校验维度匹配
    "embedding_dim": 384,                                        # *新增：防模型变更报错
    "documents": {
        "uploaded_docs/报告.docx": {
            "file_hash": "a1b2c3d4e5f6...",
            "chunks": 42,
            "added_at": "2026-07-04T10:30:00",
            "updated_at": "2026-07-04T15:00:00"
        }
    },
    "total_chunks": 132
}
```

### 5.2 核心函数设计

```python
# ── 工具函数 ──
def _compute_hash(file_path: str) -> str:
    """计算文件 MD5（8KB 分块读取，兼容大文件）"""

def _load_meta() -> dict:
    """加载 db_meta.json，不存在返回空结构"""

def _save_meta(meta: dict):
    """原子写入（先写 .tmp 临时文件，再 os.replace 原子替换）"""

def _get_lock() -> FileLock:
    """获取全局文件锁"""

def _make_chunk_id(file_hash: str, index: int) -> str:
    """生成稳定唯一 chunk ID：文件MD5_分块序号 → 天然幂等"""
    return f"{file_hash}_{index:04d}"

def _validate_model() -> bool:
    """校验当前嵌入模型与建库时一致，不一致提示全量重建"""

# ── 增量添加 ──
def add_document(file_path: str, skip_duplicate: bool = True) -> dict:
    """
    安全添加流程：
    1. 校验嵌入模型版本（不一致 → 抛异常提示全量重建）
    2. 获取文件锁
    3. 计算文件 MD5，查 db_meta.json 判断真重复
       - 路径+哈希一致 → {"filename": ..., "chunks_added": 0, "skipped": True}
       - 路径相同+哈希不同 → 自动走 update_document
    4. 解析 → 切块
    5. 用稳定 ID（fileHash_index）批量写入 Chroma
    6. try/except：异常时回滚（remove_document 清理已写入 chunk）
    7. 更新 db_meta.json
    8. 释放文件锁
    
    Returns: {"file_path": str, "chunks_added": int, "skipped": bool}
    """

# ── 删除文档 ──
def remove_document(file_path: str) -> dict:
    """按 file_path 字段删除某文档的所有 chunks（含历史数据兜底）
    
    流程：
    1. 获取文件锁
    2. 优先按精准相对路径匹配: col.get(where={"file_path": file_path})
    3. 没匹配到 → 兜底按纯文件名匹配老数据: col.get(where={"source": basename})
    4. col.delete(ids) 批量删除
    5. db_meta.json 同步移除条目
    6. 释放文件锁
    
    Returns: {"file_path": str, "chunks_removed": int}
    """

# ── 数据修复 ──
def repair() -> dict:
    """双源一致性校验：Chroma ⇔ db_meta.json 双向校准
    1. Chroma 有、meta 没有 → 从 Chroma 重建 meta 条目
    2. Meta 有、Chroma 没有 → 从 meta 移除已不存在条目
    3. 校验文档 chunk 数量一致性
    Returns: {"added_to_meta": int, "removed_from_meta": int, "chunks_fixed": int}
    """

# ── 快照备份 ──
def _backup_snapshot(operation: str) -> str:
    """破坏性操作前自动创建快照（db_meta.json + chunk ID 列表）
    Returns: 快照时间戳
    """

# ── 快照回退 ──
def rollback(timestamp: str = None) -> dict:
    """回退到指定快照状态（默认最近一次）
    1. 对比快照与当前状态
    2. 恢复 db_meta.json
    3. 输出差异报告（哪些文档可恢复、哪些 chunk 已不可逆）
    Returns: {"restored_documents": int, "lost_chunks": int, "note": str}
    """

# ── 安全更新 ──
def update_document(file_path: str) -> dict:
    """先写新 → 后删旧（安全更新模式）
    
    1. 解析新版本 chunks
    2. 批量写入向量库（带临时标记，避免去重干扰）
    3. 写入成功 → 删除旧版本 chunks（where={"file_path": file_path, "file_hash": old_hash}）
    4. 写入失败 → 不删旧，清理已写入的新 chunk，抛出异常
    5. 更新 db_meta.json
    
    Returns: {"file_path": str, "chunks_removed": int, "chunks_added": int}
    """

# ── 查看清单 ──
def list_documents() -> list[dict]:
    """直接读 db_meta.json，不扫 Chroma
    Returns: [{"file_path": str, "chunks": int, "file_hash": str, "added_at": str}, ...]
    """

# ── 去重检测 ──
def is_duplicate(file_path: str) -> bool:
    """路径 + 哈希双重校验，返回 True 表示真重复"""

# ── 增强状态 ──
def get_status() -> dict:
    """增强版状态：文档数 + chunk 总数 + 嵌入模型 + 存储路径"""
```

### 5.3 ChromaDB 按 metadata 删除（含历史数据兜底）

```python
def remove_document(file_path: str) -> int:
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
    col = client.get_collection("langchain")

    # 1. 优先按精准的相对路径匹配
    results = col.get(where={"file_path": file_path})

    # 2. 没匹配到的话，兜底按纯文件名匹配老数据（未 migrate 的历史库）
    if not results['ids']:
        basename = os.path.basename(file_path)
        results = col.get(where={"source": basename})

    ids = results['ids']
    if ids:
        col.delete(ids=ids)
    return len(ids)
```

### 5.4 命令行工具

新增独立脚本 `kb_manager.py`，不依赖 Streamlit，日常终端操作：

```bash
# 查看知识库状态
python kb_manager.py status

# 查看文档清单
python kb_manager.py list

# 添加文档
python kb_manager.py add uploaded_docs/新文档.pdf

# 删除文档（按相对路径，自动兼容老数据）
python kb_manager.py remove "uploaded_docs/新文档.pdf"

# 更新文档（内容修改后刷新，先写新后删旧）
python kb_manager.py update uploaded_docs/新文档.pdf

# 批量添加整个目录
python kb_manager.py add-dir uploaded_docs/ --recursive
python kb_manager.py add-dir uploaded_docs/ --stop-on-error    # 默认：遇错停止+回滚
python kb_manager.py add-dir uploaded_docs/ --continue-on-error # 跳过失败文件继续

# 清空全部
python kb_manager.py clear

# 迁移老数据（补全 file_path/file_hash/added_at 元数据）
python kb_manager.py migrate

# 双源一致性修复（Chroma ⇔ db_meta.json 双向校准）
python kb_manager.py repair

# 快照回退（误操作后一键恢复）
python kb_manager.py rollback                  # 回退到最近一次操作前
python kb_manager.py rollback --list           # 列出所有可用快照
python kb_manager.py rollback 20260704_153000  # 回退到指定快照
```

---

## 六、改动文件清单

| 文件 | 改动 | 说明 |
|------|------|------|
| `vector_store.py` | 新增 ~230 行 | `add_document`、`remove_document`、`update_document`、`list_documents`、`is_duplicate`、`get_status`、`repair`、`rollback`、`_backup_snapshot`、`_compute_hash`、`_make_chunk_id`、`_load_meta`、`_save_meta`、`_get_lock`、`_validate_model` |
| `kb_manager.py`（新） | ~160 行 | 命令行工具：status/list/add/remove/update/add-dir/clear/migrate/repair/rollback |
| `app.py` | ~30 行 | 侧边栏增加「增量添加 / 全量重建」切换、文档清单展示、删除按钮 |
| `config.py` | +7 行 | `KB_META_FILE`、`KB_LOCK_FILE`、`SOFT_DELETE`、`SNAPSHOT_MAX_COUNT` 配置 |
| `requirements.txt` | +1 行 | `filelock` |

---

## 七、Streamlit 侧边栏改造

```
改前：                                  改后：
┌────────────────────┐                  ┌────────────────────┐
│ 📄 文档上传         │                  │ 📄 文档管理         │
│ [选择文件]          │                  │ [选择文件]          │
│ [🚀 构建知识库]     │                  │ ○ 全量重建（清空旧库）│
│                    │                  │ ● 增量添加（追加）   │
│ 状态: 已就绪 / 未构建│                 │ [🚀 执行]           │
└────────────────────┘                  │                    │
                                        │ 📊 知识库状态       │
                                        │ 文档数: 5           │
                                        │ Chunks: 132         │
                                        │ [📋 查看清单]       │
                                        │ [🗑 管理文档]       │
                                        └────────────────────┘
```

---

## 八、验证方法

1. `python kb_manager.py status` → 确认知识库就绪状态
2. `python kb_manager.py add uploaded_docs/新文档.pdf` → `python kb_manager.py list` → 确认文档数和 chunk 数增加
3. 再次 `python kb_manager.py add uploaded_docs/新文档.pdf` → 应提示「路径+哈希一致，跳过」
4. 修改文档内容后再次 `python kb_manager.py add uploaded_docs/新文档.pdf` → 哈希不同 → 自动走更新
5. `python kb_manager.py remove "uploaded_docs/新文档.pdf"` → `list` → 确认已移除
6. 对未 migrate 的老库执行 remove → 兜底按 `source` 字段匹配成功删除
7. 模拟更新失败场景 → 旧版本数据未被删除（先写新后删旧保障）
8. `python kb_manager.py repair` → 手动制造不一致 → 修复成功
9. 修改 db_meta.json 中的 `embedding_model` → add 时提示「嵌入模型已变更，请全量重建」
10. `python kb_manager.py add-dir uploaded_docs/ --continue-on-error` → 部分文件失败不影响其余
11. Streamlit 侧边栏切换「增量添加」上传 → 不丢失旧知识库
12. 两个终端同时执行 `add` → 一个成功，一个提示"知识库正在操作，请稍后再试"
13. `python kb_manager.py remove "某文档.pdf"` → `python kb_manager.py rollback` → 文档恢复
14. `python kb_manager.py rollback --list` → 列出所有可用快照
