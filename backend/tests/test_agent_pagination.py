"""AER-FR-010：消息列表返回最近一页并支持稳定游标加载更早历史。"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionFactory
from app.models import AgentRun, EditSession
from app.models.tool_run import RunStatus
from tests.test_agent import fake_planner, send, signed_in, tool_calls
from tests.test_sessions import open_session

TOTAL = 55
SHARED_TAIL = 5  # 最后 5 轮共享同一时间戳，验证 (created_at, id) 双键稳定排序


async def seed_turns(session_body: dict, count: int = TOTAL) -> list[str]:
    """直接落库构造历史轮次，绕过规划调用；返回全部轮次 id。"""
    async with SessionFactory() as db:
        record = await db.get(EditSession, uuid.UUID(session_body["id"]))
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for index in range(count):
            minutes = index if index < count - SHARED_TAIL else count - SHARED_TAIL
            db.add(
                AgentRun(
                    user_id=record.user_id,
                    session_id=record.id,
                    revision=record.revision,
                    goal=f"第{index + 1}句",
                    reply="好的。",
                    plan=[],
                    status=RunStatus.SUCCEEDED,
                    created_at=base + timedelta(minutes=minutes),
                )
            )
        await db.commit()
        rows = await db.scalars(
            select(AgentRun).where(AgentRun.session_id == record.id).order_by(AgentRun.created_at)
        )
        return [str(row.id) for row in rows]


async def test_default_page_returns_the_latest_window(signed_in: httpx.AsyncClient):
    session = await open_session(signed_in)
    await seed_turns(session)

    turns = (await signed_in.get(f"/api/sessions/{session['id']}/messages")).json()

    assert len(turns) == 50
    goals = {turn["goal"] for turn in turns}
    # 默认返回最近 50 条（第 6～55 句），而不是最早的 50 条
    assert turns[0]["goal"] == "第6句"
    assert "第1句" not in goals
    assert "第55句" in goals


async def test_before_cursor_loads_older_pages_without_duplicates(signed_in: httpx.AsyncClient):
    session = await open_session(signed_in)
    all_ids = await seed_turns(session)

    first_page = (await signed_in.get(f"/api/sessions/{session['id']}/messages")).json()
    collected = [turn["id"] for turn in first_page]
    oldest = first_page[0]["id"]
    for _ in range(10):  # 上限防护：游标不再前进时停止翻页
        page = (
            await signed_in.get(
                f"/api/sessions/{session['id']}/messages",
                params={"before": oldest},
            )
        ).json()
        if not page or page[0]["id"] == oldest:
            break
        collected.extend(turn["id"] for turn in page)
        oldest = page[0]["id"]

    assert len(collected) == TOTAL
    assert set(collected) == set(all_ids)  # 无重复、无遗漏（含同时间戳轮次）


async def test_limit_parameter_is_honored(signed_in: httpx.AsyncClient):
    session = await open_session(signed_in)
    await seed_turns(session)

    turns = (
        await signed_in.get(f"/api/sessions/{session['id']}/messages", params={"limit": 3})
    ).json()

    assert len(turns) == 3
    # 最近 3 条落在共享时间戳的尾部 5 轮内，按 (created_at, id) 稳定排序
    assert {turn["goal"] for turn in turns} <= {f"第{n}句" for n in range(51, 56)}


async def test_stale_cursor_returns_empty_page(signed_in: httpx.AsyncClient):
    session = await open_session(signed_in)
    await seed_turns(session)

    page = (
        await signed_in.get(
            f"/api/sessions/{session['id']}/messages", params={"before": str(uuid.uuid4())}
        )
    ).json()

    assert page == []


async def test_fifty_first_turn_stays_visible_and_confirmable(
    signed_in: httpx.AsyncClient, fake_planner
):
    """已有 50 轮时，第 51 轮待确认计划在首屏可见且可确认执行（AC-019）。"""
    fake_planner(
        tool_calls(
            ("flip_layer", {"direction": "horizontal"}),
            ("rotate_layer", {"angle": 15}),
        )
    )
    session = await open_session(signed_in)
    await seed_turns(session, count=50)

    turn = await send(signed_in, session["id"], "第51句：翻转再旋转")

    page = (await signed_in.get(f"/api/sessions/{session['id']}/messages")).json()
    assert len(page) == 50
    assert page[-1]["id"] == turn["id"]
    assert page[-1]["status"] == "queued"

    confirmed = (
        await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    ).json()
    assert confirmed["status"] == "succeeded"

    refreshed = (await signed_in.get(f"/api/sessions/{session['id']}/messages")).json()
    assert refreshed[-1]["status"] == "succeeded"
    assert refreshed[-1]["steps"][0]["status"] == "succeeded"
