"""知识库管理 API 测试：权限控制 + 文档列表/删除/上传。

不触发真实重建（embedding 依赖 Ollama）；rebuild 仅测权限与触发分支（mock）。
"""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.rag.config import CANONICAL_DIR

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _register(client) -> str:
    username = f"kb_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    return resp.json()["access_token"]


async def _admin_token(client) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "123456"}
    )
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_kb_requires_admin(client):
    """普通用户访问知识库管理返回 403，未登录返回 401。"""
    # 未登录
    resp = await client.get("/api/kb/documents")
    assert resp.status_code == 401

    # 普通用户
    token = await _register(client)
    resp = await client.get("/api/kb/documents", headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_can_list_documents(client):
    """admin 能列出知识库文档（分页结构）。"""
    token = await _admin_token(client)
    resp = await client.get("/api/kb/documents", headers=_auth(token))
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "total" in data and "items" in data
    assert data["total"] > 0
    assert len(data["items"]) > 0
    assert all("path" in d and "filename" in d for d in data["items"])


async def test_list_documents_pagination(client):
    """分页参数生效：每页条数受 page_size 约束，翻页不越界。"""
    token = await _admin_token(client)
    resp = await client.get(
        "/api/kb/documents", params={"page": 1, "page_size": 5}, headers=_auth(token)
    )
    data = resp.json()
    assert data["page"] == 1
    assert data["page_size"] == 5
    assert len(data["items"]) <= 5

    # 超范围页返回空 items，但 total 不变
    resp2 = await client.get(
        "/api/kb/documents", params={"page": 99999, "page_size": 5}, headers=_auth(token)
    )
    data2 = resp2.json()
    assert data2["items"] == []
    assert data2["total"] == data["total"]


async def test_delete_document_requires_admin_and_valid_path(client):
    """删除需 admin；路径穿越被拒。"""
    token = await _register(client)
    # 普通用户 403
    resp = await client.delete("/api/kb/documents", params={"path": "xxx"}, headers=_auth(token))
    assert resp.status_code == 403

    admin = await _admin_token(client)
    # 路径穿越：../ 指向允许目录之外 → 400
    resp = await client.delete(
        "/api/kb/documents", params={"path": "../outside/evil.md"}, headers=_auth(admin)
    )
    assert resp.status_code == 400


async def test_upload_validates_filename(client):
    """上传文件名非法返回 400。"""
    admin = await _admin_token(client)
    # 非 .md
    resp = await client.post(
        "/api/kb/documents",
        files={"file": ("bad.txt", b"x", "text/plain")},
        headers=_auth(admin),
    )
    assert resp.status_code == 400

    # id 不含下划线分隔
    resp = await client.post(
        "/api/kb/documents",
        files={"file": ("nounderscore.md", b"x", "text/markdown")},
        headers=_auth(admin),
    )
    assert resp.status_code == 400


async def test_upload_and_delete_roundtrip(client, monkeypatch):
    """admin 上传一个临时文档后可删除（不触发重建）。"""
    admin = await _admin_token(client)

    # 用一个已存在的分类目录避免新建
    subdirs = [d.name for d in CANONICAL_DIR.iterdir() if d.is_dir()]
    category = subdirs[0] if subdirs else None

    # 随机文件名避免与上次残留冲突（id 后段须为纯数字，符合 A-001 格式）
    import random

    filename = f"Z-{random.randint(10000, 99999)}_测试临时文档.md"
    resp = await client.post(
        "/api/kb/documents",
        files={"file": (filename, "# 标题\n正文".encode(), "text/markdown")},
        data={"category": category} if category else {},
        headers=_auth(admin),
    )
    assert resp.status_code == 200, resp.text
    path = resp.json()["path"]

    # 删除
    resp = await client.delete(
        "/api/kb/documents", params={"path": path}, headers=_auth(admin)
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # 再次删除应返回 deleted=False
    resp = await client.delete(
        "/api/kb/documents", params={"path": path}, headers=_auth(admin)
    )
    assert resp.json()["deleted"] is False


async def test_rebuild_requires_admin(client):
    """重建需 admin；普通用户 403。"""
    token = await _register(client)
    resp = await client.post("/api/kb/rebuild", headers=_auth(token))
    assert resp.status_code == 403

    admin = await _admin_token(client)
    resp = await client.post("/api/kb/rebuild", headers=_auth(admin))
    # 触发成功返回 triggered=True（后台任务不阻塞）
    assert resp.status_code == 200
    assert resp.json()["triggered"] is True
