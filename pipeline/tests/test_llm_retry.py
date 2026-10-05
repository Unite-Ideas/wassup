import json
import httpx
import pytest

from wassup.newsroom.llm import OllamaJSON


def _llm(handler) -> OllamaJSON:
    llm = OllamaJSON(model="m", url="http://ollama")
    llm.client = httpx.Client(transport=httpx.MockTransport(handler))
    return llm


def test_cut_off_json_is_retried_colder():
    temps = []

    def handler(req):
        body = json.loads(req.content)
        temps.append(body["options"]["temperature"])
        content = '{"report": "ok"}' if len(temps) > 1 else '{"report": "rambling on and on'
        return httpx.Response(200, json={"message": {"content": content}})

    assert _llm(handler)("prompt", {}) == {"report": "ok"}
    assert temps == [0.2, 0.0]


def test_gives_up_after_second_failure():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(httpx.ReadTimeout):
        _llm(handler)("prompt", {})
