"""Structured calls to the local model through Ollama: a prompt and a JSON schema in, a
parsed object out."""
from __future__ import annotations

import json
import logging
from typing import Callable

import httpx

from ..config import load_yaml, settings

log = logging.getLogger(__name__)

# (prompt, schema) -> parsed answer. Tests swap in a fake.
LLM = Callable[[str, dict], dict]


def desk_model() -> str:
    return (load_yaml("newsroom.yaml").get("desks") or {}).get("model") or settings().triage_model


class OllamaJSON:
    def __init__(self, model: str | None = None, url: str | None = None):
        self.model = model or desk_model()
        self.url = (url or settings().ollama_url).rstrip("/")
        self.client = httpx.Client(timeout=900)

    def __call__(self, prompt: str, schema: dict) -> dict:
        # The model is shared with triage and translation, so a call can wait a long time for its
        # turn, and now and then it rambles until it runs out of room and the JSON is cut off.
        # One more try, colder and with a firm length limit, fixes nearly all of these.
        for attempt, temperature in enumerate((0.2, 0.0)):
            try:
                r = self.client.post(f"{self.url}/api/chat", json={
                    "model": self.model, "stream": False, "think": False, "format": schema,
                    "options": {"temperature": temperature, "num_ctx": settings().ollama_num_ctx, "num_predict": 3000},
                    "messages": [{"role": "user", "content": prompt}],
                })
                r.raise_for_status()
                return json.loads(r.json()["message"]["content"])
            except (httpx.TimeoutException, json.JSONDecodeError) as e:
                if attempt:
                    raise
                log.warning("desk model call failed (%s), retrying", e)
        raise AssertionError("unreachable")


def obj(**props) -> dict:
    """Shorthand for a JSON schema object where every property is required."""
    return {"type": "object", "properties": props, "required": list(props)}


STR = {"type": "string"}
INTS = {"type": "array", "items": {"type": "integer"}}
STRS = {"type": "array", "items": {"type": "string"}}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}


class Extractive:
    """A stand in for the model that builds answers from the headlines in the prompt. No AI and
    poor judgment: for tests, and for keeping the newsroom ticking when Ollama is down. Select it
    with NEWSROOM_LLM=extractive."""

    def __call__(self, prompt: str, schema: dict) -> dict:
        import os
        import re
        import time

        time.sleep(float(os.environ.get("NEWSROOM_FAKE_DELAY", "0")))  # to test slow runs

        props = schema.get("properties", {})
        listed = re.findall(r"^\[(\d+)\] (.+?)(?: \||$)", prompt, re.M)
        heads = re.findall(r"^- \([^)]*\) (.+?)(?: \||$)", prompt, re.M)
        if "important" in props:
            n = len(listed)
            return {"summary": f"{n} stories moved. Leading: {listed[0][1] if listed else 'nothing'}.",
                    "important": list(range(min(n, 3))), "follow": [0] if n else [], "unfollow": [], "misrouted": [], "misplaced": []}
        if "brief" in props:
            return {"brief": " ".join(h.rstrip(".") + "." for h in heads[:3]) or "No coverage text available.",
                    "key_points": heads[:3], "watch_for": "", "escalate": False, "confidence": 0.3}
        if "links" in props:
            return {"links": [{"candidate": 0, "relation": "parallels", "reason": "Similar coverage on another desk."}] if listed else []}
        if "answer" in props:
            ids = [int(x) for x in re.findall(r"^Story (\d+):", prompt, re.M)]
            return {"answer": "Coverage found: " + "; ".join(heads[:5]) if heads else "No coverage found.",
                    "story_ids": ids[:3], "follow_ids": []}
        if "report" in props:
            return {"report": "Tracking: " + (prompt.split("Your current read of the desk:", 1)[-1].split("\n", 1)[0].strip() or "quiet")}
        return {k: "" for k in props}


def default_llm() -> LLM:
    import os

    return Extractive() if os.environ.get("NEWSROOM_LLM") == "extractive" else OllamaJSON()
