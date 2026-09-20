"""Shared fixtures.

The board payloads in ``tests/fixtures/`` are a dated snapshot captured
2026-09-18. Boards drift, so tests read the snapshot rather than the live
endpoints; refreshing it means re-checking that the gotchas it exercises still
reproduce.
"""

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"

#: The rubric and drafting material the suite runs against live in ``examples/``
#: rather than here. They are the worked example the project ships, so a test
#: failure is the thing that stops the documentation going stale — and keeping
#: them out of the fixtures directory is also what keeps a real candidate's
#: resume and salary floor out of a public repository.
EXAMPLE_DIR = Path(__file__).parent.parent / "examples"


@pytest.fixture(scope="session")
def fixtures():
    """Return a loader for recorded board payloads."""

    def load(name: str):
        return json.loads((FIXTURE_DIR / name).read_text())

    return load


@pytest.fixture(scope="session")
def profile():
    """The real rubric, copied into the fixtures so tests run offline."""
    from winnow.profile import Profile

    return Profile.load(EXAMPLE_DIR / "profile.yaml")


@pytest.fixture
def make_posting():
    """Build a Posting with sensible defaults, overriding only what a test is about."""
    from datetime import UTC, datetime

    from winnow.models import Posting, RemoteSource, RemoteStatus

    def build(**overrides):
        defaults = {
            "source": "greenhouse",
            "source_id": "1",
            "company": "Example Co",
            "title": "Director of Business Systems",
            "source_url": "https://boards.greenhouse.io/example/jobs/1",
            "discovered_via": "greenhouse",
            "first_seen_at": datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
            "remote": RemoteStatus.REMOTE,
            "remote_source": RemoteSource.STRUCTURED,
            "locations": ("Remote (United States)",),
            "description_text": "Own and build out the BizOps function.",
            "description_complete": True,
        }
        return Posting(**{**defaults, **overrides})

    return build


@pytest.fixture
def conn(tmp_path):
    """An open, migrated store on a throwaway database."""
    from winnow import store

    connection = store.connect(tmp_path / "test.db")
    store.migrate(connection)
    yield connection
    connection.close()
