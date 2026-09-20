"""Curated lists of company boards, so a fresh install has somewhere to start.

A board is per company, and winnow only polls companies you name. That is a
deliberate design — automated board discovery was measured at roughly 50%
accuracy and returned confidently wrong answers — but it leaves a new install
with nothing in it and a quickstart that asks a stranger to go and find careers
URLs before the tool can demonstrate anything.

These files close that gap. They are organised by **sector**, one file per
category, because a board carries every role a company has and the rubric's
title gates decide which of them surface. Organising by role would mean the
same board in twenty files.

Anything cross-cutting — remote-first, open-source, nonprofit — is a **tag** on
the entry rather than a file of its own. A company belongs to one sector and
carries as many tags as fit, which is what keeps two contributors adding to
different files instead of colliding on one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SeedEntry:
    """One company in a seed list.

    Attributes:
        name: How the company should appear in the digest.
        board: The board URL, as a human would paste it. Stored rather than the
            parsed identifier because it is the form a reviewer can click, and
            because it is what changes when a company moves vendor.
        category: The file it came from.
        tags: Cross-cutting attributes, for filtering an import.
        verified: When somebody last confirmed the board answers. A slug that
            worked a year ago is not evidence that it works now.
        note: Anything a contributor thought the next reader should know.
    """

    name: str
    board: str
    category: str
    tags: tuple[str, ...] = ()
    verified: str = ""
    note: str = ""


def load(path: Path | str, *, tags: Sequence[str] = ()) -> list[SeedEntry]:
    """Read one seed file or a directory of them.

    Args:
        path: A ``.yaml`` file, or a directory containing them.
        tags: Keep only entries carrying every tag given. An empty sequence
            keeps everything; a tag nothing matches returns nothing, rather
            than falling back to the whole directory.

    Returns:
        The entries, in file order.

    Raises:
        ValueError: If a file does not match the schema. The message names the
            file, because a contributor reading CI output needs to know which
            of forty entries was wrong.
    """
    path = Path(path)
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]

    entries: list[SeedEntry] = []
    for file in files:
        entries.extend(_read(file))

    if not tags:
        return entries
    wanted = set(tags)
    return [entry for entry in entries if wanted <= set(entry.tags)]


def _read(file: Path) -> list[SeedEntry]:
    try:
        document = yaml.safe_load(file.read_text()) or {}
    except yaml.YAMLError as error:
        raise ValueError(f"{file}: not valid YAML: {error}") from error

    category = str(document.get("category") or file.stem)
    companies = document.get("companies")
    if not isinstance(companies, list):
        raise ValueError(f"{file}: 'companies' must be a list")

    entries = []
    for index, raw in enumerate(companies):
        if not isinstance(raw, dict):
            raise ValueError(f"{file}: entry {index} is not a mapping")
        name, board = raw.get("name"), raw.get("board")
        if not name or not board:
            raise ValueError(f"{file}: entry {index} needs both 'name' and 'board'")
        entries.append(
            SeedEntry(
                name=str(name),
                board=str(board),
                category=category,
                tags=tuple(str(tag) for tag in raw.get("tags") or ()),
                verified=str(raw.get("verified") or ""),
                note=str(raw.get("note") or ""),
            )
        )
    return entries


def resolve(name: str, directory: Path | str) -> Path:
    """Turn what somebody typed into a seed file or directory.

    A path is what a repository has; a category name is what a person types.
    Both work, because an installed copy has no ``seeds/`` beside the working
    directory and naming the file would then only work for someone standing in
    a clone.

    Args:
        name: A path, or a bare category name such as ``developer-tools``.
        directory: Where the shipped lists live.

    Returns:
        The file or directory to load.

    Raises:
        FileNotFoundError: If neither resolves. The message lists the
            categories that do exist, because a typo should say what was meant
            rather than only that nothing was found.
    """
    given = Path(name)
    if given.exists():
        return given

    directory = Path(directory)
    for candidate in (directory / name, directory / f"{name}.yaml"):
        if candidate.exists():
            return candidate

    available = ", ".join(sorted(p.stem for p in directory.glob("*.yaml"))) or "none installed"
    raise FileNotFoundError(f"no seed list {name!r} — available: {available}")


def categories(directory: Path | str = "seeds") -> dict[str, str]:
    """Return each category and its description, for listing them to a human."""
    found = {}
    for file in sorted(Path(directory).glob("*.yaml")):
        document = yaml.safe_load(file.read_text()) or {}
        found[file.stem] = str(document.get("description") or "").strip()
    return found
