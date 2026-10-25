from app.tasks.deletion import cleanup_account, recover_deletions
from app.tasks.ping import ping, reap_stale
from app.tasks.tools import run_tool

TASKS = [ping, run_tool, reap_stale, cleanup_account, recover_deletions]

__all__ = [
    "TASKS",
    "cleanup_account",
    "ping",
    "reap_stale",
    "recover_deletions",
    "run_tool",
]
