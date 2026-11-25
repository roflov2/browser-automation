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
