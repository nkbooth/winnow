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

872 boards, every one of them probed before it was written down.

| File | Boards | Contains |
|---|---|---|
| `artificial-intelligence.yaml` | 59 | Model labs, training infrastructure, and companies whose product is a model |
| `cloud-and-infrastructure.yaml` | 45 | Hosting, networking, operating systems, and the platforms other software runs on |
| `commerce-and-logistics.yaml` | 57 | Retail, marketplaces, freight, fleets, and moving physical things |
| `data-and-analytics.yaml` | 46 | Warehouses, pipelines, observability, and product analytics |
| `developer-tools.yaml` | 51 | Software engineers are the customer: CI, editors, APIs, deployment |
| `education.yaml` | 46 | Learning platforms, courseware, and schools |
| `energy-and-climate.yaml` | 45 | Decarbonisation, energy systems, and climate accounting |
| `fintech-and-payments.yaml` | 89 | Payments, banking, cards, and crypto exchanges |
| `gaming.yaml` | 33 | Game studios and engines |
| `government-and-civic.yaml` | 22 | Public sector, civic technology, and the agencies that buy it |
| `hardware-and-robotics.yaml` | 47 | Physical products, devices, and autonomous systems |
| `healthcare-and-biotech.yaml` | 72 | Care delivery, health platforms, and life sciences |
| `marketing-and-sales-tech.yaml` | 38 | Customer messaging, CRM, and the systems revenue teams run on |
| `media-and-publishing.yaml` | 33 | Publishing platforms, creator tools, and communities |
| `nonprofit-and-foundations.yaml` | 20 | Foundations, nonprofits, and public-benefit organisations |
| `productivity-and-collaboration.yaml` | 39 | Documents, design, chat, files, and how teams work together |
| `security-and-privacy.yaml` | 54 | Security products, identity, and privacy-first software |
| `workforce-and-hr.yaml` | 76 | Payroll, benefits, hiring, and employment infrastructure |

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
is `remotecom`. Across the sweeps that built these files, **roughly six in ten
guesses were wrong** — they 404, or they belong to somebody else.

Two scripts exist so that guesses become facts before they reach a file:

```bash
./scripts/find-boards.py names.tsv   # sector<TAB>Company -> the board, if any
./scripts/verify-seeds.py            # re-check everything already listed
```

`find-boards.py` tries the slugs a company plausibly registered against every
vendor and reports only what answered.

### Verification is not uniform

The vendors differ in what they will tell you, and that matters more than the
hit rate:

| Vendor | States its own name | Check available |
|---|---|---|
| Greenhouse | Yes | Name match |
| Workable | Yes | Name match |
| SmartRecruiters | Yes | Name match |
| Ashby | Sometimes | Name match where the board sets a page title |
| Lever | No | Slug only |

A **name match is the only check that catches a slug belonging to a different
company** — the failure that put Element Solutions' board under Element during
early resolver testing. Where no name is published, the entry carries a note
saying so, and the slug being an exact normalisation of the company name makes
a collision unlikely rather than impossible. Those entries deserve a human
glance.

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
