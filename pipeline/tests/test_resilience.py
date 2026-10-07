import json
import threading

import httpx
import numpy as np
import pytest

from wassup.embed import DIM, OllamaEmbedder


def _ollama(bad_word: str, calls: list):
    def handler(request: httpx.Request):
        texts = json.loads(request.content)["input"]
        calls.append(len(texts))
        if any(bad_word in t for t in texts):
            return httpx.Response(500, text='{"error":"failed to encode response: json: unsupported value: NaN"}')
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIM - 1) for _ in texts]})
    return handler


def test_one_bad_input_does_not_fail_the_batch():
    calls: list = []
    e = OllamaEmbedder("http://ollama", "bge-m3")
    e.client = httpx.Client(transport=httpx.MockTransport(_ollama("☠", calls)))
    texts = [f"headline {i}" for i in range(16)]
    texts[11] = "☠☠☠"  # nothing left after cleaning, so it cannot be retried
    vecs = e.embed(texts)
    assert vecs.shape == (16, DIM)
    assert not vecs[11].any()
    assert all(vecs[i].any() for i in range(16) if i != 11)
    assert len(calls) < 16  # it bisects instead of retrying every item alone


def test_bad_input_is_retried_cleaned_up():
    calls: list = []
    e = OllamaEmbedder("http://ollama", "bge-m3")
    e.client = httpx.Client(transport=httpx.MockTransport(_ollama("​", calls)))
    vecs = e.embed(["fine", "zero​width space headline"])
    assert vecs[1].any()


@pytest.mark.usefixtures("database")
def test_parallel_collectors_do_not_deadlock():
    from datetime import datetime, timezone

    from wassup import db
    from wassup.collectors.base import RawItem, ensure_source, store_items
    from wassup.geo import gazetteer

    g = gazetteer()
    cities = g.cities[:60]
    errors = []

    def worker(n: int):
        try:
            with db.connect() as conn:
                sid = ensure_source(conn, f"par{n}", f"Par {n}", "rss")
                conn.commit()
                # Opposite place orders in the two threads: the case that used to deadlock.
                order = cities if n % 2 else list(reversed(cities))
                items = [RawItem(url=f"https://par{n}.com/{i}", title=f"Item {i} from {n}", published_at=datetime.now(timezone.utc),
                                 places=[p], entities=[("person", f"Person {i % 7}")]) for i, p in enumerate(order)]
                store_items(conn, sid, items)
                conn.commit()
        except Exception as ex:  # pragma: no cover
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors


def test_ollama_broken_for_everything_raises():
    e = OllamaEmbedder("http://ollama", "bge-m3")
    e.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(RuntimeError, match="every input"):
        e.embed(["a b", "c d", "e f"])


def test_nan_input_retried_with_variants():
    # Fails on the exact text and on its plain word form, works once reworded.
    def handler(request: httpx.Request):
        t = json.loads(request.content)["input"]
        if any(x in ("China-US summit ends. More text", "China US summit ends More text") or x.startswith("China-US summit ends. ") for x in t) or len(t) > 1:
            return httpx.Response(500, text="NaN")
        return httpx.Response(200, json={"embeddings": [[1.0] + [0.0] * (DIM - 1) for _ in t]})
    e = OllamaEmbedder("http://ollama", "bge-m3")
    e.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert e._embed(["China-US summit ends. More text"]).any()


def test_lanes_retry_a_step_that_gave_way_in_a_deadlock(monkeypatch):
    import threading
    import time

    import psycopg

    from wassup import scheduler

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(scheduler.db, "connect", lambda: FakeConn())
    calls = []

    def step(conn):
        calls.append(time.time())
        if len(calls) == 1:
            raise psycopg.errors.DeadlockDetected("deadlock detected")

    stop = threading.Event()
    t = threading.Thread(target=scheduler._lane_loop, args=([("x", 600, step)], stop), daemon=True)
    t.start()
    deadline = time.time() + 10
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.1)
    stop.set()
    assert len(calls) == 2 and calls[1] - calls[0] < 5  # retried within seconds, not after 600
