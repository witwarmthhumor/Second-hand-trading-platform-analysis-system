"""竞品分析报告生成服务。

汇总已入库竞品 + 评分维度，生成 Markdown 报告并落盘 reports/ 目录。
"""

import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competitor import Competitor, DataStatus

# 报告输出目录：backend/reports/（生成产物，不入 Git）
REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"

_SAFE_FILENAME = re.compile(r"^[\w\u4e00-\u9fff\-_.]+$")


def _safe_filename(name: str) -> bool:
    """防路径穿越：只允许中英文、数字、下划线、连字符、点。"""
    return bool(_SAFE_FILENAME.match(name)) and ".." not in name


def _md_escape(text: str | None) -> str:
    """表格单元格内的换行与竖线转义。"""
    if not text:
        return "-"
    return text.replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _overview_table(competitors: list[Competitor]) -> str:
    rows = [
        "| 名称 | 类型 | 状态 | 定位 | 定价 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in competitors:
        rows.append(
            f"| {_md_escape(c.name)} | {_md_escape(c.type)} "
            f"| {_md_escape(c.launch_status)} | {_md_escape(c.positioning)} "
            f"| {_md_escape(c.pricing)} |"
        )
    return "\n".join(rows)


def _ratings_table(competitors: list[Competitor], dims: list[str]) -> str:
    rows = [
        "| 竞品 | " + " | ".join(dims) + " | 总分 |",
        "| --- | " + " | ".join(["---"] * (len(dims) + 1)) + " |",
    ]
    for c in competitors:
        raw = (c.ext_fields or {}).get("ratings") or {}
        scores = [int(raw.get(d, 0) or 0) for d in dims]
        rows.append(
            f"| {_md_escape(c.name)} | "
            + " | ".join(str(s) for s in scores)
            + f" | {sum(scores)} |"
        )
    return "\n".join(rows)


def _detail_sections(competitors: list[Competitor]) -> str:
    parts: list[str] = []
    for i, c in enumerate(competitors, 1):
        parts.append(f"### {i}. {c.name}\n")
        parts.append(
            f"- **类型**：{_md_escape(c.type)}　**上线状态**：{_md_escape(c.launch_status)}"
        )
        parts.append(f"- **官网**：{c.website or '-'}")
        parts.append(f"- **定位**：{_md_escape(c.positioning)}")
        parts.append(f"- **核心功能**：{_md_escape(c.features)}")
        parts.append(f"- **目标用户**：{_md_escape(c.target_users)}")
        parts.append(f"- **业务模式**：{_md_escape(c.business_model)}")
        parts.append(f"- **定价**：{_md_escape(c.pricing)}")
        parts.append(f"- **优势**：{_md_escape(c.strengths)}")
        parts.append(f"- **不足**：{_md_escape(c.weaknesses)}")
        tags = c.tags or []
        if tags:
            parts.append(f"- **标签**：{'、'.join(str(t) for t in tags)}")
        if c.remark:
            parts.append(f"- **备注**：{_md_escape(c.remark)}")
        parts.append("")
    return "\n".join(parts)


def _conclusion(competitors: list[Competitor], dims: list[str]) -> str:
    """极简规则结论：总分最高者、各维度最高者。"""
    if not competitors:
        return "暂无已入库竞品。"
    lines: list[str] = []

    totals = {}
    for c in competitors:
        raw = (c.ext_fields or {}).get("ratings") or {}
        totals[c.name] = sum(int(raw.get(d, 0) or 0) for d in dims)
    best = max(totals, key=lambda n: totals[n])
    lines.append(
        f"- **综合评分最高**：{best}（总分 {totals[best]}，满分 {5 * len(dims)}）。"
    )

    for d in dims:
        scores = {}
        for c in competitors:
            raw = (c.ext_fields or {}).get("ratings") or {}
            scores[c.name] = int(raw.get(d, 0) or 0)
        top = max(scores, key=lambda n: scores[n])
        if scores[top] > 0:
            lines.append(f"- **{d}维度最强**：{top}（{scores[top]}/5）。")

    lines.append(
        "- 以上结论由评分数据规则汇总生成，仅供参考；业务判断请结合优势/不足与定价信息综合评估。"
    )
    return "\n".join(lines)


def build_markdown(competitors: list[Competitor], dims: list[str]) -> str:
    """生成 Markdown 报告全文。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""# 古玩·近代钱币竞品分析报告

> 生成时间：{now}　|　竞品数量：{len(competitors)}　|　评分维度：{len(dims)} 维（0~5 分）

## 一、竞品总览

{_overview_table(competitors)}

## 二、评分对比

{_ratings_table(competitors, dims)}

## 三、详细档案

{_detail_sections(competitors)}

## 四、结论摘要

{_conclusion(competitors, dims)}

---
*本报告由竞品分析系统自动生成，数据经半自动抓取 + AI 提炼 + 人工校对。*
"""


async def collect_competitors(
    session: AsyncSession, ids: list[int] | None = None
) -> list[Competitor]:
    """取已入库竞品：指定 ids 则按 ids 过滤，否则取全部。"""
    stmt = select(Competitor).where(Competitor.data_status == DataStatus.STORED.value)
    if ids:
        stmt = stmt.where(Competitor.id.in_(ids))
    stmt = stmt.order_by(Competitor.id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def save_report(markdown: str) -> str:
    """落盘报告，返回文件名（reports/ 目录自动创建）。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"竞品分析报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    (REPORTS_DIR / filename).write_text(markdown, encoding="utf-8")
    return filename


def resolve_report_path(filename: str) -> Path:
    """校验文件名并返回报告绝对路径（防路径穿越）。"""
    if not _safe_filename(filename) or not filename.endswith(".md"):
        raise ValueError("非法文件名")
    path = REPORTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path
