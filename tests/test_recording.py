"""Every extract() result is saved to extractions.jsonl with its instruction and a timestamp."""

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from pydantic import BaseModel

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
