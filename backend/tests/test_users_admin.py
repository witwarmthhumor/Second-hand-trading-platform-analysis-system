"""用户管理 API 测试（仅管理员）：账号增删改查与两条防锁死守卫。

被测对象：``app/api/users.py``。核心承诺：

1. **权限边界**：全部端点挂在 ``require_admin`` 上——未认证 401、普通用户 403，
   一个端点都不例外；
2. **CRUD 正确性**：创建（409 重名 / 422 弱密码与非法角色走 schema）、更新
   （改名 409 / 重置密码后新密码可登录 / 角色调整生效）、删除（级联清理从属数据）；
3. **两条防锁死守卫**：不能删自己；不能删除/降级最后一个 admin。
"""

import secrets

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.models.user import ROLE_ADMIN, ROLE_USER, User

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _admin_headers(client) -> dict[str, str]:
    """以内置管理员登录，返回 Authorization 头（conftest 已 seed admin/123456）。"""
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "123456"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create_user(client, headers, username: str, password: str = "secret123", role: str = ROLE_USER) -> dict:
    """管理员建一个用户，返回出参（断言 201 已由调用处可见）。"""
    resp = await client.post(
        "/api/users",
        json={"username": username, "password": password, "role": role},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _admin_count_from_db() -> int:
    """直查库数 admin（防锁死守卫的独立口径，不信 HTTP 出参）。"""
    from app.db import async_session_factory

    async with async_session_factory() as session:
        result = await session.execute(
            select(User).where(User.role == ROLE_ADMIN)
        )
        return len(result.scalars().all())


async def _demote_all_other_admins(client, headers: dict[str, str], keep_id: int) -> None:
    """把除 ``keep_id`` 外的全部 admin 降级，制造「只剩一个 admin」的确定性前提。

    全量回归时其它用例（如 test_chat_session_service 的 svc_admin）可能在库里
    留下额外的 admin——「最后一个 admin」守卫按**全库计数**判定，测试不能假设
    内置 admin 是唯一的。显式清场后守卫的前提才可复现。
    """
    resp = await client.get("/api/users", headers=headers)
    assert resp.status_code == 200, resp.text
    for user in resp.json():
        if user["role"] == ROLE_ADMIN and user["id"] != keep_id:
            demote = await client.put(
                f"/api/users/{user['id']}", json={"role": ROLE_USER}, headers=headers
            )
            assert demote.status_code == 200, demote.text


# ---------------------------------------------------------------------------
# 权限边界
# ---------------------------------------------------------------------------


async def test_未认证访问用户列表返回401(client):
    resp = await client.get("/api/users")
    assert resp.status_code == 401


async def test_普通用户访问用户管理一律403(client):
    """列表/创建/更新/删除四个端点对普通用户全部 403——权限是端点级的。"""
    import secrets

    username = f"user_{secrets.token_hex(4)}"
    resp = await client.post(
        "/api/auth/register", json={"username": username, "password": "secret123"}
    )
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.get("/api/users", headers=headers)).status_code == 403
    assert (
        await client.post(
            "/api/users",
            json={"username": "x_user", "password": "secret123"},
            headers=headers,
        )
    ).status_code == 403
    assert (
        await client.put("/api/users/1", json={"role": "admin"}, headers=headers)
    ).status_code == 403
    assert (await client.delete("/api/users/1", headers=headers)).status_code == 403


# ---------------------------------------------------------------------------
# 创建（增）
# ---------------------------------------------------------------------------


async def test_管理员创建用户并可登录(client):
    headers = await _admin_headers(client)
    username = f"mgr_{secrets.token_hex(4)}"
    created = await _create_user(client, headers, username, "secret123", ROLE_USER)

    assert created["username"] == username
    assert created["role"] == ROLE_USER
    assert created["created_at"]  # 管理列表要有创建时间
    assert "password" not in created and "password_hash" not in created

    # 新账号能正常登录（密码按同一套 hash 落库）
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    assert resp.status_code == 200


async def test_创建重名用户返回409(client):
    headers = await _admin_headers(client)
    username = f"dup_{secrets.token_hex(4)}"
    await _create_user(client, headers, username)

    resp = await client.post(
        "/api/users",
        json={"username": username, "password": "secret123"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_创建用户弱密码与非法角色被422拦截(client):
    headers = await _admin_headers(client)

    # 弱密码：与自助注册同一套强度校验（弱口令黑名单）
    resp = await client.post(
        "/api/users",
        json={"username": f"weak_{secrets.token_hex(3)}", "password": "123456"},
        headers=headers,
    )
    assert resp.status_code == 422

    # 非法角色：schema 层白名单拦下，进不了业务代码
    resp = await client.post(
        "/api/users",
        json={
            "username": f"badrole_{secrets.token_hex(3)}",
            "password": "secret123",
            "role": "superuser",
        },
        headers=headers,
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 更新（改）
# ---------------------------------------------------------------------------


async def test_重置密码后旧密码失效新密码可登录(client):
    headers = await _admin_headers(client)
    username = f"pwd_{secrets.token_hex(4)}"
    created = await _create_user(client, headers, username, "secret123")

    resp = await client.put(
        f"/api/users/{created['id']}", json={"password": "newpass456"}, headers=headers
    )
    assert resp.status_code == 200, resp.text

    old_login = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    assert old_login.status_code == 401
    new_login = await client.post(
        "/api/auth/login", json={"username": username, "password": "newpass456"}
    )
    assert new_login.status_code == 200


async def test_改名生效且与既有用户名冲突时409(client):
    headers = await _admin_headers(client)
    name_a = f"ren_a_{secrets.token_hex(3)}"
    name_b = f"ren_b_{secrets.token_hex(3)}"
    user_a = await _create_user(client, headers, name_a)
    await _create_user(client, headers, name_b)

    # 改成已存在的用户名 → 409
    resp = await client.put(
        f"/api/users/{user_a['id']}", json={"username": name_b}, headers=headers
    )
    assert resp.status_code == 409

    # 改成全新用户名 → 生效且能用新名登录
    name_c = f"ren_c_{secrets.token_hex(3)}"
    resp = await client.put(
        f"/api/users/{user_a['id']}", json={"username": name_c}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["username"] == name_c
    login = await client.post(
        "/api/auth/login", json={"username": name_c, "password": "secret123"}
    )
    assert login.status_code == 200


async def test_角色调整生效_降级最后一个admin被409拒绝(client):
    """防锁死守卫②：库里只剩一个 admin 时，把他降级为 user 必须被拒。"""
    headers = await _admin_headers(client)
    # 再建一个 admin，确认「多于一个时可以降级」
    extra_admin = await _create_user(
        client, headers, f"adm_{secrets.token_hex(4)}", "secret123", ROLE_ADMIN
    )
    resp = await client.put(
        f"/api/users/{extra_admin['id']}", json={"role": ROLE_USER}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["role"] == ROLE_USER

    # 清场：把库里其它 admin 全部降级，让内置管理员成为最后一个
    admin_user = await client.get("/api/auth/me", headers=headers)
    admin_id = admin_user.json()["id"]
    await _demote_all_other_admins(client, headers, keep_id=admin_id)

    # 现在降级最后一个 admin → 409；库里 admin 数不变
    resp = await client.put(
        f"/api/users/{admin_id}", json={"role": ROLE_USER}, headers=headers
    )
    assert resp.status_code == 409
    assert (await _admin_count_from_db()) == 1


# ---------------------------------------------------------------------------
# 删除（删）
# ---------------------------------------------------------------------------


async def test_删除用户_删除后无法登录(client):
    headers = await _admin_headers(client)
    username = f"del_{secrets.token_hex(4)}"
    created = await _create_user(client, headers, username)

    resp = await client.delete(f"/api/users/{created['id']}", headers=headers)
    assert resp.status_code == 204

    gone = await client.delete(f"/api/users/{created['id']}", headers=headers)
    assert gone.status_code == 404  # 二次删除 404（幂等边界明确）

    login = await client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    assert login.status_code == 401


async def test_删除自己被409拒绝(client):
    """防锁死守卫①：admin 不能删当前登录账号——误点自删等于把自己锁在系统外。"""
    headers = await _admin_headers(client)
    me = await client.get("/api/auth/me", headers=headers)
    my_id = me.json()["id"]

    resp = await client.delete(f"/api/users/{my_id}", headers=headers)
    assert resp.status_code == 409
    # 账号仍在：之后的管理操作照常可用
    assert (await client.get("/api/users", headers=headers)).status_code == 200


async def test_删除最后一个admin被409拒绝(client):
    """防锁死守卫②（删除路径）：库里只剩一个 admin 时不能删。"""
    headers = await _admin_headers(client)
    admin_user = await client.get("/api/auth/me", headers=headers)
    admin_id = admin_user.json()["id"]
    # 清场：其它用例可能留了额外 admin，先降级到只剩内置管理员
    await _demote_all_other_admins(client, headers, keep_id=admin_id)

    resp = await client.delete(f"/api/users/{admin_id}", headers=headers)
    assert resp.status_code == 409
    assert (await _admin_count_from_db()) == 1


async def test_列表含种子管理员且出参不含敏感字段(client):
    headers = await _admin_headers(client)
    resp = await client.get("/api/users", headers=headers)
    assert resp.status_code == 200
    users = resp.json()
    assert any(u["username"] == "admin" and u["role"] == ROLE_ADMIN for u in users)
    for u in users:
        assert set(u) == {"id", "username", "role", "created_at"}


# ---------------------------------------------------------------------------
# 批量删除（全有或全无）
# ---------------------------------------------------------------------------


async def test_批量删除多个用户_一次删净且无法登录(client):
    headers = await _admin_headers(client)
    names = [f"batch_{secrets.token_hex(4)}" for _ in range(3)]
    ids = [(await _create_user(client, headers, n))["id"] for n in names]

    resp = await client.post(
        "/api/users/batch-delete", json={"user_ids": ids}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"deleted": 3, "requested": 3}

    # 全部删净：登录 401 + 重复 id 去重后再删 404
    for n in names:
        login = await client.post(
            "/api/auth/login", json={"username": n, "password": "secret123"}
        )
        assert login.status_code == 401
    gone = await client.post(
        "/api/users/batch-delete", json={"user_ids": ids}, headers=headers
    )
    assert gone.status_code == 404


async def test_批量删除含自己的id_整批拒绝且一个都没删(client):
    headers = await _admin_headers(client)
    victim = await _create_user(client, headers, f"self_{secrets.token_hex(4)}")
    me = (await client.get("/api/auth/me", headers=headers)).json()["id"]

    resp = await client.post(
        "/api/users/batch-delete", json={"user_ids": [victim["id"], me]}, headers=headers
    )
    assert resp.status_code == 409

    # 原子性：victim 仍在（还能被单独查到/编辑）
    still = await client.put(
        f"/api/users/{victim['id']}", json={"role": ROLE_USER}, headers=headers
    )
    assert still.status_code == 200


async def test_批量删除会删光admin_整批拒绝(client):
    """防锁死守卫（批量路径）：把库里全部 admin 都选进批次 → 409 一个不删。"""
    headers = await _admin_headers(client)
    await _create_user(
        client, headers, f"adm2_{secrets.token_hex(4)}", "secret123", ROLE_ADMIN
    )
    admin_users = [
        u for u in (await client.get("/api/users", headers=headers)).json()
        if u["role"] == ROLE_ADMIN
    ]
    assert len(admin_users) >= 2

    resp = await client.post(
        "/api/users/batch-delete",
        json={"user_ids": [u["id"] for u in admin_users]},
        headers=headers,
    )
    assert resp.status_code == 409
    assert (await _admin_count_from_db()) == len(admin_users)


async def test_批量删除含不存在的id_404且一个都没删(client):
    headers = await _admin_headers(client)
    victim = await _create_user(client, headers, f"miss_{secrets.token_hex(4)}")

    resp = await client.post(
        "/api/users/batch-delete",
        json={"user_ids": [victim["id"], 99999999]},
        headers=headers,
    )
    assert resp.status_code == 404

    # 原子性：victim 仍在
    still = await client.put(
        f"/api/users/{victim['id']}", json={"role": ROLE_USER}, headers=headers
    )
    assert still.status_code == 200


async def test_批量删除的权限边界(client):
    """未认证 401；普通用户 403——批量入口与单个删除同一权限边界。"""
    resp = await client.post("/api/users/batch-delete", json={"user_ids": [1]})
    assert resp.status_code == 401

    username = f"bd_{secrets.token_hex(4)}"
    token = (
        await client.post(
            "/api/auth/register", json={"username": username, "password": "secret123"}
        )
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/api/users/batch-delete", json={"user_ids": [1]}, headers=headers
    )
    assert resp.status_code == 403
