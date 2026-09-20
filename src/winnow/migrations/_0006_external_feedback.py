"""Allow a pass on evidence the agent could never have seen.

The existing reasons all name something in the posting: the comp, the title,
the culture language, the return-to-office hints. They assume the decision was
made from what winnow showed. Some are not — a word from someone who worked
there, a recruiter's aside, something heard at a meetup. That is the most
valuable reason of the set, because it is the only one that cannot be derived
from the posting text, and recording it as `timing` or `bad_culture_signal`
would put it in the calibration bucket of a signal the scorer *could* have
caught, and so read as a scoring miss rather than as outside knowledge.

SQLite cannot alter a CHECK constraint, so the table is rebuilt. Nothing holds
a foreign key into `decisions`, which is what makes that safe here.
"""

SQL = """
CREATE TABLE decisions_rebuilt (
    id         INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    decision   TEXT NOT NULL CHECK (decision IN ('interested', 'pass', 'defer')),
    reason     TEXT CHECK (reason IN ('comp_too_low', 'rto_suspected', 'no_growth_signal',
                                      'boring_domain', 'bad_culture_signal',
                                      'title_beneath_target', 'already_applied', 'timing',
                                      'external_feedback')),
    note       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO decisions_rebuilt (id, cluster_id, decision, reason, note, created_at)
    SELECT id, cluster_id, decision, reason, note, created_at FROM decisions;

DROP TABLE decisions;

ALTER TABLE decisions_rebuilt RENAME TO decisions;

CREATE INDEX decisions_cluster ON decisions(cluster_id);
"""
