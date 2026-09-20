---
title: "Commands"
description: "Every winnow subcommand, what it does, and which ones are safe to run on a schedule."
docType: "reference"
lastVerified: "2026-09-20"
weight: 30
---

# Commands

Every subcommand of `winnow`. Run `winnow <command> --help` for flags.

## Setup

| Command | Description |
|---|---|
| `init [--directory PATH]` | Write a rubric, settings and drafting stubs. Adopts an existing rubric instead of re-interviewing. |
| `db init` | Create the database, or apply pending migrations. Safe to re-run. |
| `company add NAME --board URL` | Record a board. Calls it to confirm it answers before storing. |
| `company list` | List known boards and their status. |
| `company tier NAME TIER` | Set a company's compensation tier: `mid_market`, `large_corporate` or `crypto`. |

## Daily operation

| Command | Description | Unattended |
|---|---|---|
| `run [--dry-run]` | Migrate, poll, score, age, digest, prune. What a timer calls. | Yes |
| `fetch` | Poll every board once and store what survives the gates. | Yes |
| `score` | Score every cluster that has not been scored. Costs model calls. | Yes |
| `digest [--dry-run]` | Render and deliver the digest. | Yes |
| `prune` | Drop postings that left their board beyond the retention window. Scores and decisions survive. | Yes |
| `notify MESSAGE` | Send one message to the configured destination. Useful for testing delivery. | Yes |

## Interactive

| Command | Description | Unattended |
|---|---|---|
| `review` | Triage the queue. The only path that can send mail. | **No** |
| `discover [--days N]` | Sweep the aggregator for employers not on your board list. Requires Adzuna keys. | No |
| `listen` | Watch the mailbox and advance applications. Long-running. | Yes, as a service |

## Review keys

`review` is keyboard-driven and works over SSH.

| Key | Action |
|---|---|
| `i` | Interested — moves to the working set |
| `p` | Pass — prompts for a structured reason |
| `d` | Defer — moves to the deferred list |
| `o` | Open the posting, or copy its link when there is no browser |
| `D` | Draft a cover letter, or reopen the existing one |
| `e` | Record the version actually sent |
| `a` | Mark the application submitted |
| `s` | Send the draft, after confirming |
| `u` | Undo the most recent decision |
| `A` | Show everything scored, including below the threshold |
| `v` | Change list: review, interested, applied, deferred |
| `m` | Review pending merge candidates |
| `t` | Tunables: threshold, digest cap, measured rates |
| `r` / `q` | Reload / quit |

## What the review list shows

The review queue holds scored, undecided clusters **at or above your
`score_threshold`**, highest first. Anything the rubric scored below it is held
back and the count is shown — `18 awaiting review · 49 below 70` — because a
list that quietly got shorter reads as a quiet day. `A` reveals them.

`vetoed` in the right-hand column means the model found a disqualifier in the
posting's prose that the structured gates could not see: an onsite or hybrid
requirement, relocation, an hours cap, or generated boilerplate. A veto
overrides the score rather than reducing it, which is why a vetoed row can
still read 88.

Vetoed rows are shown; gated postings are not. The difference is what the
judgement rests on. A gate reads a stated structured field and is trusted. A
veto is a model reading prose — it must quote the span it relied on, and an
unquotable veto is dropped, but a quotable one can still be a misreading.
Hiding those would make a model error invisible and unappealable.

## Pass reasons

Recorded with every pass, and aggregated into calibration.

| Reason | Meaning |
|---|---|
| `comp_too_low` | Below the applicable floor |
| `rto_suspected` | Return-to-office language the gates did not catch |
| `no_growth_signal` | No scope expansion or ceiling |
| `boring_domain` | A real match, in work you do not want |
| `bad_culture_signal` | Grind-culture or churn signals |
| `title_beneath_target` | Seniority below target |
| `already_applied` | Duplicate of an existing application |
| `timing` | Right role, wrong moment |
| `external_feedback` | Something you learned outside the posting |
| `wrong_function` | The title gate misfired; not this kind of work at all |

The last two are not judgements about the job. `external_feedback` records
knowledge winnow never had access to. `wrong_function` is feedback about winnow
itself — its count appears in the `t` panel, because the fix is narrowing the
title anchors in your rubric.

## Environment variables

| Variable | Overrides |
|---|---|
| `WINNOW_CONFIG` | Path to `config.toml` |
| `WINNOW_PROFILE` | Path to `profile.yaml` |
| `WINNOW_ASSETS` | Path to the drafting assets directory |
| `WINNOW_DB` | Path to the SQLite database |

## See also

- [Settings reference](configuration.md)
- [Rubric reference](rubric.md)
