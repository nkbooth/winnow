"""Keep what was actually sent, not only what was proposed.

A draft is a proposal. What goes to an employer is the version after a human
has cut the paragraph that overclaimed and rewritten the opening. The gap
between the two is the most direct evidence in the system of what the drafter
gets wrong, and it was being thrown away every time.

``body`` stays the proposal, so the pair remains comparable. ``final_body`` is
what was sent, and ``final_source`` records how it was learned: pasted in
during review, or read back out of the Sent folder.
"""

SQL = """
ALTER TABLE drafts ADD COLUMN final_body TEXT;
ALTER TABLE drafts ADD COLUMN final_source TEXT
    CHECK (final_source IS NULL OR final_source IN ('pasted', 'sent_folder'));
"""
