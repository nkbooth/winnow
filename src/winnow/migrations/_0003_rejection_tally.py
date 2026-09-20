"""Record why a poll rejected what it rejected.

The digest has to distinguish "no new matches" from "Greenhouse did not
answer", and it has to be able to say "4 vetoed (3 RTO, 1 comp floor)" without
claiming more than it knows. A per-run tally is enough for both, and it stays
bounded — storing every rejected posting would be thousands of rows a week,
almost all of them engineering jobs that were never candidates.
"""

SQL = """
ALTER TABLE poll_runs ADD COLUMN rejection_tally TEXT NOT NULL DEFAULT '{}';
"""
