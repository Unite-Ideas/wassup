import json
from datetime import datetime, timezone

import httpx
import pytest

from wassup.translate import Translator


def _mock(translations: dict[str, str], calls: list):
    def handler(request: httpx.Request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:30b"}]})
        prompt = json.loads(request.content)["messages"][0]["content"]
        lines = [l.split(". ", 1)[1] for l in prompt.split("\n") if l[:1].isdigit() and ". " in l]
        calls.append(len(lines))
        return httpx.Response(200, json={"message": {"content": json.dumps({"translations": [translations.get(l, l) for l in lines]})}})
    return handler


def test_translator_batches_in_order():
    calls: list = []
    t = Translator("http://ollama", "qwen3:30b")
    t.client = httpx.Client(transport=httpx.MockTransport(_mock({"Путин": "Putin", "Киев": "Kyiv"}, calls)))
    assert t.available()
    assert t.translate(["Путин", "Киев"]) == ["Putin", "Kyiv"]
    assert calls == [2]


@pytest.mark.usefixtures("database")
def test_translation_lets_triage_route_foreign_story():
    from wassup import db
    from wassup.cluster import process_new
    from wassup.collectors.base import RawItem, ensure_source, store_items
    from wassup.translate import run_translate
    from wassup.triage import run_triage

    ru = "Российские дроны атаковали Киев ночью"
    calls: list = []
    t = Translator("http://ollama", "qwen3:30b")
    t.client = httpx.Client(transport=httpx.MockTransport(_mock({ru: "Russian drones attacked Kyiv overnight"}, calls)))
    with db.connect() as conn:
        sid = ensure_source(conn, "ru_test", "RU test", "rss", language="ru")
        store_items(conn, sid, [RawItem(url="https://ru.example/1", title=ru, language="ru",
                                        published_at=datetime.now(timezone.utc))])
        conn.commit()
        process_new(conn)
        run_triage(conn)
        before = conn.execute("SELECT s.id, s.routed FROM stories s JOIN items i ON i.story_id = s.id WHERE i.url = 'https://ru.example/1'").fetchone()
        assert not before["routed"]  # Russian keywords alone are not enough here
        assert run_translate(conn, translator=t) >= 1
        run_triage(conn)
        after = conn.execute("SELECT title, title_en, routed, desk FROM stories WHERE id = %s", (before["id"],)).fetchone()
        assert after["title"] == ru and after["title_en"] == "Russian drones attacked Kyiv overnight"
        assert after["routed"] and after["desk"] == "russia_ukraine"
        assert run_translate(conn, translator=t) == 0  # nothing left to do

    from fastapi.testclient import TestClient
    from wassup.api import app

    detail = TestClient(app).get(f"/api/stories/{before['id']}").json()
    assert detail["title"] == "Russian drones attacked Kyiv overnight" and detail["title_original"] == ru
    assert detail["items"][0]["title_original"] == ru
