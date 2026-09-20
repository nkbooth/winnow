"""The deployment units.

The credential split is architectural rather than behavioural: the two
unattended processes are not trusted to refrain from sending mail, they are
given nothing that could send it. That property lives in these unit files, so
it is asserted here — a later edit that hands the digest timer the mail token
would otherwise be invisible until it mattered.
"""

import re
from pathlib import Path

import pytest

DEPLOY = Path("deploy")

DIGEST = DEPLOY / "winnow-digest.container"
LISTENER = DEPLOY / "winnow-listener.container"
TIMER = DEPLOY / "winnow-digest.timer"


@pytest.mark.parametrize("unit", [DIGEST, LISTENER, TIMER])
def test_the_units_exist(unit):
    assert unit.is_file()


def test_the_digest_timer_cannot_reach_the_mailbox_credential():
    """The wall, not the policy: no mail token in this process's environment."""
    text = DIGEST.read_text()
    assert "op-token-mail" not in text
    assert "winnow-mail" not in text
    assert "op-token-digest" in text


def test_the_listener_gets_the_mailbox_but_is_still_send_less():
    text = LISTENER.read_text()
    exec_line = next(line for line in text.splitlines() if line.startswith("Exec="))
    assert "op-token-mail" in text
    assert exec_line == "Exec=listen"


def test_the_digest_runs_the_whole_day_in_one_process():
    """A shell pipeline in a unit file would need to override the entrypoint."""
    text = DIGEST.read_text()
    assert next(line for line in text.splitlines() if line.startswith("Exec=")) == "Exec=run"


def test_the_token_is_named_as_a_file_never_inlined():
    """Environment= would put it in systemctl show, the journal and every ps."""
    for unit in (DIGEST, LISTENER):
        text = unit.read_text()
        assert "OP_SERVICE_ACCOUNT_TOKEN_FILE=" in text
        inlined = text.replace("OP_SERVICE_ACCOUNT_TOKEN_FILE=", "")
        assert "OP_SERVICE_ACCOUNT_TOKEN=" not in inlined


def test_the_units_are_rootless_like_the_rest_of_the_host():
    """Everything else on the host is a user quadlet; consistency is the point."""
    for unit in (DIGEST, LISTENER):
        text = unit.read_text()
        assert "%h/.config/winnow" in text
        assert "/etc/containers/systemd" not in text


def test_the_digest_runs_as_a_oneshot_under_a_timer():
    """A timer, not a service: nothing to keep alive, nothing to watch die."""
    assert "Type=oneshot" in DIGEST.read_text()
    assert "OnCalendar=" in TIMER.read_text()
    assert "Persistent=true" in TIMER.read_text()


def test_the_store_is_on_a_persistent_mount_in_both_units():
    for unit in (DIGEST, LISTENER):
        assert "/var/lib/winnow" in unit.read_text()


def test_the_containerfile_builds_a_runtime_without_a_toolchain():
    text = (Path("Containerfile")).read_text()
    assert text.count("FROM") >= 2
    assert "ubi-minimal" in text
    assert "USER" in text


def test_both_units_pull_the_same_published_image():
    """A drift between the two would deploy two different winnows."""
    images = {
        next(line for line in unit.read_text().splitlines() if line.startswith("Image="))
        for unit in (DIGEST, LISTENER)
    }
    assert images == {"Image=ghcr.io/CHANGEME/winnow:latest"}


def test_the_units_authenticate_to_the_private_registry():
    """The package is private because the repository is.

    Passed as a podman argument, not the `AuthFile=` key: podman 5.8's quadlet
    rejects that key outright, and a unit that fails to generate is a unit that
    does not exist.
    """
    for unit in (DIGEST, LISTENER):
        text = unit.read_text()
        directives = [line for line in text.splitlines() if line and not line.startswith("#")]
        assert "PodmanArgs=--authfile=%h/.config/winnow/registry-auth.json" in directives
        assert not any(line.startswith("AuthFile=") for line in directives)


def test_the_units_pull_a_newer_image_on_restart():
    """Otherwise the deployed image silently never changes."""
    for unit in (DIGEST, LISTENER):
        assert "Pull=newer" in unit.read_text()


def test_the_units_keep_the_host_user_id():
    """Without this, a core-owned token file is unreadable inside the container."""
    for unit in (DIGEST, LISTENER):
        assert "UserNS=keep-id" in unit.read_text()


def test_the_units_share_the_host_network():
    """The homeserver is on this box's loopback; rootless netns cannot reach it."""
    for unit in (DIGEST, LISTENER):
        assert "Network=host" in unit.read_text()


def test_the_shared_volume_uses_the_shared_selinux_label():
    """`:Z` gives each container a private category pair.

    Two containers mounting one volume with `:Z` relabel it out from under each
    other, and the loser gets EACCES on files it created moments earlier. It
    presented as an intermittent 1Password failure.
    """
    for unit in (DIGEST, LISTENER):
        directives = [line for line in unit.read_text().splitlines() if line.startswith("Volume=")]
        assert directives, "expected volume mounts"
        for line in directives:
            assert not line.endswith(":Z"), line
            assert not line.endswith(",Z"), line


WRAPPER = DEPLOY / "winnow-remote.sh"


def test_the_wrapper_forces_a_terminal_for_the_tui():
    """`tailscale ssh` cannot pass -t, and Textual with TERM=dumb paints nothing."""
    text = WRAPPER.read_text()
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("tailscale ssh" in line for line in code)
    assert "SSH_FLAGS=(-t)" in text
    assert "TERM=" in text


def test_the_wrapper_syncs_the_rubric_both_ways_for_review():
    """profile.yaml lives in the notes repo and on the server.

    The tunables panel writes to the server's copy because that is the one it
    can reach, so the two would fork — and a forked rubric is a scoring history
    that cannot be compared with itself. Syncing is not a step anyone should
    have to remember.
    """
    text = WRAPPER.read_text()
    assert "cat > $REMOTE_PROFILE" in text, "notes copy should be pushed out first"
    assert "cat $REMOTE_PROFILE" in text, "server copy should be pulled back after"
    assert 'cp "$UPDATED" "$PROFILE"' in text


def test_only_review_may_write_the_rubric():
    text = WRAPPER.read_text()
    assert 'CONFIG_MODE="rw"' in text
    assert 'CONFIG_MODE="ro"' in text


def test_only_review_is_given_the_credential_that_can_send():
    """The digest token cannot read the mailbox vault at all; review's can.

    This is the one place that credential reaches an interactive process, and
    the only process with a code path to SMTP.
    """
    text = WRAPPER.read_text()
    assert 'TOKEN="op-token-mail"' in text
    assert 'TOKEN="op-token-digest"' in text
    assert "OP_SERVICE_ACCOUNT_TOKEN_FILE=/etc/winnow/$TOKEN" in text


def test_the_drafting_assets_are_deployed_not_fetched():
    """A draft may only make claims from material that was handed to it."""
    installer = (DEPLOY / "install.sh").read_text()
    for asset in ("work-inventory.md", "cover-letter-assets.md", "resume-a-business-systems.md"):
        assert asset in installer
    assert "WINNOW_ASSETS=/etc/winnow/assets" in WRAPPER.read_text()


FAILURE_UNIT = DEPLOY / "winnow-failure@.service"


def test_both_unattended_units_report_their_own_failure():
    """The digest reports breakage inside a run it finishes.

    A run that dies before reaching the digest, or a listener that exits, says
    nothing at all — which is indistinguishable from a quiet day, and that is
    the one thing this design refuses to allow.
    """
    for unit in (DIGEST, LISTENER):
        assert "OnFailure=winnow-failure@%N.service" in unit.read_text()


def test_the_failure_notifier_exists_and_is_a_oneshot():
    text = FAILURE_UNIT.read_text()
    assert "Type=oneshot" in text
    assert "notify" in text
    assert "%i" in text, "should name the unit that failed"


def test_the_failure_notifier_gets_no_more_credential_than_the_timer():
    """A notifier is not a reason to widen the credential boundary."""
    text = FAILURE_UNIT.read_text()
    assert "op-token-digest" in text
    assert "op-token-mail" not in text
    assert "/var/winnow" not in text, "it does not need the store to say a unit died"


def test_the_wrapper_hands_every_path_the_container_needs():
    """Caught in deployment twice: a path the code defaults to, unmounted.

    Inside the container the operator's home is not the operator's home, so
    every location has to be named explicitly. A missing WINNOW_CONFIG means
    the review screen finds no settings and `s` fails at the point of sending.
    """
    script = Path("deploy/winnow-remote.sh").read_text()

    for variable in ("WINNOW_DB", "WINNOW_PROFILE", "WINNOW_CONFIG", "WINNOW_ASSETS"):
        assert f"-e {variable}=" in script, f"{variable} is not passed to the container"


def test_the_digest_may_run_long_enough_to_finish_a_large_board_list():
    """Polling is sequential, so the run scales with how many boards there are.

    A timeout that fits twenty boards and not two hundred kills the run
    part-way, and a part-way run looks exactly like a quiet morning — the one
    failure this project consistently refuses to allow.
    """
    unit = Path("deploy/winnow-digest.container").read_text()
    timeout = int(re.search(r"TimeoutStartSec=(\d+)", unit).group(1))

    assert timeout >= 3600
