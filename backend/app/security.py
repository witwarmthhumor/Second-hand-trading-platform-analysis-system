"""安全工具：bcrypt 密码哈希与 JWT 签发/校验。

- 密码用 bcrypt 哈希存储（自带盐，无需手动管理 salt）
- JWT 使用 HS256，secret 从 settings.jwt_secret 注入（必须经 .env 配置）
- access token 有效期由 settings.jwt_expire_minutes 控制（默认 24h）
"""

import re
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings

# JWT 签发算法
JWT_ALGORITHM = "HS256"

# 密码策略：最短长度
PASSWORD_MIN_LENGTH = 8
# 弱口令黑名单（小写比较）：命中即拒绝，覆盖最常见默认口令
WEAK_PASSWORDS = frozenset(
    {
        "123456",
        "12345678",
        "123456789",
        "password",
        "passw0rd",
        "admin123",
        "admin888",
        "qwerty123",
        "abc12345",
        "11111111",
        "00000000",
        "iloveyou",
    }
)

_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")


def validate_password_strength(password: str) -> str:
    """校验密码强度，不通过则抛 ValueError（由调用方转成 422/400）。

    规则（对普通用户足够，避免过度复杂导致体验下降）：
    - 长度 >= PASSWORD_MIN_LENGTH（8）
    - 必须同时包含字母与数字（挡住纯数字、纯字母等弱口令）
    - 不在常见弱口令黑名单内
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码长度至少 {PASSWORD_MIN_LENGTH} 位")
    if not _HAS_LETTER.search(password) or not _HAS_DIGIT.search(password):
        raise ValueError("密码需同时包含字母与数字")
    if password.lower() in WEAK_PASSWORDS:
        raise ValueError("密码过于简单，请更换更复杂的密码")
    return password


def is_weak_default_password(password: str) -> bool:
    """判断是否仍在使用内置默认口令（启动时据此告警）。"""
    return password.lower() in WEAK_PASSWORDS


def hash_password(plain: str) -> str:
    """对明文密码做 bcrypt 哈希。"""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验明文密码是否匹配哈希。"""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # 哈希格式损坏时返回 False，而非抛异常
        return False


def create_access_token(user_id: int, username: str, role: str) -> str:
    """签发 access token，payload 含 sub/username/role/exp/iat。"""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """校验并解码 token，返回 payload；无效/过期抛 jwt 异常。"""
    return jwt.decode(token, settings.jwt_secret, algorithms=[JWT_ALGORITHM])
