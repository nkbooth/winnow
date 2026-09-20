"""The records every adapter produces and every downstream module consumes.

Three rules from the adapter contract are encoded here rather than documented:

1. Unknown is not a value. Every inferable field is tri-state, and ``UNKNOWN``
   must never satisfy a gate in either direction.
2. Every inferred field carries its provenance. Ashby states remote status as a
   boolean; Greenhouse requires guessing from a location string. The scorer
   cannot weigh those differently unless the adapter says which it was.
3. Comp provenance is a correctness concern. A model-predicted figure may flag
   but may never satisfy a floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class RemoteStatus(StrEnum):
    """Work arrangement, as far as it can be established."""

    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"


class RemoteSource(StrEnum):
    """Where a remote status came from, strongest first."""

    STRUCTURED = "structured"
    LOCATION_STRING = "location_string"
    DESCRIPTION_TEXT = "description_text"
    ABSENT = "absent"


class EmploymentType(StrEnum):
    """Engagement shape. Contract work is additive and hour-capped."""

    FULL_TIME = "FULL_TIME"
    CONTRACT = "CONTRACT"
    PART_TIME = "PART_TIME"
    INTERN = "INTERN"
    UNKNOWN = "UNKNOWN"


class EmploymentTypeSource(StrEnum):
    """Where an employment type came from, strongest first."""

    STRUCTURED = "structured"
    CUSTOM_FIELD = "custom_field"
    TEXT = "text"
    ABSENT = "absent"


class CompSource(StrEnum):
    """Provenance of a compensation figure.

    ``WITHHELD`` means the employer configured comp and chose not to publish it,
    which is semantically different from ``ABSENT``. ``PREDICTED`` is a modelled
    number and may never satisfy a floor gate.
    """

    STATED = "stated"
    PARSED = "parsed"
    PREDICTED = "predicted"
    WITHHELD = "withheld"
    ABSENT = "absent"


class CompInterval(StrEnum):
    """Period a compensation figure is quoted over."""

    YEAR = "YEAR"
    HOUR = "HOUR"
    UNKNOWN = "UNKNOWN"


class PostedAtPrecision(StrEnum):
    """How precisely a posting date is known.

    ``RELATIVE`` covers Workday's prose dates. "Posted 30+ Days Ago" is not a
    date, but it is a ghost-job signal, so the imprecision is preserved rather
    than resolved to a guess.
    """

    EXACT = "exact"
    DAY = "day"
    RELATIVE = "relative"
    UNKNOWN = "unknown"


class LocationClass(StrEnum):
    """Coarse location bucket used as part of the dedupe cluster key.

    An enum rather than a string is what keeps Canadian and US postings of one
    role in separate clusters without parsing every city name.
    """

    US_REMOTE = "US_REMOTE"
    US_HYBRID = "US_HYBRID"
    US_ONSITE = "US_ONSITE"
    NON_US = "NON_US"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Board:
    """A company's board on one ATS vendor.

    The identifier is a mapping, not a string: Greenhouse needs ``{"slug": ...}``
    while Workday needs a (tenant, datacenter, site) triple.
    """

    vendor: str
    identifier: dict[str, str]
    company: str
    company_id: int | None = None
    board_id: int | None = None


@dataclass(frozen=True)
class Posting:
    """One job posting, normalised out of one vendor's payload.

    ``(source, source_id)`` is the natural key. Nothing downstream of an adapter
    ever sees a vendor-shaped payload, which is what makes adding a vendor a
    registry row rather than a refactor.
    """

    source: str
    source_id: str
    company: str
    title: str
    source_url: str
    discovered_via: str
    first_seen_at: datetime
    fingerprint: str = ""
    apply_url: str | None = None
    posted_at: datetime | None = None
    posted_at_precision: PostedAtPrecision = PostedAtPrecision.UNKNOWN
    posted_at_text: str | None = None
    updated_at: datetime | None = None
    remote: RemoteStatus = RemoteStatus.UNKNOWN
    remote_source: RemoteSource = RemoteSource.ABSENT
    locations: tuple[str, ...] = ()
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    employment_type_source: EmploymentTypeSource = EmploymentTypeSource.ABSENT
    comp_min: int | None = None
    comp_max: int | None = None
    comp_currency: str | None = None
    comp_interval: CompInterval = CompInterval.UNKNOWN
    comp_source: CompSource = CompSource.ABSENT
    description_text: str | None = None
    description_complete: bool = False
    department: str | None = None
    team: str | None = None
    raw: dict = field(default_factory=dict, repr=False)
