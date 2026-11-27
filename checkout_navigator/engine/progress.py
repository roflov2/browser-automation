"""What the loop remembers between steps, so it never retries the same useless action on the same screen.

Pure bookkeeping with no browser or model calls, so it can be unit-tested:

* **False overlay alarms.** When the overlay helper found nothing to close and the DOM scan still says "overlay",
  that page state (URL + DOM hash, and the URL + matched element) is remembered. For it, the model is no longer told
  an overlay is visible, the fallback does not pick the overlay route, and stagnation recovery does not try again.
* **Route bans.** An action (a helper route, or DO_ACT with one instruction) that had no effect twice on the same
  state is excluded from the next decision on that state.
* **Stuck detection.** Steps that land on a URL + DOM state already seen since the URL last changed count as no
  progress; after `stuck_limit` of them in a row the loop ends with STUCK.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

State = tuple[str, str]  # (url, compute_dom_hash)

NO_EFFECT_LIMIT = 2  # an action that changed nothing this many times on one state is banned there
SKIP_LINK = re.compile(r"\bskip\s+(to|navigation)\b", re.IGNORECASE)


def action_key(route: str, instruction: str | None = None) -> str:
    """What counts as 'the same action': the route for a helper, the route plus instruction for DO_ACT."""
    if route != "DO_ACT":
        return route
    text = " ".join((instruction or "").lower().split())
    return f'DO_ACT "{text}"' if text else route


def is_skip_link(text: str | None) -> bool:
    """A 'Skip to main content' style accessibility link (or an instruction to click one)."""
    return bool(text and SKIP_LINK.search(text))


@dataclass
class Progress:
    stuck_limit: int = 4
    false_overlay_states: set[State] = field(default_factory=set)
    false_overlay_elements: set[tuple[str, str]] = field(default_factory=set)  # (url, matched element description)
    no_effect: dict[State, Counter[str]] = field(default_factory=dict)
    seen_on_url: set[str] = field(default_factory=set)  # DOM hashes seen since the URL last changed
    url: str = ""
    stuck_steps: int = 0
    pending: tuple[State, str] | None = None  # the last decision, until its effect is known

    # -- overlays ---------------------------------------------------------------------------------------------
    def mark_false_overlay(self, state: State, matches: list[str] | None = None) -> None:
        self.false_overlay_states.add(state)
        for match in matches or []:
            self.false_overlay_elements.add((state[0], match))

    def overlay_is_false_alarm(self, state: State, matches: list[str] | None = None) -> bool:
        if state in self.false_overlay_states:
            return True
        return bool(matches) and all((state[0], m) in self.false_overlay_elements for m in matches or [])

    # -- route bans -------------------------------------------------------------------------------------------
    def decided(self, state: State, key: str) -> None:
        """A decision is about to run on `state`."""
        self.pending = (state, key)

    def outcome(self, effective: bool) -> None:
        """The helper reported its result. A reported failure is a strike right away."""
        if self.pending is None:
            return
        if not effective:
            self._strike(*self.pending)
            self.pending = None

    def settled(self, state: State) -> None:
        """The page after the last decision. Unchanged means that decision had no effect after all."""
        if self.pending is not None and self.pending[0] == state:
            self._strike(*self.pending)
        self.pending = None

    def _strike(self, state: State, key: str) -> None:
        self.no_effect.setdefault(state, Counter())[key] += 1

    def banned(self, state: State) -> list[str]:
        return sorted(k for k, n in self.no_effect.get(state, Counter()).items() if n >= NO_EFFECT_LIMIT)

    # -- stuck ------------------------------------------------------------------------------------------------
    def observe(self, state: State) -> bool:
        """Record an observed state. True once `stuck_limit` steps in a row brought nothing new."""
        url, dom_hash = state
        if url != self.url:
            self.url, self.seen_on_url, self.stuck_steps = url, {dom_hash}, 0
        elif dom_hash in self.seen_on_url:
            self.stuck_steps += 1
        else:
            self.seen_on_url.add(dom_hash)
            self.stuck_steps = 0
        return self.stuck_steps >= self.stuck_limit
