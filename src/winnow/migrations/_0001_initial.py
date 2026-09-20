"""Initial schema.

Two shapes here are deliberate and were chosen because retrofitting them is
expensive:

* ``company_boards.identifier`` is JSON, not a slug column. A Workday board is a
  (tenant, datacenter, site) triple and Teamtailor turned up unplanned during
  resolver testing, so the vendor list is a registry rather than an enum.
* ``decisions`` and ``scores`` key on the cluster, not on ``(source, source_id)``.
  A role passed on must not return via a second source or as a repost six weeks
  later.
"""

SQL = """
CREATE TABLE companies (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    excluded       INTEGER NOT NULL DEFAULT 0,
    exclude_reason TEXT,
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 1Password trades as AgileBits; aggregators spell companies their own way.
-- Unmatched names get their own company row rather than a guessed match.
CREATE TABLE company_aliases (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    alias      TEXT NOT NULL UNIQUE
);

CREATE TABLE company_boards (
    id                     INTEGER PRIMARY KEY,
    company_id             INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    vendor                 TEXT NOT NULL,
    identifier             TEXT NOT NULL,
    source                 TEXT NOT NULL
                           CHECK (source IN ('manual', 'greenhouse_name_match',
                                             'human_confirmed_probe')),
    confidence             REAL,
    status                 TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'stale', 'retired')),
    verified_at            TEXT,
    last_ok_at             TEXT,
    consecutive_empty_polls INTEGER NOT NULL DEFAULT 0,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (vendor, identifier)
);

CREATE TABLE clusters (
    id                  INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    normalized_title    TEXT NOT NULL,
    location_class      TEXT NOT NULL
                        CHECK (location_class IN ('US_REMOTE', 'US_HYBRID', 'US_ONSITE',
                                                  'NON_US', 'UNKNOWN')),
    canonical_posting_id INTEGER REFERENCES postings(id) ON DELETE SET NULL,
    repost_count        INTEGER NOT NULL DEFAULT 0,
    first_seen_at       TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (company_id, normalized_title, location_class)
);

CREATE TABLE postings (
    id                     INTEGER PRIMARY KEY,
    source                 TEXT NOT NULL,
    source_id              TEXT NOT NULL,
    board_id               INTEGER REFERENCES company_boards(id) ON DELETE SET NULL,
    company_id             INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    cluster_id             INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
    previous_posting_id    INTEGER REFERENCES postings(id) ON DELETE SET NULL,
    fingerprint            TEXT NOT NULL,
    company                TEXT NOT NULL,
    title                  TEXT NOT NULL,
    source_url             TEXT NOT NULL,
    apply_url              TEXT,
    discovered_via         TEXT NOT NULL,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT NOT NULL,
    disappeared_at         TEXT,
    posted_at              TEXT,
    posted_at_precision    TEXT NOT NULL
                           CHECK (posted_at_precision IN ('exact', 'day', 'relative', 'unknown')),
    posted_at_text         TEXT,
    updated_at             TEXT,
    remote                 TEXT NOT NULL
                           CHECK (remote IN ('REMOTE', 'HYBRID', 'ONSITE', 'UNKNOWN')),
    remote_source          TEXT NOT NULL
                           CHECK (remote_source IN ('structured', 'location_string',
                                                    'description_text', 'absent')),
    locations              TEXT NOT NULL DEFAULT '[]',
    employment_type        TEXT NOT NULL
                           CHECK (employment_type IN ('FULL_TIME', 'CONTRACT', 'PART_TIME',
                                                      'INTERN', 'UNKNOWN')),
    employment_type_source TEXT NOT NULL
                           CHECK (employment_type_source IN ('structured', 'custom_field',
                                                             'text', 'absent')),
    comp_min               INTEGER,
    comp_max               INTEGER,
    comp_currency          TEXT,
    comp_interval          TEXT NOT NULL
                           CHECK (comp_interval IN ('YEAR', 'HOUR', 'UNKNOWN')),
    comp_source            TEXT NOT NULL
                           CHECK (comp_source IN ('stated', 'parsed', 'predicted',
                                                  'withheld', 'absent')),
    description_text       TEXT,
    description_complete   INTEGER NOT NULL DEFAULT 0,
    department             TEXT,
    team                   TEXT,
    raw                    TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source, source_id)
);

CREATE INDEX postings_cluster ON postings(cluster_id);
CREATE INDEX postings_fingerprint ON postings(fingerprint);

-- Written at scoring time and never updated. Holds the breakdown rather than
-- only the total: a final score cannot be reverse-engineered into the factors
-- that produced it, and without those, calibration is impossible.
CREATE TABLE scores (
    id             INTEGER PRIMARY KEY,
    cluster_id     INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    posting_id     INTEGER REFERENCES postings(id) ON DELETE SET NULL,
    score          INTEGER NOT NULL,
    breakdown      TEXT NOT NULL,
    vetoes         TEXT NOT NULL DEFAULT '[]',
    flags          TEXT NOT NULL DEFAULT '[]',
    why_fits       TEXT,
    concern        TEXT,
    rubric_version TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX scores_cluster ON scores(cluster_id);

-- Structured reasons are what make the signal aggregable; the free-text note is
-- what makes it intelligible six months later.
CREATE TABLE decisions (
    id         INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    decision   TEXT NOT NULL CHECK (decision IN ('interested', 'pass', 'defer')),
    reason     TEXT CHECK (reason IN ('comp_too_low', 'rto_suspected', 'no_growth_signal',
                                      'boring_domain', 'bad_culture_signal',
                                      'title_beneath_target', 'already_applied', 'timing')),
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX decisions_cluster ON decisions(cluster_id);

-- The only ground truth in the system. Everything else is opinion.
CREATE TABLE outcomes (
    id             INTEGER PRIMARY KEY,
    cluster_id     INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    outcome        TEXT NOT NULL CHECK (outcome IN ('no_response', 'auto_reject',
                                                    'recruiter_screen', 'interview',
                                                    'offer', 'withdrawn')),
    occurred_on    TEXT,
    -- Verbatim when the employer states one; the highest-value text in the
    -- system and the part nobody logs by hand.
    rejection_text TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-source outcome per run, so the digest can distinguish "no new matches"
-- from "Greenhouse did not answer".
CREATE TABLE poll_runs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    board_id      INTEGER REFERENCES company_boards(id) ON DELETE SET NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT NOT NULL CHECK (outcome IN ('ok', 'partial', 'failed')),
    postings_seen INTEGER NOT NULL DEFAULT 0,
    error         TEXT
);

CREATE TABLE drafts (
    id              INTEGER PRIMARY KEY,
    cluster_id      INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CHECK (kind IN ('cover_letter', 'recruiter_reply',
                                                  'follow_up')),
    resume_variant  TEXT,
    recipient       TEXT,
    subject         TEXT,
    body            TEXT NOT NULL,
    built_from      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at         TEXT,
    sent_message_id TEXT
);

-- Inbound employer mail, correlated back to an application by thread. Anything
-- the classifier is unsure about stays unclassified and surfaces in review
-- rather than being guessed at.
CREATE TABLE messages (
    id             INTEGER PRIMARY KEY,
    message_id     TEXT NOT NULL UNIQUE,
    in_reply_to    TEXT,
    references_ids TEXT,
    direction      TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    company_id     INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    cluster_id     INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
    from_addr      TEXT,
    subject        TEXT,
    received_at    TEXT,
    classification TEXT CHECK (classification IN ('confirmation', 'rejection',
                                                  'recruiter_outreach', 'scheduling',
                                                  'other', 'unclassified')),
    confidence     REAL,
    body_text      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The ambiguous fuzzy-match band. A false merge hides a real job permanently
-- and silently; a false split costs one redundant digest line. So the pair goes
-- to a human instead of being decided.
CREATE TABLE merge_confirmations (
    id         INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    other_id   INTEGER NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    similarity REAL NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'merged', 'split')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (posting_id, other_id)
);
"""
