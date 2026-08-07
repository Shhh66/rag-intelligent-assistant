# Pytest 单元测试计划

> 目标：为项目新增 4 个测试文件、15~18 个测试用例，全部不依赖 LLM / 网络 / 数据库，跑一次 < 10 秒。
> 对简历：可以写"熟练使用 pytest 编写单元测试，项目包含 15+ 自动化测试用例"。

---

## 一、现状

目前每个模块末尾有一段 `if __name__ == "__main__":` 自测代码，依赖人工看屏幕输出判断对错。改为 pytest 后，机器自动判断 ✅/❌。

### 现有自测代码分布（17 个文件）

| 文件 | 自测内容 |
|------|---------|
| `long_term_memory.py` | 记忆存取、用户隔离、去重更新 |
| `tool_audit.py` | 审计日志写入、入参脱敏 |
| `query_rewriter.py` | 查询改写（clarify / multi） |
| `tools_edu.py` | CAS 登录、课表抓取 |
| `bm25_index.py` | BM25 索引构建与搜索 |
| `hybrid_retriever.py` | RRF 融合检索 |
| `reranker.py` | BGE 重排 + LLM 降级 |
| `adapters.py` | RAGAS LLM/嵌入包装 |
| `run_eval.py` | 完整 A/B 评测链路 |
| `skill_registry.py` | Skills 三层匹配 |
| `skill_executor.py` | Skill 编排执行 |
| `scheduler.py` | 工具并行调度 |
| `observability.py` | LangFuse 连接测试 |
| 其他 | ... |

---

## 二、本次范围（第一批，选 4 个最稳的）

**选择标准：纯函数 / 不调 LLM / 不连外网 / 不写数据库。**

| 测试文件 | 测什么 | 来源模块 | 用例数 | 难度 |
|---------|--------|---------|:---:|:--:|
| `tests/test_clean_answer.py` | 答案清洗函数 | `rag_eval/run_eval.py` 的 `clean_answer_for_eval()` | 5 | ⭐ |
| `tests/test_encrypt.py` | 密码加密函数 | `tools_edu.py` 的 `encrypt_password()` / `_random_string()` | 3 | ⭐ |
| `tests/test_long_term_memory.py` | 记忆存取/隔离/去重 | `long_term_memory.py` | 6 | ⭐⭐ |
| `tests/test_tool_audit.py` | 审计日志写入/脱敏 | `tool_audit.py` | 4 | ⭐ |

---

## 三、每个文件的测试点

### 3.1 `test_clean_answer.py` —— 答案清洗

被测函数：`clean_answer_for_eval(answer: str) -> str`

功能：删掉答案里的来源标注、参考列表、免责声明，只保留纯事实内容供 RAGAS 打分。

| 用例 | 输入 | 预期输出 |
|------|------|---------|
| 删来源标注 | `"中山陵值得去（来源：旅游手册，第3页）"` | `"中山陵值得去"` |
| 删参考列表 | `"答案正文\n> 📚 参考来源：\n> - xxx手册"` | `"答案正文"`（参考块删除） |
| 删免责声明 | `"答案是A\n> ⚠️ 本回答由大模型生成，仅供参考"` | `"答案是A"`（免责行删除） |
| 保留正常正文 | `"ISAC 是通信感知一体化技术"` | 不变 |
| 空输入不崩 | `""` | `""` |

### 3.2 `test_encrypt.py` —— 密码加密

被测函数：
- `_random_string(n: int) -> str`：生成 n 位随机字符串
- `encrypt_password(password: str, salt: str) -> str`：AES-CBC 加密

| 用例 | 输入 | 预期 |
|------|------|------|
| 随机串长度 | `_random_string(16)` | 长度 = 16 |
| 加密结果是 Base64 | `encrypt_password("mypassword", "abcdefghijklmnop")` | 合法 Base64 字符串 |
| 同一密码两次加密不同 | 同上 × 2 | 两次结果不同（IV 随机） |

### 3.3 `test_long_term_memory.py` —— 长期记忆

被测模块：`LongTermMemory` 单例

| 用例 | 操作 | 断言 |
|------|------|------|
| 存了能查到 | 存 `"Alice 是通信大三学生"` → 检索 `"学生专业"` | 结果非空 |
| 用户隔离 | alice 存一条，bob 检索 | bob 查不到 alice 的数据 |
| 相似去重 | 存第一条 → 存一条语义相似的 | 总条数不变（旧被新替） |
| 清空 | `clear("alice")` 后检索 | 结果为空 |
| 关闭后不崩 | `LONG_TERM_MEMORY_ENABLED = False` | `retrieve()` 返回 `[]` |
| 置信度排序 | 存一条 conf=0.9 + 一条 conf=0.5 | 高置信度排前面 |

### 3.4 `test_tool_audit.py` —— 工具审计

被测函数：`log_tool_call(trace_id, tool, args, result, latency, success, error, retry_count)`

| 用例 | 操作 | 断言 |
|------|------|------|
| 日志文件生成 | 调一次 `log_tool_call` | `tool_audit.jsonl` 文件存在 |
| trace_id 写入 | 调 `log_tool_call(trace_id="test-123")` | 日志里有 `"test-123"` |
| 敏感字段掩码 | 传 `args={"api_key": "secret123"}` | 日志里不出现 `"secret123"` |
| 超长入参截断 | 传一个 1000 字符的参数 | 日志里该字段长度 < 500 |

---

## 四、实施步骤

### Step 1：安装 pytest + 创建目录（2 分钟）

```bash
cd rag_assistant
venv\Scripts\activate.bat
pip install pytest

mkdir tests
# 创建空的 tests\__init__.py
```

### Step 2：写测试代码（按顺序，共约 100 行，30 分钟）

**顺序：test_clean_answer → test_encrypt → test_tool_audit → test_long_term_memory**

从最简单、最容易出成果的开始，建立信心。

### Step 3：运行验证（3 分钟）

```bash
pytest tests/ -v
```

预期输出：所有用例绿色 `PASSED`。

### Step 4：加入 .gitignore（1 分钟）

`tool_audit.jsonl` 和 `memory.db` 是测试产生的临时文件，不要提交。

---

## 五、预期最终效果

```
tests/
├── __init__.py
├── test_clean_answer.py    # 5 用例
├── test_encrypt.py         # 3 用例
├── test_tool_audit.py      # 4 用例
└── test_long_term_memory.py # 6 用例

================== 18 passed in 2.3s ==================
```

### 简历可写

> 熟练使用 pytest 编写单元测试，覆盖数据清洗、加密逻辑、记忆管理、审计日志 4 个模块共 18 条用例，跑完全部 < 3 秒。

---

## 六、后续可扩展（第二批，本次不做）

| 模块 | 难度 | 原因 |
|------|:--:|------|
| `query_rewriter.py` | ⭐⭐⭐ | 需要 Mock LLM 调用 |
| `reranker.py` | ⭐⭐⭐ | 需要加载 568MB BGE 模型 |
| `hybrid_retriever.py` | ⭐⭐ | 需要 ChromaDB 有数据 |
| `tools_edu.py` 登录流程 | ⭐⭐⭐⭐ | 依赖校园网 + CAS 服务器 |

---

> 状态：**方案已完成，待开始实施。**
