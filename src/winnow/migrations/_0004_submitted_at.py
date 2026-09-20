"""Record when an application was actually submitted.

``sent_at`` means winnow put the draft in the post itself, which is the rarer
case: most drafts are cover letters pasted into an employer's own form, and
that act is invisible from here. Without a mark for it, the only applications
the system knew about were the ones it had emailed *and* had been answered —
and calibration was dividing interviews by that population, which is the one
population guaranteed to flatter every score band.

``submitted_at`` is the application. ``sent_at`` stays what it was: the record
of an outbound email, which is a different fact about a different act.
"""

SQL = """
ALTER TABLE drafts ADD COLUMN submitted_at TEXT;

-- Every draft this store already sent was, by definition, submitted.
UPDATE drafts SET submitted_at = sent_at WHERE sent_at IS NOT NULL;
"""
