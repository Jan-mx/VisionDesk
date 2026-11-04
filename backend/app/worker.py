from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.tasks import TASKS, reap_stale, recover_deletions

settings = get_settings()


async def startup(_: dict) -> None:
    await reap_stale({})


class WorkerSettings:
    """ARQ worker 入口。新增异步任务需在 app.tasks.TASKS 中注册。"""

    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = TASKS
    cron_jobs = [
        cron(reap_stale, minute=set(range(60))),
        # 账号清理恢复扫描（WI-20261025-03 决策 5）：每分钟重投未终结操作并
        # 回收崩溃残留登记行；投递丢失/进程重启由本扫描兜底
        cron(recover_deletions, minute=set(range(60))),
    ]
    on_startup = startup
    max_jobs = 4
    job_timeout = 300
    keep_result = 3600
