"""用户管理相关 Pydantic schema（仅管理员接口使用）。

与 ``schemas/auth.py`` 的分工：``UserOut`` 是登录态的轻量用户信息（id/username/role）；
``AdminUserOut`` 多暴露 ``created_at``（管理列表要展示），供管理接口专用——
不动 auth 的既有契约，避免登录/注册响应结构漂移。
"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.user import ROLE_ADMIN, ROLE_USER
from app.security import (
    PASSWORD_MIN_LENGTH,
    USERNAME_PATTERN,
    validate_password_strength,
)

# 用户名规则与注册口径完全一致（字母/数字/下划线/连字符，3~64）
# 角色只允许这两个值；写在 schema 层，非法值在 422 就被拦下（进不了业务代码）
_ROLE_PATTERN = rf"^({ROLE_USER}|{ROLE_ADMIN})$"


class AdminUserOut(BaseModel):
    """管理视角的用户信息（含创建时间，不含任何敏感字段）。"""

    id: int
    username: str
    role: str
    created_at: datetime


class UserCreateRequest(BaseModel):
    """管理员创建用户请求。"""

    username: str = Field(..., min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=64)
    role: str = Field(default=ROLE_USER, pattern=_ROLE_PATTERN)

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        """与自助注册同一套强度校验（长度/字母+数字/弱口令黑名单），不通过则 422。"""
        return validate_password_strength(value)


class UserUpdateRequest(BaseModel):
    """管理员更新用户请求：三个字段都可选，只更新显式传入的字段。

    - ``username``：允许改名（token 按 user.id 鉴权，改名不使已有登录失效）；
    - ``password``：管理员重置密码，走同一套强度校验；
    - ``role``：角色调整。「降最后一个 admin」的防锁死守卫在业务层（需要查库计数）。
    """

    model_config = {"extra": "forbid"}

    username: str | None = Field(default=None, min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    password: str | None = Field(default=None, min_length=PASSWORD_MIN_LENGTH, max_length=64)
    role: str | None = Field(default=None, pattern=_ROLE_PATTERN)

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str | None) -> str | None:
        """None 表示本次不改密码，跳过强度校验。"""
        if value is None:
            return None
        return validate_password_strength(value)


class UserBatchDeleteRequest(BaseModel):
    """管理员批量删除用户请求（全有或全无，守卫见 api/users.batch_delete_users）。"""

    model_config = {"extra": "forbid"}

    user_ids: list[int] = Field(..., min_length=1, max_length=100)

    @field_validator("user_ids")
    @classmethod
    def _dedupe(cls, value: list[int]) -> list[int]:
        """去重保序：重复勾选同一行不该把 requested 计数撑大。"""
        return list(dict.fromkeys(value))
