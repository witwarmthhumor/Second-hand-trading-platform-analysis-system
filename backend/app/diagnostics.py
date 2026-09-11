"""启动期自检：把「配置层面的坑」在启动时就暴露出来，而不是等线上出事。

当前覆盖：
- 内置管理员仍使用弱默认口令（部署到公网等于开门）
- LLM 未配置 / 成本熔断阈值过低（会让兜底问答与竞品提炼静默降级）
- CORS 未显式配置（前后端分离场景下生产环境易踩坑）
"""

from __future__ import annotations

from app.config import settings
from app.security import is_weak_default_password

# 成本熔断阈值低于该值视为「配置过紧」：正常一次提炼/兜底问答即可能触发
_MIN_REASONABLE_COST_LIMIT_CNY = 0.05


def collect_startup_warnings() -> list[str]:
    """收集启动期告警信息（人类可读字符串列表，空列表表示无告警）。"""
    warnings: list[str] = []

    if is_weak_default_password(settings.admin_password):
        warnings.append(
            "内置管理员仍在使用弱默认口令，请通过 .env 的 ADMIN_PASSWORD 更换后再对外部署"
        )

    if not settings.llm_api_key:
        warnings.append(
            "未配置 LLM_API_KEY：兜底问答将返回降级提示，竞品提炼退回规则提取"
        )

    if settings.llm_cost_limit_cny < _MIN_REASONABLE_COST_LIMIT_CNY:
        warnings.append(
            f"LLM 成本熔断阈值仅 ¥{settings.llm_cost_limit_cny}，单次调用即可能触发熔断"
        )

    if settings.admin_password and len(settings.admin_password) < 8:
        warnings.append("内置管理员口令长度不足 8 位，建议同步提升")

    if not settings.rate_limit_enabled:
        warnings.append(
            "接口限流已关闭（RATE_LIMIT_ENABLED=false）：LLM 接口无频率保护，"
            "异常/恶意调用可直接打穿成本"
        )

    if not settings.cors_origins:
        warnings.append(
            "CORS 未配置（CORS_ORIGINS 为空）：浏览器将无法跨域调用 API，"
            "前后端分离部署时请在 .env 配置前端来源白名单"
        )

    return warnings
