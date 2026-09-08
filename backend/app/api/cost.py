"""LLM 成本熔断审批 API。

- GET  /api/cost/status  ：查询当前累计消费与熔断状态
- POST /api/cost/approve ：用户同意继续后，用审批令牌放行一次
- POST /api/cost/reset   ：重置累计（清零重新累计）
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.ai.cost import get_cost_tracker

router = APIRouter(prefix="/api/cost", tags=["cost"])


class ApproveRequest(BaseModel):
    token: str | None = None


class CostStatus(BaseModel):
    blocked: bool
    cost_limit_cny: float
    total_cost_cny: float
    total_input_tokens: int
    total_output_tokens: int
    needs_approval: bool


@router.get("/status", response_model=CostStatus)
async def cost_status() -> CostStatus:
    return CostStatus(**get_cost_tracker().status())


@router.post("/approve", response_model=CostStatus)
async def approve_call(payload: ApproveRequest) -> CostStatus:
    """用户同意后放行一次 LLM 调用。token 无效或未熔断时返回 false（保持原状）。"""
    tracker = get_cost_tracker()
    tracker.approve(payload.token)
    return CostStatus(**tracker.status())


@router.post("/reset", response_model=CostStatus)
async def reset_cost() -> CostStatus:
    """清零累计消费并解除熔断。"""
    return CostStatus(**get_cost_tracker().reset())
