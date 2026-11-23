"""Nothing outside audit/safety.py may run an LLM action or click an element directly."""

import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).parent.parent / "audit"
SOURCES = sorted(p for p in PACKAGE.rglob("*.py") if p.name != "safety.py")
FORBIDDEN = {
    "an LLM action outside safe_act()": re.compile(r"\.act\s*\("),
    "a raw click (use safeClick)": re.compile(
        r"\.click\s*\(|\bclick\s*\(\s*\)|dispatchEvent\(new (Mouse|Pointer)Event"
    ),
    "a raw form submit": re.compile(r"\.(submit|requestSubmit)\s*\("),
    "a raw value assignment (use safeFill/safeSelect)": re.compile(r"\.value\s*=[^=]"),
}


def test_the_scan_sees_the_whole_package():
    names = {p.relative_to(PACKAGE).as_posix() for p in SOURCES}
    assert {"page.py", "runner.py", "workflows/coffee.py", "workflows/virgin.py", "workflows/ubereats/store.py",
            "workflows/ticketek/flow.py"} <= names  # fmt: skip


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(PACKAGE).as_posix())
def test_no_unguarded_actions(path):
    """Scans the raw text, so JavaScript inside string constants is covered as well as Python."""
    found = [
        f"{path.name}:{number}: {what}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for what, pattern in FORBIDDEN.items()
        if pattern.search(line)
    ]
    assert not found, "\n".join(found)


def test_the_patterns_catch_what_they_should():
    bad = ["await stagehand.act('x')", "el.click();", "btn?.click()", "form.submit()", "inp.value = target;",
           "await  stagehand . act (x)".replace(" . ", ".")]  # fmt: skip
    for line in bad:
        assert any(p.search(line) for p in FORBIDDEN.values()), line
    good = ["await safe_act(stagehand, page, 'x')", "safeClick(el)", "if (inp.value && x) {}", "o.value === '0'",
            "action.description", "await safety.act_on(stagehand, page, action)"]  # fmt: skip
    for line in good:
        assert not any(p.search(line) for p in FORBIDDEN.values()), line
