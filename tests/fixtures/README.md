# Adapter fixtures

Real payloads captured **2026-09-18**. Every normalisation gotcha in
`notes/projects/job-search/source-adapter-contract.md` is reproducible from these,
offline, with no network.

| File | Source | Exercises |
|---|---|---|
| `greenhouse_tailscale_jobs.json` | Greenhouse, 55 jobs | **Double entity-escaped `content`**; `metadata[]` custom Employment Type; remote inferable only from `location.name`; `first_published` vs `updated_at`. Also the dedupe case: **29 distinct titles across 55 postings**, split per country. |
| `greenhouse_tailscale_board.json` | Greenhouse board | `name: "Tailscale"` — the only vendor that self-identifies, used for resolver auto-accept |
| `greenhouse_404_elementsolutions.json` | Greenhouse | Error shape for a slug that exists on another vendor |
| `lever_elementsolutions_postings.json` | Lever, 2 postings | **`salaryRange` structured comp**; `workplaceType`; `categories.commitment`; `createdAt` in **epoch milliseconds** |
| `ashby_roboflow_prose_comp.json` | Ashby, 1 job (captured 2026-09-20) | **`shouldDisplayCompensationOnJobPostings: false` with the salary written into the body.** The flag hides the structured field; it says nothing about whether the employer mentioned pay. Reporting WITHHELD for one of these claims a salary was deliberately unpublished when it is on the page |
| `ashby_clickhouse_jobs.json` | Ashby, 202 jobs (captured 2026-09-20) | **Compensation, which the endpoint omits entirely unless `includeCompensation=true` is asked for.** 158 publish a salary, 44 withhold one, 42 span multiple tiers. Captured from the URL the adapter actually requests — the earlier Ashby fixture was not, which is how the missing parameter survived. |
| `ashby_1password_jobs.json` | Ashby, 62 jobs | `employmentType` enum; `isRemote` boolean; `isListed`; **`shouldDisplayCompensationOnJobPostings: false`** (withheld ≠ absent); zero within-board duplication |
| `smartrecruiters_bosch_postings.json` | SmartRecruiters, 20 of 4,819 (captured 2026-09-19) | `company.name` self-identifies; `location.remote` and `location.hybrid` as **booleans**; `typeOfEmployment.label`; no description in the list payload |
| `smartrecruiters_bosch_detail.json` | SmartRecruiters detail | `jobAd.sections.{jobDescription,qualifications,additionalInformation,companyDescription}` as HTML, plus `applyUrl` |
| `rippling_framework_jobs.json` | Rippling, 1 job (captured 2026-09-19) | The thinnest list payload of any vendor: name, department, one location string, nothing else |
| `rippling_framework_detail.json` | Rippling detail | `companyName` (with trailing space), `createdOn`, `description.{role,company}` as HTML, `employmentType` where `label` is the code and `id` is the prose, `payRangeDetails` empty when unset |
| `workable_nuvei_widget.json` | Workable, 75 rows / 61 jobs (captured 2026-09-19) | The careers-widget endpoint with `details=true`: complete descriptions, `telecommuting` boolean, structured `locations[]`, `published_on` as a bare date. **One row per location**: 8 shortcodes repeat, sharing a URL, so the adapter merges before the store sees a collision |
| `workable_fastmail_widget.json` | Workable, 0 jobs (captured 2026-09-19) | The other half of the pair. An empty board is readable and empty — the state an earlier pass mistook for an unreadable vendor |
| `careers_nextcloud.html` | Nextcloud careers page (captured 2026-09-19) | No ATS at all: WordPress accordion panels under `#openpositions`, `<h4>` titles against `Responsibilities`/`Requirements`/`Benefits` sub-headings, one shared `mailto:` and no per-job URL |
| `careers_signal.html` | Signal careers page (captured 2026-09-19) | A page with no postings on it, kept so the "we do not have any open roles" sentinel can be tested — the parser fails the day it disappears rather than reporting zero |
| `workday_redhat_jobs.json` | Workday, 20 of 148 | POST body shape; **`postedOn` as relative prose**; `remoteType` structured; no description in list payload (drives lazy detail fetch) |

Boards change. These are a dated snapshot precisely so tests do not drift with them —
do not refresh them casually, and if you do, re-check the gotchas above still reproduce.

**Capture from the URL the adapter requests, not a URL you composed by hand.** An Ashby
fixture captured with `includeCompensation=true`, against an adapter that requested the
endpoint without it, made every published salary invisible in production while the tests
stayed green for weeks. A fixture from a different URL is a test of something nobody runs.
