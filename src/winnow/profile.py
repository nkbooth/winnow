"""The scoring rubric, loaded from ``profile.yaml``.

The file is the output of the intake interview and the single source of truth for
every floor, weight and threshold in the system. Nothing downstream hardcodes one
— when a number needs to change it changes there, and the review TUI writes back
into the same file rather than keeping a second copy of the truth.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import yaml

#: Tunables the review TUI is allowed to write back. Throughput was left unknown
#: at intake by design, so these are the numbers that get answered by behaviour.
DIGEST_TUNABLES = ("score_threshold", "max_items", "always_show_top", "cadence")

_FALLBACK_TIER = "mid_market"

#: Trailing words that do not make a company a different company.
_CORPORATE_SUFFIXES = frozenset(
    {
        "labs",
        "inc",
        "inc.",
        "corp",
        "corp.",
        "co",
        "co.",
        "ltd",
        "llc",
        "gmbh",
        "ag",
        "technologies",
        "software",
        "computer",
        "foundation",
        "group",
    }
)


@dataclass(frozen=True)
class Profile:
    """A parsed rubric.

    Attributes:
        version: The ``version`` field in the file.
        content_hash: Short digest of the file's bytes, so an edit that forgets
            to bump ``version`` still changes :attr:`rubric_version`.
        raw: The whole parsed document, for the parts no accessor covers yet.
    """

    version: int
    content_hash: str
    score_threshold: int
    max_items: int
    always_show_top: int
    cadence: str
    weekly_summary: str
    retention_days: int
    silence_window_days: int
    weights: Mapping[str, int]
    hard_gates: frozenset[str]
    comp_floors: Mapping[str, int]
    contract_hourly_floor: int
    contract_max_hours: int
    max_salary_range_ratio: float
    mission_weight: int
    government_weight: int
    primary_titles: tuple[str, ...]
    discovery_titles: tuple[str, ...]
    specialist_niches: tuple[str, ...]
    raw: Mapping[str, object]
    _excluded: frozenset[str]
    _mission: frozenset[str]

    @classmethod
    def load(cls, path: Path | str) -> Profile:
        """Read and parse a rubric.

        Args:
            path: Location of ``profile.yaml``.

        Returns:
            The parsed rubric.

        Raises:
            FileNotFoundError: If the file does not exist.
            yaml.YAMLError: If it does not parse.
        """
        text = Path(path).read_text()
        document = yaml.safe_load(text)

        digest = document.get("digest") or {}
        compensation = document.get("compensation") or {}
        w2 = compensation.get("w2") or {}
        contract = compensation.get("contract") or {}
        engagement = (document.get("engagement") or {}).get("contract") or {}
        industry = document.get("industry") or {}
        strong_pull = industry.get("strong_pull") or {}
        deprioritize = industry.get("deprioritize") or {}
        crypto = (industry.get("avoid") or {}).get("crypto") or {}
        red_flags = document.get("red_flags") or {}
        titles = document.get("titles") or {}
        stealth = document.get("stealth") or {}
        process = document.get("process") or {}

        floors = {
            "mid_market": int((w2.get("mid_market") or {}).get("floor", 0)),
            "large_corporate": int((w2.get("large_corporate") or {}).get("floor", 0)),
            "crypto": int(crypto.get("unless_comp_above", 0)),
        }

        return cls(
            version=int(document.get("version", 0)),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
            score_threshold=int(digest.get("score_threshold", 70)),
            max_items=int(digest.get("max_items", 8)),
            always_show_top=int(digest.get("always_show_top", 3)),
            cadence=str(digest.get("cadence", "on_hits")),
            weekly_summary=str(digest.get("weekly_summary", "monday")),
            retention_days=int(digest.get("retention_days", 180)),
            # Past three weeks a non-reply is the answer rather than a delay.
            silence_window_days=int(process.get("silence_window_days", 21)),
            weights=MappingProxyType(dict(document.get("weights") or {})),
            hard_gates=frozenset(_gate_names(document.get("hard_gates") or [])),
            comp_floors=MappingProxyType(floors),
            contract_hourly_floor=int(contract.get("hourly_floor", 0)),
            contract_max_hours=int(engagement.get("max_hours_per_week", 0)),
            max_salary_range_ratio=float(
                (red_flags.get("salary_range_spans_tiers") or {}).get("max_ratio", 0) or 0
            ),
            mission_weight=int(strong_pull.get("weight", 0)),
            government_weight=int(
                (deprioritize.get("government_public_sector") or {}).get("weight", 0)
            ),
            primary_titles=tuple(titles.get("primary") or ()),
            discovery_titles=tuple(titles.get("discovery_only") or ()),
            specialist_niches=tuple(titles.get("specialist_niches") or ()),
            raw=MappingProxyType(document),
            _excluded=frozenset(
                name.strip().lower() for name in (stealth.get("exclude_employers") or [])
            ),
            _mission=frozenset(
                name.strip().lower() for name in (strong_pull.get("examples") or [])
            ),
        )

    @property
    def rubric_version(self) -> str:
        """Identify this rubric in a stored score.

        Scores are not comparable across rubric versions, so any calibration
        report has to be able to say which one it covers. The content hash is
        part of it because a hand edit that forgets to bump ``version`` still
        changes the scoring.
        """
        return f"v{self.version}.{self.content_hash}"

    @property
    def candidate_name(self) -> str:
        """Whose search this is. Used to sign letters and to address the drafter."""
        candidate = self.raw.get("candidate") or {}
        return str(candidate.get("name") or "")

    @property
    def mailbox(self) -> str:
        """The address outbound job correspondence is sent from."""
        candidate = self.raw.get("candidate") or {}
        return str(candidate.get("mailbox") or "")

    def floor_for(self, tier: str) -> int:
        """Return the applicable annual compensation floor.

        Args:
            tier: ``mid_market``, ``large_corporate`` or ``crypto``.

        Returns:
            The floor in whole dollars. An unrecognised tier falls back to the
            lowest floor: assuming the higher one would reject roles that in fact
            clear the bar that applies to them.
        """
        return self.comp_floors.get(tier, self.comp_floors[_FALLBACK_TIER])

    def is_excluded(self, company: str) -> bool:
        """Say whether a company is on the stealth exclusion list."""
        return company.strip().lower() in self._excluded

    def is_mission_company(self, company: str) -> bool:
        """Say whether a company is one of the named mission-fit employers.

        Matching allows a corporate suffix, because the rubric lists "Grafana"
        while the board says "Grafana Labs". It does not allow any other extra
        word: "Element" must not match "Element Solutions", which is a different
        company and the exact collision that made board resolution manual.
        """
        name = company.strip().lower()
        if name in self._mission:
            return True
        return any(
            name.startswith(f"{listed} ") and name[len(listed) + 1 :] in _CORPORATE_SUFFIXES
            for listed in self._mission
        )


def update_digest_tunables(path: Path | str, **values: object) -> None:
    """Write digest tunables back into ``profile.yaml`` in place.

    Line-level replacement rather than a YAML round-trip, because the file is
    hand-maintained and its comments carry the reasoning behind the numbers —
    ``yaml.safe_dump`` would silently delete all of it.

    Args:
        path: Location of ``profile.yaml``.
        **values: Tunables to set; keys must be in :data:`DIGEST_TUNABLES`.

    Raises:
        KeyError: If a key is not a recognised tunable, or is not present in the
            file to replace.
    """
    unknown = set(values) - set(DIGEST_TUNABLES)
    if unknown:
        raise KeyError(f"not digest tunables: {', '.join(sorted(unknown))}")

    path = Path(path)
    text = path.read_text()
    for key, value in values.items():
        pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)(\S+)", re.MULTILINE)
        text, count = pattern.subn(rf"\g<1>{value}", text, count=1)
        if count == 0:
            raise KeyError(f"{key} is not present in {path}")
    path.write_text(text)


def _gate_names(gates: list[object]) -> list[str]:
    """Flatten the hard-gate list, which mixes bare names with parameterised ones."""
    names: list[str] = []
    for gate in gates:
        if isinstance(gate, dict):
            names.extend(str(key) for key in gate)
        else:
            names.append(str(gate))
    return names
