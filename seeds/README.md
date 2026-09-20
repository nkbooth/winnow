# Seed lists

Curated company boards, so a fresh install has somewhere to start.

Winnow polls only companies you name, and a new install names none. These files
are the on-ramp: one command gets you a working search, and you prune from
there.

```bash
winnow company add --from seeds/developer-tools.yaml
winnow company add --from seeds --tag remote-first
winnow company add --from seeds                      # everything
```

Importing twice is safe and free. A board is identified by `(vendor, identifier)`,
which is unique in the database, and the duplicate check runs *before* the
board is contacted — so a re-import reports `skipped` and reaches the network
zero times. One company may hold two boards; the same board under two different
company names is still one board.

## How these are organised

**Sector is the file. Everything else is a tag.**

A board carries *every* role a company has — adding Stripe gets you all of
Stripe, and your rubric's title gates decide what surfaces. So there are no
files for roles or seniority; they would each contain the same boards.

Cross-cutting attributes are tags on the entry, which is what lets
`--tag remote-first` work across every file without listing a company twice.

A company belongs to **exactly one** file. A test enforces it.

## Categories

| File | Contains |
|---|---|
| `artificial-intelligence.yaml` | Model labs and training infrastructure |
| `cloud-and-infrastructure.yaml` | Hosting, networking, operating systems |
| `commerce-and-logistics.yaml` | Retail, marketplaces, freight, fleets |
| `data-and-analytics.yaml` | Warehouses, pipelines, observability |
| `developer-tools.yaml` | Engineers are the customer |
| `education.yaml` | Learning platforms and schools |
| `energy-and-climate.yaml` | Decarbonisation and energy systems |
| `fintech-and-payments.yaml` | Payments, banking, exchanges |
| `gaming.yaml` | Studios and engines |
| `government-and-civic.yaml` | Public sector and civic technology |
| `hardware-and-robotics.yaml` | Physical products and autonomous systems |
| `healthcare-and-biotech.yaml` | Care delivery and life sciences |
| `marketing-and-sales-tech.yaml` | CRM, messaging, revenue systems |
| `media-and-publishing.yaml` | Publishing, creator tools, communities |
| `nonprofit-and-foundations.yaml` | Foundations and public-benefit organisations |
| `productivity-and-collaboration.yaml` | Documents, design, chat, files |
| `security-and-privacy.yaml` | Security products, identity, privacy-first software |
| `workforce-and-hr.yaml` | Payroll, benefits, employment infrastructure |

Missing a sector? Add the file. The schema is below and the tests will tell you
if you got it wrong.

## Common tags

`remote-first`, `open-source`, `privacy-focused`, `nonprofit`,
`publicly-traded`, `enterprise`, `ai`, `robotics`.

Tags are free-form. Reuse an existing one rather than coining a synonym —
`remote-first` and `fully-remote` split the same set in half.

## Schema

```yaml
category: developer-tools     # must match the filename
description: >-
  One sentence a stranger can use to decide whether this file is for them.

companies:
  - name: Grafana Labs                                 # as it appears in the digest
    board: https://boards.greenhouse.io/grafanalabs    # the URL a human can click
    tags: [open-source, remote-first]                  # optional
    verified: 2026-09-20                               # when it last answered
    note: "Anything the next reader should know."      # optional, quote it
```

The board URL is stored rather than a parsed identifier, because it is the form
a reviewer can check by clicking, and because it is what changes when a company
moves vendor.

## Adding a company

1. Open the company's careers page and click into any single job.
2. Copy the board URL from the address bar. It must be a host winnow can poll —
   Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Rippling or Workable.
   See [Add a company board](../docs/how-to/add-a-board.md).
3. Add it to the right file, **in alphabetical order**. Sorting is what keeps
   two pull requests from conflicting, and a test enforces it.
4. Verify it answers:

   ```bash
   ./scripts/verify-seeds.py
   ```

5. Set `verified` to today's date.

### Do not guess slugs

A slug is rarely the company name. Sourcegraph's is `sourcegraph91`; Remote's
is `remotecom`. Of the first fifty slugs guessed while building these files,
fourteen were wrong — they either 404 or belong to a different company.

`verify-seeds.py` exists so that guesses become facts before they reach a file.
Greenhouse and Workable state their own board name, and the script reports a
mismatch, which is the only guard against adding a board that answers for
somebody else.

### What not to add

- **Boards that need employer credentials.** Teamtailor and HiBob serve job data
  only to the employer's own tokens. They are recorded in the registry as
  unpollable and cannot be seeded.
- **Aggregators.** Winnow reads company boards. Indeed and LinkedIn have no
  usable API and prohibit automation.
- **Companies you have not verified.** An entry that 404s costs every user of
  that file an error, and costs a maintainer the time to find out which one.

## What CI checks

Offline, on every pull request:

- The file parses and the category matches the filename.
- Every board URL resolves to a vendor winnow can poll.
- Entries are sorted and unique within the file.
- No company appears in two files.
- Every entry has a `verified` date.

Whether a board still answers is **not** checked in CI. A test suite that fails
because a vendor rate-limited it teaches people to ignore the test suite. Run
`verify-seeds.py` yourself.
