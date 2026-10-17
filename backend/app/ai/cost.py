"""LLM 成本估算与会话级熔断器。

产品需求：API 调用费用超过阈值（默认 1 元）即中断，经用户同意后才继续。

设计要点：
- 内存态累计 + DB 持久化（存 app_settings 表），重启不丢累计。
  record 同步更新内存后，若在事件循环内则 fire-and-forget 异步落库；
  restore 由成本 API 端点首次访问时 await 恢复。
- 三种形态统一：单次调用预检（发请求前估算）、累计消费熔断、放行审批。
- 熔断后后续调用一律被拒，直到用户通过审批端点放行一次或重置累计。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.config import settings

logger = logging.getLogger(__name__)

# 持久化在 app_settings 表的 key
_PERSIST_KEY = "llm_cost_state"


class CostTracker:
    """会话级成本记账与熔断。

    线程安全：uvicorn 单进程、异步单线程事件循环内运行，dict 读写无竞争。
    """

    def __init__(
        self,
        cost_limit_cny: float | None = None,
        input_price: float | None = None,
        output_price: float | None = None,
        usd_rate: float | None = None,
    ) -> None:
        self.cost_limit_cny = (
            cost_limit_cny
            if cost_limit_cny is not None
            else settings.llm_cost_limit_cny
        )
        self.input_price = (
            input_price
            if input_price is not None
            else settings.llm_input_price_usd_per_mtok
        )
        self.output_price = (
            output_price
            if output_price is not None
            else settings.llm_output_price_usd_per_mtok
        )
        self.usd_rate = usd_rate if usd_rate is not None else settings.usd_cny_rate

        # 累计用量与费用（元）
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_cny = 0.0
        # 熔断状态与放行令牌
        self._blocked = False
        self._pending_approval_token: str | None = None
        self._approved_once = False
        # 记录最近一次估算，供审批前端展示
        self.last_estimate_cost_cny: float | None = None
        # 是否已从 DB 恢复过（避免重复恢复覆盖运行时状态）
        self._restored = False

    # ---- 费用计算 ----
    def estimate_cost_cny(self, input_tokens: int, output_tokens: int) -> float:
        """按公开单价把 token 用量换算成人民币（预估，非账单）。"""
        usd = (
            input_tokens / 1_000_000 * self.input_price
            + output_tokens / 1_000_000 * self.output_price
        )
        return round(usd * self.usd_rate, 4)

    # ---- 状态 ----
    @property
    def blocked(self) -> bool:
        return self._blocked

    @property
    def total_cost_cny(self) -> float:
        return round(self._total_cost_cny, 4)

    @property
    def total_input_tokens(self) -> int:
        return self._total_input_tokens

    @property
    def total_output_tokens(self) -> int:
        return self._total_output_tokens

    def status(self) -> dict:
        """当前状态（供 GET 查询 / 前端展示）。"""
        return {
            "blocked": self._blocked,
            "cost_limit_cny": self.cost_limit_cny,
            "total_cost_cny": self.total_cost_cny,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "needs_approval": self._blocked,
        }

    def estimate_call_cny(self, estimated_input_tokens: int) -> float:
        """估算一次调用的完整花费（元），供各处预检复用同一口径。

        公开方法：供各处「回合级预算预检」复用同一套「输入 → 输出」比例，
        避免另写一份（两处漂移会让预估与实际记账对不上）。
        """
        # 粗略按输入占比估算输出（网页提炼输出远小于输入），给一个保守上界
        est_output = max(200, estimated_input_tokens // 5)
        return self.estimate_cost_cny(estimated_input_tokens, est_output)

    # ---- 单次调用预检 ----
    def precheck(self, estimated_input_tokens: int) -> dict:
        """发请求前调用：估算本次成本并决定是否拦截。

        拦截条件：已处于熔断态，或「本次估算 + 已累计」超过阈值。
        返回结构供 API 判断：allowed 为 False 时需征询。
        """
        est_cost = self.estimate_call_cny(estimated_input_tokens)
        self.last_estimate_cost_cny = est_cost

        projected = self._total_cost_cny + est_cost
        # 熔断态：需先批准放行
        if self._blocked:
            return {
                "allowed": False,
                "reason": "blocked",
                "estimate_cost_cny": est_cost,
                "total_cost_cny": self.total_cost_cny,
                "approval_token": self._pending_approval_token,
            }
        # 首次放行已生效但累计仍超限时也拦（边界防御，通常不会走到）
        if projected > self.cost_limit_cny:
            self._blocked = True
            self._pending_approval_token = uuid.uuid4().hex
            return {
                "allowed": False,
                "reason": "limit_exceeded",
                "estimate_cost_cny": est_cost,
                "projected_total_cny": round(projected, 4),
                "total_cost_cny": self.total_cost_cny,
                "approval_token": self._pending_approval_token,
            }
        return {
            "allowed": True,
            "estimate_cost_cny": est_cost,
            "total_cost_cny": self.total_cost_cny,
        }

    # ---- 记账（真实调用后） ----
    def record(self, input_tokens: int, output_tokens: int) -> None:
        """真实调用结束后记录用量，并把累计超限置为熔断。"""
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens
        cost = self.estimate_cost_cny(input_tokens, output_tokens)
        self._total_cost_cny += cost
        if self._total_cost_cny > self.cost_limit_cny:
            self._blocked = True
            self._pending_approval_token = uuid.uuid4().hex
        elif self._approved_once:
            # 放行的一次已用完，恢复普通熔断语义由累计决定
            self._approved_once = False
        self.schedule_persist()

    # ---- 审批放行 ----
    def approve(self, token: str | None) -> bool:
        """用户同意继续：校验令牌有效则放行一次，返回是否成功。"""
        if not self._blocked:
            return False
        if not token or token != self._pending_approval_token:
            return False
        self._blocked = False
        self._pending_approval_token = None
        self._approved_once = True
        return True

    def reset(self) -> dict:
        """重置累计与熔断（如用户想清零重新累计）。"""
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_cny = 0.0
        self._blocked = False
        self._pending_approval_token = None
        self._approved_once = False
        self.last_estimate_cost_cny = None
        self.schedule_persist()
        return self.status()

    # ---- 持久化（DB 落库 / 恢复） ----
    def to_dict(self) -> dict:
        """序列化累计状态（供落库）。"""
        return {
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "total_cost_cny": self._total_cost_cny,
            "blocked": self._blocked,
            "approved_once": self._approved_once,
        }

    def load_dict(self, data: dict) -> None:
        """从落库数据恢复累计状态（不覆盖 pending token，避免旧令牌误放行）。"""
        self._total_input_tokens = int(data.get("total_input_tokens", 0))
        self._total_output_tokens = int(data.get("total_output_tokens", 0))
        self._total_cost_cny = float(data.get("total_cost_cny", 0.0))
        self._blocked = bool(data.get("blocked", False))
        self._approved_once = bool(data.get("approved_once", False))
        self._restored = True

    def schedule_persist(self) -> None:
        """fire-and-forget 异步落库：无事件循环（如纯同步测试）则跳过。

        公开方法：审批放行等外部动作后需立即落库时，由 API 层显式触发；
        内部记账/重置也复用此方法。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无运行中的事件循环，仅保留内存态
        loop.create_task(self._persist_async())

    async def _persist_async(self) -> None:
        """把累计状态写入 app_settings 表（后台任务，失败静默不抛）。"""
        try:
            from app.db import async_session_factory
            from app.models.setting import AppSetting

            async with async_session_factory() as session:
                row = await session.get(AppSetting, _PERSIST_KEY)
                if row is None:
                    row = AppSetting(key=_PERSIST_KEY, value=self.to_dict())
                    session.add(row)
                else:
                    row.value = self.to_dict()
                await session.commit()
        except Exception as exc:
            # 落库失败不影响主流程（如 DB 未就绪），内存态仍保证本次会话熔断
            logger.warning("成本状态落库失败（不影响熔断，仅本次会话内存态生效）: %s", exc)

    async def restore_from_db(self) -> None:
        """从 app_settings 表恢复累计状态（仅首次调用生效）。"""
        if self._restored:
            return
        try:
            from app.db import async_session_factory
            from app.models.setting import AppSetting

            async with async_session_factory() as session:
                row = await session.get(AppSetting, _PERSIST_KEY)
                if row is not None and isinstance(row.value, dict):
                    self.load_dict(row.value)
                else:
                    self._restored = True
        except Exception as exc:
            self._restored = True  # DB 不可用时放弃恢复，避免重复尝试
            logger.warning("成本状态恢复失败（DB 不可用，从零累计）: %s", exc)


# 会话级单例：整个应用生命周期内共享累计
_tracker: CostTracker | None = None


def get_cost_tracker() -> CostTracker:
    """返回全局成本熔断器单例。"""
    global _tracker
    if _tracker is None:
        _tracker = CostTracker()
    return _tracker
