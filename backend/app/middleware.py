"""请求上下文中间件：request_id 注入 + 访问日志 + 指标采集。

实现为**纯 ASGI 中间件**（而非 BaseHTTPMiddleware）：后者会用内存缓冲包一层，
对 SSE 流式响应（``/api/rag/ask/stream``）有缓冲/截断风险；纯 ASGI 中间件只包裹
``send`` 消息，逐条透传，不破坏流式。
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders

from app.config import settings
from app.logging_config import set_request_id
from app.metrics import get_metrics
from app.rate_limit import POLICY_LABELS, check_request

logger = logging.getLogger("app.access")

# 路径归一化：把数字段、UUID、长十六进制串替换为 {id}，避免指标基数爆炸
_NUM_RE = re.compile(r"^\d+$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")

# 不记录访问日志的路径（存活探测噪音大）
_QUIET_PATHS = {"/health", "/metrics"}


def normalize_path(path: str) -> str:
    """把含动态参数的路径归一化为模板，如 /api/rag/sessions/abc123 → /api/rag/sessions/{id}。"""
    parts = []
    for seg in path.split("/"):
        if _NUM_RE.match(seg) or _HEX_RE.match(seg):
            parts.append("{id}")
        else:
            parts.append(seg)
    return "/".join(parts)


class RequestContextMiddleware:
    """纯 ASGI 中间件：为每个请求分配 request_id，记录访问日志与指标。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 复用上游（网关/前端）传入的 X-Request-ID，便于端到端串联；否则生成
        rid = ""
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                rid = value.decode("latin-1")
                break
        rid = rid or uuid.uuid4().hex[:12]
        set_request_id(rid)

        method = scope.get("method", "-")
        raw_path = scope.get("path", "/")
        path = normalize_path(raw_path)
        started = time.perf_counter()
        status_holder = {"status": 500}
        failed = False

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                # 回写 request_id，前端/排查时可直接对上
                MutableHeaders(scope=message).append("X-Request-ID", rid)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            failed = True
            elapsed_ms = (time.perf_counter() - started) * 1000
            get_metrics().observe_error()
            logger.exception(
                "未捕获异常",
                extra={
                    "extra_fields": {
                        "method": method,
                        "path": raw_path,
                        "duration_ms": round(elapsed_ms, 1),
                    }
                },
            )
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            status = status_holder["status"]
            get_metrics().observe_request(method, path, status, elapsed_ms)
            # 异常路径已在上面用 exception 记录，此处不重复刷访问日志
            if not failed and raw_path not in _QUIET_PATHS:
                fields = {
                    "method": method,
                    "path": raw_path,
                    "status": status,
                    "duration_ms": round(elapsed_ms, 1),
                }
                # 慢请求与 5xx 提升为 WARNING，便于快速筛选
                if status >= 500 or elapsed_ms >= settings.slow_request_ms:
                    logger.warning("请求完成", extra={"extra_fields": fields})
                else:
                    logger.info("请求完成", extra={"extra_fields": fields})


class RateLimitMiddleware:
    """接口限流中间件（R3）：超限直接短路返回 429，**不进入业务处理**。

    这是限流的价值所在——被挡下的请求不会触达 LLM，因此不会产生费用；若放在
    业务层内部判断，成本已经花掉了。

    同样实现为纯 ASGI 中间件：只需在判定通过时原样透传 ``send``，不影响 SSE 流式。
    注册在 ``RequestContextMiddleware`` 内层，这样 429 也会带上 request_id、计入
    访问日志与 4xx 状态分布（否则「谁被限流了」这条线索会丢）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        decision = check_request(scope)
        if decision is None or decision.allowed:
            await self.app(scope, receive, send)
            return

        await self._reject(scope, send, decision)

    async def _reject(self, scope, send, decision) -> None:
        """返回 429：带 Retry-After 与额度头，便于客户端退避而非盲目重试。"""
        get_metrics().observe_rate_limited(decision.policy)
        if decision.scope == "global":
            detail = "服务当前请求量过高，请稍后重试"
        else:
            label = POLICY_LABELS.get(decision.policy, decision.policy)
            detail = (
                f"请求过于频繁（{label}每分钟限 {decision.limit} 次），请稍后重试"
            )
        logger.warning(
            "请求被限流",
            extra={
                "extra_fields": {
                    "policy": decision.policy,
                    "scope": decision.scope,
                    "method": scope.get("method", "-"),
                    "path": scope.get("path", "/"),
                    "retry_after": decision.retry_after,
                }
            },
        )

        body = json.dumps(
            {
                "detail": detail,
                "code": "rate_limited",
                "policy": decision.policy,
                "scope": decision.scope,
                "retry_after": decision.retry_after,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("latin-1")),
            (b"retry-after", str(decision.retry_after).encode("latin-1")),
            (b"x-ratelimit-limit", str(decision.limit).encode("latin-1")),
            (b"x-ratelimit-remaining", b"0"),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})
