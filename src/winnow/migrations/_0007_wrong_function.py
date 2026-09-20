"""Allow a pass that says the title gate misfired.

Every other reason is about the job: the comp, the seniority, the culture, the
industry. This one is about winnow. "Business operations" is an anchor in the
title vocabulary and it also names an event coordinator's job, so the gate
surfaces roles that were never the right kind of work at all.

Distinct from ``boring_domain``, which means the industry does not interest
him — a real match he does not want. This means no match was made. Filing the
second as the first would hide the only signal in the set that says the anchor
vocabulary is too loose, and the vocabulary is a thing he can go and change.
"""

SQL = """
CREATE TABLE decisions_rebuilt (
    id         INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    decision   TEXT NOT NULL CHECK (decision IN ('interested', 'pass', 'defer')),
    reason     TEXT CHECK (reason IN ('comp_too_low', 'rto_suspected', 'no_growth_signal',
                                      'boring_domain', 'bad_culture_signal',
                                      'title_beneath_target', 'already_applied', 'timing',
                                      'external_feedback', 'wrong_function')),
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO decisions_rebuilt (id, cluster_id, decision, reason, note, created_at)
    SELECT id, cluster_id, decision, reason, note, created_at FROM decisions;

DROP TABLE decisions;

ALTER TABLE decisions_rebuilt RENAME TO decisions;

CREATE INDEX decisions_cluster ON decisions(cluster_id);
"""
