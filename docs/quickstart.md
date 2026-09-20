---
title: "Quickstart"
description: "Go from a fresh clone to your first digest: configure a rubric, add a company board, and see what survives the gates."
docType: "quickstart"
lastVerified: "2026-09-20"
weight: 10
---

# Get your first digest

Set winnow up, point it at one company, and read what it found. You end with a
scored shortlist and a clear picture of what was rejected and why.

**Time:** ~10 minutes

## Prerequisites

- Python 3.13 or later, and [uv](https://docs.astral.sh/uv/)
- An [Anthropic API key](https://console.anthropic.com/)
- One company you would work for, whose careers page you can open in a browser

## Steps

### Step 1: Install and initialise

```bash
git clone https://github.com/nkbooth/winnow
cd winnow
uv sync
uv run winnow init
```

`init` asks for your name, the address you apply from, your target job titles,
and the lowest salary you would accept. It re-asks if you leave the salary or
titles empty, because a floor of zero rejects nothing and an empty title list
matches nothing.

It writes three things to `~/.config/winnow/`: `profile.yaml` (your rubric),
`config.toml` (hosts and delivery), and `assets/` (stub files for drafting).

### Step 2: Provide your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Winnow reads credentials through references rather than storing them. `env:` is
the default; `file:` and `op://` also work. See
[Settings](reference/configuration.md#credential-references).

### Step 3: Create the database

```bash
uv run winnow db init
```

### Step 4: Add company boards

Fastest route — import a curated list:

```bash
uv run winnow company add --from seeds/developer-tools.yaml
```

`seeds/` holds 872 verified boards across 18 sectors, and `--tag remote-first`
works across all of them. See [seeds/README.md](../seeds/README.md).

To add one company yourself, open their careers page and copy the URL of the
page listing their jobs:

```bash
uv run winnow company add "Tailscale" --board https://boards.greenhouse.io/tailscale
```

Winnow recognises Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Rippling
and Workable from the URL, then calls the board to confirm it answers before
recording anything.

If your URL is not recognised, see
[Add a company board](how-to/add-a-board.md#find-the-real-board-url).

### Step 5: Poll, score, and read

```bash
uv run winnow run
```

## Verify

You should see a tally like this:

```
polled 1 boards — 3 to score
  gated  49  title_off_target
  gated   3  non_us_location
scored 3 clusters — 3 awaiting review
```

Then the digest itself, listing anything above your score threshold with the
sentence from the posting that earned each judgement.

Two results are both correct. If nothing clears the threshold, the digest says
so rather than showing you near-misses. If a board fails, the digest names the
board — winnow never reports a quiet day it cannot vouch for.

## Next steps

- [Add more boards](how-to/add-a-board.md) — one company is not a search
- [Choose where the digest goes](how-to/choose-delivery.md) — your phone, not your terminal
- [How winnow decides](explanation/how-winnow-decides.md) — why 49 postings never reached the model
- Triage what it found: `uv run winnow review`
