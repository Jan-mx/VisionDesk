"""AER-FR-007：前置步骤失败后依赖链收敛为失败终态，重试可恢复且不重跑已成功步骤。"""

import httpx
import pytest

from tests.canvas import layers, select, settle
from tests.test_agent import fake_planner, send, signed_in, tool_calls
from tests.test_sessions import open_session


@pytest.fixture
def flaky_edit(monkeypatch):
    """第一次编辑调用抛 ProviderError，之后恢复正常。"""
    from app.providers.base import ProviderError
    from app.providers.mock import MockImageProvider

    calls = {"total": 0, "failed": 0}
    original = MockImageProvider.edit

    async def flaky(self, request, on_progress=None):
        calls["total"] += 1
        if calls["total"] == 1:
            calls["failed"] += 1
            raise ProviderError("模型服务超时")
        return await original(self, request, on_progress)

    monkeypatch.setattr(MockImageProvider, "edit", flaky)
    return calls


async def test_downstream_steps_are_blocked_when_upstream_fails(
    signed_in: httpx.AsyncClient, fake_planner
):
    """三步链中间失败：后续步骤进入 blocked，整轮失败终态并展示原因（AC-013）。"""
    fake_planner(
        tool_calls(
            ("crop_canvas", {"ratio": "1:1"}),
            ("replace_region", {"prompt": "改成红色"}),  # 裁剪后选区失效，必然失败
            ("flip_layer", {"direction": "horizontal"}),
        )
    )
    session = await open_session(signed_in)
    await select(signed_in, session["id"], session["revision"], points=[{"x": 0.5, "y": 0.5}])

    turn = await send(signed_in, session["id"], "先裁成正方形再把选区改成红色再翻转")
    await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "failed"
    statuses = {step["tool"]: step["status"] for step in finished["steps"]}
    assert statuses["crop_canvas"] == "succeeded"
    assert statuses["replace_region"] == "failed"
    assert statuses["flip_layer"] == "blocked"
    assert "选区失效" in (finished["error"] or "")


async def test_retry_restores_blocked_downstream_without_redoing_upstream(
    signed_in: httpx.AsyncClient, fake_planner, flaky_edit
):
    """中间步骤失败后重试：失败步重跑、blocked 下游恢复执行，成功上游不重复（AC-014）。"""
    fake_planner(
        tool_calls(
            ("remove_background", {}),  # 成功上游
            ("replace_region", {"prompt": "改成红色"}),  # 首次失败，重试成功
            ("flip_layer", {"direction": "horizontal"}),  # 被阻塞，恢复后执行
        )
    )
    session = await open_session(signed_in)

    turn = await send(signed_in, session["id"], "去背景，把主体改成红色，再水平翻转")
    await signed_in.post(f"/api/sessions/{session['id']}/messages/{turn['id']}/confirm")
    failed_turn = await settle(signed_in, session["id"])

    assert failed_turn["status"] == "failed"
    statuses = {step["tool"]: step["status"] for step in failed_turn["steps"]}
    assert statuses["remove_background"] == "succeeded"
    assert statuses["replace_region"] == "failed"
    assert statuses["flip_layer"] == "blocked"

    retried = (
        await signed_in.post(f"/api/sessions/{session['id']}/messages/{failed_turn['id']}/retry")
    ).json()
    assert retried["status"] == "running"
    finished = await settle(signed_in, session["id"])

    assert finished["status"] == "succeeded"
    statuses = {step["tool"]: step["status"] for step in finished["steps"]}
    assert statuses == {
        "remove_background": "succeeded",
        "replace_region": "succeeded",
        "flip_layer": "succeeded",
    }
    # 上游只执行了一次：编辑调用共 2 次（1 次失败 + 1 次成功），重试没有重复去背景
    assert flaky_edit["total"] == 2
    assert flaky_edit["failed"] == 1
    # 翻转确实执行（blocked 恢复）
    after = (await signed_in.get(f"/api/sessions/{session['id']}")).json()
    assert layers(after)["base"]["transform"]["scale_x"] == -1


def test_blocked_and_canceled_steps_stay_distinguishable():
    """blocked（上游失败）与 canceled（用户取消）是两种状态，不能互相伪装。"""
    from app.agent import plan

    steps = [
        {"id": "s1", "tool": "t", "params": {}, "depends_on": [], "status": plan.FAILED},
        {"id": "s2", "tool": "t", "params": {}, "depends_on": ["s1"], "status": plan.PENDING},
        {"id": "s3", "tool": "t", "params": {}, "depends_on": [], "status": plan.CANCELED},
    ]
    blocked = plan.mark_blocked(steps)

    assert blocked[1]["status"] == plan.BLOCKED
    assert blocked[2]["status"] == plan.CANCELED
    assert plan.settle(blocked) == plan.FAILED


def test_mark_blocked_propagates_transitively():
    from app.agent import plan

    steps = [
        {"id": "s1", "tool": "t", "params": {}, "depends_on": [], "status": plan.FAILED},
        {"id": "s2", "tool": "t", "params": {}, "depends_on": ["s1"], "status": plan.PENDING},
        {"id": "s3", "tool": "t", "params": {}, "depends_on": ["s2"], "status": plan.PENDING},
    ]
    blocked = plan.mark_blocked(steps)

    assert [step["status"] for step in blocked] == [
        plan.FAILED,
        plan.BLOCKED,
        plan.BLOCKED,
    ]
    assert plan.settle(blocked) == plan.FAILED


def test_retry_plan_resets_failed_and_blocked_only():
    from app.agent import plan

    steps = [
        {"id": "s1", "tool": "t", "params": {}, "depends_on": [], "status": plan.SUCCEEDED},
        {
            "id": "s2",
            "tool": "t",
            "params": {},
            "depends_on": ["s1"],
            "status": plan.FAILED,
            "run_id": "old",
        },
        {"id": "s3", "tool": "t", "params": {}, "depends_on": ["s2"], "status": plan.BLOCKED},
    ]
    reset = plan.retryable(steps)

    assert reset[0]["status"] == plan.SUCCEEDED  # 成功步骤不重跑
    assert reset[1]["status"] == plan.PENDING and reset[1]["run_id"] is None
    assert reset[2]["status"] == plan.PENDING
