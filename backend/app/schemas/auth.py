"""认证相关 Pydantic schema。"""

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """注册请求。"""

    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$")
    password: str = Field(..., min_length=6, max_length=64)


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=64)


class ChangePasswordRequest(BaseModel):
    """修改密码请求。"""

    old_password: str = Field(..., min_length=1, max_length=64)
    new_password: str = Field(..., min_length=6, max_length=64)


class UserOut(BaseModel):
    """用户信息（不含敏感字段）。"""

    id: int
    username: str
    role: str


class TokenOut(BaseModel):
    """登录/注册成功返回的 token 与用户信息。"""

    access_token: str
    token_type: str = "bearer"
    user: UserOut
