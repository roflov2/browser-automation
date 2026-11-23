"""Uber Eats data: the schemas the LLM extracts into, and the per-run result."""

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ...core import Fee, LineItem, SiteResult

EXTRA_CATEGORIES = (
    "delivery_fee, service_fee, small_order_fee, priority_delivery, express_delivery, "
    "uber_one_membership, item_modifier_paid, checkout_upsell, tip, bag_fee, "
    "payment_surcharge, other"
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AddressAndFeedState(BaseModel):
    displayed_address: str | None = Field(
        description="Exact delivery address text currently shown in the top header location pill (e.g. '240 City Walk', '1 Jeffcott Pl'), or null if it says 'Enter delivery address' or is not set"
    )
    address_modal_or_input_open: bool = Field(
        description="True if the 'Deliver to' / address input dialog or text field is currently open on screen, "
        "or a location prompt such as 'Let us know your building type' / dropoff details is open"
    )
    address_suggestions_open: bool = Field(
        description="True if an autocomplete dropdown list of address suggestions is currently visible below the address input"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, mandatory login wall, or bot check, describe it; otherwise null"
    )
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class StoreCard(BaseModel):
    name: str = Field(description="Restaurant name as displayed (exclude supermarkets/grocery/liquor stores)")
    delivery_fee_display: str | None = Field(
        description="Advertised delivery fee on the card, e.g. '$2.99 Delivery Fee', '$0 Delivery Fee'; null if not shown"
    )
    service_fee_display: str | None = Field(description="Service fee if stated on the card; otherwise null")
    eta: str | None = Field(description="Estimated delivery time, e.g. '20-30 min'")
    available: bool = Field(
        description="True if the restaurant is currently open for delivery (not closed/unavailable)"
    )
    is_restaurant: bool = Field(
        description="True for prepared-food restaurants/cafes/takeaway; False for supermarkets, grocery, convenience or liquor stores (e.g. Coles, Woolworths, 7-Eleven, BWS)"
    )
    url: str | None = Field(description="Link (href) to the `/store/...` page, if present")


class StoreFeed(BaseModel):
    stores: list[StoreCard]


class StoreHeaderInfo(BaseModel):
    store_name: str = Field(description="Restaurant name on the store page")
    store_address_or_suburb: str | None = Field(
        description="Restaurant street address or suburb shown on the store page, if any"
    )
    advertised_delivery_fee: str | None = Field(
        description="Delivery fee shown in the store header/info banner, e.g. '$3.99 Delivery Fee'; null if not shown"
    )
    advertised_service_fee: str | None = Field(
        description="Service fee or minimum order requirement shown in the store header, if any"
    )
    store_open: bool = Field(description="True if the store is open and accepting orders now")


class MenuItem(BaseModel):
    name: str = Field(description="Clean menu item title without price, discount badge or calorie text")
    category: str | None = Field(description="Menu section heading, e.g. 'Burgers', 'Mains', 'Sides'")
    price: str | None = Field(
        default=None,
        description="Current price as displayed on the menu, e.g. '$14.70' or 'A$22.00'; null if none is shown",
    )
    sold_out: bool = Field(description="True if marked sold out or unavailable")
    is_food_or_drink: bool = Field(
        description="True for regular food, meals, sides or drinks; False for cutlery/utensils, napkins, bags, or donations"
    )


class StoreMenu(BaseModel):
    store_header: StoreHeaderInfo
    items: list[MenuItem]


class Offer(BaseModel):
    category: str = Field(description=f"One of: {EXTRA_CATEGORIES}")
    label: str = Field(
        description="Name of the extra, fee, upsell or delivery tier, e.g. 'Priority delivery', 'Service Fee', 'Add Bacon'"
    )
    price: str | None = Field(
        description="Price or rate as displayed, e.g. '$2.99', '+$3.00', '10%'; null if not shown"
    )
    price_basis: str | None = Field(description="e.g. 'per item', 'per order', 'monthly subscription', if stated")
    selected: bool = Field(description="True if currently ticked/selected/included in the order total")
    mandatory: bool = Field(description="True if required to complete the item or order and cannot be removed/declined")


class ItemModalState(BaseModel):
    has_unmet_required_choices: bool = Field(
        description="True if ANY section marked 'Required' (or 'Choose 1') does NOT have a radio/checkbox selected yet. "
        "Ignore the 'Most popular' horizontal carousel at the top if actual 'Required' sections below it are still unselected."
    )
    next_required_group: str | None = Field(
        description="Exact heading of the first unselected 'Required' section (e.g. 'DOUBLE CHEESEBURGER', 'Choice of Side'); "
        "null only if every 'Required' section already has an active selection"
    )
    cheapest_option_in_required_group: str | None = Field(
        description="Exact label of the cheapest (ideally $0 / no '+$' price) option inside `next_required_group` (e.g. 'Single')"
    )
    button_price_display: str | None = Field(
        description="Price shown on the 'Add 1 to order • $XX.XX' button at the bottom of the modal"
    )
    paid_modifier_offers: list[Offer] = Field(
        description="Optional or required paid modifiers/add-ons visible in this modal (where price > $0), up to 10 items"
    )
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class CartAndCheckoutState(BaseModel):
    step_type: str = Field(
        description="One of: store_menu (if cart drawer is NOT open yet), cart_drawer (if the side cart drawer is open), "
        "upsell_modal (if an interstitial 'Complete your meal' / 'Uber One' popup is open), "
        "checkout (if on the final '/checkout' page with Delivery Fee & Service Fee), or login_wall (if sign-in is required)"
    )
    is_checkout_page: bool = Field(
        description="True ONLY if on the final checkout/review page showing the full fee breakdown (Subtotal, Delivery Fee, Service Fee, Total)"
    )
    is_cart_visible: bool = Field(
        description="True ONLY if the cart drawer/sidebar is open and listing line items with a 'Go to checkout' button (NOT just the small green header icon)"
    )
    is_upsell_modal: bool = Field(
        description="True if an interstitial upsell modal ('Complete your meal', 'Frequently bought together', 'Try Uber One') is blocking progress"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a mandatory login/sign-in screen, phone verification, CAPTCHA, or bot check, describe it; otherwise null"
    )
    items: list[LineItem]
    subtotal: str | None = Field(description="Items subtotal before delivery and service fees, if shown")
    delivery_fee: str | None = Field(
        description="Standard delivery fee shown in the fare breakdown, e.g. '$3.99', '$0.00'; null if not yet shown"
    )
    priority_delivery_fee: str | None = Field(
        description="Extra fee advertised for 'Priority' / faster delivery option if shown, e.g. '+$2.99'; otherwise null"
    )
    service_fee: str | None = Field(
        description="Service fee shown in the fare breakdown, e.g. '$4.00'; null if not yet shown"
    )
    small_order_fee: str | None = Field(description="Small order / minimum basket fee if shown; otherwise null")
    other_fees: list[Fee] = Field(
        description="Any other fees or charges shown (e.g. bag fee, delivery adjustment, regulatory fee, tip) excluding Subtotal, Delivery Fee, Service Fee and Small Order Fee"
    )
    tax_amount: str | None = Field(description="Estimated taxes/GST if itemised separately")
    tax_included: bool | None = Field(
        default=None,
        description="True if prices are shown as including GST/tax (e.g. 'incl. GST'); False if tax is added as a "
        "separate line; null if not stated",
    )
    total: str | None = Field(description="Total amount due (or cart subtotal if full total not yet shown)")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    offers: list[Offer] = Field(
        description="Every optional upsell, delivery speed tier (Priority vs Standard), Uber One prompt, tip preset, or fee visible on this screen"
    )
    summary: str | None = Field(default=None, description="One short sentence describing this screen; null if unsure")


def extract_suburb_hint(name: str | None) -> str | None:
    """Extract a suburb hint like 'Braddon' from 'Grease Monkey (BRADDON)'."""
    m = re.search(r"\(([^)]+)\)", name or "")
    return m.group(1).strip() if m else None


@dataclass
class UberResult(SiteResult):
    store_name: str | None = None
    store_location: str | None = None
    advertised_delivery_fee_display: str | None = None
    advertised_delivery_fee: float | None = None
    advertised_service_fee_display: str | None = None
