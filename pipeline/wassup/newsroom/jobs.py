"""Background work for agent heartbeats.

Paperclip's HTTP adapter waits at most 30 seconds for an answer, and a desk check in on a local
model takes minutes. So the heartbeat endpoint accepts the run at once and the work happens
here; the agent's report is posted and its task closed when the work is done.

A couple of workers at a time keeps the desks from all hitting the GPU at once (a standup wakes
every desk in the same second). Set NEWSROOM_WORKERS to change it.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from .. import db

log = logging.getLogger(__name__)

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("NEWSROOM_WORKERS", "2")), thread_name_prefix="newsroom")
_lock = threading.Lock()
_running: dict[str, Future] = {}  # work key (issue id, or agent id for runs without a task) -> job


def busy(key: str) -> bool:
    with _lock:
        f = _running.get(key)
        return bool(f and not f.done())


def submit(key: str, payload: dict) -> bool:
    """Queue a heartbeat. Returns False when the same work is already queued or running."""
    from .desk import handle_heartbeat

    with _lock:
        f = _running.get(key)
        if f and not f.done():
            return False

        def run():
            try:
                with db.connect() as conn:
                    handle_heartbeat(conn, payload)
            except Exception:
                log.exception("newsroom job %s failed", key)
            finally:
                with _lock:
                    _running.pop(key, None)

        _running[key] = _pool.submit(run)
        return True


def wait_all(timeout: float = 60) -> None:
    """For tests: wait for queued work to finish."""
    with _lock:
        futures = list(_running.values())
    for f in futures:
        f.result(timeout=timeout)
