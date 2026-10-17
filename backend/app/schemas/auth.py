"""认证相关 Pydantic schema。"""

from pydantic import BaseModel, Field, field_validator

from app.security import (
    PASSWORD_MIN_LENGTH,
    USERNAME_PATTERN,
    validate_password_strength,
)


class RegisterRequest(BaseModel):
    """注册请求。"""

    username: str = Field(..., min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=64)

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        """统一密码强度校验（长度/字母+数字/弱口令黑名单），不通过则 422。"""
        return validate_password_strength(value)


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(..., min_length=1, max_length=64)
    # 登录不校验强度：只做存在性与长度上限校验，避免把策略细节暴露给攻击者
    password: str = Field(..., min_length=1, max_length=64)


class ChangePasswordRequest(BaseModel):
    """修改密码请求。"""

    old_password: str = Field(..., min_length=1, max_length=64)
    new_password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=64)

    @field_validator("new_password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        """新密码同样走强度校验。"""
        return validate_password_strength(value)


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
