"""The pipeline. Collectors run on their own intervals in background threads (they are network
bound). Processing steps run in lanes, also in their own threads (see LANES)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import psycopg

from . import db
from .cluster import merge_stories, process_new
from .collectors.base import Collector
from .collectors.gdelt import GdeltCollector
from .collectors.government import CongressCollector, FederalRegisterCollector
from .collectors.rss import RssCollector
from .locate import run_locate
from .newsroom.manager import run_manager
from .retention import run_retention
from .signals import update_breaking, update_links
from .tracks.deepstate import DeepStateCollector
from .translate import run_translate
from .triage import run_triage

log = logging.getLogger(__name__)


def all_collectors() -> list[Collector]:
    return [GdeltCollector(), RssCollector(), CongressCollector(), FederalRegisterCollector(), DeepStateCollector()]


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


# Steps run in lanes, each lane in its own thread, so a step that waits on the model (translation,
# the newsroom) never holds up the others. Steps in one lane take turns: grouping and merging
# share the in memory story index, so they stay together.
LANES: list[tuple[str, list[tuple[str, float, Callable]]]] = [
    ("cluster", [("process", 5, process_new), ("merge", 300, merge_stories)]),
    ("translate", [("translate", 10, run_translate)]),
    ("triage", [("triage", 15, run_triage)]),
    ("signals", [("breaking", 120, update_breaking), ("links", 600, update_links)]),
    ("locate", [("locate", 120, run_locate)]),
    ("newsroom", [("newsroom", 60, run_manager)]),
    ("upkeep", [("retention", 6 * 3600, run_retention)]),
]
STEPS = [step for _, steps in LANES for step in steps]


def _lane_loop(steps: list[tuple[str, float, Callable]], stop: threading.Event) -> None:
    next_run = {name: 0.0 for name, _, _ in steps}
    failures = {name: 0 for name, _, _ in steps}
    while not stop.is_set():
        for name, every, fn in steps:
            if time.time() < next_run[name]:
                continue
            delay = every
            try:
                with db.connect() as conn:
                    fn(conn)
                failures[name] = 0
            except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                # Two lanes touched the same stories at once; Postgres picked this one to give
                # way. Not a real failure: try again shortly.
                log.info("step %s gave way to another step; retrying", name)
                delay = 2
            except Exception as e:
                failures[name] += 1
                if failures[name] == 1:
                    log.exception("step %s failed", name)
                else:
                    log.error("step %s failed again (%d in a row): %s", name, failures[name], e)
                # Back off on repeated failures (for example Ollama not running), up to 5 minutes.
                delay = min(300, every * 2 ** failures[name])
            next_run[name] = time.time() + delay
        stop.wait(max(0.5, min(next_run.values()) - time.time()))


def run_forever(collectors: list[Collector] | None = None) -> None:
    stop = threading.Event()
    for c in collectors if collectors is not None else all_collectors():
        threading.Thread(target=_collector_loop, args=(c, stop), name=f"collect-{c.key}", daemon=True).start()
    for lane, steps in LANES:
        threading.Thread(target=_lane_loop, args=(steps, stop), name=f"lane-{lane}", daemon=True).start()
    try:
        while not stop.wait(3600):
            pass
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
        merge_stories(conn)
        while run_translate(conn):
            pass
        while run_triage(conn):
            pass
        update_breaking(conn)
        update_links(conn)
