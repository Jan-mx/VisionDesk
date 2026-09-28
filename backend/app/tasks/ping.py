async def ping(ctx: dict) -> str:
    """用于验证 API 到 worker 的投递链路是否连通。"""
    return "pong"


async def reap_stale(ctx: dict) -> int:
    from app.db import SessionFactory
    from app.services import runs

    async with SessionFactory() as session:
        return await runs.expire_stale(session)
