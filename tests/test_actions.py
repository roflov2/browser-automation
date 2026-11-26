"""llm_act's follow-up: choosing from a custom dropdown that the first action opened."""

import asyncio
from types import SimpleNamespace

from stagehand import Action

from audit import actions
from audit.actions import is_native_select, is_option_of, llm_act

FORM = "xpath=/html[1]/body[1]/div[1]/form[1]/div[1]/div[4]/div[1]/div[1]/div[1]/div[1]/div[1]"
COMBOBOX = f"{FORM}/div[1]"  # the trip-type combobox
ONE_WAY = f"{FORM}/ul[1]/li[1]"  # the 'One way' entry of the list it opens
ARRIVAL = "xpath=/html[1]/body[1]/div[1]/form[1]/div[1]/div[2]/div[1]/input[1]"  # another control of the form


def action(selector, description, method="click", arguments=None):
    return Action(selector=selector, description=description, method=method, arguments=arguments or [])


class FakeStagehand:
    """observe() returns the scripted result lists in turn; act() records what it was asked to run."""

    def __init__(self, *observed):
        self.observed = list(observed)
        self.instructions = []
        self.acted = []

    async def observe(self, instruction, **options):
        self.instructions.append(instruction)
        return SimpleNamespace(data=self.observed.pop(0) if self.observed else [])

    async def act(self, act, **options):
        self.acted.append(act)
        return SimpleNamespace(data=SimpleNamespace(success=True, action_description=act.description))


def run(stagehand, **options):
    return asyncio.run(llm_act(stagehand, "Goal: a one-way flight. Take the next action.", **options))


def test_select_option_on_a_div_becomes_a_click_then_the_option_is_picked():
    stagehand = FakeStagehand(
        [action(COMBOBOX, "Trip type combobox", "selectOptionFromDropdown", ["One way"])],
        [action(ONE_WAY, "The option to choose 'One way'")],
    )
    done = run(stagehand, follow_up=True)
    assert [(a.selector, a.method, a.arguments) for a in stagehand.acted] == [
        (COMBOBOX, "click", []),  # selectOption() does nothing on a div yet reports success: open it instead
        (ONE_WAY, "click", []),
    ]
    assert done.selector == ONE_WAY
    follow_up = stagehand.instructions[1]
    assert "'One way'" in follow_up and "Trip type combobox" in follow_up
    assert "never the control that opened it" in follow_up


def test_select_option_on_a_native_select_is_trusted():
    select = "xpath=/html[1]/body[1]/form[1]/select[1]"
    stagehand = FakeStagehand([action(select, "Trip type select", "selectOptionFromDropdown", ["One way"])])
    run(stagehand, follow_up=True)
    assert [(a.method, a.arguments) for a in stagehand.acted] == [("selectOptionFromDropdown", ["One way"])]
    assert len(stagehand.instructions) == 1


def test_follow_up_skips_the_opener_and_takes_the_option_further_down():
    stagehand = FakeStagehand(
        [action(COMBOBOX, "Trip type combobox")],
        [action(COMBOBOX, "Trip type combobox", "selectOptionFromDropdown", ["One way"]),
         action(ONE_WAY, "The option to choose 'One way'")],
    )  # fmt: skip
    done = run(stagehand, follow_up=True)
    assert [a.selector for a in stagehand.acted] == [COMBOBOX, ONE_WAY]
    assert done.selector == ONE_WAY


def test_follow_up_never_acts_on_an_unrelated_control():
    stagehand = FakeStagehand(
        [action(ARRIVAL, "Arrival location field")],
        [action(COMBOBOX, "Trip type combobox showing Return")],  # would set the trip back to Return
    )
    done = run(stagehand, follow_up=True)
    assert [a.selector for a in stagehand.acted] == [ARRIVAL]
    assert done.selector == ARRIVAL and done.success


def test_without_follow_up_only_the_first_result_is_acted_on_as_observed():
    stagehand = FakeStagehand(
        [action(COMBOBOX, "Trip type combobox", "selectOptionFromDropdown", ["One way"]),
         action(ONE_WAY, "The option to choose 'One way'")],
    )  # fmt: skip
    done = run(stagehand)
    assert [(a.selector, a.method) for a in stagehand.acted] == [(COMBOBOX, "selectOptionFromDropdown")]
    assert len(stagehand.instructions) == 1
    assert done == actions.Acted(True, "Trip type combobox")


def test_option_of_the_opened_list():
    opener = action(COMBOBOX, "Trip type combobox")
    assert is_option_of(action(ONE_WAY, "One way"), opener)  # an li
    assert is_option_of(action(f"{FORM}/div[2]/div[3]", "The 'One way' option"), opener)  # a div[role=option]
    assert is_option_of(action(f"{FORM}/div[2]/span[1]", "One way"), opener)  # inside the opener's container
    assert not is_option_of(action(COMBOBOX, "One way"), opener)  # the opener itself
    assert not is_option_of(action(ARRIVAL, "Arrival location field"), opener)
    assert not is_option_of(action("xpath=/html[1]/body[1]/footer[1]/a[1]", "Contact us"), opener)
    assert not is_option_of(action(f"{FORM}/div[2]", "Passengers button"), opener)  # a control, even nearby


def test_native_select():
    assert is_native_select("xpath=/html[1]/body[1]/form[1]/select[2]")
    assert is_native_select("form > select")
    assert not is_native_select(COMBOBOX)
    assert not is_native_select("xpath=/html[1]/body[1]/div[1]/selector-widget[1]")


# --- follow up only after a click that opened a list -----------------------------------------------------
SUBMIT = "xpath=/html[1]/body[1]/div[1]/form[1]/button[1]"
DAY = "xpath=/html[1]/body[1]/div[7]/div[4]/div[1]/div[3]/button[7]"


class FakePage:
    """evaluate() answers the ARIA hints for each xpath, from `hints` (None: not found)."""

    def __init__(self, hints):
        self.hints = hints
        self.scripts = []

    async def evaluate(self, expression):
        self.scripts.append(expression)
        return next((value for xpath, value in self.hints.items() if f'"{xpath}"' in expression), None)


def test_a_click_on_a_submit_button_or_a_day_is_not_followed_up():
    for selector, description in ((SUBMIT, "Submit search for flights."), (DAY, "The '14 Feb 2027' button")):
        stagehand = FakeStagehand([action(selector, description)], [action(ONE_WAY, "One way")])
        done = run(stagehand, follow_up=True)
        assert len(stagehand.instructions) == 1, description  # no second observe
        assert done.description == description


def test_a_click_on_a_combobox_is_still_followed_up():
    page = FakePage({COMBOBOX.removeprefix("xpath="): {"haspopup": "listbox", "role": "combobox", "expanded": "false"}})
    stagehand = FakeStagehand([action(COMBOBOX, "Search flights trip type")], [action(ONE_WAY, "One way")])
    done = run(stagehand, follow_up=True, page=page)
    assert len(stagehand.instructions) == 2  # the markup says it is a list opener, whatever the description says
    assert done.description == "One way"
    assert len(page.scripts) == 2  # read before and after the click


def test_the_markup_decides_when_it_has_hints():
    xpath = SUBMIT.removeprefix("xpath=")
    opened = FakePage({xpath: {"haspopup": None, "role": None, "expanded": "true"}})
    stagehand = FakeStagehand([action(SUBMIT, "Search options")], [action(ONE_WAY, "One way")])
    run(stagehand, follow_up=True, page=opened)
    assert len(stagehand.instructions) == 2  # aria-expanded turned true

    dialog = FakePage({xpath: {"haspopup": "dialog", "role": None, "expanded": None}})
    stagehand = FakeStagehand([action(SUBMIT, "Travel dates")], [action(ONE_WAY, "One way")])
    run(stagehand, follow_up=True, page=dialog)
    assert len(stagehand.instructions) == 1  # a dialog has no options to pick

    unreadable = FakePage({})
    stagehand = FakeStagehand([action(SUBMIT, "Travel dates")], [action(ONE_WAY, "One way")])
    run(stagehand, follow_up=True, page=unreadable)
    assert len(stagehand.instructions) == 2  # not found: the description decides, and it may be an opener


def test_opened_a_list():
    assert actions.opened_a_list("Trip type combobox", None, None)
    assert not actions.opened_a_list("Button to submit the flight search", None, None)
    assert not actions.opened_a_list("Continue", None, None)
    assert not actions.opened_a_list("Sun 14 February", None, None)
    assert not actions.opened_a_list("February 14th", None, None)
    assert actions.opened_a_list("Search flights", {"role": "combobox"}, None)
    assert not actions.opened_a_list("Fare type", {"expanded": "false"}, {"expanded": "false"})
