"""轻量进程内指标：请求计数、状态码分布、延迟统计。

定位：不引入 Prometheus 依赖，提供最小可用的运行时可观测性——出问题时能回答
「这段时间有多少请求、多少 5xx、慢请求集中在哪个端点」。

注意：指标是**单进程内存态**，多副本部署时每个副本各算各的；当前为单进程运行，
够用。重启即清零。
"""

from __future__ import annotations

import threading
import time

# 延迟分位近似用的桶上界（毫秒）
_LATENCY_BUCKETS_MS = (50, 100, 200, 500, 1000, 3000, 10000)


class Metrics:
    """线程安全的进程内指标收集器（uvicorn 多线程/异步混用下也能安全累加）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._total = 0
        # {(method, path, status): count}
        self._by_route: dict[tuple[str, str, int], int] = {}
        # 状态码分组计数：2xx/4xx/5xx
        self._by_class: dict[str, int] = {}
        self._latency_sum_ms = 0.0
        self._latency_max_ms = 0.0
        # 各延迟桶累计次数（len = len(_LATENCY_BUCKETS_MS) + 1，最后一桶为 +inf）
        self._latency_buckets = [0] * (len(_LATENCY_BUCKETS_MS) + 1)
        self._errors = 0  # 未捕获异常（500）计数
        # 被限流（429）次数，按策略分组：{policy: count}
        self._rate_limited: dict[str, int] = {}

    def observe_request(
        self, method: str, path: str, status: int, duration_ms: float
    ) -> None:
        """记录一次请求。path 应已归一化为路由模板，避免指标基数爆炸。"""
        with self._lock:
            self._total += 1
            key = (method, path, status)
            self._by_route[key] = self._by_route.get(key, 0) + 1
            cls = f"{status // 100}xx"
            self._by_class[cls] = self._by_class.get(cls, 0) + 1
            self._latency_sum_ms += duration_ms
            self._latency_max_ms = max(self._latency_max_ms, duration_ms)
            for idx, bound in enumerate(_LATENCY_BUCKETS_MS):
                if duration_ms <= bound:
                    self._latency_buckets[idx] += 1
                    break
            else:
                self._latency_buckets[-1] += 1

    def observe_error(self) -> None:
        """记录一次未捕获异常（中间件兜底 500）。"""
        with self._lock:
            self._errors += 1

    def observe_rate_limited(self, policy: str) -> None:
        """记录一次被限流拦截（429），按策略分组，便于判断是哪类接口在被刷。"""
        with self._lock:
            self._rate_limited[policy] = self._rate_limited.get(policy, 0) + 1

    def snapshot(self) -> dict:
        """导出当前指标快照（JSON 可序列化）。"""
        with self._lock:
            buckets = {
                f"<={bound}ms": self._latency_buckets[idx]
                for idx, bound in enumerate(_LATENCY_BUCKETS_MS)
            }
            buckets[f">{_LATENCY_BUCKETS_MS[-1]}ms"] = self._latency_buckets[-1]
            top_routes = sorted(
                (
                    {
                        "method": m,
                        "path": p,
                        "status": s,
                        "count": c,
                    }
                    for (m, p, s), c in self._by_route.items()
                ),
                key=lambda item: item["count"],
                reverse=True,
            )[:20]
            return {
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "requests_total": self._total,
                "unhandled_errors": self._errors,
                "rate_limited_total": sum(self._rate_limited.values()),
                "rate_limited_by_policy": dict(sorted(self._rate_limited.items())),
                "status_classes": dict(sorted(self._by_class.items())),
                "latency_avg_ms": round(self._latency_sum_ms / self._total, 2)
                if self._total
                else 0.0,
                "latency_max_ms": round(self._latency_max_ms, 2),
                "latency_buckets": buckets,
                "top_routes": top_routes,
            }

    def reset(self) -> None:
        """清零（测试用）。"""
        with self._lock:
            self._total = 0
            self._by_route.clear()
            self._by_class.clear()
            self._latency_sum_ms = 0.0
            self._latency_max_ms = 0.0
            self._latency_buckets = [0] * (len(_LATENCY_BUCKETS_MS) + 1)
            self._errors = 0
            self._rate_limited.clear()
            self._started_at = time.time()


# 进程内单例
_metrics: Metrics | None = None


def get_metrics() -> Metrics:
    """返回全局指标单例。"""
    global _metrics
    if _metrics is None:
        _metrics = Metrics()
    return _metrics
