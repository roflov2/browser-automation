"""The Uber Eats checkout loop (walk_to_checkout) with the page and the model stubbed out: no browser, no LLM."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.core import CartLost, ExtractFailed, FailureReason, LineItem
from audit.workflows.ubereats import store
from audit.workflows.ubereats.models import CartAndCheckoutState, UberResult

STORE_URL = "https://www.ubereats.com/au/store/agostinis/o6d2Nac0RyC7iXH333iOBw?diningMode=DELIVERY"


def screen(step_type: str, cart_visible: bool, items: int = 0) -> CartAndCheckoutState:
    return CartAndCheckoutState(
        step_type=step_type, is_checkout_page=False, is_cart_visible=cart_visible, is_upsell_modal=False,
        blocked_reason=None, items=[LineItem(name=f"Item {i}", variant=None, quantity=1, price="$8.00") for i in range(items)],
        subtotal="$17.00" if items else None, delivery_fee=None, priority_delivery_fee=None, service_fee=None,
        small_order_fee=None, other_fees=[], tax_amount=None, tax_included=None, total=None, currency=None, offers=[],
    )  # fmt: skip


class Screens:
    """Stagehand stand-in whose extract() reads the next screen from a list (or raises it)."""

    def __init__(self, screens: list) -> None:
        self.screens = list(screens)

    async def extract(self, instruction: str, schema: type, **_: object) -> SimpleNamespace:
        answer = self.screens.pop(0) if self.screens else screen("store_menu", cart_visible=False)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(data=answer)


class StorePage:
    async def url(self) -> str:
        return STORE_URL


@pytest.fixture
def page_steps(monkeypatch) -> dict:
    """Stub every DOM step walk_to_checkout takes; `cart_count` is what the header badge and drawer show."""
    seen = {"cart_count": 0, "checkout_clicks": 0, "shots": []}

    async def nothing(*_: object, **__: object) -> None:
        return None

    async def false(*_: object, **__: object) -> bool:
        return False

    async def cart_count(*_: object) -> int:
        return seen["cart_count"]

    async def open_cart_or_checkout(stagehand, page, cart_visible: bool) -> None:
        seen["checkout_clicks"] += cart_visible

    async def shot(page, path: Path, **_: object) -> None:
        seen["shots"].append(path.name)

    for name in ("close_item_modal", "settle"):
        monkeypatch.setattr(store, name, nothing)
    for name in ("open_cart_drawer", "is_item_modal_open", "js"):
        monkeypatch.setattr(store, name, false)
    monkeypatch.setattr(store, "get_cart_count", cart_count)
    monkeypatch.setattr(store, "items_left_in_cart", cart_count)
    monkeypatch.setattr(store, "open_cart_or_checkout", open_cart_or_checkout)
    monkeypatch.setattr(store, "shot", shot)
    return seen


def walk(screens: list, tmp_path: Path) -> tuple[UberResult, object]:
    result = UberResult(site="ubereats", out_dir=tmp_path)
    try:
        outcome = asyncio.run(store.walk_to_checkout(Screens(screens), StorePage(), result, interactive=False))  # type: ignore[arg-type]
    except CartLost as error:
        outcome = error
    return result, outcome


def test_a_cart_that_comes_up_empty_after_checkout_stops_the_run(page_steps, tmp_path):
    """The Agostinis run: the drawer showed 2 items, then 'Go to checkout' led back to the store with no cart."""
    result, outcome = walk([screen("cart_drawer", cart_visible=True, items=2)], tmp_path)
    assert isinstance(outcome, CartLost)
    assert outcome.reason is FailureReason.CART_LOST
    assert "2 item(s), subtotal $17.00" in str(outcome) and STORE_URL in str(outcome)
    assert [s["step"] for s in result.steps] == [1, 2, 3]  # checked twice, not retried up to MAX_CHECKOUT_STEPS
    assert page_steps["checkout_clicks"] == 1
    assert "step-03-cart-lost.png" in page_steps["shots"]


def test_a_cart_the_badge_still_counts_is_not_called_lost(page_steps, tmp_path):
    page_steps["cart_count"] = 2
    result, outcome = walk([screen("cart_drawer", cart_visible=True, items=2)], tmp_path)
    assert not isinstance(outcome, CartLost)
    assert len(result.steps) == store.MAX_CHECKOUT_STEPS


def test_an_empty_store_page_before_any_cart_was_seen_is_not_called_lost(page_steps, tmp_path):
    _, outcome = walk([], tmp_path)
    assert not isinstance(outcome, CartLost)
    assert outcome == (None, None)


def test_a_screen_that_cannot_be_read_is_recorded_before_the_error(page_steps, tmp_path):
    failure = ExtractFailed("extract(CartAndCheckoutState) failed: path [structuredContent, tax_included]")
    result = UberResult(site="ubereats", out_dir=tmp_path)
    stagehand = Screens([screen("cart_drawer", cart_visible=True, items=2), failure])
    with pytest.raises(ExtractFailed):
        asyncio.run(store.walk_to_checkout(stagehand, StorePage(), result, interactive=False))  # type: ignore[arg-type]
    assert result.steps[-1] == {"step": 2, "step_type": "extract_failed", "url": STORE_URL, "error": str(failure)}
    assert "step-02-extract-failed.png" in page_steps["shots"]
