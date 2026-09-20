"""The adapter contract.

``fetch_list`` and ``normalize`` are separate so that ``normalize`` is a pure
function from payload to :class:`~winnow.models.Posting`. That is what makes
every vendor gotcha testable offline against recorded fixtures, and it is worth
the small awkwardness of splitting them.

``fetch_detail`` is lazy and optional. Workday's list payload carries no
description, so a naive implementation costs one request per posting — 149 for
Red Hat alone. Detail is fetched only for postings that survive the cheap gates,
which turns N+1 into a handful. Adapters whose list payload is already complete
return ``None``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

import httpx

from winnow import config
from winnow.models import Board, Posting


@runtime_checkable
class SourceAdapter(Protocol):
    """One vendor's board, as the pipeline sees it."""

    name: str

    def fetch_list(self, board: Board) -> Iterable[dict]:
        """Request a board's postings. Network."""
        ...

    def normalize(self, raw: dict, board: Board, *, now: datetime | None = None) -> Posting:
        """Map one payload to a Posting. Pure."""
        ...

    def fetch_detail(self, posting: Posting) -> dict | None:
        """Fetch a posting's detail payload, or None when the list was complete."""
        ...


def request_headers(accept: str = "application/json") -> dict[str, str]:
    """Headers sent on every request an adapter makes.

    Passed per request rather than set on the client, so identifying ourselves
    does not depend on who built the client. A function rather than a constant
    because the contact address comes from the operator's settings, and a
    module-level constant would freeze whatever was configured at import.
    """
    return {"User-Agent": config.settings().user_agent(), "Accept": accept}


def default_client(timeout: float = 30.0) -> httpx.Client:
    """Build an HTTP client for adapter use.

    These are public endpoints used as intended. Keeping them that way means a
    real User-Agent, a timeout, and no concurrency per board.
    """
    return httpx.Client(headers=request_headers(), timeout=timeout, follow_redirects=True)
