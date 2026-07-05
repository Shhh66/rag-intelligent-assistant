"""RAG 权限管理后端 —— FastAPI + SQLite + JWT

启动: uvicorn api_server:app --port 8000
"""

import sqlite3
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer
from pydantic import BaseModel
import jwt

from config import (
    KB_PERMISSION_DB, KB_PERMISSION_SECRET_KEY,
    KB_PERMISSION_TOKEN_EXPIRE_HOURS,
)

app = FastAPI(title="RAG 权限管理 API")
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
                                   "manage_users", "view_audit", "export"])),
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
    """JWT 中间件：解析用户身份"""
    try:
        payload = jwt.decode(
            token.credentials, KB_PERMISSION_SECRET_KEY, algorithms=["HS256"]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token 无效")


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

    groups = conn.execute(
        """SELECT kg.name FROM kb_groups kg
           JOIN kb_group_members gm ON kg.id = gm.group_id
           WHERE gm.user_id = ?""", (user["id"],),
    ).fetchall()
    kb_groups = [g["name"] for g in groups]

    # 管理员可访问全部分组
    if "search_all" in permissions:
        all_groups = conn.execute("SELECT name FROM kb_groups").fetchall()
        kb_groups = [g["name"] for g in all_groups]

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


# ── 知识库分组 ──

@app.get("/api/kb/groups")
def list_kb_groups(user=Depends(get_current_user)):
    conn = _get_db()
    rows = conn.execute("SELECT * FROM kb_groups").fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


# ═══════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
