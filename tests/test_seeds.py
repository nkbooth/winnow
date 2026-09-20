"""The seed lists, and the rules a pull request adding to them must satisfy.

These files exist because a fresh install has no companies in it, and the
quickstart otherwise asks a stranger to go and find careers URLs before the
tool can show them anything. They are also the part of this repository most
likely to receive contributions, so the checks here are the review: a
contributor should learn their entry is malformed from CI, not from a
maintainer reading YAML by eye.

Everything here runs offline. Whether a board still answers is a question for
`scripts/verify-seeds.py`, which uses the network and is run deliberately.
"""

from pathlib import Path

import pytest
import yaml

from winnow import seeds
from winnow.sources.registry import match_board_url

SEED_DIR = Path("seeds")


def _files():
    return sorted(SEED_DIR.glob("*.yaml"))


def test_there_are_seed_lists_at_all():
    assert _files(), "seeds/ must contain at least one category"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.stem)
def test_every_file_matches_the_schema(path):
    document = yaml.safe_load(path.read_text())

    assert document["category"] == path.stem, "category must match the filename"
    assert document["description"].strip(), "a category nobody can define does not belong"
    assert isinstance(document["companies"], list)


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.stem)
def test_every_board_url_resolves_to_a_vendor_winnow_can_poll(path):
    """A seed entry winnow cannot parse is worse than no entry.

    It would fail at import with a message about the URL rather than about the
    list, and the contributor would have no idea which of forty lines was
    wrong.
    """
    for entry in seeds.load(path):
        matched = match_board_url(entry.board)
        assert matched is not None, f"{entry.name}: {entry.board} matches no vendor"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.stem)
def test_entries_are_sorted_and_unique(path):
    """Alphabetical order is what keeps two pull requests from conflicting."""
    names = [entry.name for entry in seeds.load(path)]

    assert names == sorted(names, key=str.casefold), "sort entries alphabetically"
    assert len(names) == len(set(names)), "duplicate company in one file"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.stem)
def test_every_entry_records_when_it_was_last_checked(path):
    """A slug that worked in 2026 is not evidence that it works now."""
    for entry in seeds.load(path):
        assert entry.verified, f"{entry.name}: set verified: YYYY-MM-DD"


def test_a_company_appears_in_only_one_category():
    """Sector is the file; everything cross-cutting is a tag.

    Two files listing one company would mean two pull requests touching it and
    an import that adds it twice.
    """
    seen: dict[str, str] = {}
    for path in _files():
        for entry in seeds.load(path):
            assert entry.name not in seen, (
                f"{entry.name} is in both {seen.get(entry.name)} and {path.stem} — "
                "pick a sector and use tags for the rest"
            )
            seen[entry.name] = path.stem


def test_loading_a_directory_returns_every_entry():
    assert len(seeds.load(SEED_DIR)) == sum(len(seeds.load(p)) for p in _files())


def test_entries_can_be_filtered_by_tag():
    tagged = seeds.load(SEED_DIR, tags=("open-source",))

    assert tagged, "no entry is tagged open-source"
    assert all("open-source" in entry.tags for entry in tagged)


def test_an_unknown_tag_returns_nothing_rather_than_everything(tmp_path):
    """Silently ignoring an unmatched filter would import the whole directory."""
    assert seeds.load(SEED_DIR, tags=("not-a-real-tag",)) == []


def test_a_malformed_file_names_itself(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("category: broken\ndescription: x\ncompanies:\n  - name: No Board\n")

    with pytest.raises(ValueError, match="broken.yaml"):
        seeds.load(bad)
