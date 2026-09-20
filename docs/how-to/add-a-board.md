---
title: "Add a company board"
description: "Record a company so winnow polls it daily, including how to find the real board URL when a careers page hides it."
docType: "howto"
lastVerified: "2026-09-20"
weight: 10
prerequisites:
  - "[Quickstart](../quickstart.md) completed"
---

# Add a company board

Point winnow at a company whose jobs you want to see. You do this once per
company; polling is automatic afterwards.

## Prerequisites

- An initialised winnow install
- The URL of the page listing that company's open jobs

## Steps

### Step 1: Find the real board URL

Most companies host their careers page themselves and embed a board from an
applicant tracking system. Winnow needs the board, not the wrapper.

Open the careers page and click through to any single job. Look at the address
bar. If it shows one of these hosts, that is the board:

| Host | Vendor |
|---|---|
| `boards.greenhouse.io` or `job-boards.greenhouse.io` | Greenhouse |
| `jobs.lever.co` | Lever |
| `jobs.ashbyhq.com` | Ashby |
| `*.myworkdayjobs.com` | Workday |
| `jobs.smartrecruiters.com` | SmartRecruiters |
| `ats.rippling.com` | Rippling |
| `apply.workable.com` | Workable |

If the address bar never leaves the company's own domain, their board is not
one winnow can poll. See [Troubleshooting](../troubleshooting.md#a-company-has-no-board-winnow-can-read).

### Step 2: Add the company

```bash
uv run winnow company add "Tailscale" --board https://boards.greenhouse.io/tailscale
```

The name is yours to choose — it appears in the digest and groups postings
across sources. The URL is parsed for the board identifier, then called to
confirm it answers.

### Step 3: Poll it

```bash
uv run winnow fetch
```

## Verify

```bash
uv run winnow company list
```

The company appears with its vendor and identifier. After a poll, `winnow run`
reports it in the board count.

## Troubleshooting

**`ProbeFailed: ... returned 404`** — the identifier in the URL is wrong. Slugs
are rarely the company name: Sourcegraph's Greenhouse slug is `sourcegraph91`.
Copy it from a live job link rather than guessing.

**`ProbeFailed: ... publishes no unauthenticated listing endpoint`** — that
vendor requires credentials issued by the employer. Teamtailor and HiBob both
do. The board is recorded as unpollable rather than silently returning nothing
forever.

**The board is added and returns zero jobs** — that is a real answer, and
winnow keeps the company so the next posting arrives as a change rather than an
absence.

## Related guides

- [Choose where the digest goes](choose-delivery.md)
- [How winnow decides](../explanation/how-winnow-decides.md)
