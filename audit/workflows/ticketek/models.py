"""Extraction schemas for Ticketek pages, and the run result."""

from dataclasses import dataclass

from pydantic import BaseModel, Field

from ...core import Fee, LineItem, SiteResult

PAGE_TYPES = (
    "home, search_results, event, ticket_selection, seat_map, waiting_room, login_prompt, cart, delivery, extras, "
    "upsell_modal, review, payment, confirmation, error, other"
)
EXTRA_CATEGORIES = (
    "service_fee, handling_fee, booking_fee, delivery_fee, payment_surcharge, ticket_insurance, refund_protection, "
    "parking, merchandise, upgrade_or_vip, donation, membership, other"
)


# ---------------------------------------------------------------------------
# Finding the event
# ---------------------------------------------------------------------------
class FindState(BaseModel):
    page_type: str = Field(description=f"Which kind of page this is. One of: {PAGE_TYPES}")
    event_name: str | None = Field(description="Name of the event if this is an event or ticket selection page")
    matching_events: list[str] = Field(
        description="On a search results or listing page: names of the on-sale events listed that match what we are "
        "looking for, in page order. Empty otherwise."
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, 'verify you are human', access-denied or bot check, describe "
        "it; otherwise null. A queue / waiting room is NOT blocked."
    )
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class Session(BaseModel):
    date: str = Field(description="Date and time of the performance as displayed, e.g. 'Sat 14 Mar 2027, 7:30pm'")
    venue: str | None = Field(description="Venue and city as displayed")
    status: str | None = Field(
        default=None,
        description="One of: on_sale, selling_fast, limited, sold_out, not_on_sale_yet, cancelled, resale_only; "
        "null if no status is shown",
    )
    from_price: str | None = Field(description="Lowest price shown for this session, e.g. 'From $89.90'; null if none")


class EventPage(BaseModel):
    event_name: str | None
    advertised_from_price: str | None = Field(
        description="Headline 'from' price advertised for the event (e.g. 'Tickets from $59.90'); null if none"
    )
    price_note: str | None = Field(
        description="What the advertised prices cover, quoted if stated, e.g. 'Prices include GST and booking fee', "
        "'+ service fee', 'Transaction fees apply'"
    )
    sessions: list[Session] = Field(description="Every performance/session listed on the page")
    is_ticket_selection: bool = Field(
        description="True if ticket types, prices and quantity controls are already visible on this page"
    )


# ---------------------------------------------------------------------------
# Choosing tickets
# ---------------------------------------------------------------------------
class PriceOption(BaseModel):
    area: str | None = Field(
        description="Seating area, section, reserve or tier, e.g. 'A Reserve', 'General Admission'"
    )
    price_type: str | None = Field(
        default=None,
        description="Ticket type as displayed, e.g. 'Adult', 'Child', 'Concession', 'Student', 'Family', 'Member'; "
        "null if the tier shows no separate ticket type",
    )
    price: str | None = Field(description="Price per ticket as displayed, e.g. '$89.90'; null if unavailable")
    available: bool = Field(description="False if sold out, unavailable or greyed out")
    price_note: str | None = Field(description="Anything said next to the price, e.g. 'incl. $4.50 booking fee'")


class TicketOptions(BaseModel):
    is_ticket_selection: bool = Field(
        description="True if ticket types/areas with prices are shown (either with quantity controls OR on a seat map / 'Choose your own' / 'Find Best available' view)"
    )
    options: list[PriceOption] = Field(
        description="Distinct ticket area / price type combinations shown with their price (summarise per seating tier/area, never per individual seat on the map; at most 15)"
    )
    price_basis: str | None = Field(
        description="What the prices cover, if stated, e.g. 'per ticket incl. booking fee', 'plus service fee'"
    )
    blocked_reason: str | None = Field(
        description="If a CAPTCHA, 'verify you are human' or bot check blocks the page, describe it; otherwise null"
    )


class HeldTickets(BaseModel):
    tickets_held: bool = Field(
        description="True if tickets are now reserved/held: a cart, basket, order summary or countdown timer for "
        "the held tickets is shown"
    )
    area: str | None = Field(description="Seating area of the held tickets")
    seats: str | None = Field(description="Seat or row numbers if shown")
    price_type: str | None
    quantity: int | None = Field(description="Number of tickets held")
    unit_price: str | None = Field(description="Price per ticket as displayed")
    total: str | None = Field(description="Total currently displayed for the held tickets")
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


# ---------------------------------------------------------------------------
# Walking to payment
# ---------------------------------------------------------------------------
class Offer(BaseModel):
    category: str = Field(description=f"One of: {EXTRA_CATEGORIES}")
    label: str = Field(description="Name of the extra or fee as displayed, e.g. 'Mobile ticket', 'Refund protection'")
    price: str | None = Field(description="Price as displayed, e.g. '$7.95', 'Free', '2.2%'; null if not shown")
    price_basis: str | None = Field(description="e.g. 'per ticket', 'per transaction', if stated")
    selected: bool = Field(description="True if it is currently ticked/selected and so included in the total")
    mandatory: bool = Field(description="True if it is a compulsory charge that cannot be declined")


class StepState(BaseModel):
    page_type: str = Field(description=f"Which step of the purchase this is. One of: {PAGE_TYPES}")
    heading: str | None = Field(description="Main heading or step name shown, e.g. 'Delivery', 'Your cart'")
    is_payment_page: bool = Field(
        description="True only on the final step where card / payment details are requested to complete the purchase"
    )
    login_required: bool = Field(
        description="True if the page requires signing in to a Ticketek account before going further"
    )
    in_queue: bool = Field(description="True if this is a queue / waiting room page (e.g. 'You are in the queue')")
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, 'verify you are human', access-denied, session-expired or "
        "bot check, describe it; otherwise null. A login page or a queue is NOT blocked."
    )
    offers: list[Offer] = Field(
        description="Every fee, delivery option, add-on, upsell or surcharge shown on this step (in the page or an "
        "open modal) with its price. Empty if none."
    )
    price_lines: list[Fee] = Field(description="Lines of the order summary / total panel currently displayed")
    total: str | None = Field(description="Order total currently displayed, e.g. '$195.75'")
    validation_errors: list[str] = Field(description="Any form validation or error messages currently displayed")
    summary: str | None = Field(default=None, description="One short sentence describing this screen; null if unsure")


class TicketLine(LineItem):
    name: str = Field(description="Ticket line as displayed, e.g. 'A Reserve - Adult'")
    variant: str | None = Field(description="Price type, e.g. 'Adult'")
    quantity: int = Field(description="Number of tickets on this line")
    price: str | None = Field(description="Line total as displayed for this line (all its tickets)")


class Surcharge(BaseModel):
    payment_method: str = Field(description="e.g. 'Visa/Mastercard', 'Amex', 'PayPal'")
    fee: str | None = Field(
        default=None, description="Fee as displayed, e.g. '1.5%', '$2.20', 'No fee'; null if no fee is shown"
    )


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(
        description="True if on the payment / review-and-pay page showing the order's price summary"
    )
    blocked_reason: str | None = Field(
        description="If a required login, CAPTCHA, 'verify you are human' or bot check blocks the page, describe it; "
        "otherwise null"
    )
    items: list[TicketLine]
    ticket_subtotal: str | None = Field(description="Total for the tickets alone, before any fees, as displayed")
    booking_fees: list[Fee] = Field(
        description="Per-ticket booking/service fees itemised separately from the ticket price, if any"
    )
    service_and_handling_fees: list[Fee] = Field(
        description="Per-transaction service, handling or transaction fees, if any"
    )
    delivery_fee: Fee | None = Field(description="The charge for the chosen delivery method, if shown (even if free)")
    extras: list[Fee] = Field(description="Optional extras included in the total (refund protection, parking, ...)")
    payment_surcharges: list[Surcharge] = Field(
        description="Card/payment-method fees advertised on the page, whether or not applied to the total yet"
    )
    total: str | None = Field(description="Total amount due as displayed")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    summary: str | None = Field(default=None, description="One short sentence describing this screen; null if unsure")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class TicketekResult(SiteResult):
    event_name: str | None = None
    session: str | None = None
    venue: str | None = None
    advertised_from_price_display: str | None = None
    advertised_price_note: str | None = None
    cheapest_offered: float | None = None  # cheapest available ticket of any type on the ticket selection page
