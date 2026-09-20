"""Text and title normalisation.

Title normalisation is biased toward splitting. A false merge hides a real job
permanently and silently; a false split costs one redundant digest line, so
seniority words survive normalisation while decorations do not.
"""

import pytest

from winnow.models import LocationClass
from winnow.normalize import fingerprint, html_to_text, normalize_title


def test_double_escaped_html_is_decoded_then_stripped():
    """Greenhouse escapes its HTML twice; unescaping once leaves visible markup."""
    raw = (
        "&lt;div class=&quot;content-intro&quot;&gt;"
        "&lt;p&gt;Hello&amp;nbsp;world&lt;/p&gt;&lt;/div&gt;"
    )
    assert html_to_text(raw) == "Hello world"


def test_plain_html_is_stripped():
    assert html_to_text("<p>Own the <strong>BizOps</strong> function</p>") == (
        "Own the BizOps function"
    )


def test_block_elements_become_line_breaks():
    text = html_to_text("<div>First</div><div>Second</div>")
    assert text.splitlines() == ["First", "Second"]


def test_none_and_empty_are_handled():
    assert html_to_text(None) is None
    assert html_to_text("") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Director, Business Systems", "director business systems"),
        ("Sr. Director of Business Systems", "senior director business systems"),
        ("Sales & Marketing Operations", "sales and marketing operations"),
        ("Analytics Engineer II", "analytics engineer 2"),
        ("Business Systems Analyst (Remote)", "business systems analyst"),
        ("Business Systems Analyst - US", "business systems analyst"),
        ("Business Systems Analyst #4471", "business systems analyst"),
        ("  Director   of   RevOps  ", "director revops"),
    ],
)
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_connective_words_do_not_split_a_cluster():
    """ "Director, Business Systems" and "Director of Business Systems" are one role."""
    assert normalize_title("Director, Business Systems") == normalize_title(
        "Director of Business Systems"
    )


def test_seniority_is_preserved():
    """These are two real roles, not one role spelled two ways."""
    assert normalize_title("Director of Business Systems") != normalize_title(
        "Senior Director of Business Systems"
    )


def test_fingerprint_is_stable_across_spellings():
    left = fingerprint("Tailscale", "Sr. Director, Business Systems", LocationClass.US_REMOTE)
    right = fingerprint("tailscale", "Senior Director of Business Systems", LocationClass.US_REMOTE)
    assert left == right


def test_fingerprint_separates_location_classes():
    us = fingerprint("Tailscale", "Analytics Engineer", LocationClass.US_REMOTE)
    non_us = fingerprint("Tailscale", "Analytics Engineer", LocationClass.NON_US)
    assert us != non_us


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The likely salary range for this position is $145,000-$155,000.",
            (145000, 155000, "USD", "YEAR"),
        ),
        ("$210,000 — $245,000 USD", (210000, 245000, "USD", "YEAR")),
        ("Compensation: $180k – $220k", (180000, 220000, "USD", "YEAR")),
        ("Rate is $200 - $300 per hour", (200, 300, "USD", "HOUR")),
        ("$125/hr", (125, 125, "USD", "HOUR")),
        ("Base salary: $250,000", (250000, 250000, "USD", "YEAR")),
    ],
)
def test_parse_comp(text, expected):
    from winnow.normalize import parse_comp

    parsed = parse_comp(text)
    assert parsed is not None
    assert (parsed.minimum, parsed.maximum, parsed.currency, parsed.interval) == expected


@pytest.mark.parametrize(
    "text",
    [
        "We have 4,000 employees in 30 countries",
        "no numbers at all",
        "",
        None,
        "a $150 stipend for your home office",
    ],
)
def test_parse_comp_declines_to_guess(text):
    """A number that cannot be a salary is not one. Silence beats a wrong floor."""
    from winnow.normalize import parse_comp

    assert parse_comp(text) is None
