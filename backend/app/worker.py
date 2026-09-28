from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.tasks import TASKS, reap_stale

settings = get_settings()


async def startup(_: dict) -> None:
    await reap_stale({})


class WorkerSettings:
    """ARQ worker 入口。新增异步任务需在 app.tasks.TASKS 中注册。"""

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = TASKS
    cron_jobs = [cron(reap_stale, minute=set(range(60)))]
    on_startup = startup
    max_jobs = 4
    job_timeout = 300
    keep_result = 3600
