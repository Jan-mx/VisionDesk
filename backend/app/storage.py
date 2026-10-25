import asyncio
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.config import get_settings


@lru_cache
def _client(endpoint: str | None = None):
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=endpoint or settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def ensure_bucket() -> None:
    bucket = get_settings().s3_bucket
    try:
        _client().head_bucket(Bucket=bucket)
    except ClientError:
        _client().create_bucket(Bucket=bucket)


async def put(key: str, data: bytes, content_type: str) -> None:
    await asyncio.to_thread(
        _client().put_object,
        Bucket=get_settings().s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


async def get(key: str) -> bytes:
    response = await asyncio.to_thread(
        _client().get_object, Bucket=get_settings().s3_bucket, Key=key
    )
    return response["Body"].read()


async def delete(key: str) -> None:
    await asyncio.to_thread(_client().delete_object, Bucket=get_settings().s3_bucket, Key=key)


# ---- 前缀列举与批量删除（WI-20261025-03：删除预览与清理任务专用）----


def _list_keys_sync(prefix: str) -> list[str]:
    """当前对象键全量分页列举（list_objects_v2）。"""
    client = _client()
    bucket = get_settings().s3_bucket
    keys: list[str] = []
    token: str | None = None
    while True:
        params: dict = {"Bucket": bucket, "Prefix": prefix}
        if token:
            params["ContinuationToken"] = token
        response = client.list_objects_v2(**params)
        keys.extend(item["Key"] for item in response.get("Contents", []))
        if not response.get("IsTruncated"):
            return keys
        token = response["NextContinuationToken"]


def _list_versions_sync(
    prefix: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(版本对象, 删除标记) 全量分页列举；键形 (Key, VersionId)。"""
    client = _client()
    bucket = get_settings().s3_bucket
    versions: list[tuple[str, str]] = []
    markers: list[tuple[str, str]] = []
    key_marker: str | None = None
    version_id_marker: str | None = None
    while True:
        params: dict = {"Bucket": bucket, "Prefix": prefix}
        if key_marker:
            params["KeyMarker"] = key_marker
            params["VersionIdMarker"] = version_id_marker
        response = client.list_object_versions(**params)
        versions.extend((v["Key"], v["VersionId"]) for v in response.get("Versions", []))
        markers.extend((m["Key"], m["VersionId"]) for m in response.get("DeleteMarkers", []))
        if not response.get("IsTruncated"):
            return versions, markers
        key_marker = response["NextKeyMarker"]
        version_id_marker = response["NextVersionIdMarker"]


def _delete_objects_sync(items: list[tuple[str, str | None]]) -> None:
    """批量删除（每批 ≤1000 项）；items 为 (Key, VersionId)，VersionId=None 删当前版本。"""
    client = _client()
    bucket = get_settings().s3_bucket
    for start in range(0, len(items), 1000):
        chunk = items[start : start + 1000]
        client.delete_objects(
            Bucket=bucket,
            Delete={
                "Objects": [
                    {"Key": key, **({"VersionId": version} if version else {})}
                    for key, version in chunk
                ],
                "Quiet": True,
            },
        )


async def list_keys(prefix: str) -> list[str]:
    """当前对象键全量列举；列举失败向上抛出，由调用方决定 503/failed。"""
    return await asyncio.to_thread(_list_keys_sync, prefix)


async def list_object_versions(
    prefix: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """版本与删除标记全量列举（版本化桶清理的优先路径）。"""
    return await asyncio.to_thread(_list_versions_sync, prefix)


async def delete_objects(items: list[tuple[str, str | None]]) -> None:
    """按 (Key, VersionId) 批量删除；对象不存在视为已处理（S3 删除幂等）。"""
    if not items:
        return
    await asyncio.to_thread(_delete_objects_sync, items)


def signed_url(key: str) -> str:
    """生成短时签名 URL。纯本地计算，不产生网络请求。"""
    settings = get_settings()
    return _client(settings.s3_public_endpoint or None).generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": key},
        ExpiresIn=settings.s3_url_ttl,
    )
