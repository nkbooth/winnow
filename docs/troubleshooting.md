---
title: "Troubleshooting"
description: "Fixes for the failures winnow reports: empty digests, boards that will not add, missing credentials, and drafts that cannot be sent."
docType: "troubleshooting"
lastVerified: "2026-09-20"
weight: 90
---

# Troubleshoot winnow

Winnow reports failures rather than absorbing them, so most problems arrive as
a specific message. Find yours below.

## The digest is empty

**Symptoms:** `run` completes and reports no items.

**Cause:** Usually correct. Most postings on most days are not for you, and the
gate tally shows what happened to them.

**Fix:**

1. Read the tally. `gated 1858 title_off_target` means your title anchors are
   doing their job.
2. If everything is gated by one rule, that rule is too tight. Lower
   `score_threshold` from the review screen (`t`), or widen `titles.primary`.
3. Confirm boards are answering: `uv run winnow company list`.

**Verify:** `uv run winnow fetch` prints a board count above zero and a tally.

---

## A board stopped appearing

**Symptoms:** The digest footer says `workday did not answer`.

**Cause:** That board failed this run. Winnow names it instead of reporting a
quiet day, because a silent board and a hiring freeze look identical.

**Fix:** Usually transient — wait for the next run. If it persists, open the
board URL in a browser. Vendors change endpoints and slugs rot.

**Verify:** The message disappears from the footer.

---

## A company has no board winnow can read

**Symptoms:** `ProbeFailed: ... publishes no unauthenticated listing endpoint`,
or the careers page never leaves the company's own domain.

**Cause:** Some vendors, including Teamtailor and HiBob, only serve job data to
credentials issued by that employer. Others render entirely in the browser.

**Fix:**

1. Check whether they post to a readable board elsewhere — some companies
   syndicate to Greenhouse while showing a custom page.
2. If not, they cannot be polled. Winnow refuses at add time rather than
   recording a board that returns nothing forever, which would read as a company
   that stopped hiring.

**Verify:** `winnow company list` shows only boards that answer.

---

## `environment variable ANTHROPIC_API_KEY is unset or empty`

**Symptoms:** Scoring or drafting fails immediately.

**Cause:** No credential resolved. Absent and empty are both errors, raised at
resolution rather than passed on as an empty string.

**Fix:**

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or point `[llm] api_key_ref` at `file:/path` or `op://vault/item/field`.

**Verify:** `uv run winnow score` runs without raising.

---

## `'hunter2' names no credential backend`

**Symptoms:** Startup fails on a key ending in `_ref`.

**Cause:** A literal value where a reference belongs. Refused deliberately —
accepting it would make the easiest thing to write in a config file also the
thing that commits a password to a repository.

**Fix:** Replace it with `env:NAME`, `file:/path` or `op://vault/item/field`.

---

## `o` does nothing in the review screen

**Symptoms:** Pressing `o` over SSH opens no browser.

**Cause:** Review runs where the database is. A server has no display, so there
is no browser to open.

**Fix:** None needed. Winnow copies the link to your clipboard with OSC 52,
which travels back over SSH, and prints the URL so you can select it if your
terminal blocks OSC 52.

**Verify:** The status line says `link copied to your clipboard`.

---

## `s` refuses to send a draft

**Symptoms:** `this draft has no recipient`.

**Cause:** Most postings have no email address. The draft is a cover letter to
paste into the employer's form, and winnow says so rather than pretending it
sent something.

**Fix:** Paste it into their form, then press `a` to record the application.
Without that keystroke the application never enters the statistics, and its
silence never counts against the score that produced it.

**Verify:** The role moves to the `applied` list (`v`).

---

## Nothing appears in the working set after pressing `i`

**Symptoms:** A role marked interested is not in any list.

**Cause:** Check which list you are viewing. `v` cycles: review, interested,
applied, deferred.

**Fix:** Press `v` once from the review queue.

---

## Get help

Open an issue at https://github.com/nkbooth/winnow/issues. Include the command
you ran and the message winnow printed — the messages are written to be
specific enough to act on.
