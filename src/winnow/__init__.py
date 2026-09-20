"""winnow — a daily job-board agent.

The deterministic half of the system described in
``~/Documents/notes/projects/job-search/``: it fetches ATS boards, normalises
payloads, gates and clusters them, asks Claude for the judgment calls only,
and delivers one ranked digest.
"""

__version__ = "0.1.0"
