---
title: "Rubric"
description: "Every section of profile.yaml: gates, compensation floors, titles, weights and digest settings, and what each one changes."
docType: "reference"
lastVerified: "2026-09-20"
weight: 20
---

# Rubric

`profile.yaml` is what winnow knows about the job you want. Every gate, weight
and threshold reads from it, so behaviour changes here rather than in code.

Default location: `~/.config/winnow/profile.yaml`. Override with `WINNOW_PROFILE`.

`winnow init` writes a starting rubric. A worked example for a fictional
candidate is in [`examples/profile.yaml`](https://github.com/nkbooth/winnow/blob/main/examples/profile.yaml),
which the test suite runs against and therefore cannot drift.

## `candidate`

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | string | Yes | Signs letters and addresses the drafter. |
| `mailbox` | string | Yes | Address applications are sent from. |
| `location` | string | No | Free text. |

## `compensation`

Floors are hard. Below floor is a decline, not a negotiation.

| Key | Type | Default | Description |
|---|---|---|---|
| `w2.mid_market.floor` | integer | — | Annual floor for most employers. |
| `w2.large_corporate.floor` | integer | — | Applied to companies tiered as large corporate. |
| `contract.hourly_floor` | integer | — | Hourly floor for contract postings. |
| `equity.below_floor` | enum | `worthless` | Whether equity can substitute for cash below floor. |

A modelled salary never satisfies a floor. Only `stated` and `parsed`
provenance may gate; `predicted` may flag.

## `hard_gates`

A list. Any gate that fires rejects the posting, which is never surfaced.

| Gate | Fires when |
|---|---|
| `comp_below_applicable_floor` | Stated compensation is under the applicable floor |
| `onsite_required` | The posting states an onsite requirement |
| `hybrid_required` | The posting states a hybrid requirement |
| `relocation_required` | The posting requires relocation |
| `contract_exceeds_15_hours_per_week` | A contract role exceeds the hours cap |
| `employer_in_stealth_list` | The employer is in `stealth.exclude_employers` |
| `crypto_sector_below: N` | A crypto-sector role pays under N |

Every gate fires on stated evidence only. A posting silent about location is
not gated by `onsite_required`.

## `location`

| Key | Type | Default | Description |
|---|---|---|---|
| `remote` | enum | `required` | `required`, `preferred`, or `no`. |
| `scope` | string | `US-wide` | Geographic scope. |
| `rto_trap_detection` | boolean | `true` | Read the description for return-to-office language a location field hides. |

## `titles`

The cheapest and most consequential gate. A title matching nothing here is
rejected before any model call.

| Key | Type | Description |
|---|---|---|
| `primary` | list | Target titles. Matched by anchor phrase, not exact string. |
| `discovery_only` | list | Surfaced only when posted compensation independently clears the floor. |
| `decline` | list | Work you do not want, whatever it is called. |

Anchors are matched on word boundaries. Without that, `erp` matches inside
`enterprise` and `india` inside `Indiana` — both were real defects.

## `weights`

Signed integers. Positive weights are what you want; negative weights are what
makes a matching title the wrong job anyway.

| Weight | Typical | Meaning |
|---|---|---|
| `growth_signal` | `+20` | Scope expansion, budget, a team to build |
| `builder_shaped` | `+15` | Tooling, automation, greenfield construction |
| `team_leadership` | `+10` | Direct reports |
| `mission_alignment` | `+15` | Company purpose matches yours |
| `governance_documentation_heavy` | `-10` | Same title, wrong work |
| `grind_culture_signals` | `-15` | Cluster of hustle language and early-stage equity-heavy pay |
| `source_proximity` | `+15` | Found at the employer rather than an aggregator |
| `ghost_job_signals` | `-20` | Posting age and repost patterns |

## `stealth`

| Key | Type | Default | Description |
|---|---|---|---|
| `current_employer_aware` | boolean | `true` | Set `false` if your search is confidential. |
| `exclude_employers` | list | `[]` | Never surfaced. Your employer belongs here. |

## `process`

| Key | Type | Default | Description |
|---|---|---|---|
| `silence_window_days` | integer | `21` | Days before an unanswered application is recorded as `no_response`. |

## `digest`

| Key | Type | Default | Description |
|---|---|---|---|
| `score_threshold` | integer | `70` | Minimum score to appear. Adjustable from the review screen. |
| `max_items` | integer | `8` | Cap per digest. |
| `always_show_top` | integer | `3` | Shown even below threshold. |
| `cadence` | enum | `on_hits` | `on_hits` or `daily`. |
| `weekly_summary` | string | `monday` | Posts even at zero, so silence is never breakage. |
| `retention_days` | integer | `180` | How long a vanished posting is kept. |

## Constraints

- `version` and the file's content hash together identify the rubric. Scores
  record it, and scores across versions are not comparable.
- Editing the file changes the hash, so a version you forget to bump still
  registers as a different rubric.
- `winnow review` writes `digest` keys back to this file from the `t` panel.

## See also

- [How winnow decides](../explanation/how-winnow-decides.md)
- [Settings reference](configuration.md)
