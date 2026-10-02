"""The pipeline loop. Collectors run on their own intervals in background threads (they are
network bound). Processing steps run in the main loop."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import db
from .cluster import process_new
from .collectors.base import Collector
from .collectors.gdelt import GdeltCollector
from .collectors.government import CongressCollector, FederalRegisterCollector
from .collectors.rss import RssCollector
from .locate import run_locate
from .newsroom.manager import run_manager
from .signals import update_breaking, update_links
from .translate import run_translate
from .triage import run_triage

log = logging.getLogger(__name__)


def all_collectors() -> list[Collector]:
    return [GdeltCollector(), RssCollector(), CongressCollector(), FederalRegisterCollector()]


def _collector_loop(c: Collector, stop: threading.Event) -> None:
    while not stop.is_set():
        t = time.time()
        try:
            with db.connect() as conn:
                n = c.run(conn)
            log.info("collector %s: %d new items in %.1fs", c.key, n, time.time() - t)
        except Exception:
            log.exception("collector %s crashed; retrying next interval", c.key)
        stop.wait(c.interval_s)


STEPS: list[tuple[str, float, Callable]] = [
    ("process", 5, process_new),
    ("translate", 10, run_translate),
    ("triage", 15, run_triage),
    ("breaking", 120, update_breaking),
    ("locate", 120, run_locate),
    ("links", 600, update_links),
    ("newsroom", 60, run_manager),
]


def run_forever(collectors: list[Collector] | None = None) -> None:
    stop = threading.Event()
    for c in collectors if collectors is not None else all_collectors():
        threading.Thread(target=_collector_loop, args=(c, stop), name=f"collect-{c.key}", daemon=True).start()
    next_run = {name: 0.0 for name, _, _ in STEPS}
    failures = {name: 0 for name, _, _ in STEPS}
    try:
        while True:
            for name, every, fn in STEPS:
                if time.time() < next_run[name]:
                    continue
                try:
                    with db.connect() as conn:
                        fn(conn)
                    failures[name] = 0
                except Exception as e:
                    failures[name] += 1
                    if failures[name] == 1:
                        log.exception("step %s failed", name)
                    else:
                        log.error("step %s failed again (%d in a row): %s", name, failures[name], e)
                # Back off on repeated failures (for example Ollama not running), up to 5 minutes.
                delay = every if not failures[name] else min(300, every * 2 ** failures[name])
                next_run[name] = time.time() + delay
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


def run_once() -> None:
    """Collect from every source once, then process everything. Handy for testing."""
    for c in all_collectors():
        with db.connect() as conn:
            log.info("collector %s: %d new", c.key, c.run(conn))
    with db.connect() as conn:
        while process_new(conn):
            pass
        while run_translate(conn):
            pass
        while run_triage(conn):
            pass
        update_breaking(conn)
        update_links(conn)
