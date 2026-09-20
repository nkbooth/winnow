#!/usr/bin/env python3
"""Find the board for a company, by trying the slugs it is likely to use.

Adding to the seed lists means answering "where is Acme's board?" a few hundred
times. Doing it by hand is slow and guessing is worse: of the first fifty slugs
guessed while these files were started, fourteen were wrong — they 404, or they
belong to somebody else entirely.

This turns a list of company names into a list of facts. It generates the slugs
a company plausibly uses, asks each vendor, and reports only what answered.

    ./scripts/find-boards.py names.tsv > found.tsv

`names.tsv` is `sector<TAB>Company Name` per line.

**Verification is not uniform**, and the output says which kind each answer got:

`name`
    The board stated its own employer name and it matched. Greenhouse,
    Workable and SmartRecruiters do this. It is the only check that catches a
    slug belonging to a different company.
`slug`
    The board answered, but the vendor publishes no name to check against —
    Lever always, Ashby usually. The slug is an exact normalisation of the
    company name, which makes a collision unlikely rather than impossible.

Treat `slug` results as needing a human glance before they go in a file.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from winnow import __version__  # noqa: E402

USER_AGENT = f"winnow/{__version__} (seed-list maintenance; +https://github.com/nkbooth/winnow)"

#: Kept small on purpose. These are public endpoints being used as intended,
#: and part of that is not hammering them because a script made it easy to.
WORKERS = 6

#: Suffixes companies drop from their slug more often than they keep.
_SUFFIXES = (
    " inc",
    " inc.",
    " llc",
    " ltd",
    " limited",
    " corp",
    " corporation",
    " co",
    " company",
    " labs",
    " technologies",
    " technology",
    " software",
    " group",
    " holdings",
    " ai",
    " io",
    " hq",
)


@dataclass(frozen=True)
class Found:
    sector: str
    name: str
    url: str
    jobs: int
    verification: str


def slugs(name: str) -> Iterator[str]:
    """Yield the slugs a company plausibly registered, best guess first."""
    lowered = name.lower().strip()
    stripped = lowered
    for suffix in _SUFFIXES:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)].strip()
            break

    seen = set()
    for base in (lowered, stripped):
        for candidate in (
            re.sub(r"[^a-z0-9]", "", base),
            re.sub(r"[^a-z0-9]+", "-", base).strip("-"),
        ):
            if candidate and candidate not in seen:
                seen.add(candidate)
                yield candidate


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _matches(company: str, stated: str) -> bool:
    """Whether a board's stated name plausibly belongs to this company."""
    left, right = _squash(company), _squash(stated)
    return bool(left and right) and (left in right or right in left)


def _greenhouse(client: httpx.Client, company: str, slug: str) -> Found | None:
    board = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    if board.status_code != 200:
        return None
    stated = str((board.json() or {}).get("name") or "")
    if stated and not _matches(company, stated):
        return None
    jobs = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    count = len((jobs.json() or {}).get("jobs") or []) if jobs.status_code == 200 else 0
    if not count:
        return None
    return Found("", company, f"https://boards.greenhouse.io/{slug}", count, "name")


def _ashby(client: httpx.Client, company: str, slug: str) -> Found | None:
    response = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if response.status_code != 200:
        return None
    count = len((response.json() or {}).get("jobs") or [])
    if not count:
        return None

    verification = "slug"
    page = client.get(f"https://jobs.ashbyhq.com/{slug}")
    title = re.search(r"<title>([^<]*)</title>", page.text or "", re.I)
    if title:
        stated = title.group(1).replace("Jobs", "").strip(" |-–—")
        if stated:
            if not _matches(company, stated):
                return None
            verification = "name"
    return Found("", company, f"https://jobs.ashbyhq.com/{slug}", count, verification)


def _lever(client: httpx.Client, company: str, slug: str) -> Found | None:
    response = client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if response.status_code != 200:
        return None
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        return None
    return Found("", company, f"https://jobs.lever.co/{slug}", len(payload), "slug")


def _workable(client: httpx.Client, company: str, slug: str) -> Found | None:
    response = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    if response.status_code != 200:
        return None
    payload = response.json() or {}
    stated = str(payload.get("name") or "")
    if stated and not _matches(company, stated):
        return None
    count = len(payload.get("jobs") or [])
    if not count:
        return None
    return Found("", company, f"https://apply.workable.com/{slug}", count, "name")


def _smartrecruiters(client: httpx.Client, company: str, slug: str) -> Found | None:
    response = client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1")
    if response.status_code != 200:
        return None
    postings = (response.json() or {}).get("content") or []
    if not postings:
        return None
    stated = str((postings[0].get("company") or {}).get("name") or "")
    if stated and not _matches(company, stated):
        return None
    total = int((response.json() or {}).get("totalFound") or len(postings))
    return Found("", company, f"https://jobs.smartrecruiters.com/{slug}", total, "name")


VENDORS = (_greenhouse, _ashby, _lever, _workable, _smartrecruiters)


def find(sector: str, company: str) -> Found | None:
    """Try every plausible slug against every vendor, first answer wins."""
    with httpx.Client(
        timeout=12.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for slug in slugs(company):
            for vendor in VENDORS:
                try:
                    found = vendor(client, company, slug)
                except httpx.HTTPError, ValueError:
                    continue
                if found is not None:
                    return Found(sector, found.name, found.url, found.jobs, found.verification)
                time.sleep(0.05)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", type=Path, help="TSV of sector<TAB>company")
    args = parser.parse_args()

    rows = [
        tuple(line.split("\t", 1))
        for line in args.names.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]

    found = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(lambda row: find(row[0].strip(), row[1].strip()), rows):
            if result is None:
                continue
            found += 1
            print(
                f"{result.sector}\t{result.name}\t{result.url}\t{result.jobs}\t{result.verification}"
            )

    print(f"# {found} of {len(rows)} found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
