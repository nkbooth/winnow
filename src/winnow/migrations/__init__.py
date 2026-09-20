"""Numbered schema migrations.

Each migration is a module holding one ``SQL`` string, applied in order inside a
transaction. Migrations are carried as Python rather than as ``.sql`` data files
so that importing the package is enough to reach them, with no packaging or
resource-loading step to get wrong.
"""

from winnow.migrations import (
    _0001_initial,
    _0002_comp_tier,
    _0003_rejection_tally,
    _0004_submitted_at,
    _0005_final_body,
    _0006_external_feedback,
    _0007_wrong_function,
)

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _0001_initial.SQL),
    (2, _0002_comp_tier.SQL),
    (3, _0003_rejection_tally.SQL),
    (4, _0004_submitted_at.SQL),
    (5, _0005_final_body.SQL),
    (6, _0006_external_feedback.SQL),
    (7, _0007_wrong_function.SQL),
)
