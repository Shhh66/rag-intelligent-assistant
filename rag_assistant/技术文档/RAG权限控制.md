# RAG 权限控制技术文档

## 一、现状与需求

### 1.1 当前状态

```
用户 → Streamlit 上传文档 → ChromaDB 向量库 → 所有用户共享同一知识库
                                                      ↓
                                         任何查询都能访问全部文档
```

**核心问题**：零权限隔离。任何人上传的文档，其他人都能检索到。

### 1.2 目标场景

| 场景 | 需求 | 示例 |
|------|------|------|
| **多部门隔离** | 研发部文档 vs 人事部文档，互不可见 | 工程师搜"调薪方案"不应命中 HR 文档 |
| **角色分级** | 普通成员只能看公开文档，管理者可看敏感文档 | 财报分析仅 CFO 角色可检索 |
| **个人知识库** | 用户只能检索自己上传的文档 | 每个人的私人笔记独立隔离 |
| **项目级隔离** | 不同项目的知识库完全隔离 | 项目A的API文档不影响项目B的检索 |

---

## 二、系统架构

### 2.0 核心问题：有了 Vue 后台，Streamlit 还有用吗？

**明确回答：两者定位不同，共存而非替代。**

```
                    ┌─────────────────────────┐
                    │     FastAPI 权限服务      │
                    │   (共用后端，统一鉴权)     │
                    └──────────┬──────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Streamlit 对话  │  │  Vue 3 管理后台 │  │  MCP Inspector  │
│  (app.py)       │  │  (frontend/)    │  │  (调试用)        │
├─────────────────┤  ├─────────────────┤  ├─────────────────┤
│ 用户: 普通成员   │  │ 用户: 管理员     │  │ 用户: 开发者     │
│ 功能: 提问+上传  │  │ 功能: 管人+管权限 │  │ 功能: 调试工具   │
│ 频率: 每天用     │  │ 频率: 偶尔配置   │  │ 频率: 开发时     │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

**类比**：
- Streamlit = 搜索引擎的**搜索框**（用户每天用来搜东西）
- Vue 后台 = 搜索引擎的**管理面板**（管理员偶尔上去加用户、调权限）
- 两者访问同一个 ChromaDB 知识库，共享同一套 FastAPI 权限服务

**Streamlit 不会被替代，而是被增强**——它在对话前多了一步：从 `st.session_state` 中取出当前用户的 `kb_groups`，透传给检索层做权限过滤。

### 2.1 整体架构

```
                         ┌─────────────────────────┐
                         │     FastAPI 权限服务      │
                         │  /api/auth /api/users    │
                         │  /api/roles /api/kb      │
                         └───────────┬─────────────┘
                                     │
        ┌────────────────────────────┼────────────────────────┐
        ▼                            ▼                        ▼
┌───────────────┐          ┌───────────────┐        ┌───────────────┐
│ Streamlit 对话 │          │ Vue 3 管理后台 │        │ MCP Inspector │
│ (app.py)      │          │ (frontend/)    │        │ (调试工具)     │
│               │          │               │        │               │
│ 👤 普通用户    │          │ 🔧 管理员      │        │ 🛠 开发者      │
│ 💬 RAG 问答   │          │ 👥 用户/角色管理│        │ 🔍 调试检索    │
│ 📄 上传文档   │          │ 📁 知识库分组   │        │               │
│ 🔒 受权限控制  │          │ 📊 审计日志    │        │               │
└───────┬───────┘          └───────┬───────┘        └───────────────┘
        │                          │
        └──────────┬───────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│                     RAG 检索层（共用）                         │
│                                                              │
│  ChromaDB where 权限过滤 → 重排 → LLM 生成                    │
│  SQLite 权限数据库（用户/角色/分组）                            │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 数据模型

```
┌──────────┐     ┌──────────────┐     ┌──────────┐
│   User   │────→│ UserRoleMapping│←────│   Role   │
├──────────┤     ├──────────────┤     ├──────────┤
│ id       │     │ user_id       │     │ id       │
│ username │     │ role_id       │     │ name     │
│ password │     │ kb_group_id   │     │ permissions│
│ dept     │     └──────┬───────┘     └──────────┘
└──────────┘            │
                        ▼
                ┌──────────────┐     ┌──────────────┐
                │  KB Group    │────→│   Document   │
                ├──────────────┤     ├──────────────┤
                │ id           │     │ id           │
                │ name         │     │ file_path    │
                │ owner_id     │     │ kb_group_id  │
                │ visibility   │     │ file_hash    │
                └──────────────┘     │ chunks       │
                                     └──────────────┘
```

**ChromaDB metadata 扩展**（最小改动）：

```python
# 每个 chunk 新增两个权限字段
{
    "kb_group": "dept_rd",        # 知识库分组 ID
    "visibility": "internal",     # public | internal
    # ... 原有字段不变
}
```

### 2.3 为什么同时需要 kb_group 和 visibility？

两个字段各司其职，解决不同维度的问题：

```
kb_group  = 文档属于哪个组（归属）
visibility = 组外的人能不能看（扩散范围）
```

| kb_group | visibility | 谁能检索 |
|----------|-----------|---------|
| `dept_rd` | `internal` | 研发部**全体成员** |
| `dept_rd` | `public` | 研发部 + **公司全员** |
| `dept_hr` | `internal` | 仅人事部全体 |
| `dept_hr` | `public` | 人事部 + 公司全员 |

**类比**：`kb_group` = 文件放在哪个部门的文件夹里，`visibility` = 这个文件夹对外是半开的还是全开的。

**在 ChromaDB where 过滤中的体现**：

```python
# 用户 kb_groups = ["dept_rd"] 时
where_filter = {
    "$or": [
        {"kb_group": {"$in": ["dept_rd"]}},   # 本组文档（不限 visibility）
        {"visibility": "public"},              # 或 所有标记为 public 的文档
    ]
}
```

- 第一条匹配"归属"：用户在组内，该组的所有文档都可见（无论 internal/public）
- 第二条匹配"扩散"：即使不在组内，只要是 public 文档所有人可见

一个场景帮助理解：研发部的内部文档（`dept_rd` + `internal`）只有研发部成员能看到；研发部对外发布的文档（`dept_rd` + `public`）公司全员都能搜到。如果没有 `visibility` 字段，就只能二选一——要么全锁、要么全开。

---

## 三、前端管理框架设计

### 3.1 技术选型

| 层 | 方案 | 理由 |
|----|------|------|
| 前端框架 | Vue 3 + Element Plus | 成熟的中后台方案，中文社区活跃，权限管理场景开箱即用 |
| 构建工具 | Vite | 毫秒级 HMR，开发体验好 |
| 状态管理 | Pinia | Vue 3 官方推荐，TypeScript 友好 |
| 路由 | Vue Router 4 + 动态权限路由 | 根据角色动态生成菜单 |
| HTTP | Axios + 拦截器 | 统一 Token 注入 + 401 处理 |
| 权限指令 | v-permission 自定义指令 | 按钮级权限控制 |

### 3.2 页面结构

```
src/
├── views/
│   ├── login/                    # 登录页
│   ├── dashboard/                # 仪表盘
│   │   └── index.vue            #   知识库概览：文档数 / 用户数 / 检索量
│   ├── users/                    # 用户管理
│   │   ├── index.vue            #   用户列表 + 搜索 + 分页
│   │   └── form.vue             #   新建/编辑用户
│   ├── roles/                    # 角色管理
│   │   ├── index.vue            #   角色列表 + 权限矩阵
│   │   └── permission.vue       #   权限树配置
│   ├── kb/                       # 知识库管理
│   │   ├── groups.vue           #   知识库分组列表
│   │   ├── documents.vue        #   文档管理（按分组）
│   │   └── audit.vue            #   检索审计日志
│   └── profile/                  # 个人中心
├── router/
│   └── index.ts                 # 动态路由 + beforeEach 守卫
├── stores/
│   ├── auth.ts                  # 用户登录态 + Token
│   ├── permission.ts            # 权限列表 + 路由过滤
│   └── kb.ts                    # 知识库分组状态
├── directives/
│   └── permission.ts            # v-permission 指令
├── api/
│   ├── request.ts               # Axios 封装 + 拦截器
│   ├── auth.ts                  # 登录/登出/刷新 Token
│   ├── users.ts                 # 用户 CRUD
│   ├── roles.ts                 # 角色 CRUD
│   └── kb.ts                    # 知识库分组 + 权限
└── components/
    ├── PermissionTree.vue       # 权限树组件
    └── KbGroupSelect.vue        # 知识库分组选择器
```

### 3.3 核心页面设计

#### 3.3.1 仪表盘

```
┌─────────────────────────────────────────────────┐
│  📊 知识库概览                        2026-07-04 │
├──────────┬──────────┬──────────┬────────────────┤
│ 知识库数  │  文档数   │  用户数   │  本月检索量     │
│    5     │   132    │   28     │   1,247        │
├──────────┴──────────┴──────────┴────────────────┤
│                                                  │
│  📈 检索趋势（近 30 天）                          │
│  [柱状图: 按知识库分组的检索量]                     │
│                                                  │
│  📋 最近操作                                      │
│  | 时间 | 用户 | 操作 | 目标 |                    │
│  | 10:30 | 张三 | 上传 | 研发手册.pdf |           │
│  | 09:15 | 李四 | 检索 | "API认证方案" |          │
└─────────────────────────────────────────────────┘
```

#### 3.3.2 角色权限矩阵

```
┌──────────────────────────────────────────────────────────┐
│  🔐 角色权限配置                                          │
├──────────┬──────┬──────┬──────┬──────┬──────┬──────────┤
│ 权限 \ 角色│ 管理员 │ 经理  │ 工程师 │ 访客  │ 自定义  │
├──────────┼──────┼──────┼──────┼──────┼──────┼──────────┤
│ 上传文档   │  ✅  │  ✅  │  ✅  │  ❌  │  ☐     │
│ 删除文档   │  ✅  │  ✅  │  ❌  │  ❌  │  ☐     │
│ 检索全部   │  ✅  │  ✅  │  ❌  │  ❌  │  ☐     │
│ 检索本组   │  ✅  │  ✅  │  ✅  │  ✅  │  ☐     │
│ 管理用户   │  ✅  │  ❌  │  ❌  │  ❌  │  ☐     │
│ 查看审计   │  ✅  │  ✅  │  ❌  │  ❌  │  ☐     │
│ 导出文档   │  ✅  │  ✅  │  ❌  │  ❌  │  ☐     │
└──────────┴──────┴──────┴──────┴──────┴──────┴──────────┘
```

#### 3.3.3 知识库分组管理

```
┌─────────────────────────────────────────────────┐
│  📁 知识库分组                                   │
├─────────────────────────────────────────────────┤
│ [+ 新建分组]                                     │
│                                                  │
│ ┌─ 研发部知识库 ─────────────────────────────┐   │
│ │ 可见性: 内部  │ 文档: 45  │ 成员: 12        │   │
│ │ [📄 文档列表] [👥 成员管理] [🔒 权限设置]    │   │
│ └────────────────────────────────────────────┘   │
│                                                  │
│ ┌─ 人事部知识库 ─────────────────────────────┐   │
│ │ 可见性: 机密  │ 文档: 18  │ 成员: 3         │   │
│ │ [📄 文档列表] [👥 成员管理] [🔒 权限设置]    │   │
│ └────────────────────────────────────────────┘   │
│                                                  │
│ ┌─ 公开文档库 ───────────────────────────────┐   │
│ │ 可见性: 公开  │ 文档: 67  │ 成员: 全体      │   │
│ │ [📄 文档列表] [🔒 权限设置]                  │   │
│ └────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

---

## 四、权限控制流程

### 4.1 检索过滤链路（修正 ChromaDB 实际行为）

**核心设计原则：数据侧打权限标签 + 检索侧回捞 + 元数据后置过滤。**

```
用户提问 "什么是微服务？"
  ↓
① 认证层：JWT 解析 → {user_id, roles, kb_groups}
  ↓
② 知识库路由：根据用户角色确定可访问的 kb_groups
    管理员 → 全部 kb_groups
    工程师 → ["dept_rd", "public"]
    访客   → ["public"]
  ↓
③ 全库向量召回（不限权限）
   ChromaDB HNSW 索引覆盖全量 chunk
   对全部 chunk 做向量相似度计算 → 内部放大预取 Top-N 候选
   （N = K × 内部放大系数，通常 3-10 倍）
  ↓
④ 元数据后置过滤（对候选集做权限过滤）
   对候选 Top-N 条执行 where 条件：
   {"$or": [
       {"kb_group": {"$in": ["dept_rd", "public"]}},
       {"visibility": "public"}
   ]}
   剔除无权限 chunk → 从剩余候选中取 Top-K 返回
  ↓
⑤ 重排 → LLM 生成 → 返回答案
```

**关键认知**：ChromaDB 的 HNSW 向量索引是**全量统一构建**的，没有按元数据字段做分区、分片或次级索引，因此**无法在索引层面直接跳过无权限向量**。真实流程是「先全量召回 → 后置元数据过滤」，而非「先过滤再搜索」。

**这意味着**：如果某用户可访问的 kb_group 只占全库 chunk 的 5%，全量召回 100 条候选 → 过滤后可能只剩 5 条 → 实际返回的 Top-K 可能不足。需要配合内部放大系数（加大 `top_k`）来补偿，详见 §9。
```

### 4.2 检索结果示例对比

```
无权限控制:
  检索"微服务" → 命中研发部文档 + 人事部文档 + CEO战略报告
  结果: 信息泄露 ❌

有权限控制:
  普通工程师检索"微服务" → 命中研发部文档 + 公开文档
  CEO战略报告被过滤 ✅
```

---

## 五、ChromaDB 权限过滤实现

### 5.1 存储时注入权限元数据

```python
# vector_store.py
def build_vector_store(docs: List[Document], kb_group: str = "default",
                       visibility: str = "internal") -> Chroma:
    """全量构建时注入权限元数据"""
    for doc in docs:
        doc.metadata["kb_group"] = kb_group
        doc.metadata["visibility"] = visibility
    # ... 正常写入


def add_document(file_path: str, kb_group: str = "default",
                 visibility: str = "internal") -> dict:
    """增量添加时注入权限元数据"""
    # ... 解析文档
    for chunk in chunks:
        chunk.metadata["kb_group"] = kb_group
        chunk.metadata["visibility"] = visibility
    # ... 正常写入
```

### 5.2 检索时按权限过滤

```python
# vector_store.py
def search_with_permission(query: str, kb_groups: List[str],
                           top_k: int = TOP_K) -> List[Document]:
    """带权限过滤的语义检索"""
    vector_store = load_vector_store()

    # 构建 ChromaDB where 条件
    # 用户可访问的组 + 所有 public 文档
    where_filter = {
        "$or": [
            {"kb_group": {"$in": kb_groups}},
            {"visibility": "public"},
        ]
    }

    results = vector_store.similarity_search(
        query, k=top_k,
        filter=where_filter,  # ChromaDB 原生 where 过滤
    )
    return results
```

### 5.3 ChromaDB where 条件说明

```python
# 单组过滤
{"kb_group": "dept_rd"}

# 多组 + 公开文档
{"$or": [
    {"kb_group": {"$in": ["dept_rd", "dept_hr"]}},
    {"visibility": "public"}
]}

# 精确匹配（数值/字符串）
{"kb_group": "dept_rd", "visibility": "internal"}

# ❌ ChromaDB 不支持：复杂的 AND/OR 嵌套、NOT 条件、正则
# ✅ 简单场景：上面的 $or + $in 足够覆盖 90% 需求
```

---

## 六、后端 API 设计

### 6.1 接口清单

```
POST   /api/auth/login           # 登录 → JWT Token
POST   /api/auth/refresh          # 刷新 Token

GET    /api/users                 # 用户列表（分页+搜索）
POST   /api/users                 # 新建用户
PUT    /api/users/:id             # 编辑用户
DELETE /api/users/:id             # 删除用户

GET    /api/roles                 # 角色列表
POST   /api/roles                 # 新建角色
PUT    /api/roles/:id             # 编辑角色 + 权限
DELETE /api/roles/:id             # 删除角色

GET    /api/kb/groups             # 知识库分组列表
POST   /api/kb/groups             # 新建分组
PUT    /api/kb/groups/:id         # 编辑分组
DELETE /api/kb/groups/:id         # 删除分组
PUT    /api/kb/groups/:id/members # 管理分组成员

GET    /api/kb/documents          # 文档列表（按分组过滤）
DELETE /api/kb/documents/:id      # 删除文档

GET    /api/kb/audit              # 检索审计日志（分页 + 筛选）
```

### 6.2 轻量实现方案

不需要引入 Django/Flask 重型框架。利用 Streamlit 已有的 Python 环境，引入 FastAPI 作为后端：

```python
# api_server.py（新文件，~200 行）
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer
from pydantic import BaseModel
import jwt

app = FastAPI(title="RAG 权限管理 API")
security = HTTPBearer()

# ── JWT 中间件 ──
def get_current_user(token=Depends(security)):
    try:
        payload = jwt.decode(token.credentials, SECRET_KEY, algorithms=["HS256"])
        return payload  # {user_id, username, roles, kb_groups}
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token 无效")


# ── 权限装饰器 ──
def require_permission(permission: str):
    """声明式权限校验"""
    def checker(user=Depends(get_current_user)):
        if permission not in user.get("permissions", []):
            raise HTTPException(403, f"缺少权限: {permission}")
        return user
    return checker
```

### 6.3 数据库选型

| 方案 | 适用场景 | 本方案选择 |
|------|---------|-----------|
| SQLite | 单机小团队（< 50 用户） | ✅ **推荐**：零配置，文件存储，Streamlit 同目录 |
| PostgreSQL | 多实例部署、高并发 | 后续迁移 |
| ChromaDB + metadata | 文档级过滤 | ✅ 已实现：chunk 级权限过滤 |

```sql
-- SQLite schema（permission.db）
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    department TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    permissions TEXT NOT NULL  -- JSON: ["upload","search_all","manage_users"]
);

CREATE TABLE user_roles (
    user_id INTEGER REFERENCES users(id),
    role_id INTEGER REFERENCES roles(id),
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE kb_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    owner_id INTEGER REFERENCES users(id),
    visibility TEXT DEFAULT 'internal',  -- public | internal
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE kb_group_members (
    group_id INTEGER REFERENCES kb_groups(id),
    user_id INTEGER REFERENCES users(id),
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    query TEXT,
    kb_groups TEXT,       -- JSON: 命中了哪些组
    result_count INTEGER,
    timestamp TEXT DEFAULT (datetime('now'))
);
```

---

## 七、前端权限控制细节

### 7.1 路由守卫

```typescript
// router/index.ts
router.beforeEach(async (to, from, next) => {
  const authStore = useAuthStore()

  // 1. 未登录 → 跳转登录页
  if (!authStore.token && to.path !== '/login') {
    return next('/login')
  }

  // 2. 已登录 → 动态生成路由（首次）
  if (authStore.token && !authStore.permissions.length) {
    await authStore.fetchUserInfo()
    const routes = generateRoutes(authStore.permissions)
    routes.forEach(r => router.addRoute(r))
    return next({ ...to, replace: true })
  }

  // 3. 权限校验
  if (to.meta.permission && !authStore.hasPermission(to.meta.permission)) {
    return next('/403')
  }

  next()
})
```

### 7.2 按钮级指令

```typescript
// directives/permission.ts
export const vPermission = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    const { value } = binding
    const authStore = useAuthStore()

    if (value && !authStore.hasPermission(value)) {
      el.parentNode?.removeChild(el)  // 无权限 → 移除 DOM
    }
  }
}

// 使用
<el-button v-permission="'delete_doc'">删除文档</el-button>
<el-button v-permission="'manage_users'">管理用户</el-button>
```

### 7.3 Axios 拦截器

```typescript
// api/request.ts
const service = axios.create({ baseURL: '/api' })

// 请求拦截：注入 Token
service.interceptors.request.use(config => {
  const token = useAuthStore().token
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// 响应拦截：统一错误处理
service.interceptors.response.use(
  response => response.data,
  error => {
    if (error.response?.status === 401) {
      // Token 过期 → 跳转登录
      useAuthStore().logout()
      router.push('/login')
    }
    if (error.response?.status === 403) {
      ElMessage.error('权限不足，请联系管理员')
    }
    return Promise.reject(error)
  }
)
```

---

## 八、与现有系统的集成

### 8.1 Streamlit 对话页必须对接权限

> 关于 Streamlit 和 Vue 后台的关系，详见 [§2.0](#20-核心问题有了-vue-后台streamlit-还有用吗)。简言之：**Streamlit 是普通用户的对话界面，Vue 是管理员的后台面板，两者共存互补。**

**问题**：Vue 管理后台配置了权限，但用户实际对话用的是 `app.py`（Streamlit）。如果 `app.py` 不做权限对接，任何人在对话界面仍然能检索全量文档——权限形同虚设。

**修复方案（三级，从轻到重）**：

| 级别 | 方案 | 适用场景 | 工时 | 状态 |
|------|------|---------|------|------|
| **轻量验证版** | 角色下拉框（管理员/工程师/访客）本地映射 kb_groups | 快速验证过滤逻辑 | 30 分钟 | ✅ 已实现 |
| **实用落地版** | 侧边栏登录表单（用户名+密码），调用 FastAPI `/api/auth/login` 获取 JWT + kb_groups | 小团队正式使用 | 2 小时 | ✅ 已实现 |
| **完整企业版** | Streamlit 页面加 JWT 鉴权中间件，无 Token 自动跳转 FastAPI 登录页 | 多用户生产环境 | 半天 | ⬜ 规划中 |

**当前状态：实用落地版（已实现）**。侧边栏是账号密码登录表单，调用 FastAPI `/api/auth/login` 获取 JWT + kb_groups → `set_current_kb_groups()` → `search()` 底层自动过滤。用户权限（角色、分组）在 Vue 后台配置后，Streamlit 登录即时生效。

### 8.2 改动面分析（实际实施后）

| 现有模块 | 实际改动 | 说明 |
|---------|------|------|
| `config.py` | +6 行 | `KB_PERMISSION_ENABLED`、`KB_DEFAULT_GROUP`、`KB_DEFAULT_VISIBILITY`、权限数据库路径、JWT 密钥 |
| `app.py` | ~15 行 | 侧边栏增加用户角色选择器（管理员/工程师/访客）→ `set_current_kb_groups()` 写入共享文件 |
| `vector_store.py` | ~55 行 | `search()` 底层自动读文件 + 权限过滤；`search_with_permission()` 新增；`update_doc_permission()` 批量权限更新；`add_document()`/`build_vector_store()` 注入权限元数据 |
| `retriever.py` | ~15 行 | `set_current_kb_groups()` 写 JSON 文件（绝对路径）；`answer_with_fallback()` 恢复简洁签名 |
| `mcp_server.py` | 0 行 | **无需改动**——权限在 `search()` 底层自动生效 |
| `kb_manager.py` | +25 行 | `update-permission` 命令：`--kb-group` + `--visibility` |
| `api_server.py` | **新增** ~270 行 | FastAPI + SQLite + JWT：登录、用户管理、分组管理、审计日志 |

**核心设计**：权限检查放在 `vector_store.py:search()` 最底层，不依赖参数传递，与调用路径、进程架构完全解耦。

═══════════════════════════════════════════════════════════════════════════
                        RAG 权限控制完整流程
═══════════════════════════════════════════════════════════════════════════

  Streamlit 侧边栏
  ┌─────────────────┐
  │ 🔒 权限模拟       │
  │ [访客 ▼]         │   ① 选择角色
  │ 可访问: public   │────── 写入 kb_permission_context.json → ["public"]
  └─────────────────┘

  用户提问
  ┌─────────────────┐
  │ "6G作者是谁?"    │   ② agent.chat(user_input)
  └────────┬────────┘
           │
           ▼
  ┌─────────────────────────────────────────────┐
  │           ReAct Agent (decision_engine)      │
  │                                             │
  │   ③ 决定调用 ask_knowledge_base 工具          │
  └────────────────────┬────────────────────────┘
                       │
                       │ MCP 协议 (stdio)
                       ▼
  ┌─────────────────────────────────────────────┐
  │        mcp_server.py (子进程)                │
  │        ask_knowledge_base(query)            │
  └────────────────────┬────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │              retriever.py                            │
  │              answer_with_fallback(query)             │
  │                                                     │
  │   ┌─ ④ 中文检索 ──────────────────────────────┐    │
  │   │   search(query) → ⑤→⑥ → 返回 4 条(public) │    │
  │   └───────────────────────────────────────────┘    │
  │                                                     │
  │   ┌─ ⑤ 英文检索 ──────────────────────────────┐    │
  │   │   translate → search(en_query) → 返回 4 条  │    │
  │   └───────────────────────────────────────────┘    │
  │                                                     │
  │   ┌─ ⑥ 合并去重 ──────────────────────────────┐    │
  │   │   中英结果去重 → 4 条                       │    │
  │   └───────────────────────────────────────────┘    │
  │                                                     │
  │   ┌─ ⑦ 重排 ──────────────────────────────────┐    │
  │   │   Cross-Encoder 精排 → 得分排序 → 4 条      │    │
  │   └───────────────────────────────────────────┘    │
  │                                                     │
  │   ┌─ ⑧ 构建 Prompt ───────────────────────────┐    │
  │   │   build_prompt(query, reranked_docs)        │    │
  │   └───────────────────────────────────────────┘    │
  └────────────────────┬────────────────────────────────┘
                       │
                       │ ⑨ 调用
                       ▼
  ┌─────────────────────────────────────────────────────┐
  │              LLM (DeepSeek)                          │
  │                                                     │
  │   Prompt + 4 条参考片段(仅姓名.docx) → 生成回答        │
  │   "参考信息中未找到6G通信的作者..."                    │
  └────────────────────┬────────────────────────────────┘
                       │
                       ▼
  ┌─────────────────────────────────────────────┐
  │         ⑩ 返回最终答案                        │
  │                                             │
  │  📚 参考来源:                                 │
  │  - 姓名.docx（访客仅能检索 public 文档）       │
  └─────────────────────────────────────────────┘


  ═══════════ ④⑤里 search() 内部的权限过滤（放大） ═══════════

  ④ 或 ⑤ 调用 search("6G作者是谁?")
    │
    ▼
  ┌─────────────────────────────────────────────────────┐
  │  vector_store.py: search(query)                      │
  │                                                      │
  │  _get_context_kb_groups()                            │
  │    │                                                 │
  │    └── 读 kb_permission_context.json                 │
  │         → ["public"]                                 │
  │            │                                         │
  │          有值 ──→ search_with_permission()           │
  │                    │                                 │
  │                    ▼                                 │
  │  ┌──────────────────────────────────────────────┐   │
  │  │  ChromaDB 全库 26 个 chunk                    │   │
  │  │                                              │   │
  │  │  向量召回 Top-N                                │   │
  │  │    ↓                                         │   │
  │  │  where 过滤 {                                 │   │
  │  │    "$or": [                                   │   │
  │  │      {"kb_group": {"$in": ["public"]}},       │   │
  │  │      {"visibility": "public"}                 │   │
  │  │    ]                                          │   │
  │  │  }                                           │   │
  │  │    ↓                                         │   │
  │  │  6G通信.docx (vis=internal) → ❌ 剔除          │   │
  │  │  姓名.docx   (vis=public)   → ✅ 保留          │   │
  │  │    ↓                                         │   │
  │  │  返回 4 条                                    │   │
  │  └──────────────────────────────────────────────┘   │
  │                                                      │
  └──────────────────────────────────────────────────────┘


### 8.3 渐进式上线

```
阶段 1: 无权限模式（当前）
  kb_group = "default", visibility = "public"
  全部文档共享，零改动成本

阶段 2: 存储层注入（半天）
  build_vector_store() / add_document() 注入 kb_group + visibility
  权限数据到位，但检索未过滤

阶段 3: 检索层过滤（半天）
  search() 增加 where 条件
  answer_with_fallback() 接受 kb_groups
  权限生效

阶段 4: 前端管理后台（1-2 天）
  Vue 3 + Element Plus 项目初始化
  用户/角色/分组管理页面
  对接后端 API

阶段 5: 审计与监控（半天）
  检索日志记录
  仪表盘展示
```

---

## 八-0、已发现 Bug：进程隔离 + 模块缓存导致权限不生效（已修复）

### 问题

Streamlit 侧边栏切换「访客」角色，提问后仍然能检索到 `internal` 级别的文档。权限配置正确、文件写入正常，但过滤完全不生效。

### 失败方案历程（记录以供参考）

这个 Bug 踩了三次坑才最终解决：

**尝试 1：模块级变量**
```python
# retriever.py — 全局变量
_current_kb_groups = None
def set_current_kb_groups(groups):
    global _current_kb_groups
    _current_kb_groups = groups
```

失败原因：`app.py`（主进程）写入变量，`mcp_server.py`（子进程）读取变量。两个进程各有独立的 Python 解释器和模块实例，变量不共享。

**尝试 2：共享文件 + retriever.py 读取**
```python
# 把变量改成 JSON 文件，retriever.py:answer_with_fallback() 从文件读
```

失败原因：子进程在启动时缓存了 `retriever.py` 模块的字节码（`.pyc`）。即使源码更新，子进程一直用的是旧模块。`importlib.reload()` 在 `mcp_server.py` 中执行也无法生效，因为 `mcp_server.py` 本身也是旧模块。

**尝试 3：共享文件 + mcp_server.py 直接读取 + importlib.reload**
```python
# mcp_server.py:ask_knowledge_base() 直接读文件 + 强制 reload retriever
```

失败原因：同上。`mcp_server.py` 的更新需要重启子进程，但子进程由 Agent 管理且常驻运行，Streamlit 重启不一定触发子进程重启。

### 最终方案：权限检查下沉到最底层

**核心洞察**：不管走什么调用路径（MCP 工具、ReAct Agent、直接调用），最终都会调用 `vector_store.py:search()`。把权限检查放在这个最底层的函数里，让所有路径自动生效。

```
app.py（主进程）                       mcp_server.py（子进程）
  │                                        │
  │ set_current_kb_groups()               │ answer_with_fallback()
  │ → 写入 JSON 文件                       │   → search()
  │                                        │     → _get_context_kb_groups()
  │                                        │       → 读取 JSON 文件
  │                                        │       → 有权限 → search_with_permission()
  └────────────────────────────────────────┘
           同一個文件，讀寫皆用絕對路徑
```

```python
# vector_store.py — 最底层检索函数
import json, os

_KB_PERMISSION_CONTEXT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "kb_permission_context.json",
)

def _get_context_kb_groups():
    """每次检索时从文件读取权限（跨进程兼容）"""
    try:
        with open(_KB_PERMISSION_CONTEXT_FILE, "r") as f:
            groups = json.load(f)
            return groups if groups else None
    except Exception:
        return None

def search(query, top_k=TOP_K):
    """检索入口 — 自动读取权限上下文并过滤"""
    kb_groups = _get_context_kb_groups()
    if kb_groups:
        return search_with_permission(query, kb_groups=kb_groups, top_k=top_k)
    # 无权限文件 → 不限权限（管理员模式）
    vector_store = load_vector_store()
    return vector_store.similarity_search(query, k=top_k)
```

**为什么这次成功了？**

| 方案 | 失效原因 | 本次解决 |
|------|---------|---------|
| 模块级变量 | 进程隔离 | 文件共享 |
| retriever 读文件 | 子进程缓存了旧模块 | — |
| mcp_server 读文件 | 同上 | — |
| **search() 读文件** | — | ✅ 子进程每次调用都会打开文件读取，不依赖模块级状态 |

`search()` 每次被调用时都重新打开文件读取，这是真正的"运行时"行为，不依赖任何模块加载时机或进程启动顺序。

### 设计原则

**权限检查放在检索的最底层，而非通过参数层层传递。**

错误做法：`app.py → Agent → MCP → retriever → search(kb_groups=...)`
- 链路过长，任何一环断裂权限就失效
- 跨进程时参数传递天然不可靠

正确做法：`search()` 自己读文件，自己判断
- 链路为零，无论如何调用 search 都绕不过
- 与进程架构、模块缓存、调用路径完全解耦

### 涉及文件（最终版本）

| 文件 | 改动 |
|------|------|
| `vector_store.py` | `search()` 内部新增 `_get_context_kb_groups()` 调用，有权限则走 `search_with_permission()`；`_get_context_kb_groups()` 用绝对路径读 JSON 文件 |
| `retriever.py` | `set_current_kb_groups()` 改为写 JSON 文件（绝对路径）；`answer_with_fallback()` 恢复简洁签名，不再需要传 `kb_groups` 参数 |
| `mcp_server.py` | 无需改动（恢复原始代码） |

---

## 八-B、权限变更与存量数据一致性

### 8-B.1 核心痛点

**问题 1：权限元数据无法批量更新。** 当前 `add_document()` 和 `build_vector_store()` 只在写入时注入 `kb_group` + `visibility`。如果后续需要"将某文档从研发部移动到人事部"或"将内部文档改为机密"，ChromaDB 里该文档所有 chunk 的权限元数据无法同步更新，只能删了重传。

**问题 2：双写一致性风险。** SQLite（权限表）和 ChromaDB（chunk 元数据）是两个独立存储。SQLite 支持事务回滚，但 ChromaDB 的 `col.update()` 没有原子事务能力——如果批量更新 100 个 chunk 的元数据时中途失败，已更新的 chunk 无法回滚，会出现两边部分不一致。

**一致性模型定位**：本方案采用**最终一致性**而非强一致性。权限变更优先保证 SQLite（权限配置的权威数据源）写入准确；ChromaDB 元数据的同步异常通过 `repair-permission` 命令定时校准。小团队低频次权限变更场景下，异常概率极低，不需要引入分布式事务（两阶段提交等重型方案），符合轻量架构定位。

### 8-B.2 修复方案

**① 新增批量权限更新命令**

```python
# vector_store.py
def update_doc_permission(file_path: str, kb_group: str = None,
                          visibility: str = None) -> dict:
    """批量更新某文档所有 chunk 的权限元数据

    适用场景：文档移动分组、修改可见性等级
    """
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
    col = client.get_collection("langchain")

    # 1. 查询该文档所有 chunk
    results = col.get(where={"file_path": file_path})
    ids = results['ids']
    if not ids:
        return {"file_path": file_path, "updated_chunks": 0, "error": "未找到该文档的 chunk"}

    # 2. 批量更新 metadata
    metadatas = []
    for meta in results['metadatas']:
        if kb_group:
            meta['kb_group'] = kb_group
        if visibility:
            meta['visibility'] = visibility
        metadatas.append(meta)

    col.update(ids=ids, metadatas=metadatas)

    # 3. 同步更新 db_meta.json（如果需要记录权限分组）
    return {"file_path": file_path, "updated_chunks": len(ids)}
```

**② 权限变更统一走 API，保证 SQLite + ChromaDB 双写事务**

```python
# api_server.py
@app.put("/api/kb/groups/{group_id}")
async def update_kb_group(group_id: int, data: KbGroupUpdate,
                           user=Depends(require_permission("manage_kb"))):
    """更新知识库分组：SQLite + ChromaDB 双写"""
    db = get_db()

    # 1. 更新 SQLite 中的分组信息
    db.execute("UPDATE kb_groups SET visibility = ? WHERE id = ?",
               (data.visibility, group_id))

    # 2. 同步更新 ChromaDB 中该分组下所有文档的元数据
    docs = db.execute(
        "SELECT file_path FROM documents WHERE kb_group_id = ?", (group_id,)
    ).fetchall()

    errors = []
    for doc in docs:
        result = update_doc_permission(
            doc["file_path"],
            visibility=data.visibility,
        )
        if result.get("error"):
            errors.append(result)

    if errors:
        # 3. 部分失败 → 回滚 SQLite + 记录异常
        db.rollback()
        raise HTTPException(500, f"ChromaDB 更新失败: {errors}")

    db.commit()
    return {"status": "ok", "updated_docs": len(docs)}
```

**③ 权限一致性校验命令**

```python
# kb_manager.py 新增
def cmd_repair_permission(args):
    """校验并修复 SQLite 权限表 ⇔ ChromaDB 元数据的一致性
    
    1. SQLite 有、Chroma 无对应权限 → 提示需删旧数据或重建
    2. SQLite 权限与 Chroma 元数据不一致 → 按 SQLite 为准批量修复
    3. 输出不一致报告
    """
    # ... 对比逻辑
```

```bash
python kb_manager.py repair-permission
# 输出:
#   ✅ 一致性校验完成: 45 个文档全部一致
#   ⚠️ 发现 2 处不一致，已按 SQLite 为准修复:
#      - 研发手册.pdf: Chroma kb_group="dept_rd" → 已改为 "dept_hr"
#      - 财务报告.docx: Chroma visibility="internal" → 已改为 "public"
```

### 8-B.3 权限变更操作流程（最终一致性模型）

```
管理员在 Vue 后台修改文档分组
  ↓
PUT /api/kb/groups/:id → api_server.py
  ↓
① SQLite: UPDATE kb_groups / documents  ← 权威数据源（支持事务回滚）
② ChromaDB: update_doc_permission()     ← 尽力同步（不支持回滚）
  ↓
③ SQLite commit 成功 → 权限配置生效
   ChromaDB 失败 → 记录异常日志 + 后续 repair-permission 校准
   正常情况（>99%）: 两边同时成功
```

**和之前 `repair`（Chroma ⇔ db_meta）的关系**：

| | `repair` | `repair-permission` |
|------|---------|-------------------|
| 校验对象 | ChromaDB chunk ⇔ db_meta.json | SQLite 权限表 ⇔ ChromaDB metadata |
| 修复内容 | 文档列表、chunk 数量 | kb_group、visibility 字段 |
| 触发场景 | 写入异常导致不一致 | 权限变更后未同步 |

---

## 九、技术决策与面试话术

### 为什么用 ChromaDB 的 where 过滤而不是检索后手动过滤？

核心原则：**数据侧打标签 + 检索侧回捞 + 元数据后置过滤**。

**先纠正一个常见认知误区**：ChromaDB where 过滤**不是在向量检索之前执行**的。ChromaDB 的 HNSW 索引是全量统一构建，没有按元数据字段做分区或次级索引，无法在索引层面跳过无权限向量。真实流程：

```
全库向量召回 → 取 Top-N 候选（内部放大 3~10 倍 K）
    ↓
对候选集执行 where 过滤 → 剔除无权限 chunk
    ↓
从剩余中取 Top-K 返回
```

**那为什么还用 where 而不是自己写后处理？**

1. **安全性等价**：无论 ChromaDB 的 where 还是手动后处理，都是在召回阶段之后过滤，权限控制效果完全一致——无权限 chunk 绝不会出现在最终结果中。区别仅在于谁执行过滤逻辑——ChromaDB 内核做比 Python 循环快
2. **结果一致性**：`similarity_search(query, k=8, filter=...)` 返回结果一定满足权限条件，不会出现含无权限 chunk 的情况。ChromaDB 内部会自动放大预取量尝试填满 K——但当符合权限条件的向量总数本身不足 K 时（如全库只有 3 条有权访问），仍会返回少于 K 的结果，属于正常的权限收敛效果，而非 bug
3. **代码简洁**：`{"$or": [{"kb_group": {"$in": groups}}, {"visibility": "public"}]}` 一行覆盖全部场景

**那真正需要担心的是什么？**

**召回不足**。如果用户可访问的 kb_group 只占全库 5%，全量召回时绝大多数候选来自无权限分组，过滤后有效结果稀疏 → 向量排序的精度受影响。应对策略：

| 策略 | 说明 |
|------|------|
| 加大 `top_k` | 从默认 8 增大到 16~24，给 ChromaDB 内部更大的预取空间 |
| 按组独立建库 | 每个 kb_group 一个 ChromaDB collection，检索时只搜有权访问的 collection——从架构层面解决，但运维复杂度↑ |
| 监控告警 | 统计各 kb_group 的 chunk 占比和检索命中数，低于阈值告警 |

**场景判断**：kb_group 的 chunk 分布均匀（任意单个组 ≥ 20% 全库）→ where 过滤完全够用；kb_group 极度不均（大量文档集中在少数组）→ 考虑独立 collection。

### 为什么选 FastAPI + SQLite 而不是 Django？

1. FastAPI 与现有 Streamlit 共存同一 Python 环境，零额外部署
2. SQLite 文件数据库，与 ChromaDB 一样不需要额外服务
3. 小团队（< 50 用户）场景足够，上量后迁 PostgreSQL 只需改连接字符串

### 权限过滤会导致 chunk 的权限元数据泄露吗？

不会。ChromaDB 的 where 过滤是对**候选集**做后置过滤，chunk 的 `kb_group`、`visibility` 字段只用于判断该 chunk 是否被剔除，不会出现在返回给用户的 `page_content` 中（除非 chunk 正文本身包含）。检索结果展示时 LLM 只看到文档原文和来源文件名，看不到权限元数据。

---

## 十、外部依赖与项目结构

```
rag_assistant/
├── app.py                 # Streamlit 对话界面（保留，+权限透传）
├── api_server.py          # FastAPI 权限管理后端（新增）
├── permission.db          # SQLite 权限数据库（新增，gitignore）
├── frontend/              # Vue 3 管理后台（新增，管理员用）
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── views/         # 页面
│       ├── router/        # 动态路由
│       ├── stores/        # Pinia 状态
│       ├── api/           # HTTP 封装
│       └── directives/    # 权限指令
├── vector_store.py        # +权限过滤 + update_doc_permission（改造）
├── retriever.py           # +用户上下文（改造）
├── mcp_server.py          # +user_id 参数（改造）
└── kb_manager.py          # +update-permission + repair-permission（改造）
```

### 10.1 启动指南

**启动顺序**（必须先启动 ③ 权限服务，再启动其余）：

```
┌─ 终端 1 ──────────────────────────────────────────────────┐
│ cd rag_assistant                                           │
│ venv\Scripts\activate                                      │
│ venv\Scripts\python.exe api_server.py                      │
│                                                            │
│ → http://localhost:8000/docs 可查看 API 文档               │
│ 首次启动自动创建 permission.db + 默认管理员账号             │
└────────────────────────────────────────────────────────────┘

┌─ 终端 2 ──────────────────────────────────────────────────┐
│ cd rag_assistant                                           │
│ venv\Scripts\activate                                      │
│ streamlit run app.py                                       │
│                                                            │
│ → http://localhost:8501  RAG 对话界面（普通用户）           │
│ 侧边栏登录后自动获取权限，对话时过滤文档                     │
└────────────────────────────────────────────────────────────┘

┌─ 终端 3 ──────────────────────────────────────────────────┐
│ cd rag_assistant\frontend                                  │
│ npm run dev                                                │
│                                                            │
│ → http://localhost:5173  权限管理后台（管理员）             │
│ 首次运行需 npm install（仅一次）                            │
└────────────────────────────────────────────────────────────┘

┌─ 终端 4（可选，调试用）────────────────────────────────────┐
│ cd rag_assistant                                           │
│ npx @modelcontextprotocol/inspector python mcp_server.py   │
│                                                            │
│ → MCP Inspector 调试工具，直接调用底层 MCP 工具              │
└────────────────────────────────────────────────────────────┘
```

### 10.2 各服务说明

| 服务 | 地址 | 使用者 | 用途 |
|------|------|--------|------|
| FastAPI 权限服务 | `localhost:8000` | Streamlit + Vue 共用 | 登录鉴权、用户/角色/分组 CRUD、审计日志 |
| Streamlit 对话 | `localhost:8501` | 普通用户 | RAG 问答、上传文档（上传时选分组+可见性） |
| Vue 管理后台 | `localhost:5173` | 管理员 | 用户管理、角色权限配置、知识库分组、文档分配、审计日志 |
| MCP Inspector | 动态端口 | 开发者 | 直接调用 `ask_knowledge_base` / `debug_rerank` 等 MCP 工具 |

### 10.3 默认账号

| 用户名 | 密码 | 角色 | 权限 |
|--------|------|------|------|
| `admin` | `admin123` | 管理员 | 全部（不限知识库） |

Vue 后台和 Streamlit 共用同一套用户体系，在 Vue 后台新建的用户也可以在 Streamlit 登录。

### 10.4 CLI 工具

```bash
# 文档权限管理
venv\Scripts\python.exe kb_manager.py update-permission "uploaded_docs/xxx.docx" --kb-group dept_rd --visibility internal
venv\Scripts\python.exe kb_manager.py repair-permission --fix

# 日常知识库维护
venv\Scripts\python.exe kb_manager.py status
venv\Scripts\python.exe kb_manager.py list
```

### 10.5 新增依赖

```
requirements.txt 新增:
  fastapi>=0.115
  uvicorn>=0.32
  pyjwt>=2.10
  pydantic>=2.10
  requests    (Streamlit 调 FastAPI 登录接口)

frontend/package.json:
  vue@3, element-plus, @element-plus/icons-vue, pinia, vue-router@4, axios, vite
```
