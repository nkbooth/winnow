# winnow

A daily job-board agent that throws away almost everything you show it.

Winnowing separates grain from chaff by blowing air through the pile. That is
the job here. A single poll of 22 company boards sees roughly 2,900 postings,
rejects 1,858 on the title alone, and surfaces eleven. The ratio is the design,
not a side effect of it.

Winnow polls the boards of companies you name, rejects what cannot possibly fit
using rules that need no model, scores what survives against a rubric you wrote,
and delivers a short daily digest. It drafts cover letters that may only make
claims traceable to your own written material, and it watches your mailbox so
rejections and interview requests update themselves.

**Disclaimers**: 1. This is vibe coded. I'm an engineer, so it's more than "hey claude, make me a thing," but bugs may exist.  Use at your own risk.  2. I most definitely would not turn an AI agent loose on your primary email account - if you do, that's on you (not me).  Winnow uses deterministic tools to take actions instead of the LLM directly, but still - YOLO was out in 2013.

---

## What makes it different

Most tools in this space optimise for volume. This one optimises for the
opposite, and three decisions follow from that:

**Cheap rules run before the model.** Compensation floors, remote requirements,
employment type and title relevance are settled deterministically. Of ~2,900
postings, roughly 40 ever reach a language model. Scoring 2,900 postings would
cost more than the job search.

**Missing evidence never satisfies a gate.** A posting that does not state its
location is `UNKNOWN`, not "probably remote". Gates fire on stated evidence
only, in one direction. A gate that treats silence as a pass quietly admits
exactly the roles it exists to exclude.

**Silence is reported, never implied.** If a board stops answering, the digest
says the board stopped answering. It does not report a quiet day. Every
component that can return nothing distinguishes "there is nothing" from "I
could not see", because those look identical from the outside and mean opposite
things.

The same discipline runs through drafting: every factual claim in a letter comes
back with the span of your own material it was taken from, and those spans are
checked. A claim that cannot be traced is shown to you rather than sent.

## Requirements

- Python 3.13 or later
- An Anthropic API key for scoring and drafting
- Optional: an IMAP mailbox, to track replies
- Optional: [ntfy](https://ntfy.sh) or Matrix, to get the digest on your phone

## Install

```bash
git clone https://github.com/nkbooth/winnow
cd winnow
uv sync
uv run winnow init
```

`winnow init` asks six questions, writes a rubric and a settings file, and
prints what is still missing. If you already have a rubric it adopts it instead
of interviewing you.

Then get a working search in one command:

```bash
uv run winnow db init
uv run winnow company add --from seeds --tag remote-first
uv run winnow run
```

`seeds/` holds 65 verified company boards across 18 sectors. Import a file, a
tag, or the lot, then prune. See [seeds/README.md](seeds/README.md) — it is
built to take pull requests.

The [quickstart](docs/quickstart.md) walks through the same ground more slowly.

## Documentation

**Start here**

- [Quickstart](docs/quickstart.md) — empty database to first digest

**How-to guides**

- [Add a company board](docs/how-to/add-a-board.md)
- [Seed lists](seeds/README.md) — 65 verified boards, and how to add yours
- [Choose where the digest goes](docs/how-to/choose-delivery.md)
- [Run it on a server](docs/how-to/deploy.md)

**Reference**

- [Settings (`config.toml`)](docs/reference/configuration.md)
- [Rubric (`profile.yaml`)](docs/reference/rubric.md)
- [Commands](docs/reference/cli.md)

**Explanation**

- [How winnow decides](docs/explanation/how-winnow-decides.md)

**When something breaks**

- [Troubleshooting](docs/troubleshooting.md)

## What it will not do

- Apply on your behalf. Nothing is sent without a keystroke against one
  specific draft.
- Scrape LinkedIn or Indeed. Neither has a usable API and both prohibit it.
- Invent experience. The claim checker exists to make that a visible failure
  rather than an invisible one.

## Contributing

Bug reports and patches are welcome. Two expectations:

Tests come first, and they assert behaviour rather than implementation. The
suite runs offline against dated payloads captured from real boards, so a
vendor changing their API breaks a test rather than a morning.

Comments explain **why**. The code says what it does; a comment earns its place
by recording a constraint, a measurement, or a mistake worth not repeating.

```bash
./scripts/dev.sh uv run pytest        # 700+ tests, no network
./scripts/dev.sh uv run ruff check src tests
```

## Licence

MIT. See [LICENSE](LICENSE).
