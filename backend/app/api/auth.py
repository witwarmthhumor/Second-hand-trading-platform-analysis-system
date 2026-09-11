"""认证 API：注册 / 登录 / 当前用户 / 修改密码。

- POST /api/auth/register       注册（开放自助注册）
- POST /api/auth/login          登录，签发 access token
- GET  /api/auth/me             当前登录用户信息
- POST /api/auth/change-password 修改密码（需登录，校验旧密码）
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    TokenOut,
    UserOut,
)
from app.security import create_access_token, hash_password, verify_password
from app.services import login_guard

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(id=user.id, username=user.username, role=user.role)


def _token_out(user: User) -> TokenOut:
    token = create_access_token(user.id, user.username, user.role)
    return TokenOut(access_token=token, user=_user_out(user))


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    """注册新用户（普通用户角色），成功后直接返回 token（自动登录）。"""
    stmt = select(User).where(User.username == payload.username)
    result = await session.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role="user",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _token_out(user)


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenOut:
    """登录：校验用户名密码，签发 token。

    防爆破：同一用户名连续失败达到阈值即锁定一段时间；锁定期内直接 429，
    即使密码正确也拒绝，避免在线暴力破解。
    """
    # 先看是否处于锁定窗口（在查库前拦截，减少无效 DB 查询）
    retry_after = login_guard.check_locked(payload.username)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )

    stmt = select(User).where(User.username == payload.username)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        login_guard.register_failure(payload.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    login_guard.register_success(payload.username)
    return _token_out(user)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    """返回当前登录用户信息。"""
    return _user_out(user)


@router.post("/change-password", response_model=dict)
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """修改当前用户密码：校验旧密码，更新为新密码。"""
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="旧密码错误")

    user.password_hash = hash_password(payload.new_password)
    await session.commit()
    return {"changed": True}
