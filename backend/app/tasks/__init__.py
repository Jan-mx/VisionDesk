from app.tasks.ping import ping, reap_stale
from app.tasks.tools import run_tool

TASKS = [ping, run_tool, reap_stale]

__all__ = ["TASKS", "ping", "reap_stale", "run_tool"]
