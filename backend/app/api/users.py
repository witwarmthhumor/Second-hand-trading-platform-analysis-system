"""用户管理 API（仅管理员）：账号增删改查。

- GET    /api/users          用户列表（含创建时间，按 id 升序）
- POST   /api/users          创建用户（用户名/密码/角色，密码走统一强度校验）
- PUT    /api/users/{id}     更新用户（改名 / 重置密码 / 调整角色，均可选字段）
- DELETE /api/users/{id}     删除用户（级联清理其会话/反馈/Agent 运行记录）

为什么全部挂在 ``require_admin`` 上：账号管理是权限的根——能改角色就能自我提权，
必须整体收进管理员边界，一个端点都不例外。

两条防锁死守卫（写在业务层因为需要查库计数）：
1. **不能删除当前登录账号自己**——误点自删等于当场把自己锁在系统外；
2. **不能删除/降级最后一个 admin**——否则系统再无管理员，只能进库手改。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import require_admin
from app.models.user import ROLE_ADMIN, User
from app.schemas.users import (
    AdminUserOut,
    UserBatchDeleteRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from app.security import hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


def _out(user: User) -> AdminUserOut:
    """ORM → 管理视角出参（含 created_at；password_hash 永不出参）。"""
    return AdminUserOut(
        id=user.id, username=user.username, role=user.role, created_at=user.created_at
    )


async def _get_or_404(session: AsyncSession, user_id: int) -> User:
    """按 id 取用户；不存在返回 404（不泄露「存在但权限不足」之类的差异）。"""
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return user


async def _count_admins(session: AsyncSession) -> int:
    """锁住全部 admin 行并计数（防锁死守卫的并发安全口径）。

    ``SELECT ... FOR UPDATE`` 把「计数 → 删除/降级 → 提交」之间的窗口串行化：
    并发的账号管理写操作在锁上排队，杜绝「两个请求各自通过守卫、最终 0 个
    admin」的竞态（质量审计 P2）。只锁 admin 行，普通用户的增删不受影响。
    """
    result = await session.execute(
        select(User.id).where(User.role == ROLE_ADMIN).with_for_update()
    )
    return len(result.scalars().all())


@router.get("", response_model=list[AdminUserOut])
async def list_users(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AdminUserOut]:
    """用户列表（管理视角，含创建时间），按 id 升序。"""
    result = await session.execute(select(User).order_by(User.id))
    return [_out(user) for user in result.scalars()]


@router.post("", response_model=AdminUserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreateRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """创建用户：用户名唯一（409），密码强度与自助注册同规（422 由 schema 拦）。"""
    exists = await session.execute(
        select(User).where(User.username == payload.username)
    )
    if exists.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _out(user)


@router.put("/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """更新用户：username / password / role 三个字段都可选，只动显式传入的。

    降级守卫：把「最后一个 admin」改成 user 会被 409 拒绝——系统必须
    永远至少保留一名管理员（改自己同样受限，守卫不区分目标是否为操作者）。
    """
    user = await _get_or_404(session, user_id)

    if payload.role is not None and user.role == ROLE_ADMIN and payload.role != ROLE_ADMIN:
        # 目标当前是 admin 且这次要摘掉 admin：只剩他一个时不放行。
        if await _count_admins(session) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="不能降级最后一个管理员",
            )

    if payload.username is not None and payload.username != user.username:
        exists = await session.execute(
            select(User).where(User.username == payload.username, User.id != user_id)
        )
        if exists.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")
        user.username = payload.username

    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.role is not None:
        user.role = payload.role

    await session.commit()
    await session.refresh(user)
    return _out(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """删除用户及其全部从属数据（会话/反馈等按 FK 级联清理；agent_runs 表
    有意保留——只撤代码不撤数据，历史迁移仍会级联到它）。

    守卫：① 不能删自己（操作者当场被锁在系统外）；② 不能删最后一个 admin。
    """
    user = await _get_or_404(session, user_id)
    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="不能删除当前登录的账号"
        )
    if user.role == ROLE_ADMIN and await _count_admins(session) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="不能删除最后一个管理员"
        )

    await session.delete(user)
    await session.commit()


@router.post("/batch-delete")
async def batch_delete_users(
    payload: UserBatchDeleteRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """批量删除用户：**全有或全无**——任一守卫不满足则整批拒绝、一个都不删。

    守卫（与单个删除同源，批量下整体判定）：
    - 列表包含当前登录账号 → 409（自删等于当场把自己锁在系统外）；
    - 任一 id 不存在 → 404（用户删除是重操作，**不静默跳过**——这点刻意与
      rag 会话批量删除的「跳过缺失」不同：账号删错代价不可逆）；
    - 本批会把 admin 全删光 → 409（至少保留一名）。

    Returns:
        ``{"deleted": 实删数, "requested": 去重后请求数}``。
    """
    ids = list(payload.user_ids)
    if admin.id in ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="批量列表中包含当前登录的账号，已整体拒绝",
        )

    rows = (await session.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    found = {user.id: user for user in rows}
    missing = [uid for uid in ids if uid not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"用户不存在（id：{missing[:5]}），已整体拒绝",
        )

    admins_to_delete = sum(1 for u in found.values() if u.role == ROLE_ADMIN)
    if admins_to_delete and admins_to_delete >= await _count_admins(session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="批量列表包含全部管理员，至少需保留一名，已整体拒绝",
        )

    # 一条批量 DELETE 落库（性能优化：N 用户 = 1 次往返而非 N 次）。子表的
    # 会话/反馈/Agent 运行记录按 FK 的 ondelete="CASCADE" 由数据库级联清理；
    # User 未声明 ORM relationship，所以单删时代的逐条 session.delete 本就
    # 不加载子行——批量下换成集合语句没有任何行为差异，只有往返数的差别。
    # synchronize_session=False：会话随即提交结束，无需同步内存中的身份映射。
    result = await session.execute(
        delete(User).where(User.id.in_(ids)).execution_options(synchronize_session=False)
    )
    await session.commit()
    return {"deleted": int(result.rowcount), "requested": len(ids)}
