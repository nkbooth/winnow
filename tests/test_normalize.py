"""Text and title normalisation.

Title normalisation is biased toward splitting. A false merge hides a real job
permanently and silently; a false split costs one redundant digest line, so
seniority words survive normalisation while decorations do not.
"""

import pytest

from winnow.models import CompInterval, LocationClass
from winnow.normalize import fingerprint, html_to_text, normalize_title, parse_comp


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


# ---------------------------------------------------------------------------
# Compensation buried in prose that also mentions other money
# ---------------------------------------------------------------------------


def test_a_salary_after_other_money_is_still_found():
    """Found live on a Wrike posting stating $180,000—$205,000 plainly.

    The parser read the first money figure in the text and gave up when it was
    not credible as a wage. That description mentions a $14B market, a $40
    monthly allowance and a $500 stipend before it gets to the salary, so the
    whole range was invisible and the posting reached review as comp-absent.

    Benefits and market-size claims appear before the pay range constantly.
    Greenhouse and Workday bury comp in prose when they carry it at all, so
    this is the path by which those boards reach the comp gate at all.
    """
    text = (
        "Collaborative work management is a $14B category growing at 15% a year.\n"
        "Working from Home Allowance ($40 / Monthly)\n"
        "$500 Working from Home home office set-up Stipend\n"
        "Total compensation pay range\n"
        "$180,000—$205,000 USD\n"
    )

    parsed = parse_comp(text)

    assert parsed is not None
    assert (parsed.minimum, parsed.maximum) == (180000, 205000)
    assert parsed.currency == "USD"
    assert parsed.interval is CompInterval.YEAR


def test_the_range_nearest_compensation_language_wins():
    """Two credible ranges: the one the posting calls pay is the pay."""
    text = (
        "We raised $200,000,000 - $250,000,000 in our Series D.\n"
        "The base salary range for this role is $150,000 - $175,000 USD.\n"
    )

    parsed = parse_comp(text)

    assert (parsed.minimum, parsed.maximum) == (150000, 175000)


def test_a_lone_credible_salary_is_still_read():
    assert parse_comp("The salary for this role is $185,000.").minimum == 185000


def test_money_that_could_not_be_a_wage_is_not_one():
    """A stipend and a market size are not salaries, and neither is a range."""
    assert parse_comp("A $14B market. Allowance ($40 / Monthly). $500 stipend.") is None


def test_an_hourly_rate_after_other_money_is_still_hourly():
    text = "We are a $2B company.\nThe range for this contract is $200 - $250 per hour.\n"

    parsed = parse_comp(text)

    assert parsed.interval is CompInterval.HOUR
    assert (parsed.minimum, parsed.maximum) == (200, 250)


def test_a_currency_code_between_the_figures_does_not_break_the_range():
    """ "$113,000 USD and $158,000 USD" is a range, not a single figure.

    The separator pattern allowed only the connector between the two numbers,
    so a currency code after the first one broke the pair and the maximum
    collapsed onto the minimum. A floor gate reads comp_max, so that
    understates a role by the whole width of its band.
    """
    text = "The annual base salary for this role is between $113,000 USD and $158,000 USD."

    parsed = parse_comp(text)

    assert (parsed.minimum, parsed.maximum) == (113000, 158000)


def test_a_range_written_with_per_year_still_pairs():
    parsed = parse_comp("$150,000 per year to $180,000 per year")

    assert (parsed.minimum, parsed.maximum) == (150000, 180000)


def test_two_unrelated_figures_are_not_forced_into_a_range():
    """A separator has to be a separator, not any words at all."""
    parsed = parse_comp("The salary is $180,000. We also raised $400,000,000 last year.")

    assert parsed.maximum == 180000
