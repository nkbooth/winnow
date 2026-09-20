"""Source adapters, one module per vendor.

``fetch`` and ``normalize`` are separated in every adapter: normalisation is a
pure function from payload to :class:`~winnow.models.Posting`, so the whole
layer is testable offline against the dated fixtures in ``tests/fixtures/``.

``ADAPTERS`` is the other half of the registry in :mod:`winnow.sources.registry`:
that module holds each vendor's addressing as data, this one names the code that
maps its payloads. A vendor may appear in the registry without an adapter — a
board can be recorded before it can be polled — but never the reverse.
"""

from __future__ import annotations

import httpx

from winnow.sources.ashby import AshbyAdapter
from winnow.sources.base import SourceAdapter
from winnow.sources.careers_page import CareersPageAdapter
from winnow.sources.greenhouse import GreenhouseAdapter
from winnow.sources.lever import LeverAdapter
from winnow.sources.rippling import RipplingAdapter
from winnow.sources.smartrecruiters import SmartRecruitersAdapter
from winnow.sources.workable import WorkableAdapter
from winnow.sources.workday import WorkdayAdapter

ADAPTERS: dict[str, type] = {
    GreenhouseAdapter.name: GreenhouseAdapter,
    LeverAdapter.name: LeverAdapter,
    AshbyAdapter.name: AshbyAdapter,
    WorkdayAdapter.name: WorkdayAdapter,
    SmartRecruitersAdapter.name: SmartRecruitersAdapter,
    RipplingAdapter.name: RipplingAdapter,
    WorkableAdapter.name: WorkableAdapter,
    CareersPageAdapter.name: CareersPageAdapter,
}


def adapter_for(vendor: str, client: httpx.Client | None = None) -> SourceAdapter:
    """Build the adapter for one vendor.

    Args:
        vendor: Registry vendor name.
        client: HTTP client to use, chiefly for tests.

    Returns:
        A ready adapter.

    Raises:
        KeyError: If no adapter implements that vendor. Boards for such vendors
            can be recorded but not polled, and the caller decides what to do
            about it rather than being handed a silent no-op.
    """
    return ADAPTERS[vendor](client=client)
