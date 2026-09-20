"""Filesystem locations, and the settings found at them.

Defaults follow the XDG layout, so a fresh install has somewhere to put things
without being told. Every path takes an environment override, which is what
keeps tests and one-off runs away from a real store.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from winnow.settings import Settings
from winnow.settings import load as load_settings

DB_ENV_VAR = "WINNOW_DB"
PROFILE_ENV_VAR = "WINNOW_PROFILE"
ASSETS_ENV_VAR = "WINNOW_ASSETS"
CONFIG_ENV_VAR = "WINNOW_CONFIG"
SEEDS_ENV_VAR = "WINNOW_SEEDS"

_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")

_DEFAULT_DB = _XDG_DATA / "winnow" / "winnow.db"
_DEFAULT_CONFIG_DIR = _XDG_CONFIG / "winnow"
_DEFAULT_PROFILE = _DEFAULT_CONFIG_DIR / "profile.yaml"


def db_path() -> Path:
    """Return the SQLite store's location."""
    override = os.environ.get(DB_ENV_VAR)
    return Path(override) if override else _DEFAULT_DB


def profile_path() -> Path:
    """Return the location of ``profile.yaml``, the scoring rubric.

    The output of the intake interview, edited by hand and by the review TUI,
    and the one source of truth for every weight and threshold.
    """
    override = os.environ.get(PROFILE_ENV_VAR)
    return Path(override) if override else _DEFAULT_PROFILE


def assets_path() -> Path:
    """Return the directory holding the drafting material.

    The cover-letter paragraphs, the work inventory and the resume variants —
    the only things a draft is allowed to make claims from. Kept beside the
    rubric, since they are the same kind of thing: yours, not the code's.
    """
    override = os.environ.get(ASSETS_ENV_VAR)
    return Path(override) if override else _DEFAULT_CONFIG_DIR / "assets"


def seeds_path() -> Path:
    """Return the directory holding the shipped seed lists.

    Looked for where a package or image would have put them before falling
    back to a checkout, so ``--from developer-tools`` works the same whether
    winnow was installed, containerised, or is being run from a clone.
    """
    override = os.environ.get(SEEDS_ENV_VAR)
    if override:
        return Path(override)
    for candidate in (Path("/usr/share/winnow/seeds"), Path("seeds")):
        if candidate.is_dir():
            return candidate
    return Path("seeds")


def config_path() -> Path:
    """Return the location of ``config.toml``, the infrastructure settings."""
    override = os.environ.get(CONFIG_ENV_VAR)
    return Path(override) if override else _DEFAULT_CONFIG_DIR / "config.toml"


def settings() -> Settings:
    """Load the settings, reading the file once per process.

    Cached because it is consulted from adapters on every request, and a config
    file that changes mid-run would make a poll behave differently at its end
    than at its start.
    """
    return _cached_settings(str(config_path()))


@lru_cache(maxsize=4)
def _cached_settings(path: str) -> Settings:
    return load_settings(Path(path))
