"""安全工具：bcrypt 密码哈希与 JWT 签发/校验。

- 密码用 bcrypt 哈希存储（自带盐，无需手动管理 salt）
- JWT 使用 HS256，secret 从 settings.jwt_secret 注入（必须经 .env 配置）
- access token 有效期由 settings.jwt_expire_minutes 控制（默认 24h）
"""

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings

# JWT 签发算法
JWT_ALGORITHM = "HS256"


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
