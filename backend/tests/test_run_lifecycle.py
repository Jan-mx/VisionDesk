from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.tool_run import RunStatus
from app.services.runs import is_stale


@pytest.fixture(scope="session", autouse=True)
def bucket():
    yield


@pytest.fixture(scope="session", autouse=True)
async def isolated_redis():
    yield


@pytest.fixture(autouse=True)
async def cleanup_users():
    yield


def run(status: RunStatus, *, age: int, heartbeat_age: int | None = None):
    now = datetime.now(UTC)
    return SimpleNamespace(
        status=status,
        created_at=now - timedelta(seconds=age),
        started_at=now - timedelta(seconds=age),
        heartbeat_at=(
            now - timedelta(seconds=heartbeat_age) if heartbeat_age is not None else None
        ),
    )


def test_running_job_with_expired_heartbeat_is_stale():
    assert is_stale(run(RunStatus.RUNNING, age=500, heartbeat_age=400), 300)


def test_recent_heartbeat_keeps_running_job_alive():
    assert not is_stale(run(RunStatus.RUNNING, age=500, heartbeat_age=10), 300)


def test_old_queued_job_is_stale_but_terminal_job_is_not():
    assert is_stale(run(RunStatus.QUEUED, age=400), 300)
    assert not is_stale(run(RunStatus.SUCCEEDED, age=400), 300)
