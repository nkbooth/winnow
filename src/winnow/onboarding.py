"""First run: look first, ask second.

Two people arrive at a fresh install. One has nothing, and has to be asked what
work they want and what it has to pay — every gate downstream reads those two
answers and neither has a sensible default. The other already has a rubric,
because they were running this before it had this name or someone handed them
one, and making them reproduce a file they already own by answering forty
questions is a good way to lose them.

So ``init`` inspects the directory before it opens its mouth. What it writes is
deliberately a *starting* rubric rather than a complete one: gates, weights and
a threshold that do something defensible on day one, with the expectation that
the review TUI and a hand edit will move them once real postings have been
judged. The alternative — interviewing someone through every weight in the file
— asks for judgements nobody can make before seeing any output.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

#: Asked of the operator. Returns the answer, or the default for an empty one.
Ask = Callable[[str, str, str], str]

#: Filenames the drafter expects, with the one-line purpose of each.
ASSET_STUBS = {
    "work-inventory.md": "what you have actually done, in your own words",
    "cover-letter-assets.md": "paragraphs you already like, reusable as they are",
    "email-signature.md": "how you sign off",
    "resume-a-business-systems.md": "resume variant A",
    "resume-b-consulting.md": "resume variant B",
    "resume-c-technical.md": "resume variant C",
}


@dataclass(frozen=True)
class InitResult:
    """What ``init`` found and what it did about it."""

    adopted_rubric: bool
    adopted_settings: bool
    next_steps: list[str] = field(default_factory=list)


def init(directory: Path | str, *, ask: Ask) -> InitResult:
    """Set a directory up as a winnow configuration, asking only what is missing.

    Args:
        directory: Where ``profile.yaml``, ``config.toml`` and ``assets/`` go.
        ask: Prompts the operator. Injected so the interview is testable and so
            a non-interactive install can supply answers from elsewhere.

    Returns:
        What was adopted and what the operator still has to do.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    profile_path = directory / "profile.yaml"
    config_path = directory / "config.toml"

    adopted_rubric = profile_path.exists()
    answers: dict[str, str] = {}
    if not adopted_rubric:
        answers = interview(profile_path, ask=ask)

    adopted_settings = config_path.exists()
    if not adopted_settings:
        contact = answers.get("contact") or answers.get("mailbox") or ""
        config_path.write_text(_settings_template(contact))

    _write_asset_stubs(directory / "assets")

    return InitResult(
        adopted_rubric=adopted_rubric,
        adopted_settings=adopted_settings,
        next_steps=_next_steps(directory, adopted_rubric),
    )


def interview(path: Path | str, *, ask: Ask) -> dict[str, str]:
    """Ask the few questions that have no defensible default, and write a rubric.

    Deliberately short. The questions here are the ones where a wrong answer
    makes the tool silently useless: a salary floor of zero gates nothing, and
    an empty title list matches nothing. Everything else ships with a starting
    value and is meant to be moved later, from the review screen, against real
    postings rather than in the abstract.

    Args:
        path: Where to write ``profile.yaml``.
        ask: Prompts the operator.

    Returns:
        The answers given, so a caller can reuse them for other files.
    """
    answers = {
        "name": _required(ask, "name", "Your name (letters are signed with it)"),
        "mailbox": _required(ask, "mailbox", "Email address you apply from"),
        "location": ask("location", "Where you live (city, state)", ""),
        "titles": _required(
            ask, "titles", "Target job titles, comma separated (these gate everything)"
        ),
        "floor": str(_required_number(ask, "floor", "Lowest salary you would accept")),
        "remote": ask("remote", "Remote required? (required / preferred / no)", "required"),
    }
    answers["target"] = ask("target", "Salary you are aiming for", "")
    answers["contact"] = ask(
        "contact", "Contact address for the User-Agent sent to job boards", answers["mailbox"]
    )

    Path(path).write_text(_rubric_template(answers))
    return answers


def _required(ask: Ask, key: str, prompt: str) -> str:
    """Ask until there is an answer. These are the ones with no default."""
    while True:
        value = ask(key, prompt, "").strip()
        if value:
            return value


def _required_number(ask: Ask, key: str, prompt: str) -> int:
    """Ask until the answer is a number above zero.

    A floor of zero is not a permissive setting, it is a broken one: the comp
    gate stops rejecting anything and the digest fills with roles paying half
    what the operator needs.
    """
    while True:
        raw = ask(key, prompt, "").strip().replace(",", "").replace("$", "")
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value


def _write_asset_stubs(directory: Path) -> None:
    """Create the drafting files, each explaining what belongs in it.

    Empty files would let drafting start and fail at the claim check with
    nothing traceable and no indication why. A stub that says what the file is
    for fails the same way but tells the operator what to do about it.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for name, purpose in ASSET_STUBS.items():
        path = directory / name
        if path.exists():
            continue
        path.write_text(_asset_stub(name, purpose))


def _asset_stub(name: str, purpose: str) -> str:
    return f"""# {name.removesuffix(".md").replace("-", " ").title()}

**Replace this.** This file is {purpose}.

Every factual claim a cover letter makes must be traceable to a span in this
file, another asset here, or a resume variant. The drafter returns the span it
took each claim from and those spans are checked, so a claim that is not
written down here cannot be made at all.

Write in your own voice and be specific. Vague material produces vague letters,
and the claim checker cannot tell the difference between a claim you forgot to
write down and one the model invented.

See `examples/assets/` in the winnow repository for a worked set.
"""


def _settings_template(contact: str) -> str:
    return f'''# winnow settings — where things live and how to reach them.
#
# Credentials are never written here. Every *_ref below is a reference that
# names its own backend:
#
#   env:NAME                    an environment variable
#   file:/path                  a file's contents (podman/k8s secrets, systemd)
#   op://vault/item/field       1Password, via its CLI
#
# A bare value is refused, so this file cannot become where a password lives.

[identity]
# Sent in the User-Agent to every job board polled, so an employer can see who
# is reading their listings. Being reachable is part of using a public endpoint
# as intended.
contact = "{contact}"

[llm]
# Scoring and drafting both need this. Nothing else here is required.
api_key_ref = "env:ANTHROPIC_API_KEY"

[notify]
# terminal | ntfy | matrix
#
# terminal needs nothing and writes the digest where you can read it. It cannot
# push, which matters for the one message that is time-sensitive: an interview
# request arriving mid-afternoon is poorly served by a file you read tomorrow.
# ntfy is the low-effort push option — a topic URL, no account, no server.
backend = "terminal"

[notify.terminal]
# Empty means standard output.
path = ""

# [notify.ntfy]
# topic_url = "https://ntfy.sh/choose-something-unguessable"

# [notify.matrix]
# homeserver_ref = "env:WINNOW_MATRIX_HOMESERVER"
# room_id_ref = "env:WINNOW_MATRIX_ROOM_ID"
# token_ref = "env:WINNOW_MATRIX_TOKEN"

# [mail]
# Optional. Without it winnow still polls, scores and digests; what stops is
# reading employer replies and sending a draft.
# imap_host = ""
# smtp_host = ""
# message_id_domain = ""
# username_ref = "env:WINNOW_MAIL_USERNAME"
# password_ref = "env:WINNOW_MAIL_PASSWORD"

# [adzuna]
# Optional, and only used by `winnow discover`. Free tier.
# app_id_ref = "env:WINNOW_ADZUNA_APP_ID"
# app_key_ref = "env:WINNOW_ADZUNA_APP_KEY"
'''


def _next_steps(directory: Path, adopted: bool) -> list[str]:
    """What is still required before the tool can do anything useful.

    Reported rather than assumed done: an install that looks finished and
    cannot score is a worse outcome than one that says what is missing.
    """
    steps = [
        "Set ANTHROPIC_API_KEY (or point [llm] api_key_ref somewhere else) — "
        "scoring and drafting both need it.",
        f"Fill in {directory / 'assets'} — the drafter may only make claims it "
        "can trace to those files.",
        'Add a board: winnow company add "Their Name" --board <pasted careers URL>',
        "Then: winnow run",
    ]
    if adopted:
        steps.insert(0, f"Adopted the rubric already at {directory / 'profile.yaml'}.")
    return steps


def _rubric_template(answers: dict[str, str]) -> str:
    """Render a starting rubric from the few answers that were asked for.

    The weights and the gates are not asked about. They are judgements nobody
    can make before seeing the tool reject anything, so they ship with values
    that are defensible on day one and are meant to be moved from the review
    screen once there are real postings to move them against.
    """
    titles = [title.strip() for title in answers["titles"].split(",") if title.strip()]
    rendered_titles = "\n".join(f"    - {title}" for title in titles)
    floor = int(answers["floor"])
    target = answers.get("target", "").strip().replace(",", "").replace("$", "")
    target_line = f"      target: {int(target)}" if target.isdigit() else "      target: null"
    remote = (answers.get("remote") or "required").strip().lower()

    return f"""# winnow scoring rubric
#
# Written by `winnow init` on {date.today().isoformat()}. Everything downstream
# reads this file — the gates, the scorer's weights, the digest's threshold —
# so change behaviour here rather than in the code.
#
# The weights below were not asked about. They are judgements that are hard to
# make before seeing the tool reject anything, so they start somewhere
# defensible and are meant to be moved from the review screen (`t`) once real
# postings have been judged.

version: 1
updated: {date.today().isoformat()}
candidate:
  name: {answers["name"]}
  location: {answers.get("location") or "unset"}
  mailbox: {answers["mailbox"]}

engagement:
  w2:
    enabled: true
    status: primary
  contract:
    enabled: false
    status: additive
    max_hours_per_week: 15

# Floors are hard. Below floor is a decline, not a negotiation — a role that
# cannot pay is not a role, however good it looks.
compensation:
  w2:
    mid_market:
      floor: {floor}
{target_line}
    large_corporate:
      floor: {floor}
      target: null
  contract:
    hourly_floor: {max(1, floor // 1000)}
  equity:
    below_floor: worthless
    above_floor: valuable

# Any of these true means the posting is rejected and never surfaced. Evidence
# only: a posting that does not say is not gated, because treating silence as a
# yes would quietly admit exactly the roles these exist to exclude.
hard_gates:
  - comp_below_applicable_floor
  - onsite_required
  - hybrid_required
  - relocation_required
  - employer_in_stealth_list

location:
  remote: {remote}
  scope: US-wide
  rto_trap_detection: true

titles:
  primary:
{rendered_titles}
  discovery_only: []
  decline: []

industry:
  constrained: false

# Positive weights are what you are looking for; negative ones are what makes a
# matching title the wrong job anyway.
weights:
  growth_signal: +20
  builder_shaped: +15
  team_leadership: +10
  mission_alignment: +15
  governance_documentation_heavy: -10
  grind_culture_signals: -15
  source_proximity: +15
  ghost_job_signals: -20

red_flags: {{}}

stealth:
  current_employer_aware: true
  exclude_employers: []

process:
  silence_window_days: 21

digest:
  score_threshold: 70
  max_items: 8
  always_show_top: 3
  cadence: on_hits
  weekly_summary: monday
  retention_days: 180
"""
