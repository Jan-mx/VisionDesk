from types import SimpleNamespace

import pytest

from app.providers.background import MattingCheckpoint
from app.services import background


@pytest.fixture(scope="session", autouse=True)
def bucket():
    yield


@pytest.fixture(scope="session", autouse=True)
async def isolated_redis():
    yield


@pytest.fixture(autouse=True)
async def cleanup_users():
    yield


async def test_background_service_persists_checkpoint_without_losing_run_result(monkeypatch):
    run = SimpleNamespace(progress=50, result={"items": [{"status": "running"}]})
    reports: list[dict] = []

    async def report(_session, target, progress, stage, result):
        target.progress = max(target.progress, progress)
        target.result = result
        reports.append(result)

    class FakeProvider:
        async def remove(self, image, **kwargs):
            assert kwargs["checkpoint"] is None
            await kwargs["on_checkpoint"](MattingCheckpoint("job-1", "https://bria/status"))
            await kwargs["on_progress"](60, "云端处理中")
            return b"result"

    monkeypatch.setattr(background.runs, "report", report)
    monkeypatch.setattr(background, "get_background_removal_provider", lambda: FakeProvider())

    assert await background.remove(object(), run, b"source", key="batch:0") == b"result"
    assert reports[-1]["items"] == [{"status": "running"}]
    assert reports[-1]["_background_jobs"]["batch:0"]["request_id"] == "job-1"
