"""Credential resolution, and the one convention op does not implement.

`op` reads its service-account token from `OP_SERVICE_ACCOUNT_TOKEN` — the
value, in the environment. It ignores `OP_SERVICE_ACCOUNT_TOKEN_FILE`, which
was verified rather than assumed: with a garbage file it silently fell back to
the desktop app instead of failing.

Putting the token in a unit file's `Environment=` would mean the secret sits in
`systemctl show`, the journal, and every `ps` listing on the host. So the units
name a file and this module reads it.
"""

import subprocess

import pytest

from winnow import secrets


@pytest.fixture
def fake_op(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(cmd, 0, stdout="resolved-value\n", stderr="")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    return calls


def test_a_reference_resolves(fake_op):
    assert secrets.op_read("op://v/i/f") == "resolved-value"
    assert fake_op[0]["cmd"][:2] == ["op", "read"]


def test_a_token_file_is_read_into_the_environment(fake_op, tmp_path, monkeypatch):
    token = tmp_path / "op-token"
    token.write_text("ops_thetoken\n")
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_FILE", str(token))

    secrets.op_read("op://v/i/f")

    assert fake_op[0]["env"]["OP_SERVICE_ACCOUNT_TOKEN"] == "ops_thetoken"


def test_an_existing_token_in_the_environment_wins(fake_op, tmp_path, monkeypatch):
    """An operator who exported a token meant it; the file is the fallback."""
    token = tmp_path / "op-token"
    token.write_text("ops_fromfile")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_fromenv")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_FILE", str(token))

    secrets.op_read("op://v/i/f")

    assert fake_op[0]["env"] is None, "the ambient environment is used unchanged"


def test_a_missing_token_file_is_an_error_not_a_silent_fallback(monkeypatch, tmp_path):
    """Falling through to the desktop app is exactly what unattended cannot do."""
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_FILE", str(tmp_path / "absent"))

    with pytest.raises(RuntimeError, match="OP_SERVICE_ACCOUNT_TOKEN_FILE"):
        secrets.op_read("op://v/i/f")


def test_a_failing_op_surfaces_its_error(monkeypatch):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such vault")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="no such vault"):
        secrets.op_read("op://nope/i/f")


def test_an_empty_value_is_an_error(monkeypatch):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="  \n", stderr="")

    monkeypatch.setattr(secrets.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="empty"):
        secrets.op_read("op://v/i/f")


# ---------------------------------------------------------------------------
# Resolving a reference by its scheme, so 1Password is one option and not the
# only one.
# ---------------------------------------------------------------------------


def test_an_environment_reference_reads_the_environment(monkeypatch):
    """The zero-dependency option: anyone can set a variable."""
    monkeypatch.setenv("WINNOW_TEST_SECRET", "hunter2")

    assert secrets.resolve("env:WINNOW_TEST_SECRET") == "hunter2"


def test_a_missing_environment_variable_is_an_error_not_an_empty_string(monkeypatch):
    """An empty credential fails at authentication, a long way from its cause."""
    monkeypatch.delenv("WINNOW_TEST_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="WINNOW_TEST_SECRET"):
        secrets.resolve("env:WINNOW_TEST_SECRET")


def test_a_file_reference_reads_the_file_and_strips_the_newline(tmp_path):
    """Podman secrets, Kubernetes secrets and systemd credentials are all files."""
    path = tmp_path / "token"
    path.write_text("s3cret\n")

    assert secrets.resolve(f"file:{path}") == "s3cret"


def test_a_missing_file_says_which_file(tmp_path):
    with pytest.raises(RuntimeError, match="nowhere"):
        secrets.resolve(f"file:{tmp_path / 'nowhere'}")


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "empty"
    path.write_text("\n")

    with pytest.raises(RuntimeError):
        secrets.resolve(f"file:{path}")


def test_a_one_password_reference_still_goes_to_the_cli(monkeypatch):
    """The existing deployment keeps working, unchanged."""
    asked: list[str] = []
    monkeypatch.setattr(secrets, "op_read", lambda ref: asked.append(ref) or "from-1password")

    assert secrets.resolve("op://vault/item/field") == "from-1password"
    assert asked == ["op://vault/item/field"]


def test_a_literal_value_is_refused_rather_than_guessed(monkeypatch):
    """A bare string in a config file is almost always a pasted secret.

    Accepting it would make the easiest thing to write also the thing that
    commits a password to a repository. The error names the schemes instead.
    """
    with pytest.raises(ValueError, match="env:|file:|op://"):
        secrets.resolve("hunter2")
