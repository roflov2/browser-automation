"""Every extract() result is saved to extractions.jsonl with its instruction and a timestamp."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from audit.core import ExtractFailed
from audit.runner import RecordingStagehand


class Total(BaseModel):
    total: str | None


class FakeStagehand:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def extract(self, instruction, schema, **options):
        self.calls.append((instruction, schema, options))
        return SimpleNamespace(data=schema(total="$67.00"), metadata=None)

    async def metrics(self):
        return "metrics"


def test_extractions_are_appended(tmp_path):
    fake = FakeStagehand()
    stagehand = RecordingStagehand(fake)
    path = tmp_path / "extractions.jsonl"

    async def scenario():
        await stagehand.extract("before recording", Total)
        stagehand.record_to(path)
        first = await stagehand.extract("Read the total", Total, timeout=5)
        await stagehand.extract("Read it again", Total)
        return first, await stagehand.metrics()

    first, metrics = asyncio.run(scenario())
    assert first.data.total == "$67.00"  # the caller gets the untouched result
    assert metrics == "metrics"  # everything else passes through
    assert fake.calls[1] == ("Read the total", Total, {"timeout": 5})

    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [e["instruction"] for e in entries] == ["Read the total", "Read it again"]
    assert entries[0]["schema"] == "Total"
    assert entries[0]["data"] == {"total": "$67.00"}
    assert datetime.fromisoformat(entries[0]["timestamp"]).tzinfo is not None


def test_model_calls_are_counted():
    class Fake(FakeStagehand):
        async def observe(self, instruction, **options):
            return []

    stagehand = RecordingStagehand(Fake())

    async def scenario():
        await stagehand.extract("Read the total", Total)
        await stagehand.observe("find the button")
        await stagehand.observe("find it again")
        await stagehand.metrics()  # not a model call

    asyncio.run(scenario())
    assert stagehand.llm_calls == 3


REJECTED = RuntimeError(
    '[{"code":"invalid_type","expected":"boolean","received":"null","path":["structuredContent","form_visible"]}]'
)


class Flaky(FakeStagehand):
    """Fails each model call with `errors` in turn, then answers."""

    def __init__(self, *errors: Exception) -> None:
        super().__init__()
        self.errors = list(errors)

    async def extract(self, instruction, schema, **options):
        if self.errors:
            self.calls.append((instruction, schema, options))
            raise self.errors.pop(0)
        return await super().extract(instruction, schema, **options)

    async def observe(self, instruction, **options):
        self.calls.append((instruction, None, options))
        if self.errors:
            raise self.errors.pop(0)
        return ["found"]


def test_a_rejected_answer_is_asked_for_once_more(caplog):
    observing = RecordingStagehand(Flaky(REJECTED))
    assert asyncio.run(observing.observe("find the button")) == ["found"]
    assert observing.llm_calls == 2
    assert "did not fit the schema; asking once more" in caplog.text

    extracting = RecordingStagehand(Flaky(REJECTED))
    assert asyncio.run(extracting.extract("Read the total", Total)).data.total == "$67.00"
    assert extracting.llm_calls == 2


def test_a_second_rejection_or_another_error_is_not_retried():
    twice = RecordingStagehand(Flaky(REJECTED, REJECTED))
    with pytest.raises(RuntimeError, match="structuredContent"):
        asyncio.run(twice.observe("find the button"))
    assert twice.llm_calls == 2

    other = Flaky(RuntimeError("model returned no content"))
    with pytest.raises(RuntimeError, match="no content"):
        asyncio.run(RecordingStagehand(other).observe("find the button"))
    assert len(other.calls) == 1

    with pytest.raises(ExtractFailed, match="structuredContent"):
        asyncio.run(RecordingStagehand(Flaky(REJECTED, REJECTED)).extract("x", Total))


def test_an_observe_hiccup_is_retried_once(caplog, monkeypatch):
    from audit import actions

    monkeypatch.setattr(actions, "HICCUP_PAUSE_S", 0)
    hiccup = RuntimeError("RPCError: -32001 Session with given id not found")
    observing = RecordingStagehand(Flaky(hiccup))
    assert asyncio.run(observing.observe("find the button")) == ["found"]
    assert observing.llm_calls == 2
    assert "observe: browser session hiccup" in caplog.text
    with pytest.raises(RuntimeError, match="Session with given id"):
        asyncio.run(RecordingStagehand(Flaky(hiccup, hiccup)).observe("find the button"))


def test_an_act_hiccup_is_left_to_llm_act():
    class Acting(FakeStagehand):
        calls_made = 0

        async def act(self, action, **options):
            Acting.calls_made += 1
            raise RuntimeError("Session with given id not found")

    with pytest.raises(RuntimeError):
        asyncio.run(RecordingStagehand(Acting()).act("click"))
    assert Acting.calls_made == 1  # re-acting needs a fresh observe first, which only llm_act can do
