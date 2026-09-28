import base64
from io import BytesIO

import httpx
import pytest
from PIL import Image

from app.providers.background import (
    BriaBackgroundRemovalProvider,
    MattingCheckpoint,
)
from app.providers.base import ProviderError


@pytest.fixture(scope="session", autouse=True)
def bucket():
    """Provider unit tests do not need the integration-test object store."""
    yield


@pytest.fixture(scope="session", autouse=True)
async def isolated_redis():
    yield


@pytest.fixture(autouse=True)
async def cleanup_users():
    yield


def png(*, alpha: bool = False) -> bytes:
    image = Image.new("RGBA" if alpha else "RGB", (64, 48), (30, 90, 180, 128))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def test_bria_submits_polls_and_downloads_transparent_result():
    source = png()
    output = png(alpha=True)
    submitted: list[MattingCheckpoint] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/remove_background"):
            body = __import__("json").loads(request.content)
            assert body == {
                "image": base64.b64encode(source).decode("ascii"),
                "preserve_alpha": True,
                "sync": False,
            }
            assert request.headers["api_token"] == "secret"
            return httpx.Response(
                202,
                json={"request_id": "job-1", "status_url": "https://bria.test/status/job-1"},
            )
        if request.url.path == "/status/job-1":
            return httpx.Response(
                200,
                json={"status": "COMPLETED", "result": {"image_url": "https://cdn.test/out.png"}},
            )
        if request.url.path == "/out.png":
            assert "api_token" not in request.headers
            return httpx.Response(200, content=output, headers={"content-type": "image/png"})
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        result = await provider.remove(source, on_checkpoint=submitted.append)

    assert result == output
    assert submitted == [MattingCheckpoint("job-1", "https://bria.test/status/job-1")]
    assert [request.method for request in requests] == ["POST", "GET", "GET"]


async def test_bria_resumes_checkpoint_without_resubmitting():
    output = png(alpha=True)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path == "/status/job-1":
            return httpx.Response(
                200,
                json={"status": "COMPLETED", "result": {"image_url": "https://cdn.test/out.png"}},
            )
        return httpx.Response(200, content=output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        result = await provider.remove(
            png(), checkpoint=MattingCheckpoint("job-1", "https://bria.test/status/job-1")
        )

    assert result == output
    assert methods == ["GET", "GET"]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "鉴权"),
        (403, "鉴权"),
        (413, "过大"),
        (415, "格式"),
        (422, "拒绝"),
        (429, "繁忙"),
        (500, "暂时不可用"),
    ],
)
async def test_bria_maps_submit_errors(status: int, message: str):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"detail": "raw"}))
    ) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        with pytest.raises(ProviderError, match=message):
            await provider.remove(png())


async def test_bria_rejects_result_without_alpha():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"result": {"image_url": "https://cdn.test/out.png"}, "request_id": "job-1"},
            )
        return httpx.Response(200, content=png(alpha=False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        with pytest.raises(ProviderError, match="透明通道"):
            await provider.remove(png())


async def test_bria_reports_remote_error_state():
    responses = iter(
        [
            httpx.Response(
                202,
                json={"request_id": "job-1", "status_url": "https://bria.test/status/job-1"},
            ),
            httpx.Response(200, json={"status": "ERROR", "error": {"message": "moderated"}}),
        ]
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: next(responses))
    ) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        with pytest.raises(ProviderError, match="moderated"):
            await provider.remove(png())


async def test_bria_retries_rate_limit_once_before_submission():
    output = png(alpha=True)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "POST":
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={"request_id": "job-1", "result": {"image_url": "https://cdn.test/out.png"}},
            )
        return httpx.Response(200, content=output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        assert await provider.remove(png()) == output

    assert calls == 2


async def test_bria_waits_and_retries_rate_limited_status_poll():
    output = png(alpha=True)
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "POST":
            return httpx.Response(
                202,
                json={"request_id": "job-1", "status_url": "https://bria.test/status/job-1"},
            )
        if request.url.path == "/status/job-1":
            polls += 1
            if polls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={"status": "COMPLETED", "result": {"image_url": "https://cdn.test/out.png"}},
            )
        return httpx.Response(200, content=output)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BriaBackgroundRemovalProvider(
            token="secret", base_url="https://bria.test/v2", client=client, poll_interval=0
        )
        assert await provider.remove(png()) == output

    assert polls == 2
