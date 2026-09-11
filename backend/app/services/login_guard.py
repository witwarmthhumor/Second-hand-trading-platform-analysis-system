"""登录失败锁定：防暴力破解的进程内计数器。

策略：同一用户名连续失败达到 ``login_max_failures`` 次即锁定
``login_lockout_minutes`` 分钟；锁定期内即使密码正确也拒绝，到期自动解锁；
登录成功清零计数。

实现取舍：
- 进程内内存态（当前为单进程部署）。多副本部署时应改为 Redis 共享计数，
  否则攻击者可分散到各副本绕过阈值。
- 按**用户名**计数（而非 IP）：避免共享出口 IP 下的误伤；代价是攻击者可
  用大量用户名做喷洒式尝试。两者结合最稳，本轮先落地用户名维度。
"""

from __future__ import annotations

import threading
import time

from app.config import settings


class LoginLocked(Exception):
    """账号处于锁定窗口内。"""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"账号已锁定，请 {retry_after_seconds} 秒后重试")
        self.retry_after_seconds = retry_after_seconds


_lock = threading.Lock()
# {username: {"failures": int, "locked_until": float}}
_state: dict[str, dict[str, float]] = {}


def _now() -> float:
    return time.monotonic()


def check_locked(username: str) -> int | None:
    """若该用户名处于锁定期，返回剩余秒数；否则返回 None 并清理过期锁。"""
    with _lock:
        entry = _state.get(username)
        if entry is None:
            return None
        locked_until = entry.get("locked_until", 0.0)
        if locked_until and locked_until > _now():
            return int(locked_until - _now()) + 1
        # 锁已过期：清理，允许重新尝试
        if locked_until:
            _state.pop(username, None)
        return None


def register_failure(username: str) -> None:
    """记录一次登录失败；达到阈值则进入锁定窗口。"""
    with _lock:
        entry = _state.setdefault(username, {"failures": 0.0, "locked_until": 0.0})
        entry["failures"] = float(entry.get("failures", 0.0)) + 1
        if entry["failures"] >= settings.login_max_failures:
            entry["locked_until"] = _now() + settings.login_lockout_minutes * 60
            entry["failures"] = 0.0  # 锁定后计数清零，解锁后重新累计


def register_success(username: str) -> None:
    """登录成功：清空该用户名的失败记录。"""
    with _lock:
        _state.pop(username, None)


def reset() -> None:
    """清空全部状态（测试用）。"""
    with _lock:
        _state.clear()
