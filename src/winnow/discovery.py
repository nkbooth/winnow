"""Aggregator leads, reduced to a question worth asking a human.

A sweep of Adzuna returns a few hundred rows, most of them staffing agencies
reselling the same role. What is actually useful in there is the short list of
employers that are hiring for this profile's titles and are *not* already on the
board list — because each one is a URL away from entering the real pipeline.

Nothing here writes to the store. A lead is a suggestion to go and look, and it
becomes real only when a human pastes a board URL into ``company add``.
"""

from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict
from collections.abc import Iterable
from typing import Protocol

from winnow.models import CompInterval, CompSource
from winnow.profile import Profile
from winnow.sources.adzuna import Lead
from winnow.titles import TitleTier, classify_title

#: Name fragments that mark a reseller rather than an employer. The profile
#: declines staffing agencies outright - worst measured conversion of any
#: channel - and on a keyword aggregator they are most of the result set, so
#: leaving them in buries the few real employers a sweep finds.
_RESELLER_MARKERS = (
    "staffing",
    "recruit",
    "talent",
    "consulting group",
    "consultants",
    "solutions inc",
    "software services",
    "technologies inc",
    "robert half",
    "insight global",
    "apex systems",
    "teksystems",
    "randstad",
    "adecco",
    "kforce",
    "motion recruitment",
    "cybercoders",
    "jobot",
    "aerotek",
    "collabera",
    "compunnel",
    "diverse lynx",
)

_PUNCTUATION = re.compile(r"[^a-z0-9]+")
_SUFFIXES = ("inc", "llc", "ltd", "limited", "corp", "corporation", "gmbh", "co")


class LeadSource(Protocol):
    """Anything that can answer a title phrase with leads."""

    def search(self, phrase: str, *, days: int = 30, **kwargs: object) -> list[Lead]:
        """Search one phrase."""
        ...


def sweep(client: LeadSource, profile: Profile, *, days: int = 30) -> list[Lead]:
    """Search every primary title in the rubric.

    Args:
        client: The lead source, normally an :class:`AdzunaClient`.
        profile: The rubric, which supplies the title vocabulary.
        days: How far back to look.

    Returns:
        The leads, de-duplicated by advert. One ad answers several title
        phrases, and a human should see it once.
    """
    found: OrderedDict[str, Lead] = OrderedDict()
    for title in profile.primary_titles:
        for lead in client.search(title, days=days):
            found.setdefault(lead.identifier, lead)
    return list(found.values())


def on_target(leads: Iterable[Lead], profile: Profile) -> list[Lead]:
    """Keep the leads whose title is one of the rubric's, and that comp allows.

    A phrase search matches the advert body, not only its title, so a sweep for
    *director of sales operations* comes back full of local account executives
    whose ads happen to contain the words. Left unfiltered the output was 183
    companies, which nobody reads. The title gate here is the same one the ATS
    path applies, so a lead is judged by the standard a posting would be.

    Compensation only ever removes a lead on *stated* evidence. A modelled
    figure may not decide a gate in either direction, which is why Adzuna's
    ubiquitous predicted salaries leave a lead alone.

    Args:
        leads: Candidate leads.
        profile: The rubric.

    Returns:
        The leads worth a human's attention.
    """
    return [
        lead
        for lead in leads
        if classify_title(lead.title, profile) is not TitleTier.OFF_TARGET
        and not _stated_below_floor(lead, profile)
    ]


def one_per_role(leads: Iterable[Lead]) -> list[Lead]:
    """Collapse the same role advertised in several places.

    Aggregators split one opening across every town it might be commutable
    from - one employer listed the same job in seven. The lead is the employer
    and the role; the town is not what makes it worth looking up.
    """
    seen: OrderedDict[tuple[str, str], Lead] = OrderedDict()
    for lead in leads:
        seen.setdefault((lead.company.lower(), lead.title.lower()), lead)
    return list(seen.values())


def untracked(conn: sqlite3.Connection, leads: Iterable[Lead]) -> list[Lead]:
    """Drop leads for companies already known, and for resellers.

    Args:
        conn: An open store connection.
        leads: Candidate leads.

    Returns:
        The leads worth showing.
    """
    known = _known_names(conn)
    return [
        lead
        for lead in leads
        if _normalise(lead.company) not in known and not _is_reseller(lead.company)
    ]


def by_company(leads: Iterable[Lead]) -> OrderedDict[str, list[Lead]]:
    """Group leads by employer, the employer with the most roles first.

    Args:
        leads: Leads to group.

    Returns:
        Employer name to their leads. Several open roles is a stronger signal
        than one, so those employers are worth looking up first.
    """
    grouped: dict[str, list[Lead]] = {}
    for lead in leads:
        grouped.setdefault(lead.company, []).append(lead)
    ordered = sorted(grouped.items(), key=lambda item: -len(item[1]))
    return OrderedDict(ordered)


def _stated_below_floor(lead: Lead, profile: Profile) -> bool:
    """Whether a published figure puts this role under the applicable floor."""
    if lead.comp_source is not CompSource.STATED or lead.comp_max is None:
        return False
    if lead.comp_interval is CompInterval.HOUR:
        return lead.comp_max < profile.contract_hourly_floor
    if lead.comp_interval is CompInterval.YEAR:
        # The lowest floor: a lead names a company to look up, and the tier that
        # company falls into is not known until someone does.
        return lead.comp_max < min(profile.comp_floors.values())
    return False


def _known_names(conn: sqlite3.Connection) -> set[str]:
    """Every company name and alias already in the store, normalised."""
    names = {row[0] for row in conn.execute("select name from companies")}
    names |= {row[0] for row in conn.execute("select alias from company_aliases")}
    return {_normalise(name) for name in names if name}


def _normalise(name: str) -> str:
    """Reduce a company name to something two spellings of it agree on."""
    collapsed = _PUNCTUATION.sub("", name.lower())
    for suffix in _SUFFIXES:
        if collapsed.endswith(suffix) and len(collapsed) > len(suffix):
            collapsed = collapsed[: -len(suffix)]
            break
    return collapsed


def _is_reseller(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _RESELLER_MARKERS)
