import asyncio
import base64
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.providers.base import ProgressCallback, ProviderError
from app.services.images import MAX_FILE_BYTES, ImageRejected, probe

CheckpointCallback = Callable[["MattingCheckpoint"], Awaitable[None] | None]


@dataclass(frozen=True)
class MattingCheckpoint:
    request_id: str
    status_url: str

    @classmethod
    def from_dict(cls, value: object) -> "MattingCheckpoint | None":
        if not isinstance(value, dict):
            return None
        request_id, status_url = value.get("request_id"), value.get("status_url")
        if not isinstance(request_id, str) or not isinstance(status_url, str):
            return None
        return cls(request_id=request_id, status_url=status_url)

    def as_dict(self) -> dict[str, str]:
        return {"request_id": self.request_id, "status_url": self.status_url}


class BriaBackgroundRemovalProvider:
    name = "bria"

    def __init__(
        self,
        *,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        settings = get_settings()
        self.token = token if token is not None else settings.bria_api_token
        self.base_url = (base_url or settings.bria_base_url).rstrip("/")
        self.timeout = timeout or settings.bria_timeout_seconds
        self.client = client
        self.poll_interval = poll_interval

    async def remove(
        self,
        image: bytes,
        *,
        checkpoint: MattingCheckpoint | None = None,
        on_checkpoint: CheckpointCallback | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> bytes:
        if not self.token:
            raise ProviderError("BRIA API Key 未配置")
        meta = probe(image)
        if self.client is not None:
            return await self._remove(
                self.client, image, meta.width, meta.height, checkpoint, on_checkpoint, on_progress
            )
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            return await self._remove(
                client, image, meta.width, meta.height, checkpoint, on_checkpoint, on_progress
            )

    async def _remove(
        self,
        client: httpx.AsyncClient,
        image: bytes,
        width: int,
        height: int,
        checkpoint: MattingCheckpoint | None,
        on_checkpoint: CheckpointCallback | None,
        on_progress: ProgressCallback | None,
    ) -> bytes:
        try:
            if checkpoint is None:
                await _progress(on_progress, 45, "提交云端抠图")
                request = {
                    "image": base64.b64encode(image).decode("ascii"),
                    "preserve_alpha": True,
                    "sync": False,
                }
                endpoint = f"{self.base_url}/image/edit/remove_background"
                response = await client.post(
                    endpoint, headers={"api_token": self.token}, json=request
                )
                if response.status_code == 429:
                    await asyncio.sleep(_retry_after(response))
                    response = await client.post(
                        endpoint, headers={"api_token": self.token}, json=request
                    )
                _raise_api_error(response)
                payload = _json(response)
                direct = _image_url(payload)
                if direct:
                    await _progress(on_progress, 85, "下载抠图结果")
                    return await _download(client, direct, width, height)
                checkpoint = _checkpoint(payload, urlparse(self.base_url).hostname)
                if on_checkpoint is not None:
                    pending = on_checkpoint(checkpoint)
                    if inspect.isawaitable(pending):
                        await pending

            await _progress(on_progress, 60, "云端处理中")
            deadline = time.monotonic() + self.timeout
            while True:
                if time.monotonic() >= deadline:
                    raise ProviderError("BRIA 云端处理超时，请稍后重试")
                response = await client.get(
                    _safe_status_url(checkpoint.status_url, urlparse(self.base_url).hostname),
                    headers={"api_token": self.token},
                )
                if response.status_code == 429:
                    await asyncio.sleep(_retry_after(response))
                    continue
                _raise_api_error(response)
                await _progress(on_progress, 60, "云端处理中")
                payload = _json(response)
                state = str(payload.get("status") or "").upper()
                if state == "COMPLETED":
                    url = _image_url(payload)
                    if not url:
                        raise ProviderError("BRIA 返回结果缺少图片地址")
                    await _progress(on_progress, 85, "下载抠图结果")
                    return await _download(client, url, width, height)
                if state in {"ERROR", "UNKNOWN"}:
                    raise ProviderError(_remote_error(payload))
                if state != "IN_PROGRESS":
                    raise ProviderError("BRIA 返回了无法识别的任务状态")
                await asyncio.sleep(self.poll_interval)
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError("BRIA 云端服务连接超时，请稍后重试") from exc


def _checkpoint(payload: dict, allowed_host: str | None) -> MattingCheckpoint:
    checkpoint = MattingCheckpoint.from_dict(payload)
    if checkpoint is None:
        raise ProviderError("BRIA 未返回任务跟踪信息")
    _safe_status_url(checkpoint.status_url, allowed_host)
    return checkpoint


def _safe_status_url(url: str, allowed_host: str | None) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not allowed_host or parsed.hostname != allowed_host:
        raise ProviderError("BRIA 返回了无效的任务地址")
    return url


def _json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError("BRIA 返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise ProviderError("BRIA 返回了无效响应")
    return payload


def _image_url(payload: dict) -> str | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("image_url")
    return value if isinstance(value, str) and value.startswith("https://") else None


def _raise_api_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    if status in {401, 403}:
        message = "BRIA API 鉴权失败，请检查 API Key"
    elif status == 413:
        message = "图片过大，BRIA 无法处理"
    elif status == 415:
        message = "图片格式不受 BRIA 支持"
    elif status == 422:
        message = "图片被 BRIA 拒绝处理"
    elif status == 429:
        message = "BRIA 服务繁忙或额度受限，请稍后重试"
    elif status >= 500:
        message = "BRIA 服务暂时不可用，请稍后重试"
    else:
        message = f"BRIA 请求失败（{status}）"
    raise ProviderError(message)


def _retry_after(response: httpx.Response) -> float:
    try:
        return max(0.0, min(10.0, float(response.headers.get("Retry-After", "1"))))
    except ValueError:
        return 1.0


def _remote_error(payload: dict) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        raw = error.get("message") or error.get("detail")
        if isinstance(raw, str) and raw.strip():
            return f"BRIA 处理失败：{raw.strip()}"
    if isinstance(error, str) and error.strip():
        return f"BRIA 处理失败：{error.strip()}"
    return "BRIA 云端处理失败"


async def _download(client: httpx.AsyncClient, url: str, width: int, height: int) -> bytes:
    response = await client.get(url)
    if response.status_code >= 400:
        raise ProviderError("BRIA 结果下载失败")
    data = response.content
    if len(data) > MAX_FILE_BYTES:
        raise ProviderError("BRIA 返回的图片超过大小限制")
    try:
        meta = probe(data)
    except ImageRejected as exc:
        raise ProviderError("BRIA 返回的图片无效") from exc
    if (meta.width, meta.height) != (width, height):
        raise ProviderError("BRIA 返回的图片尺寸与原图不一致")
    if not meta.has_alpha:
        raise ProviderError("BRIA 返回的图片缺少透明通道")
    return data


async def _progress(callback: ProgressCallback | None, value: int, stage: str) -> None:
    if callback is not None:
        await callback(value, stage)


class MockBackgroundRemovalProvider:
    name = "mock"

    async def remove(self, image: bytes, **_: object) -> bytes:
        from app.edits.pixels import corner_matte

        return corner_matte(image)
