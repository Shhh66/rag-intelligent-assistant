"""RAG 权限管理后端 —— FastAPI + SQLite + JWT

启动: uvicorn api_server:app --port 8000
"""

import sys
import sqlite3
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import jwt

from config import (
    KB_PERMISSION_DB, KB_PERMISSION_SECRET_KEY,
    KB_PERMISSION_TOKEN_EXPIRE_HOURS,
)

app = FastAPI(title="RAG 权限管理 API")

# CORS：允许前端 localhost:5173 跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer()


# ═══════════════════════════════════════════════════════════════
# SQLite 初始化
# ═══════════════════════════════════════════════════════════════

def _get_db():
    """获取 SQLite 连接"""
    conn = sqlite3.connect(KB_PERMISSION_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化权限数据库表"""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            department TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            permissions TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER REFERENCES users(id),
            role_id INTEGER REFERENCES roles(id),
            PRIMARY KEY (user_id, role_id)
        );

        CREATE TABLE IF NOT EXISTS kb_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            owner_id INTEGER DEFAULT 0,
            visibility TEXT DEFAULT 'internal',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS kb_group_members (
            group_id INTEGER REFERENCES kb_groups(id),
            user_id INTEGER REFERENCES users(id),
            PRIMARY KEY (group_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER DEFAULT 0,
            query TEXT DEFAULT '',
            kb_groups TEXT DEFAULT '[]',
            result_count INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()

    # 初始化默认角色
    existing_roles = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
    if existing_roles == 0:
        conn.execute(
            "INSERT INTO roles (name, permissions) VALUES (?, ?)",
            ("管理员", json.dumps(["upload", "delete_doc", "search_all",
                                   "manage_users", "manage_kb", "view_audit", "export"])),
        )
        conn.execute(
            "INSERT INTO roles (name, permissions) VALUES (?, ?)",
            ("工程师", json.dumps(["upload", "search_group"])),
        )
        conn.execute(
            "INSERT INTO roles (name, permissions) VALUES (?, ?)",
            ("访客", json.dumps(["search_group"])),
        )
        conn.commit()

    # 初始化默认管理员
    existing_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing_users == 0:
        pwd = hashlib.sha256("admin123".encode()).hexdigest()
        conn.execute(
            "INSERT INTO users (username, password_hash, department) VALUES (?, ?, ?)",
            ("admin", pwd, "技术部"),
        )
        # 分配管理员角色
        conn.execute(
            "INSERT INTO user_roles (user_id, role_id) VALUES (1, 1)"
        )
        conn.commit()

    conn.close()


# 启动时初始化
init_db()


# ═══════════════════════════════════════════════════════════════
# Pydantic 模型
# ═══════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    department: str = ""


class UserUpdate(BaseModel):
    username: str = None
    department: str = None
    is_active: bool = None


class RoleUpdate(BaseModel):
    permissions: list = None


class KbGroupCreate(BaseModel):
    name: str
    visibility: str = "internal"


class KbGroupUpdate(BaseModel):
    name: str = None
    visibility: str = None


class DocPermUpdate(BaseModel):
    kb_group: str = None
    visibility: str = None


# ═══════════════════════════════════════════════════════════════
# JWT 认证
# ═══════════════════════════════════════════════════════════════

def _create_token(user_id: int, username: str, permissions: list,
                  kb_groups: list) -> str:
    """签发 JWT Token"""
    payload = {
        "user_id": user_id,
        "username": username,
        "permissions": permissions,
        "kb_groups": kb_groups,
        "exp": datetime.now(timezone.utc) + timedelta(hours=KB_PERMISSION_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, KB_PERMISSION_SECRET_KEY, algorithm="HS256")


def get_current_user(token=Depends(security)):
    """JWT 鉴权 + 用户级限流：解析用户身份，顺带限流（超限 429）。

    限流挂在 get_current_user 而非逐个端点——它是所有鉴权端点的公共入口
    （login 除外），一次覆盖全部。fail-open：Redis 挂了限流放行。
    """
    try:
        payload = jwt.decode(
            token.credentials, KB_PERMISSION_SECRET_KEY, algorithms=["HS256"]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token 无效")

    # 用户级限流：每个登录用户有请求频率配额，超限 429
    try:
        from rate_limiter import get_user_limiter
        username = payload.get("username", "unknown")
        if not get_user_limiter().allow(username):
            print(f"   🚦 用户限流触发 (user={username})", file=sys.stderr, flush=True)
            raise HTTPException(429, "请求过于频繁，请稍后重试")
    except HTTPException:
        raise
    except Exception as e:
        print(f"   ⚠️ 用户限流检查失败(忽略): {e}", file=sys.stderr, flush=True)

    return payload


def require_permission(permission: str):
    """声明式权限校验装饰器"""
    def checker(user=Depends(get_current_user)):
        if permission not in user.get("permissions", []):
            raise HTTPException(403, f"缺少权限: {permission}")
        return user
    return checker


# ═══════════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
def login(req: LoginRequest):
    """登录 → JWT Token"""
    conn = _get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ? AND is_active = 1",
        (req.username,),
    ).fetchone()

    if not user:
        raise HTTPException(401, "用户名或密码错误")

    # 简单密码验证（生产环境应用 bcrypt）
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    if pwd_hash != user["password_hash"]:
        raise HTTPException(401, "用户名或密码错误")

    # 收集权限和可访问分组
    roles = conn.execute(
        """SELECT r.permissions FROM roles r
           JOIN user_roles ur ON r.id = ur.role_id
           WHERE ur.user_id = ?""", (user["id"],),
    ).fetchall()

    permissions = set()
    for r in roles:
        permissions.update(json.loads(r["permissions"]))

    # 无角色 → 默认访客权限
    if not permissions:
        permissions = {"search_group"}

    groups = conn.execute(
        """SELECT kg.name FROM kb_groups kg
           JOIN kb_group_members gm ON kg.id = gm.group_id
           WHERE gm.user_id = ?""", (user["id"],),
    ).fetchall()
    kb_groups = [g["name"] for g in groups]

    # 管理员 → None 表示不限权限（不做任何过滤）
    if "search_all" in permissions:
        kb_groups = None  # None = search() 走无过滤路径

    # 非管理员且无分组 → 至少能看到公开文档
    if kb_groups is not None and not kb_groups:
        kb_groups = ["public"]

    conn.close()

    token = _create_token(
        user["id"], user["username"],
        list(permissions), kb_groups,
    )
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "permissions": list(permissions),
            "kb_groups": kb_groups,
        },
    }


@app.get("/api/auth/me")
def me(user=Depends(get_current_user)):
    """获取当前用户信息"""
    return user


# ── 用户管理 ──

@app.get("/api/users")
def list_users(user=Depends(require_permission("manage_users"))):
    conn = _get_db()
    rows = conn.execute("SELECT id, username, department, is_active, created_at FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/users")
def create_user(req: UserCreate, user=Depends(require_permission("manage_users"))):
    conn = _get_db()
    existing = conn.execute("SELECT id FROM users WHERE username = ?", (req.username,)).fetchone()
    if existing:
        raise HTTPException(409, f"用户名已存在: {req.username}")

    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()
    conn.execute(
        "INSERT INTO users (username, password_hash, department) VALUES (?, ?, ?)",
        (req.username, pwd_hash, req.department),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "username": req.username}


# ── 角色管理 ──

@app.get("/api/roles")
def list_roles(user=Depends(get_current_user)):
    conn = _get_db()
    rows = conn.execute("SELECT * FROM roles").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 知识库分组 ──

@app.get("/api/kb/groups")
def list_kb_groups(user=Depends(get_current_user)):
    conn = _get_db()
    rows = conn.execute("SELECT * FROM kb_groups").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        # 成员数
        mc = conn.execute(
            "SELECT COUNT(*) FROM kb_group_members WHERE group_id = ?", (r["id"],)
        ).fetchone()[0]
        d["member_count"] = mc
        # 文档数：从 db_meta.json 读取（ChromaDB 索引）
        d["doc_count"] = 0
        result.append(d)
    conn.close()

    # 从 db_meta.json 获取每个分组的文档数
    try:
        from vector_store import _load_meta
        meta = _load_meta()
        for d in result:
            count = sum(
                1 for fp, info in meta.get("documents", {}).items()
                if info.get("kb_group") == d["name"]
            )
            d["doc_count"] = count if count else d["doc_count"]
    except Exception:
        pass

    # ChromaDB 兜底：db_meta.json 没有权限信息的旧文档，从向量库直接统计
    try:
        import chromadb
        from config import VECTOR_DB_PATH
        client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
        col = client.get_collection("langchain")
        for d in result:
            if d["doc_count"] == 0:
                data = col.get(where={"kb_group": d["name"]})
                if data["ids"]:
                    unique_files = set(
                        m.get("file_path") or m.get("source", "")
                        for m in data["metadatas"]
                    )
                    d["doc_count"] = len(unique_files)
    except Exception:
        pass

    return result


@app.post("/api/kb/groups")
def create_kb_group(req: KbGroupCreate,
                     user=Depends(require_permission("upload"))):
    conn = _get_db()
    conn.execute(
        "INSERT INTO kb_groups (name, owner_id, visibility) VALUES (?, ?, ?)",
        (req.name, user["user_id"], req.visibility),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "name": req.name}


@app.get("/api/kb/groups/{group_id}/members")
def list_group_members(group_id: int, user=Depends(get_current_user)):
    """获取分组成员列表"""
    conn = _get_db()
    members = conn.execute(
        "SELECT u.id, u.username FROM users u "
        "JOIN kb_group_members gm ON u.id = gm.user_id "
        "WHERE gm.group_id = ?", (group_id,),
    ).fetchall()
    conn.close()
    return [dict(m) for m in members]


@app.put("/api/kb/groups/{group_id}/members")
def manage_group_members(group_id: int, member_ids: list[int] = None,
                          action: str = "list",
                          user=Depends(get_current_user)):
    """管理分组成员：action=list|add|remove"""
    conn = _get_db()
    existing = conn.execute("SELECT * FROM kb_groups WHERE id = ?", (group_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, f"分组不存在: {group_id}")

    if action == "list":
        members = conn.execute(
            "SELECT u.id, u.username FROM users u "
            "JOIN kb_group_members gm ON u.id = gm.user_id "
            "WHERE gm.group_id = ?", (group_id,),
        ).fetchall()
        conn.close()
        return [dict(m) for m in members]

    if action == "add" and member_ids:
        for uid in member_ids:
            conn.execute(
                "INSERT OR IGNORE INTO kb_group_members (group_id, user_id) VALUES (?, ?)",
                (group_id, uid),
            )
        conn.commit()
    elif action == "remove" and member_ids:
        for uid in member_ids:
            conn.execute(
                "DELETE FROM kb_group_members WHERE group_id = ? AND user_id = ?",
                (group_id, uid),
            )
        conn.commit()

    conn.close()
    return {"status": "ok"}


@app.put("/api/kb/groups/{group_id}")
def update_kb_group(group_id: int, req: KbGroupUpdate,
                     user=Depends(require_permission("manage_kb"))):
    """更新分组：SQLite + ChromaDB 双写"""
    from vector_store import update_doc_permission

    conn = _get_db()
    existing = conn.execute("SELECT * FROM kb_groups WHERE id = ?", (group_id,)).fetchone()
    if not existing:
        raise HTTPException(404, f"分组不存在: {group_id}")

    # 1. 更新 SQLite
    if req.name:
        conn.execute("UPDATE kb_groups SET name = ? WHERE id = ?", (req.name, group_id))
    if req.visibility:
        conn.execute("UPDATE kb_groups SET visibility = ? WHERE id = ?", (req.visibility, group_id))

    # 2. 同步 ChromaDB 中该分组文档的权限元数据
    #    注意：需要 documents 表（当前为规划，可用 file_path 查询替代）
    conn.commit()

    # 记录审计日志
    conn.execute(
        "INSERT INTO audit_log (user_id, query, kb_groups) VALUES (?, ?, ?)",
        (user["user_id"], f"update_group_{group_id}", json.dumps({"group_id": group_id})),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "group_id": group_id}


# ── 用户编辑/删除 ──

@app.put("/api/users/{user_id}")
def update_user(user_id: int, req: UserUpdate,
                user=Depends(require_permission("manage_users"))):
    conn = _get_db()
    existing = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(404, f"用户不存在: {user_id}")
    if req.username:
        conn.execute("UPDATE users SET username = ? WHERE id = ?", (req.username, user_id))
    if req.department is not None:
        conn.execute("UPDATE users SET department = ? WHERE id = ?", (req.department, user_id))
    if req.is_active is not None:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(req.is_active), user_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "user_id": user_id}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, user=Depends(require_permission("manage_users"))):
    if user_id == 1:
        raise HTTPException(403, "不能删除默认管理员")
    conn = _get_db()
    conn.execute("DELETE FROM user_roles WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM kb_group_members WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "user_id": user_id}


# ── 用户分组管理 ──

@app.get("/api/users/{user_id}/groups")
def get_user_groups(user_id: int, user=Depends(require_permission("manage_users"))):
    """获取用户所属的知识库分组"""
    conn = _get_db()
    rows = conn.execute(
        "SELECT kg.id, kg.name FROM kb_groups kg "
        "JOIN kb_group_members gm ON kg.id = gm.group_id "
        "WHERE gm.user_id = ?", (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.put("/api/users/{user_id}/groups")
def set_user_groups(user_id: int, group_ids: list[int],
                     user=Depends(require_permission("manage_users"))):
    """设置用户的知识库分组（全量替换）"""
    conn = _get_db()
    conn.execute("DELETE FROM kb_group_members WHERE user_id = ?", (user_id,))
    for gid in group_ids:
        conn.execute(
            "INSERT OR IGNORE INTO kb_group_members (group_id, user_id) VALUES (?, ?)",
            (gid, user_id),
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "user_id": user_id, "group_ids": group_ids}


# ── 角色 CRUD ──

@app.post("/api/roles")
def create_role(user=Depends(require_permission("manage_users"))):
    conn = _get_db()
    conn.execute("INSERT INTO roles (name, permissions) VALUES (?, '[]')", ("新角色",))
    conn.commit()
    role_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"status": "ok", "id": role_id, "name": "新角色"}


@app.put("/api/roles/{role_id}")
def update_role(role_id: int, req: RoleUpdate,
                user=Depends(require_permission("manage_users"))):
    if role_id <= 3:
        raise HTTPException(403, "默认角色不可编辑，请创建新角色")
    conn = _get_db()
    if req.permissions is not None:
        conn.execute("UPDATE roles SET permissions = ? WHERE id = ?",
                     (json.dumps(req.permissions), role_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "role_id": role_id}


@app.delete("/api/roles/{role_id}")
def delete_role(role_id: int, user=Depends(require_permission("manage_users"))):
    if role_id <= 3:
        raise HTTPException(403, "默认角色不可删除")
    conn = _get_db()
    conn.execute("DELETE FROM user_roles WHERE role_id = ?", (role_id,))
    conn.execute("DELETE FROM roles WHERE id = ?", (role_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "role_id": role_id}


# ── 知识库分组删除 ──

@app.delete("/api/kb/groups/{group_id}")
def delete_kb_group(group_id: int, user=Depends(require_permission("manage_kb"))):
    conn = _get_db()
    conn.execute("DELETE FROM kb_group_members WHERE group_id = ?", (group_id,))
    conn.execute("DELETE FROM kb_groups WHERE id = ?", (group_id,))
    conn.commit()
    conn.close()
    return {"status": "ok", "group_id": group_id}


# ── 文档管理（对接 kb_manager.py）──

@app.get("/api/kb/documents")
def list_documents(user=Depends(get_current_user)):
    """获取知识库文档列表（含权限信息）"""
    try:
        from vector_store import list_documents as list_docs
        docs = list_docs()

        # 补充权限信息（从 ChromaDB 读取每个文档的 kb_group + visibility）
        try:
            import os, chromadb
            from config import VECTOR_DB_PATH
            client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
            col = client.get_collection("langchain")
            for doc in docs:
                fp = doc.get("file_path", "")
                sample = None
                # 优先按 file_path 查
                if fp:
                    sample = col.get(where={"file_path": fp}, limit=1)
                # 没匹配 → 兜底按 source（纯文件名）查（兼容老数据 file_path 为空的情况）
                if not sample or not sample["metadatas"]:
                    basename = os.path.basename(fp) if fp else ""
                    if basename:
                        sample = col.get(where={"source": basename}, limit=1)
                if sample and sample["metadatas"]:
                    doc["kb_group"] = sample["metadatas"][0].get("kb_group", "")
                    doc["visibility"] = sample["metadatas"][0].get("visibility", "")
        except Exception:
            pass

        return docs
    except Exception as e:
        return {"error": str(e), "hint": "请确保知识库已构建"}


@app.put("/api/kb/documents/{filename:path}/permission")
def update_doc_perm(filename: str, body: DocPermUpdate,
                     user=Depends(require_permission("manage_kb"))):
    """更新文档权限分组"""
    try:
        from vector_store import update_doc_permission
        result = update_doc_permission(filename, kb_group=body.kb_group, visibility=body.visibility)
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/kb/documents/{filename:path}")
def delete_document(filename: str, user=Depends(require_permission("delete_doc"))):
    """删除文档"""
    try:
        from vector_store import remove_document
        result = remove_document(filename)
        return result
    except Exception as e:
        raise HTTPException(500, str(e))


# ── 审计日志 ──

@app.get("/api/kb/audit")
def list_audit_logs(limit: int = 50, user=Depends(require_permission("view_audit"))):
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Token 刷新 ──

@app.post("/api/auth/refresh")
def refresh_token(user=Depends(get_current_user)):
    new_token = _create_token(
        user["user_id"], user["username"],
        user.get("permissions", []), user.get("kb_groups", []),
    )
    return {"token": new_token}


# ═══════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
