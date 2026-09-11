"""接口限流（R3）：按用户/IP 的滑动窗口限流，保护 LLM 成本与接口可用性。

背景：``/api/rag/ask(stream)``、``/api/fetch``、``/api/changes/monitor``、
``/api/report/export``、``/api/kb/rebuild`` 都会触发 LLM 调用或重计算，此前
完全无限流——一个脚本循环就能把 LLM 成本打穿（见 ``docs/现状分析与优化方向.md`` R3）。

算法：**滑动窗口日志**（每个 key 一个时间戳双端队列）。相比固定窗口不会在窗口
边界出现 2 倍突发；相比令牌桶实现更简单、行为更可预期。本场景阈值量级是「每分钟
数十次」，每 key 最多存 limit 个浮点数，内存开销可忽略。

维度：**已登录按 user id，匿名按客户端 IP**——避免共享出口 IP 下多用户互相误伤。
登录/注册固定按 IP（攻击者可换用户名喷洒，按账号计数挡不住）。昂贵接口在此之上
再叠一层**全局桶**，防止用多账号绕开单用户额度把 LLM 成本打穿。

取舍：进程内内存态（当前单进程部署，与 ``metrics``/``login_guard`` 一致）。
多副本部署时必须换成 Redis 共享计数，否则实际阈值会被副本数放大。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import jwt

from app.config import settings
from app.security import JWT_ALGORITHM

logger = logging.getLogger("app.ratelimit")

# 限流策略名
POLICY_LLM = "llm"
POLICY_AUTH = "auth"
POLICY_DEFAULT = "default"

# 面向用户的中文名（429 响应会直接展示给前端，避免出现 llm/auth 这类内部术语）
POLICY_LABELS = {
    POLICY_LLM: "智能问答/抓取",
    POLICY_AUTH: "登录/注册",
    POLICY_DEFAULT: "接口",
}

# 昂贵接口：会触发 LLM 调用或重计算，单独使用更严的额度
_COSTLY_ROUTES = frozenset(
    {
        ("POST", "/api/rag/ask"),
        ("POST", "/api/rag/ask/stream"),
        ("POST", "/api/fetch"),
        ("POST", "/api/changes/monitor"),
        ("POST", "/api/report/export"),
        ("POST", "/api/kb/rebuild"),
    }
)

# 认证接口：按 IP 限（与登录失败锁定互补：后者挡单账号爆破，这里挡多账号喷洒）
_AUTH_ROUTES = frozenset(
    {
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/register"),
    }
)

# 豁免路径：存活探测/指标/文档，不能因限流导致探活失败
_EXEMPT_PATHS = frozenset(
    {
        "/health",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
        "/favicon.ico",
    }
)

# 空闲 key 的回收阈值：桶内最后一条记录早于「当前 - 该秒数」即视为可回收
_STALE_SECONDS = 3600
# 计数超过该数量时才触发一次回收扫描（避免每次请求都遍历字典）
_PRUNE_THRESHOLD = 4096
_PRUNE_EVERY = 512


@dataclass(frozen=True)
class Policy:
    """一条限流策略。

    - ``limit`` <= 0 表示不限制（便于用配置一键关掉某类接口的限流）
    - ``global_limit`` > 0 时额外叠加全局桶（跨主体总量兜底）
    - ``by_ip`` 为 True 时主体固定用 IP，忽略登录态
    """

    name: str
    limit: int
    window_seconds: float = 60.0
    global_limit: int = 0
    by_ip: bool = False


@dataclass(frozen=True)
class Decision:
    """一次限流判定结果。``allowed=False`` 时由中间件短路返回 429。"""

    allowed: bool
    policy: str
    scope: str  # user（单主体）| global（全局桶）
    limit: int
    remaining: int
    retry_after: int  # 建议等待秒数；allowed=True 时为 0


def _llm_policy() -> Policy:
    return Policy(
        POLICY_LLM,
        settings.rate_limit_llm_per_minute,
        global_limit=settings.rate_limit_llm_global_per_minute,
    )


def _auth_policy() -> Policy:
    return Policy(POLICY_AUTH, settings.rate_limit_auth_per_minute, by_ip=True)


def _default_policy() -> Policy:
    return Policy(POLICY_DEFAULT, settings.rate_limit_default_per_minute)


def select_policy(method: str, path: str) -> Policy | None:
    """按方法与路径选策略；返回 None 表示本请求不限流。

    策略在每次调用时组装（而非模块级常量），这样测试与运行期改配置立即生效。
    """
    if not settings.rate_limit_enabled:
        return None
    if method.upper() == "OPTIONS" or path in _EXEMPT_PATHS:
        return None
    # 兼容尾斜杠（/api/fetch/ 与 /api/fetch 视为同一路由）
    normalized = path.rstrip("/") or path
    route = (method.upper(), normalized)
    if route in _COSTLY_ROUTES:
        return _llm_policy()
    if route in _AUTH_ROUTES:
        return _auth_policy()
    if path.startswith("/api/"):
        return _default_policy()
    return None


def client_ip(scope) -> str:
    """取客户端 IP。仅当显式信任反向代理时才读 X-Forwarded-For（否则可被伪造绕过）。"""
    if settings.rate_limit_trust_proxy:
        for key, value in scope.get("headers", []):
            if key == b"x-forwarded-for":
                first = value.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
    client = scope.get("client")
    return client[0] if client else "unknown"


def _user_id_from_scope(scope) -> str | None:
    """从 Authorization 头解析出用户 id；无 token / 解析失败返回 None（降级为按 IP）。"""
    for key, value in scope.get("headers", []):
        if key != b"authorization":
            continue
        raw = value.decode("latin-1")
        if not raw[:7].lower() == "bearer ":
            return None
        token = raw[7:].strip()
        try:
            payload = jwt.decode(
                token, settings.jwt_secret, algorithms=[JWT_ALGORITHM]
            )
        except Exception:  # noqa: BLE001  过期/伪造 token 一律降级为按 IP 计数
            return None
        sub = payload.get("sub")
        return str(sub) if sub else None
    return None


def subject_for(scope, policy: Policy) -> str:
    """计算限流主体：登录态按用户，匿名（或 by_ip 策略）按 IP。"""
    if not policy.by_ip:
        user_id = _user_id_from_scope(scope)
        if user_id:
            return f"u:{user_id}"
    return f"ip:{client_ip(scope)}"


class RateLimiter:
    """滑动窗口限流器（线程安全）。

    用 ``threading.Lock`` 而非 asyncio.Lock：与 ``Metrics`` 保持一致，且同步端点
    跑在线程池里时同样安全；临界区只有几次 deque 操作，不构成瓶颈。
    """

    def __init__(self, *, prune_threshold: int = _PRUNE_THRESHOLD) -> None:
        self._lock = threading.Lock()
        # {key: deque[monotonic 时间戳]}，key = "<policy>|<subject>"
        self._buckets: dict[str, deque[float]] = {}
        # {policy: 被拒次数}，供 /metrics 观测
        self._rejected: dict[str, int] = {}
        self._prune_threshold = prune_threshold
        self._checks_since_prune = 0

    def check(self, policy: Policy, subject: str) -> Decision:
        """判定并消费一次配额。命中即计数（无论后续业务是否成功）。"""
        now = time.monotonic()
        with self._lock:
            self._prune_if_needed(now)
            allowed, remaining, retry_after = self._consume(
                f"{policy.name}|{subject}", policy.limit, policy.window_seconds, now
            )
            if not allowed:
                self._reject(policy.name)
                return Decision(False, policy.name, "user", policy.limit, 0, retry_after)

            if policy.global_limit > 0:
                g_allowed, _, g_retry = self._consume(
                    f"{policy.name}|__global__",
                    policy.global_limit,
                    policy.window_seconds,
                    now,
                )
                if not g_allowed:
                    self._reject(policy.name)
                    return Decision(
                        False, policy.name, "global", policy.global_limit, 0, g_retry
                    )

            return Decision(True, policy.name, "user", policy.limit, remaining, 0)

    # ---- 内部实现（调用方需已持锁）----

    def _consume(
        self, key: str, limit: int, window: float, now: float
    ) -> tuple[bool, int, int]:
        """滑动窗口消费一次；返回 (是否放行, 剩余, 建议等待秒数)。"""
        if limit <= 0:
            return True, -1, 0

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = deque()

        # 滑出窗口的历史记录先清掉：窗口是「最近 window 秒」而非「整分钟」
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            # 最早那次请求滑出窗口时才有额度，据此给出精确等待秒数
            retry_after = max(1, int(bucket[0] + window - now) + 1)
            return False, 0, retry_after

        bucket.append(now)
        return True, limit - len(bucket), 0

    def _reject(self, policy: str) -> None:
        self._rejected[policy] = self._rejected.get(policy, 0) + 1

    def _prune_if_needed(self, now: float) -> None:
        """定期回收空闲 key，避免长期运行后 key 无限增长。"""
        self._checks_since_prune += 1
        if (
            self._checks_since_prune < _PRUNE_EVERY
            and len(self._buckets) <= self._prune_threshold
        ):
            return
        self._checks_since_prune = 0
        stale_cutoff = now - _STALE_SECONDS
        for key in [k for k, b in self._buckets.items() if not b or b[-1] <= stale_cutoff]:
            self._buckets.pop(key, None)

    # ---- 观测与测试辅助 ----

    def snapshot(self) -> dict:
        """导出限流状态（供 /metrics）。"""
        with self._lock:
            return {
                "enabled": settings.rate_limit_enabled,
                "tracked_keys": len(self._buckets),
                "rejected_by_policy": dict(sorted(self._rejected.items())),
                "policies": {
                    POLICY_LLM: {
                        "per_minute": settings.rate_limit_llm_per_minute,
                        "global_per_minute": settings.rate_limit_llm_global_per_minute,
                    },
                    POLICY_AUTH: {
                        "per_minute": settings.rate_limit_auth_per_minute,
                        "by": "ip",
                    },
                    POLICY_DEFAULT: {
                        "per_minute": settings.rate_limit_default_per_minute
                    },
                },
            }

    def reset(self) -> None:
        """清空窗口与被拒计数（测试用）。"""
        with self._lock:
            self._buckets.clear()
            self._rejected.clear()
            self._checks_since_prune = 0


def check_request(scope) -> Decision | None:
    """中间件入口：不限流返回 None，否则返回判定结果。"""
    policy = select_policy(scope.get("method", ""), scope.get("path", ""))
    if policy is None:
        return None
    return get_rate_limiter().check(policy, subject_for(scope, policy))


# 进程内单例
_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """返回全局限流器单例。"""
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
