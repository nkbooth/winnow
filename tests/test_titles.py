"""Title tiering.

This is the cheap gate that keeps the LLM bill proportionate: Tailscale's board
alone is 29 clusters, and most of them are engineering roles that no amount of
judgment would make relevant. Tiering is deterministic and reads its targets
from the rubric.
"""

import pytest

from winnow.profile import Profile
from winnow.titles import TitleTier, classify_title


@pytest.fixture(scope="module")
def profile():
    return Profile.load("examples/profile.yaml")


@pytest.mark.parametrize(
    "title",
    [
        "Director of Business Systems",
        "Director, Business Systems",
        "Director of Business Operations",
        "Head of Business Operations",
        "Senior Manager, Business Systems",
        "Director of Revenue Operations",
        "Director, RevOps",
        "ERP Program Manager",
        "Principal Business Systems Analyst",
    ],
)
def test_primary_titles(title, profile):
    assert classify_title(title, profile) is TitleTier.PRIMARY


@pytest.mark.parametrize(
    "title",
    [
        "Business Systems Analyst",
        "Business Process Analyst",
        "ERP Analyst",
        "Systems Operations Manager",
        "Solutions Analyst",
    ],
)
def test_discovery_titles(title, profile):
    assert classify_title(title, profile) is TitleTier.DISCOVERY


@pytest.mark.parametrize(
    "title",
    [
        "Analytics Engineer, Data",
        "Customer Reliability Engineer",
        "Senior Backend Engineer",
        "Junior Consultant",
        "Strategic Account Manager - Telco",
        "Deal Management Analyst Intern",
        "Staff Product Designer",
    ],
)
def test_off_target_titles(title, profile):
    assert classify_title(title, profile) is TitleTier.OFF_TARGET


def test_decoration_does_not_change_the_tier(profile):
    assert classify_title("Director of Business Systems (Remote) #4471", profile) is (
        TitleTier.PRIMARY
    )


def test_most_of_a_real_board_is_off_target(fixtures, profile):
    """The gate has to actually bite, or the LLM runs on everything."""
    titles = {job["title"] for job in fixtures("greenhouse_tailscale_jobs.json")["jobs"]}
    off_target = [t for t in titles if classify_title(t, profile) is TitleTier.OFF_TARGET]
    assert len(off_target) >= len(titles) - 3


@pytest.mark.parametrize(
    "title",
    [
        # Every one of these reached the scorer on the first real run, because
        # the "erp" anchor was matched as a substring and "enterprise" contains
        # it. They cost real money to be told they were sales and engineering.
        "Senior Enterprise Account Executive, Acquisition | West | Remote",
        "Enterprise Account Executive, Growth | Southeast | Remote",
        "Senior Sales Director – Enterprise, Benelux",
        "Staff Backend Engineer - Grafana Enterprise | US | Remote",
        "Staff Product Engineer, Enterprise AI",
        "Sr. Director, Enterprise Data",
        "Director, Enterprise Architecture, Automation and Integration",
    ],
)
def test_enterprise_is_not_erp(title, profile):
    assert classify_title(title, profile) is TitleTier.OFF_TARGET


@pytest.mark.parametrize(
    "title",
    [
        "ERP Program Manager",
        "Director of ERP",
        "ERP Analyst",
        "Senior ERP Systems Manager",
    ],
)
def test_real_erp_titles_still_match(title, profile):
    assert classify_title(title, profile) is not TitleTier.OFF_TARGET


def test_anchors_match_whole_words_only(profile):
    """The general form of the bug: substrings inside longer words."""
    for title in ("Revopsyche Researcher", "Bizopsy Technician"):
        assert classify_title(title, profile) is TitleTier.OFF_TARGET


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Modern SaaS names for the work the rubric already targets. All seven
        # were OFF_TARGET on a live scan of twelve boards, 2026-09-19.
        ("Director, GTM Engineering", TitleTier.PRIMARY),
        ("Head of Revenue Systems", TitleTier.PRIMARY),
        ("Director of Enterprise Systems", TitleTier.PRIMARY),
        ("Senior Enterprise Systems Engineer, AI Productivity", TitleTier.DISCOVERY),
        ("Senior Commercial Deal Desk Analyst", TitleTier.DISCOVERY),
        ("Revenue Systems Manager", TitleTier.DISCOVERY),
        ("GTM Systems Analyst", TitleTier.DISCOVERY),
    ],
)
def test_the_go_to_market_systems_vocabulary_is_in_scope(title, expected, profile):
    assert classify_title(title, profile) is expected


@pytest.mark.parametrize(
    "title",
    [
        # Widening the vocabulary must not sweep in the sales org, which is
        # what most of those boards' "enterprise" titles actually are.
        "Account Executive Enterprise DACH",
        "Enterprise Account Executive Germany/EMEA",
        "Business Development Representative",
        "Director, Product Marketing",
        "Director, Strategic Finance",
        "Senior Staff Product Marketing Manager - Enterprise Password Manager",
    ],
)
def test_widening_does_not_admit_the_sales_org(title, profile):
    assert classify_title(title, profile) is TitleTier.OFF_TARGET
