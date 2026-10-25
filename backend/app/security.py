import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import get_settings

SESSION_COOKIE = "session"
_ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def issue_token(user_id: uuid.UUID, token_version: int) -> str:
    """签发携带会话版本（ver）的 JWT；版本不匹配即视为已撤销。"""
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "ver": token_version,
        "exp": datetime.now(UTC) + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def read_token(token: str) -> tuple[uuid.UUID, int | None] | None:
    """解析会话令牌，返回 (user_id, ver)；无 ver 的旧格式令牌 ver=None（一律视为失效）。

    任何无效情形统一返回 None 交由调用方处理为未认证。
    """
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])
        ver = payload.get("ver")
        if isinstance(ver, bool) or not isinstance(ver, int):
            ver = None
        return uuid.UUID(payload["sub"]), ver
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None


# ---- 会话删除预览 token（PRD LIB-FR-015 / WI 决策 3）----
# HMAC-SHA256，密钥从 jwt_secret 域分隔派生；不落库、不打日志。
PREVIEW_TTL = timedelta(minutes=5)
_PREVIEW_DOMAIN = b"deletion-preview/v1"


class PreviewTokenError(Exception):
    """token 签名非法或载荷损坏（区别于过期），对应 422。"""


class PreviewTokenExpired(Exception):
    """token 超过有效期，对应 409 delete_preview_expired。"""


def _preview_secret() -> bytes:
    return hmac.new(get_settings().jwt_secret.encode(), _PREVIEW_DOMAIN, hashlib.sha256).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_deletion_token(
    user_id: uuid.UUID, session_id: uuid.UUID, collection_digest: str, delete_digest: str
) -> tuple[str, datetime]:
    """签发绑定 user/session 与 C(X)/D(X) 摘要的确认凭证，返回 (token, expires_at)。"""
    issued_at = datetime.now(UTC)
    expires_at = issued_at + PREVIEW_TTL
    payload = {
        "u": str(user_id),
        "s": str(session_id),
        "c": collection_digest,
        "d": delete_digest,
        "iat": int(issued_at.timestamp()),
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = _b64encode(hmac.new(_preview_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}", expires_at


def read_deletion_token(token: str) -> dict:
    """校验签名与有效期，返回载荷；签名非法抛 PreviewTokenError，过期抛 PreviewTokenExpired。"""
    parts = token.split(".")
    if len(parts) != 2:
        raise PreviewTokenError("预览确认凭证格式非法")
    body, signature = parts
    expected = hmac.new(_preview_secret(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64decode(signature)):
        raise PreviewTokenError("预览确认凭证签名非法")
    try:
        payload = json.loads(_b64decode(body))
        issued_at = datetime.fromtimestamp(payload["iat"], tz=UTC)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PreviewTokenError("预览确认凭证载荷非法") from exc
    if datetime.now(UTC) - issued_at > PREVIEW_TTL:
        raise PreviewTokenExpired
    return payload
