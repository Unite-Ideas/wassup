"""Background work for agent heartbeats.

Paperclip's HTTP adapter waits at most 30 seconds for an answer, and a desk check in on a local
model takes minutes. So the heartbeat endpoint accepts the run at once and the work happens
here; the agent's report is posted and its task closed when the work is done.

A couple of runs at a time keeps the desks from all hitting the GPU at once (a standup wakes
every desk in the same second). Set NEWSROOM_WORKERS to change it.
"""
from __future__ import annotations

import logging
import os
import threading

from .. import db

log = logging.getLogger(__name__)

# Daemon threads rather than a thread pool: a pool makes the app wait for any desk run in
# progress (minutes on a local model) before it can stop. A run cut short by a shutdown is
# simply woken again by Paperclip later.
_slots = threading.BoundedSemaphore(int(os.environ.get("NEWSROOM_WORKERS", "2")))
_lock = threading.Lock()
_running: dict[str, threading.Event] = {}  # work key (issue id, or agent id for runs without a task) -> set when done


def busy(key: str) -> bool:
    with _lock:
        e = _running.get(key)
        return bool(e and not e.is_set())


def submit(key: str, payload: dict) -> bool:
    """Queue a heartbeat. Returns False when the same work is already queued or running."""
    from .desk import handle_heartbeat

    with _lock:
        e = _running.get(key)
        if e and not e.is_set():
            return False
        done = threading.Event()
        _running[key] = done

    def run():
        try:
            with _slots:
                with db.connect() as conn:
                    handle_heartbeat(conn, payload)
        except Exception:
            log.exception("newsroom job %s failed", key)
        finally:
            with _lock:
                if _running.get(key) is done:
                    _running.pop(key, None)
            done.set()

    threading.Thread(target=run, name=f"newsroom-{key}", daemon=True).start()
    return True


def wait_all(timeout: float = 60) -> None:
    """For tests: wait for queued work to finish."""
    with _lock:
        events = list(_running.values())
    for e in events:
        if not e.wait(timeout):
            raise TimeoutError("newsroom jobs still running")
