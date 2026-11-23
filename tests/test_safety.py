"""The purchase guard's rules and vetting logic (no browser)."""

import asyncio
import contextlib
from types import SimpleNamespace
from typing import ClassVar

import pytest

from audit import safety
from audit.core import FailureReason, UnsafeActionRefused
from audit.safety import Guard, Mode, check_action, click_verdict, field_verdict, safe_act

# Labels that must never be clickable. The first nine are the brief's list.
MUST_BLOCK = [
    "Place order", "Pay now", "Pay $54.20", "Confirm and pay", "Purchase", "Complete booking", "Buy now",
    "Start free trial", "Join Uber One",
    "PLACE ORDER", "Place your order", "Pay", "Pay A$54.20", "Pay AUD 54.20", "Pay with card", "Confirm & pay",
    "Complete order", "Complete purchase", "Submit order", "Confirm booking", "Complete your payment", "Purchase now",
    "Buy it now", "Buy with Shop Pay", "Book with Apple Pay", "Checkout · Pay $54.20", "Checkout with PayPal",
    "Shop Pay", "G Pay", "Google Pay", "Apple Pay", "PayPal", "Afterpay", "Zip", "PayID",
    "Try Uber One free for 4 weeks", "Save an extra $4.99 on this order with a free trial of Uber One",
    "Join now", "Join for free", "Start your free trial", "Subscribe & save 10%", "Sign up", "Create account",
]  # fmt: skip
# Labels the workflows legitimately click. The first seven are the brief's list.
MUST_ALLOW = [
    "Checkout", "Go to checkout", "Continue to checkout", "Continue to payment", "Continue", "Add to cart", "Next",
    "Check out", "CHECKOUT", "View cart", "Add to bag", "Add 1 to order • $25.00", "Add to order", "Update order",
    "One-time purchase", "Purchase options", "Skip", "No thanks", "Not now", "Maybe later", "Close", "Accept all cookies",
    "Find flights", "Select", "Continue as guest", "Skip seat selection", "Shop", "Shop Coffee", "Menu", "Order online",
    "Pickup", "Clear cart", "250g", "Standard", "Payment", "Proceed to payment", "Deliver now", "Zip code", "Sign in",
]  # fmt: skip
SENSITIVE_FIELDS = [
    "cc-number cardnumber Card number", "Card number", "cardNumber", "cc-csc cvv CVV", "Security code", "CVC",
    "cc-exp exp-date MM / YY", "Expiry date", "Expiration date (MM / YY)", "Discount code or gift card", "Gift card",
    "Voucher code", "Promo code", "Coupon", "Travel Bank",
]  # fmt: skip
ORDINARY_FIELDS = [
    "email Email", "given-name firstName First name", "Last name", "tel Phone number", "Date of birth",
    "Delivery address", "Search Uber Eats", "postal-code Postcode", "Fly from", "Add an order note",
]  # fmt: skip


@pytest.mark.parametrize("label", MUST_BLOCK)
def test_denied_labels(label):
    assert click_verdict(label) is not None


@pytest.mark.parametrize("label", MUST_ALLOW)
def test_allowed_labels(label):
    assert click_verdict(label) is None


def test_deny_wins_over_any_other_part():
    assert click_verdict(["Continue", "Place order"]) is not None
    assert click_verdict(["", "  \n "]) is None  # unlabelled is not denied in normal mode


def test_long_labels_are_read_from_the_start():
    assert click_verdict("Place order " + "x" * 500) is not None
    assert click_verdict("x" * 500 + " Place order") is None  # e.g. a whole card; its own button is checked separately


@pytest.mark.parametrize("label", ["Order now", "Book now", "Confirm", "Submit"])
def test_ambiguous_labels_are_denied_only_on_checkout_urls(label):
    assert click_verdict(label, url="https://restaurant.test/menu") is None
    assert click_verdict(label, url="https://shop.test/checkouts/cn/abc") is not None
    assert click_verdict(label, url="https://book.air.test/dx/#/payment?x=1") is not None


@pytest.mark.parametrize(
    "label", ["Skip", "No thanks", "No, thanks", "Not now", "Maybe later", "Close", "×", "Close dialog"]
)
def test_restricted_mode_allows_dismissals(label):
    assert click_verdict(label, mode=Mode.RESTRICTED) is None


@pytest.mark.parametrize("label", ["Continue", "Next", "Edit", "Go to checkout", "Add to cart", "", "Place order"])
def test_restricted_mode_refuses_everything_else(label):
    assert click_verdict(label, mode=Mode.RESTRICTED) is not None


def test_frozen_mode_refuses_everything():
    assert click_verdict("Skip", mode=Mode.FROZEN) == safety.FROZEN_REASON
    assert click_verdict("Close", mode=Mode.FROZEN) is not None


@pytest.mark.parametrize("descriptor", SENSITIVE_FIELDS)
def test_sensitive_fields(descriptor):
    assert field_verdict(descriptor) is not None


@pytest.mark.parametrize("descriptor", ORDINARY_FIELDS)
def test_ordinary_fields(descriptor):
    assert field_verdict(descriptor) is None


def test_card_number_detection():
    assert safety.looks_like_card_number("4111 1111 1111 1111")
    assert safety.looks_like_card_number("5555-5555-5555-4444")
    assert not safety.looks_like_card_number("0400 000 000")
    assert not safety.looks_like_card_number("4111 1111 1111 1112")
    assert not safety.looks_like_card_number("15 June 1988")


# --- Guard state ---------------------------------------------------------------------------------
class FakePage:
    """A page that answers evaluate() with whatever the test queued for it."""

    def __init__(
        self, url: str = "https://shop.test/products/x", element: dict | None = None, blocked: list | None = None
    ):
        self.current_url = url
        self.element = element
        self.blocked = blocked or []
        self.scripts: list[str] = []

    async def url(self) -> str:
        return self.current_url

    async def evaluate(self, expression: str):
        self.scripts.append(expression)
        if "window.__auditGuard.drain()" in expression and "auditChain" not in expression:
            out, self.blocked = self.blocked, []
            return out
        return self.element


def test_checkout_urls_are_restricted_without_being_asked():
    guard = Guard()
    assert guard.mode_for("https://shop.test/collections/all") is Mode.NORMAL
    assert guard.mode_for("https://shop.test/cart") is Mode.NORMAL
    assert guard.mode_for("https://shop.test/pages/payment-options") is Mode.NORMAL
    assert guard.mode_for("https://shop.test/checkouts/cn/abc/en-au?x=1") is Mode.RESTRICTED
    assert guard.mode_for("https://www.ubereats.com/au/checkout?ps=1") is Mode.RESTRICTED
    assert guard.mode_for("https://book.air.test/dx/VADX/#/payment?journeyType=one-way") is Mode.RESTRICTED


def test_freeze_is_per_host_and_pushed_to_the_page():
    guard, page = Guard(), FakePage("https://shop.test/checkouts/cn/abc")
    asyncio.run(guard.freeze(page))
    assert guard.mode_for("https://shop.test/anything") is Mode.FROZEN
    assert guard.mode_for("https://restaurant.test/menu") is Mode.NORMAL  # other hosts are unaffected
    assert '"frozen": ["shop.test"]' in page.scripts[-1]
    asyncio.run(guard.restrict(page))
    assert guard.mode_for("https://shop.test/") is Mode.FROZEN  # restrict never loosens a freeze


def test_active_guard_is_scoped_to_the_run():
    with safety.active_guard() as guard:
        assert safety.current_guard() is guard
        guard.frozen.add("shop.test")
    assert safety.current_guard().frozen == set()


# --- Vetting an observed action --------------------------------------------------------------------
def vet(description="Go to checkout button", selector="xpath=/html/body/button[1]", method="click", arguments=(),
        element=None, url="https://shop.test/cart", mode=Mode.NORMAL):  # fmt: skip
    check_action(description, selector, method, list(arguments), element, url=url, mode=mode)


def test_element_label_is_trusted_over_the_description():
    labelled = {"found": True, "labelled": True, "click": None, "field": None}
    vet(description="Close the 'Start free trial' popup", element=labelled)  # label is clean, so allowed
    denied = {**labelled, "click": "purchase, payment or membership control: Place order"}
    with pytest.raises(UnsafeActionRefused, match="Place order"):
        vet(description="Go to checkout button", element=denied)  # a harmless description does not help


def test_unresolved_element_falls_back_to_the_description():
    with pytest.raises(UnsafeActionRefused):
        vet(description="Click the 'Place order' button", element=None)
    with pytest.raises(UnsafeActionRefused):
        vet(description="Pay now", element={"found": False})
    vet(description="Go to checkout button", element=None)
    vet(description="Close the 'Start free trial' popup", element=None)  # reads as a dismissal


def test_unlabelled_element_falls_back_to_the_description():
    icon = {"found": True, "labelled": False, "click": None, "field": None}
    with pytest.raises(UnsafeActionRefused):
        vet(description="The Shop Pay express checkout button", element=icon)
    vet(description="Close button (X) of the newsletter popup", element=icon)


def test_selector_is_checked_too():
    with pytest.raises(UnsafeActionRefused):
        vet(description="The main button", selector='[data-testid="place-order-button"]', element=None)
    with pytest.raises(UnsafeActionRefused):
        vet(
            description="The main button", selector="#pay-now", element={"found": True, "labelled": True, "click": None}
        )


def test_typing_into_payment_fields_is_refused():
    card = {"found": True, "labelled": True, "click": None, "field": "payment or voucher field: cc-number"}
    with pytest.raises(UnsafeActionRefused, match="cc-number"):
        vet(description="Email field", method="fill", arguments=["x"], element=card)
    with pytest.raises(UnsafeActionRefused):
        vet(description="The CVV field of the payment form", method="type", arguments=["123"], element=None)
    with pytest.raises(UnsafeActionRefused, match="card number"):
        vet(description="Order note", method="fill", arguments=["4111 1111 1111 1111"],
            element={"found": True, "labelled": True, "click": None, "field": None})  # fmt: skip
    vet(description="First name field", method="fill", arguments=["Alex"],
        element={"found": True, "labelled": True, "click": None, "field": None})  # fmt: skip


def test_restricted_and_frozen_modes():
    ok = {"found": True, "labelled": True, "click": None, "field": None}
    refused = {**ok, "click": "only dismiss controls may be clicked on a checkout page: Continue"}
    vet(description="Skip button", element=ok, mode=Mode.RESTRICTED)  # the page already applied the mode
    with pytest.raises(UnsafeActionRefused):
        vet(description="Continue button", element=refused, mode=Mode.RESTRICTED)
    with pytest.raises(UnsafeActionRefused):
        vet(description="Continue button", element=None, mode=Mode.RESTRICTED)  # unresolved: fail closed
    vet(description="Close the upsell dialog", element=None, mode=Mode.RESTRICTED)
    with pytest.raises(UnsafeActionRefused):
        vet(description="Order note", method="fill", arguments=["hi"], element=ok, mode=Mode.RESTRICTED)
    with pytest.raises(UnsafeActionRefused, match="final checkout"):
        vet(description="Skip button", element=ok, mode=Mode.FROZEN)


# --- safe_act ----------------------------------------------------------------------------------
class FakeStagehand:
    def __init__(self, actions: list):
        self.actions = actions
        self.observed: list[tuple] = []
        self.acted: list[tuple] = []

    async def observe(self, instruction, **options):
        self.observed.append((instruction, options))
        return SimpleNamespace(data=self.actions)

    async def act(self, action, **options):
        self.acted.append((action, options))
        return SimpleNamespace(data=SimpleNamespace(success=True, action_description=f"did: {action.description}"))


def action(description: str, selector: str = "xpath=/html/body/button[1]", method: str = "click", arguments=()):
    return SimpleNamespace(description=description, selector=selector, method=method, arguments=list(arguments))


CLEAN = {"found": True, "labelled": True, "click": None, "field": None}


def test_safe_act_observes_vets_then_acts_on_the_vetted_action():
    proposed = action("Go to checkout button")
    stagehand, page = FakeStagehand([proposed, action("Place order")]), FakePage(element=CLEAN)
    done = asyncio.run(safe_act(stagehand, page, "Click checkout", variables={"name": "Alex"}))
    assert done == safety.Acted(True, "did: Go to checkout button")
    assert stagehand.acted[0][0] is proposed  # the Action object, never the instruction string
    assert (
        stagehand.observed[0][1]
        == stagehand.acted[0][1]
        == {"variables": {"name": "Alex"}, "timeout": safety.LLM_TIMEOUT_MS}
    )


def test_safe_act_refuses_without_acting():
    denied = {**CLEAN, "click": "purchase, payment or membership control: Place order"}
    stagehand, page = FakeStagehand([action("Go to checkout button")]), FakePage(element=denied)
    with pytest.raises(UnsafeActionRefused) as refusal:
        asyncio.run(safe_act(stagehand, page, "Click checkout"))
    assert stagehand.acted == []
    assert refusal.value.reason is FailureReason.UNSAFE_ACTION_REFUSED


def test_safe_act_reports_nothing_found_without_acting():
    stagehand = FakeStagehand([])
    assert asyncio.run(safe_act(stagehand, FakePage(), "Click checkout")) == safety.Acted(
        False, "no matching element found"
    )
    assert stagehand.acted == []


def test_safe_act_raises_when_the_page_blocked_the_click():
    page = FakePage(
        element=CLEAN, blocked=[{"why": "purchase, payment or membership control: Pay now", "kind": "click"}]
    )
    with pytest.raises(UnsafeActionRefused, match="Pay now"):
        asyncio.run(safe_act(FakeStagehand([action("Continue button")]), page, "Continue"))


def test_safe_act_does_not_even_observe_on_a_frozen_page():
    stagehand, page = FakeStagehand([action("Skip")]), FakePage("https://shop.test/checkouts/cn/abc", element=CLEAN)

    async def scenario():
        with safety.active_guard() as guard:
            await guard.freeze(page)
            await safe_act(stagehand, page, "Skip the upsell")

    with pytest.raises(UnsafeActionRefused, match="final checkout"):
        asyncio.run(scenario())
    assert stagehand.observed == []


def test_a_refusal_cannot_be_swallowed_by_broad_handlers():
    """Workflows wrap steps in `except Exception` and `suppress(Exception)`; a refusal must pass through both."""
    assert not issubclass(UnsafeActionRefused, Exception)

    def step():
        try:
            with contextlib.suppress(Exception):
                raise UnsafeActionRefused("Place order")
        except Exception:  # noqa: BLE001 - this is the pattern under test
            return "swallowed"
        return "continued"

    with pytest.raises(UnsafeActionRefused):
        step()


def test_follow_up_takes_a_vetted_second_step():
    """Opening a custom dropdown, then picking the option: both steps are observed and vetted."""
    opener, option = action("Title dropdown", "xpath=//div[1]"), action("Option 'Mr'", "xpath=//li[2]")

    class TwoStep(FakeStagehand):
        async def observe(self, instruction, **options):
            self.observed.append((instruction, options))
            return SimpleNamespace(data=[opener] if len(self.observed) == 1 else [option])

    stagehand = TwoStep([])
    done = asyncio.run(safe_act(stagehand, FakePage(element=CLEAN), "Select 'Mr' for the Title field", follow_up=True))
    assert [a for a, _ in stagehand.acted] == [opener, option]
    assert done.description == "did: Option 'Mr'"

    same = FakeStagehand([opener])  # the model points at the same element again: nothing more to do
    asyncio.run(safe_act(same, FakePage(element=CLEAN), "Select 'Mr' for the Title field", follow_up=True))
    assert len(same.acted) == 1

    denied = {**CLEAN, "click": "purchase, payment or membership control: Pay now"}
    refusing = TwoStep([])
    page = FakePage(element=CLEAN)
    original = page.evaluate

    async def evaluate(expression):
        if "auditChain" in expression and len(refusing.observed) == 2:
            return denied
        return await original(expression)

    page.evaluate = evaluate
    with pytest.raises(UnsafeActionRefused):
        asyncio.run(safe_act(refusing, page, "Select 'Mr' for the Title field", follow_up=True))
    assert [a for a, _ in refusing.acted] == [opener]


def test_dismiss_on_skips_a_refused_popup_control_without_acting():
    denied = {**CLEAN, "click": "purchase, payment or membership control: Subscribe"}
    stagehand, page = FakeStagehand([]), FakePage(element=denied)
    assert asyncio.run(safety.dismiss_on(stagehand, page, action("Subscription popup Subscribe button"))) is None
    assert stagehand.acted == []


def test_dismiss_on_acts_on_a_clean_close_button():
    stagehand, page = FakeStagehand([]), FakePage(element=CLEAN)
    done = asyncio.run(safety.dismiss_on(stagehand, page, action("Close")))
    assert done == safety.Acted(True, "did: Close")


def test_dismiss_on_still_raises_when_the_page_blocked_the_click():
    blocked = [{"why": "purchase, payment or membership control: Subscribe", "kind": "click"}]
    page = FakePage(element=CLEAN, blocked=blocked)
    with pytest.raises(UnsafeActionRefused, match="Subscribe"):
        asyncio.run(safety.dismiss_on(FakeStagehand([]), page, action("Close")))


def test_dismiss_popups_skips_subscribe_and_presses_escape(monkeypatch):
    from audit import page as page_helpers

    async def no_settle(page, ms=0):
        pass

    monkeypatch.setattr(page_helpers, "settle", no_settle)

    class EscPage(FakePage):
        keys: ClassVar[list[str]] = []

        async def key_press(self, key):
            self.keys.append(key)

    denied = {**CLEAN, "click": "purchase, payment or membership control: Subscribe"}
    stagehand, page = FakeStagehand([action("Subscribe button")]), EscPage(element=denied)
    asyncio.run(page_helpers.dismiss_popups(stagehand, page, "newsletter popup"))  # must not raise
    assert stagehand.acted == []
    assert page.keys == ["Escape"]


def test_a_submit_the_checkout_page_makes_itself_is_logged_not_fatal(caplog):
    blocked = [{"why": "form submit on a checkout page", "kind": "submit"}]
    with safety.active_guard(), caplog.at_level("INFO"):
        safety.raise_if_page_refused("https://shop.example/checkouts/cn/abc", blocked)
        safety.raise_if_page_refused("https://shop.example/anything", [])  # nothing cancelled, nothing to do
    assert "nothing was sent" in caplog.text


def test_a_page_side_block_on_an_ordinary_page_still_ends_the_run():
    blocked = [{"why": "purchase, payment or membership control: Buy now", "kind": "click"}]
    with safety.active_guard(), pytest.raises(UnsafeActionRefused, match="Buy now"):
        safety.raise_if_page_refused("https://shop.example/products/kenya", blocked)


def test_a_page_side_block_on_a_frozen_host_is_not_fatal():
    blocked = [{"why": safety.FROZEN_REASON, "kind": "submit"}]
    with safety.active_guard() as guard:
        guard.frozen.add("pay.example")
        safety.raise_if_page_refused("https://pay.example/summary", blocked)
