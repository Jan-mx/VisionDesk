"""通用分页助手（WI-20261025-02 实现要求 3，PRD §8.2）。

- PageParams：page≥1、page_size 1~100（默认 25）、order asc/desc（默认 desc），
  由 pydantic 校验，越界一律 422；sort 默认 created_at，白名单由各资源子类以
  Literal 收紧，白名单外同样 422；
- paginate：total 用相同过滤条件独立 count（统计列以相关子查询出现，不产生
  JOIN，不抬高 total）；主排序值相同按 id 同方向稳定排序；越界页自然返回
  空 items 与真实 total。
"""

from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute


class PageParams(BaseModel):
    """列表查询公共参数；sort 白名单由资源子类以 Literal 固定。"""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)
    order: Literal["asc", "desc"] = "desc"
    sort: str = "created_at"


class Page[ItemT](BaseModel):
    """统一分页响应：items + total + page + page_size。"""

    items: list[ItemT]
    total: int
    page: int
    page_size: int


async def paginate(
    session: AsyncSession,
    stmt: Select,
    params: PageParams,
    sort_map: Mapping[str, InstrumentedAttribute],
    *,
    nulls_last: Collection[str] = (),
) -> tuple[Sequence[Any], int]:
    """执行分页查询，返回 (rows, total)。

    stmt 应以单一 ORM 实体为第一个 select 目标（稳定排序取其 id 列），
    统计列以相关子查询形式出现在 select 中。nulls_last 列出可空排序列名
    （如素材 title）：这些列的空值行在 asc/desc 两个方向都稳定尾随
    （WI-20261025-04 决策 1）。
    """
    column = sort_map[params.sort]
    if params.sort in nulls_last:
        order = (
            column.asc().nulls_last()
            if params.order == "asc"
            else column.desc().nulls_last()
        )
    else:
        order = column.asc() if params.order == "asc" else column.desc()
    entity = stmt.column_descriptions[0]["entity"]
    id_order = entity.id.asc() if params.order == "asc" else entity.id.desc()

    # total 与列表同一过滤条件独立 count：包成子查询数行数，不受 select 中
    # 统计子查询影响，也不会因 JOIN 重复计数（WI 实现要求 7）
    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = (
        await session.execute(
            stmt.order_by(order, id_order)
            .offset((params.page - 1) * params.page_size)
            .limit(params.page_size)
        )
    ).all()
    return rows, total
