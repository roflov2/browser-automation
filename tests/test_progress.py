"""Pure-function tests: route banning, false-overlay memory, stuck detection, prompt building. No browser."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from checkout_navigator.engine.cdp_guard import Observation
from checkout_navigator.engine.classifier import (
    NextActionModel,
    _decision_prompt,
    _fallback,
    button_summary,
    determine_next_step,
    form_summary,
)
from checkout_navigator.engine.progress import Progress, action_key, is_skip_link

S = ("https://air.example/", "hash1")
S2 = ("https://air.example/", "hash2")

AIRLINE_CONTROLS: list[dict[str, Any]] = [
    {"tag": "a", "role": "", "type": "", "text": "Skip to main content", "label": "", "value": ""},
    {"tag": "input", "role": "combobox", "type": "text", "text": "", "label": "Departing from", "value": "Canberra",
     "required": True},
    {"tag": "input", "role": "combobox", "type": "text", "text": "", "label": "Flying to", "value": "Sydney"},
    {"tag": "button", "role": "", "type": "button", "text": "14 Feb 2027", "label": "Travel dates", "value": ""},
    {"tag": "input", "role": "", "type": "email", "text": "", "label": "Email", "value": "alex@example.com"},
    {"tag": "input", "role": "", "type": "text", "text": "", "label": "Promo code", "value": ""},
    {"tag": "button", "role": "", "type": "submit", "text": "Search flights", "label": "", "value": ""},
    {"tag": "button", "role": "", "type": "button", "text": "Menu", "label": "", "value": ""},
]  # fmt: skip


def _observation(overlay: bool = True, actions: list[Any] | None = None) -> Observation:
    return Observation(
        actions=actions or [],
        dom={"overlay": overlay, "overlay_matches": ['div.cookie-bar "We use cookies" (edge bar)'],
             "controls": AIRLINE_CONTROLS, "headings": ["Book a flight"]},
        url=S[0],
    )  # fmt: skip


def test_action_key() -> None:
    assert action_key("DO_DISMISS_OVERLAY", "whatever") == "DO_DISMISS_OVERLAY"
    assert action_key("DO_ACT", "Click  the 'Search'  button") == "DO_ACT \"click the 'search' button\""


def test_skip_link() -> None:
    assert is_skip_link("Click the 'Skip to main content' link")
    assert is_skip_link("Skip navigation")
    assert not is_skip_link("Skip this step")
    assert not is_skip_link("Search flights")


def test_reported_failure_twice_bans_route() -> None:
    p = Progress()
    for _ in range(2):
        p.decided(S, "DO_DISMISS_OVERLAY")
        p.outcome(False)
        p.settled(S)  # unchanged page must not count the same failure again
    assert p.banned(S) == ["DO_DISMISS_OVERLAY"]
    assert p.banned(S2) == []


def test_silent_no_change_counts_once() -> None:
    p = Progress()
    p.decided(S, 'DO_ACT "click x"')
    p.outcome(True)  # the helper thought it worked ...
    p.settled(S)  # ... but the page is the same
    assert p.banned(S) == []
    p.decided(S, 'DO_ACT "click x"')
    p.outcome(True)
    p.settled(S2)  # this time the page changed
    assert p.banned(S) == []
    p.decided(S, 'DO_ACT "click x"')
    p.outcome(True)
    p.settled(S)
    assert p.banned(S) == ['DO_ACT "click x"']


def test_false_overlay_memory() -> None:
    p = Progress()
    assert not p.overlay_is_false_alarm(S, ["div.x"])
    p.mark_false_overlay(S, ["div.x"])
    assert p.overlay_is_false_alarm(S, ["div.x"])
    assert p.overlay_is_false_alarm(S2, ["div.x"])  # same URL and same element, slightly different DOM
    assert not p.overlay_is_false_alarm(S2, ["div.x", "div.y"])
    assert not p.overlay_is_false_alarm(("https://other.example/", "hash2"), ["div.x"])


def test_stuck_after_limit_and_oscillation() -> None:
    p = Progress(stuck_limit=4)
    assert not p.observe(S)
    assert [p.observe(S) for _ in range(4)] == [False, False, False, True]
    q = Progress(stuck_limit=4)
    q.observe(S)
    q.observe(S2)  # new state: progress
    assert [q.observe(s) for s in (S, S2, S, S2)] == [False, False, False, True]  # A/B/A/B is stuck too
    r = Progress(stuck_limit=2)
    r.observe(S)
    r.observe(S)
    assert not r.observe(("https://air.example/results", "hash1"))  # URL change resets


def test_form_and_button_summaries() -> None:
    fields = form_summary(AIRLINE_CONTROLS)
    assert "Departing from [combobox] = Canberra *required" in fields
    assert "Travel dates [picker button] = 14 Feb 2027" in fields
    assert "Promo code [text] = (empty)" in fields
    assert "alex@example.com" not in fields and "Email [email] = (filled)" in fields
    assert "Menu" not in fields
    buttons = button_summary(AIRLINE_CONTROLS)
    assert "'Search flights'" in buttons and "Skip to" not in buttons
    long = [{"tag": "input", "type": "text", "label": f"Field number {i}", "value": ""} for i in range(200)]
    assert len(form_summary(long)) < 1000


def test_prompt_overlay_and_avoid() -> None:
    obs = _observation()
    assert "Overlay visible: True (div.cookie-bar" in _decision_prompt(obs, [])
    prompt = _decision_prompt(obs, [], ["DO_DISMISS_OVERLAY"], overlay_false_alarm=True)
    assert "Overlay visible: False" in prompt
    assert "Avoid (already tried on this exact screen with no effect): DO_DISMISS_OVERLAY" in prompt
    assert "Avoid" not in _decision_prompt(obs, [])


def test_fallback_respects_false_alarm_and_bans() -> None:
    search = SimpleNamespace(description="the 'Search flights' button", method="click", selector="/x")
    skip = SimpleNamespace(description="'Skip to main content' link", method="click", selector="/y")
    obs = _observation(actions=[skip, search])
    assert _fallback(obs, "r").route == "DO_DISMISS_OVERLAY"
    chosen = _fallback(obs, "r", overlay_false_alarm=True)
    assert chosen.route == "DO_ACT" and chosen.target is search
    banned = action_key("DO_ACT", "Click the 'Search flights' button")
    assert _fallback(obs, "r", [banned, "DO_DISMISS_OVERLAY"]).target is None


class _FakeStagehand:
    def __init__(self, *answers: NextActionModel) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def extract(self, prompt: str, schema: Any, timeout: int) -> Any:
        self.prompts.append(prompt)
        return SimpleNamespace(data=self.answers.pop(0))


def test_rejected_overlay_decision_is_reasked() -> None:
    search = SimpleNamespace(description="the 'Search flights' button", method="click", selector="/x")
    fake = _FakeStagehand(
        NextActionModel(route="DO_DISMISS_OVERLAY", reason="popup"),
        NextActionModel(route="DO_ACT", target_index=0, instruction="Click the 'Search flights' button", reason="go"),
    )
    decision = asyncio.run(determine_next_step(fake, _observation(actions=[search]), overlay_false_alarm=True))
    assert decision.route == "DO_ACT" and decision.target is search
    assert len(fake.prompts) == 2 and "rejected" in fake.prompts[1]


def test_repeatedly_rejected_decision_falls_back() -> None:
    search = SimpleNamespace(description="the 'Search flights' button", method="click", selector="/x")
    fake = _FakeStagehand(
        NextActionModel(route="DO_DISMISS_OVERLAY", reason="popup"),
        NextActionModel(route="DO_DISMISS_OVERLAY", reason="popup"),
    )
    decision = asyncio.run(determine_next_step(fake, _observation(actions=[search]), avoid=["DO_DISMISS_OVERLAY"]))
    assert decision.route == "DO_ACT" and decision.target is search
