"""Drafting a cover letter for one opportunity.

The design's rule for this is "never invented experience". A rule nothing
checks is a hope, so the model returns each claim it makes alongside the span
of the work inventory or resume it came from, and every span is verified
against the assets before the draft reaches the queue. Claims that cannot be
traced are attached to the draft and shown in review rather than quietly
dropped — an invented credential is the one mistake in this whole system that
cannot be walked back.

That check has a real limit worth stating: it verifies the claims the model
declares, not every sentence of the prose. It catches fabrication that the
model is willing to attribute, which is most of it, and it is not a substitute
for reading the letter.

Nothing here sends anything. This module builds text and hands it to a queue;
only `winnow review` resolves a credential that could mail it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from winnow.models import Posting

PROMPT_VERSION = "1"

RESUME_FILES = {
    "A": "resume-a-business-systems.md",
    "B": "resume-b-consulting.md",
    "C": "resume-c-technical.md",
}

_WHITESPACE = re.compile(r"\s+")

#: Spans shorter than this match too easily to mean anything.
_MINIMUM_EVIDENCE = 12

_TOKEN = re.compile(r"[a-z0-9$%][a-z0-9$%.,-]*")

#: Words too common to carry a factual claim. What is left after removing them
#: is what a fabrication has to invent: numbers, names, titles, scope.
_COMMON = frozenset(
    # A stop list reads better as prose than as seventy quoted strings.
    """the a an and or of to in on at for with by from as is are was were be been being
    i we my our your their it its this that these those he she they them who whom which
    what when have has had do does did will would can could should may might must not no
    yes if then than about into over under after before between during through out up
    down off again further once here there all any both each few more most other some
    such only own same so too very just now one two three four five six seven eight nine
    ten years year people team teams work working""".split()  # noqa: SIM905
)


class DraftingError(RuntimeError):
    """The assets are missing, or the model returned something unusable."""


@dataclass(frozen=True)
class Assets:
    """The material a draft may be built from, and nothing else."""

    cover_letter_assets: str
    work_inventory: str
    signature: str
    resume_variants: dict[str, str]

    def searchable(self) -> str:
        """Everything a claim may be traced to, normalised for matching.

        The reusable paragraphs are included: they are his own words and carry
        real claims — the management philosophy appears nowhere else — and
        leaving them out flagged three true statements on the first live draft.
        """
        parts = [self.work_inventory, self.cover_letter_assets, *self.resume_variants.values()]
        return _normalise("\n".join(parts))

    def vocabulary(self) -> set[str]:
        """Every distinctive word the source material actually uses."""
        return _distinctive(self.searchable())


@dataclass(frozen=True)
class Draft:
    """One proposed cover letter, with its provenance attached."""

    kind: str
    resume_variant: str
    subject: str
    body: str
    built_from: dict
    unverified: tuple[dict, ...] = ()
    recipient: str | None = None
    tailoring_notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_attention(self) -> bool:
        """True when a claim could not be traced to the candidate's own history."""
        return bool(self.unverified)


class Drafter(Protocol):
    """Whatever writes the letter. A model, or a stub in tests."""

    model: str

    def complete(self, system: str, user: str) -> dict:
        """Return a structured draft."""
        ...


def load_assets(directory: Path | str) -> Assets:
    """Read the drafting material from disk.

    Args:
        directory: Where the assets were deployed.

    Returns:
        The loaded assets.

    Raises:
        DraftingError: If anything is missing. Drafting from a partial set would
            produce a letter that quietly omits the strongest material, which is
            worse than refusing.
    """
    directory = Path(directory)
    try:
        variants = {key: (directory / name).read_text() for key, name in RESUME_FILES.items()}
        return Assets(
            cover_letter_assets=(directory / "cover-letter-assets.md").read_text(),
            work_inventory=(directory / "work-inventory.md").read_text(),
            signature=(directory / "email-signature.md").read_text(),
            resume_variants=variants,
        )
    except OSError as error:
        raise DraftingError(f"drafting assets unavailable at {directory}: {error}") from error


def build_system_prompt(
    assets: Assets, sent_letters: Sequence[str] = (), candidate: str = ""
) -> str:
    """Build the instruction half, identical for every posting in a run.

    The assets go here rather than in the user half because they do not change
    between opportunities, and paying for them per draft is the only real cost
    in this path.

    Args:
        assets: The loaded material.
        candidate: Whose letter this is, from the rubric. Left out rather than
            guessed at when unset — a letter signed by nobody is better than
            one signed by whoever the code was written for.
        sent_letters: Letters actually sent, newest first. Instructions can
            describe a register; only examples demonstrate one, and these are
            the only evidence of how he really writes once a human has been
            through it. Omitted entirely when there are none, rather than
            leaving an empty heading that reads as "he has sent nothing".

    Returns:
        The system prompt.
    """
    whose = f", for {candidate}" if candidate else ""
    name = candidate or "the candidate"
    return f"""You draft a cover letter for one job posting{whose}.

Rules, in order of importance:

1. **Never invent experience.** Every factual claim about what {name} has
   built, led, or shipped must come from the work inventory or a resume below. You will
   return each claim with the exact span you took it from, and those spans are
   checked. A claim you cannot quote is a claim you must not make.
2. Write in their register: plain, specific, unshowy. No "I am excited to apply",
   no "passionate", no restating the job description back at them.
3. Use the reusable paragraphs where they fit. They are already in their voice.
4. Pick the resume variant that fits the posting and say why in one sentence.
   `resume_variant` must be exactly one of "A", "B" or "C" — the letter alone,
   not the resume's title.
5. Give tailoring notes: which existing bullets to lead with and which of the
   posting's words to mirror. Reordering only — never new bullets.

Length: four short paragraphs at most. It should read like a person who has
done the work, not like an application.

--- REUSABLE PARAGRAPHS (his own words) ---
{assets.cover_letter_assets}

--- WORK INVENTORY (the only source of factual claims) ---
{assets.work_inventory}

--- RESUME A: business systems / operations director ---
{assets.resume_variants["A"]}

--- RESUME B: consulting ---
{assets.resume_variants["B"]}

--- RESUME C: technical / privacy-tech ---
{assets.resume_variants["C"]}

--- SIGNATURE FORMS ---
{assets.signature}{_sent_letters_section(sent_letters)}"""


def _sent_letters_section(sent_letters: Sequence[str]) -> str:
    """Render past letters as examples of voice, fenced against being mined.

    A letter he sent last week names a company he applied to last week. Copied
    forward into a new one it is a false claim about this employer, and the
    span check would not catch it — the spans are checked against the inventory
    and the resumes, and a sentence lifted from an old letter was never in
    either. So the fence has to be stated rather than assumed.
    """
    if not sent_letters:
        return ""
    letters = "\n\n---\n\n".join(sent_letters)
    return f"""

--- LETTERS HE ACTUALLY SENT (voice reference, newest first) ---

These went out after he revised them, so they show his register more exactly
than any description of it can. Match the rhythm, the plainness, the way he
concedes a gap and moves on.

They are **not a source of facts**. They name other companies and other roles.
Take nothing factual from them — every claim still has to come from the work
inventory or a resume, with its span, as rule 1 requires.

{letters}"""


def build_user_prompt(posting: Posting) -> str:
    """Build the per-posting half.

    Args:
        posting: The opportunity being applied to.

    Returns:
        The user prompt.
    """
    return (
        f"Company: {posting.company}\n"
        f"Title: {posting.title}\n"
        f"Locations: {', '.join(posting.locations) or 'not stated'}\n"
        f"Source: {posting.source_url}\n\n"
        f"Posting text:\n{posting.description_text or '(no description captured)'}"
    )


def draft_cover_letter(
    posting: Posting,
    assets: Assets,
    drafter: Drafter,
    *,
    sent_letters: Sequence[str] = (),
    candidate: str = "",
    now: datetime | None = None,
) -> Draft:
    """Draft a cover letter for one posting.

    Args:
        posting: The opportunity.
        assets: The material the letter may draw on.
        drafter: The model wrapper.
        sent_letters: Previously sent letters, shown as voice reference only.
        candidate: Whose letter this is, from the rubric.
        now: Timestamp recorded in the provenance.

    Returns:
        The draft, with any untraceable claims attached rather than removed.

    Raises:
        DraftingError: If the response is not a usable draft. An empty body or
            an invented resume variant is a malfunction, not a draft to edit.
    """
    now = now or datetime.now(UTC)
    response = drafter.complete(
        build_system_prompt(assets, sent_letters, candidate), build_user_prompt(posting)
    )

    variant = str(response.get("resume_variant") or "")
    if variant not in RESUME_FILES:
        raise DraftingError(f"unknown resume variant {variant!r}")

    body = str(response.get("body") or "")
    if not body.strip():
        raise DraftingError("the draft has no body")

    haystack = assets.searchable()
    vocabulary = assets.vocabulary()
    claims = list(response.get("claims") or [])

    unverified = []
    for claim in claims:
        missing = _untraceable_words(claim.get("evidence"), haystack, vocabulary)
        if missing is not None:
            unverified.append({**claim, "missing": missing})

    return Draft(
        kind="cover_letter",
        resume_variant=variant,
        subject=str(
            response.get("subject")
            or (f"{posting.title} — {candidate}" if candidate else posting.title)
        ),
        body=body,
        tailoring_notes=tuple(str(note) for note in response.get("tailoring_notes") or ()),
        unverified=tuple(unverified),
        built_from={
            "company": posting.company,
            "title": posting.title,
            "source_url": posting.source_url,
            "resume_variant": variant,
            "variant_reason": response.get("variant_reason"),
            "claims": claims,
            "model": drafter.model,
            "prompt_version": PROMPT_VERSION,
            "drafted_at": now.isoformat(),
        },
    )


def _untraceable_words(evidence: object, haystack: str, vocabulary: set[str]) -> list[str] | None:
    """Say whether a claim's evidence is supported, and if not, by what.

    An exact span is the strong case. Short of that, the question worth asking
    is not whether the model quoted cleanly but whether it invented anything:
    a paraphrase reuses the source's distinctive words, while a fabrication has
    to introduce new ones. So a span whose every distinctive word appears
    somewhere in the material passes, and a span that does not comes back
    naming the words that gave it away.

    Returns:
        ``None`` when the claim is supported, otherwise the missing words.
    """
    if not isinstance(evidence, str):
        return ["(no evidence offered)"]

    span = _normalise(evidence)
    if len(span) < _MINIMUM_EVIDENCE:
        return ["(evidence too short to check)"]
    if span in haystack:
        return None

    missing = sorted(word for word in _distinctive(span) if word not in vocabulary)
    return missing or None


def _distinctive(text: str) -> set[str]:
    """The words in a span that could carry a factual claim.

    Trailing separators are dropped before comparison. The token pattern keeps
    ``$ % . , -`` so that ``$80m``, ``10%`` and ``2015-2019`` survive intact,
    but that also makes ``workcenters,`` a different word from ``workcenters``,
    and a claim quoting the second against a source containing the first was
    reported as untraceable. A checker that cries wolf is one nobody reads.
    """
    tokens = (token.rstrip(".,-") for token in _TOKEN.findall(text))
    return {token for token in tokens if token not in _COMMON and len(token) >= 3}


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().lower()
