"""Credential resolution.

Nothing in this project stores a secret. Every credential is a *reference*
resolved at the moment it is used, which is what makes the credential boundary
real: the digest timer and the inbound listener are handed references to the
things they need and to nothing else, so a bug in either cannot send mail
however it misbehaves.

A reference names its own backend by scheme, so no separate setting decides
which one is in use:

``env:NAME``
    An environment variable. The option that needs nothing installed, and the
    one a container platform, a systemd unit or a shell already knows how to
    provide.
``file:/path``
    A file's contents. Podman secrets, Kubernetes secrets and systemd
    credentials all arrive this way.
``op://vault/item/field``
    1Password, through its CLI. Useful where one already exists, and the only
    backend here that can be scoped per service account.

A bare string is refused rather than treated as the value. Accepting it would
make the easiest thing to write in a config file also the thing that commits a
password to a repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: `op` reads its service-account token from the environment as a value. It has
#: no file equivalent — verified, not assumed: given a garbage file it silently
#: falls back to the desktop app, which is precisely what an unattended process
#: must never do. Putting the token in a unit's `Environment=` instead would
#: leave it in `systemctl show`, the journal and every `ps` listing, so the
#: units name a file and this module reads it.
TOKEN_ENV_VAR = "OP_SERVICE_ACCOUNT_TOKEN"
TOKEN_FILE_ENV_VAR = "OP_SERVICE_ACCOUNT_TOKEN_FILE"


def resolve(reference: str) -> str:
    """Resolve a credential reference through the backend its scheme names.

    Args:
        reference: ``env:NAME``, ``file:/path`` or ``op://vault/item/field``.

    Returns:
        The secret value.

    Raises:
        ValueError: If the reference names no known scheme.
        RuntimeError: If the backend cannot produce a value. Every backend
            treats absent and empty the same way and fails here, because a
            silently empty credential becomes an authentication error much
            later and a long way from its cause.
    """
    if reference.startswith("op://"):
        return op_read(reference)
    if reference.startswith("env:"):
        return _from_environment(reference[len("env:") :])
    if reference.startswith("file:"):
        return _from_file(reference[len("file:") :])
    raise ValueError(
        f"{reference!r} names no credential backend — use env:NAME, file:/path "
        "or op://vault/item/field. A bare value is refused so that a config "
        "file cannot become the place a password is stored."
    )


def _from_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"environment variable {name} is unset or empty")
    return value


def _from_file(path: str) -> str:
    try:
        value = Path(path).read_text().strip()
    except OSError as error:
        raise RuntimeError(f"cannot read credential file {path}: {error}") from error
    if not value:
        raise RuntimeError(f"credential file {path} is empty")
    return value


def op_read(reference: str) -> str:
    """Resolve one ``op://`` reference through the 1Password CLI.

    Args:
        reference: An ``op://vault/item/field`` reference.

    Returns:
        The secret value.

    Raises:
        RuntimeError: If the CLI fails, the reference resolves empty, or a named
            token file is unreadable. All three are failures to surface rather
            than defaults to fall back on — a silent empty credential turns into
            an authentication error much later, a long way from its cause.
    """
    result = subprocess.run(
        ["op", "read", reference],
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"op read failed for {reference}: {result.stderr.strip()}")
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"op read returned empty value for {reference}")
    return value


def _environment() -> dict[str, str] | None:
    """Build the environment `op` should run with.

    Returns:
        ``None`` to inherit the ambient environment, which is the normal case
        and what an interactive session wants. A copy carrying the token when
        the deployment named a token file instead — an operator who exported a
        token meant it, so the file is only ever the fallback.

    Raises:
        RuntimeError: If the named file cannot be read. Continuing would let
            `op` fall through to a desktop app that is not running, and fail
            somewhere less obvious.
    """
    if os.environ.get(TOKEN_ENV_VAR):
        return None

    token_file = os.environ.get(TOKEN_FILE_ENV_VAR)
    if not token_file:
        return None

    try:
        token = Path(token_file).read_text().strip()
    except OSError as error:
        raise RuntimeError(f"{TOKEN_FILE_ENV_VAR}={token_file} is unreadable: {error}") from error
    if not token:
        raise RuntimeError(f"{TOKEN_FILE_ENV_VAR}={token_file} is empty")

    return {**os.environ, TOKEN_ENV_VAR: token}
