#!/usr/bin/env python3
"""Check that every board in the seed lists still answers.

Uses the network, so it is not part of the test suite: a CI run that fails
because a vendor rate-limited it teaches people to ignore CI. Run it before a
release, or when adding entries.

    ./scripts/verify-seeds.py                # check everything in seeds/
    ./scripts/verify-seeds.py --candidates candidates.tsv   # check proposed ones

A candidates file is `name<TAB>board-url` per line, which is how a batch of
guesses becomes a batch of facts before any of them reach a seed file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from winnow import seeds  # noqa: E402
from winnow.resolver import HttpProbe, ProbeStatus  # noqa: E402
from winnow.sources.registry import match_board_url  # noqa: E402


def check(name: str, url: str, probe: HttpProbe) -> tuple[str, str]:
    """Return a status word and a detail for one board."""
    matched = match_board_url(url)
    if matched is None:
        return "UNPARSED", "no vendor pattern matches this URL"

    vendor, identifier = matched
    result = probe.probe(vendor.name, identifier)

    if result.status is ProbeStatus.OK:
        # Greenhouse and Workable state their own name, which is the only guard
        # against a slug that answers for the wrong company.
        stated = result.board_name or ""
        mismatch = ""
        if stated and name.lower().replace(" ", "") not in stated.lower().replace(" ", ""):
            mismatch = f" — board says {stated!r}"
        return "OK", f"{result.job_count} jobs{mismatch}"
    if result.status is ProbeStatus.EMPTY:
        return "EMPTY", "answers, no open roles"
    return result.status.name, result.detail or ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, help="TSV of name<TAB>url to check")
    parser.add_argument("--seeds", type=Path, default=Path("seeds"))
    args = parser.parse_args()

    if args.candidates:
        pairs = [
            tuple(line.split("\t", 1))
            for line in args.candidates.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        pairs = [(entry.name, entry.board) for entry in seeds.load(args.seeds)]

    probe = HttpProbe(timeout=20.0)
    failures = 0
    for name, url in pairs:
        status, detail = check(name.strip(), url.strip(), probe)
        if status not in ("OK", "EMPTY"):
            failures += 1
        print(f"{status:9} {name:28} {detail}")

    print(f"\n{len(pairs)} checked, {failures} not usable")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
