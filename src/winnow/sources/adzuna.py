"""Adzuna: an aggregator, used only for what an aggregator can honestly give.

Measured 2026-09-19 against the live API and against the postings the ATS path
had already confirmed:

* Every description is truncated — 225 of 225 across nine title queries.
* 58% of salaries are modelled rather than published.
* The only URL is an ``adzuna.com`` tracker, and the employer link behind its
  detail page answers 403 to anything that is not a browser.
* Of 18 confirmed postings it surfaced 8, about a day later than the board did,
  and the misses were whole employers: Canonical 0 of 6, Nextcloud 0 of 2.

Two contract rules therefore rule Adzuna out as a posting source: ``source_url``
must be the canonical URL at the employer and never an aggregator redirect, and
a truncated description sets ``description_complete`` false, which the scorer
refuses. Rather than bend either rule, this module produces :class:`Lead` —
an employer and a title, enough for a human to go and find the board. A lead
has no ``source_url`` and no ``description_text``, so there is no field here
that could drift into the store looking like a posting.

The 44% figure is the answer to a question the design wanted measured: whether
a paid aggregator is worth its subscription. Half the roles, a day late, with
entire employers missing, is what the free tier of one is worth.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

import httpx

from winnow.models import CompInterval, CompSource
from winnow.normalize import parse_iso
from winnow.secrets import resolve as resolve_secret
from winnow.sources.base import default_client, request_headers

#: The free tier's ceiling per request. Asking for more is silently truncated.
_PAGE_SIZE = 50

_SEARCH_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


@dataclass(frozen=True)
class AdzunaCredentials:
    """The application credentials, resolved at the moment they are used.

    Attributes:
        app_id: The literal id, for tests. Left ``None`` in production so the
            reference is resolved instead.
        app_key: The literal key, for tests.
        app_id_ref: ``op://`` reference to the id.
        app_key_ref: ``op://`` reference to the key.
    """

    app_id: str | None = None
    app_key: str | None = None
    app_id_ref: str = "env:WINNOW_ADZUNA_APP_ID"
    app_key_ref: str = "env:WINNOW_ADZUNA_APP_KEY"

    def resolve(self) -> tuple[str, str]:
        """Return the id and key, reading 1Password only when needed."""
        app_id = self.app_id or resolve_secret(self.app_id_ref)
        app_key = self.app_key or resolve_secret(self.app_key_ref)
        return app_id, app_key


@dataclass(frozen=True)
class Lead:
    """An employer worth looking up, and the role that suggested it.

    Deliberately not a :class:`~winnow.models.Posting`. There is no employer
    URL to record and no complete description to score, so a lead ends at the
    point where a human pastes a board URL and the ATS path takes over.

    Attributes:
        identifier: Adzuna's own id for the ad.
        company: The employer, as Adzuna names them.
        title: The role title.
        location: Adzuna's display name for the location.
        created: When the ad appeared on Adzuna, which is not when the employer
            posted it — measured about a day later.
        comp_min: Lower bound, where one is given.
        comp_max: Upper bound, where one is given.
        comp_interval: Always yearly where a figure exists.
        comp_source: ``PREDICTED`` for a modelled figure, which may flag but
            never satisfy a floor; ``STATED`` for a published one.
        category: Adzuna's own taxonomy label.
        contract_time: ``full_time``, ``part_time``, or absent.
        aggregator_url: The Adzuna page. Named for what it is so that it can
            never be mistaken for the employer's.
        description_excerpt: The first few hundred characters, always truncated.
    """

    identifier: str
    company: str
    title: str
    location: str | None
    created: datetime | None
    comp_min: int | None
    comp_max: int | None
    comp_interval: CompInterval
    comp_source: CompSource
    category: str | None
    contract_time: str | None
    aggregator_url: str
    description_excerpt: str | None


def normalize_lead(raw: dict) -> Lead:
    """Map one Adzuna result to a lead.

    Args:
        raw: One entry from the search response.

    Returns:
        The lead. Pure, so the whole mapping is testable against the recorded
        page in ``tests/fixtures/``.
    """
    comp_min, comp_max, comp_interval, comp_source = _compensation(raw)
    location = (raw.get("location") or {}).get("display_name")

    return Lead(
        identifier=str(raw["id"]),
        company=str((raw.get("company") or {}).get("display_name") or ""),
        title=str(raw["title"]),
        location=str(location) if location else None,
        created=parse_iso(raw.get("created")),
        comp_min=comp_min,
        comp_max=comp_max,
        comp_interval=comp_interval,
        comp_source=comp_source,
        category=(raw.get("category") or {}).get("label"),
        contract_time=raw.get("contract_time"),
        aggregator_url=str(raw.get("redirect_url") or ""),
        description_excerpt=raw.get("description"),
    )


class AdzunaClient:
    """Searches Adzuna for employers worth resolving."""

    name = "adzuna"

    def __init__(
        self,
        client: httpx.Client | None = None,
        credentials: AdzunaCredentials | None = None,
    ) -> None:
        self._client = client or default_client()
        self._credentials = credentials or AdzunaCredentials()

    def search(
        self, phrase: str, *, days: int = 30, country: str = "us", page: int = 1
    ) -> list[Lead]:
        """Search one title phrase.

        Args:
            phrase: Matched as a phrase. A bare keyword search matches the ad
                *body*, which fills the results with staffing agencies naming a
                product as a skill — "GitLab" alone returns 15,248 ads, almost
                none of them GitLab's.
            days: How far back to look.
            country: Adzuna's country partition.
            page: 1-based page number.

        Returns:
            The leads on that page. Rows naming no employer are dropped: a lead
            exists to name somewhere to go and look, and one that names nobody
            is not a weaker lead but no lead at all.

        Raises:
            httpx.HTTPStatusError: If the API does not answer with success.
            RuntimeError: If the credentials cannot be resolved.
        """
        app_id, app_key = self._credentials.resolve()
        response = self._client.get(
            _SEARCH_URL.format(country=country, page=page),
            params={
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": _PAGE_SIZE,
                "what_phrase": phrase,
                "max_days_old": days,
                "content-type": "application/json",
            },
            headers=request_headers(),
        )
        response.raise_for_status()
        return named_only(normalize_lead(row) for row in response.json().get("results", []))


def named_only(leads: Iterable[Lead]) -> list[Lead]:
    """Keep the leads that name an employer."""
    return [lead for lead in leads if lead.company]


def _compensation(raw: dict) -> tuple[int | None, int | None, CompInterval, CompSource]:
    """Read the salary, keeping a modelled figure marked as modelled.

    ``salary_is_predicted`` is Adzuna's own admission that it made the number
    up. It arrives as the string ``"1"``, and losing it would let a modelled
    $205k satisfy a $200k floor — the one failure the comp provenance rule in
    the adapter contract exists to prevent.
    """
    minimum, maximum = raw.get("salary_min"), raw.get("salary_max")
    if not isinstance(minimum, int | float) or not isinstance(maximum, int | float):
        return None, None, CompInterval.UNKNOWN, CompSource.ABSENT

    predicted = str(raw.get("salary_is_predicted") or "") == "1"
    return (
        int(minimum),
        int(maximum),
        CompInterval.YEAR,
        CompSource.PREDICTED if predicted else CompSource.STATED,
    )
