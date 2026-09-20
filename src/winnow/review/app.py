"""The review TUI.

Keyboard-driven, runs over SSH inside zellij, and the only process that ever
holds a credential capable of sending mail. The queue drains: a decision removes
the row, which is the difference between a queue and a feed.

Every score is shown with the evidence that produced it. A score with no visible
reasoning is a number to argue with.
"""

from __future__ import annotations

import os
import sqlite3
import webbrowser
from datetime import datetime
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from winnow import config, learning, store
from winnow.drafting import DraftingError, draft_cover_letter, load_assets
from winnow.llm import ClaudeDrafter
from winnow.profile import Profile
from winnow.review import data


class ReasonScreen(ModalScreen[str | None]):
    """Asks which structured reason a pass carries.

    A pass with no reason is a rejection nobody can learn from, so this is a
    required step rather than an optional note.
    """

    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Pass — why?"),
            ListView(*(ListItem(Label(reason), id=reason) for reason in data.PASS_REASONS)),
            id="reason-dialog",
        )

    @on(ListView.Selected)
    def _chose(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RevisionScreen(ModalScreen[str | None]):
    """Holds the letter that is actually going to be sent.

    Prefilled with the proposal, so a small edit stays a small edit and a paste
    of a version revised elsewhere is one select-all away. Saving records the
    text beside the proposal rather than over it — the pair is the whole point,
    since the difference between them is the only correction the drafter ever
    gets.
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
    ]

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    @property
    def text(self) -> str:
        """The current contents of the pane."""
        return self.query_one("#revision", TextArea).text

    @text.setter
    def text(self, value: str) -> None:
        self.query_one("#revision", TextArea).text = value

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("What actually went out — ctrl+s to save, escape to discard"),
            TextArea(self._body, id="revision"),
            id="revision-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#revision", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Asks before anything leaves the building.

    The credential boundary keeps the unattended processes from sending mail.
    This is the behavioural half of the same rule: no bulk send, no send as a
    consequence of any other action succeeding, and nothing sent without an
    answer to a question about this specific draft.
    """

    BINDINGS = [
        Binding("escape", "decline", "cancel"),
        Binding("n", "decline", "no"),
        Binding("y", "accept", "yes"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        yield Vertical(Static(id="confirm-body"), id="confirm-dialog")

    def on_mount(self) -> None:
        self.query_one("#confirm-body", Static).update(f"{self._question}\n\ny / n")

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_decline(self) -> None:
        self.dismiss(False)


class TunablesScreen(ModalScreen[str | None]):
    """Shows the adjustable numbers beside the rate that justifies changing them."""

    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("up", "raise_threshold", "threshold +1"),
        Binding("down", "lower_threshold", "threshold -1"),
    ]

    def __init__(self, view: data.TunablesView, profile_path: Path) -> None:
        super().__init__()
        self._view = view
        self._profile_path = profile_path
        self._threshold = view.score_threshold

    def compose(self) -> ComposeResult:
        # Content is set in on_mount rather than passed to the constructor:
        # Textual 8 does not render a Static constructed with a bare string.
        yield Vertical(Static(id="tunables-body"), id="tunables-dialog")

    def on_mount(self) -> None:
        self.query_one("#tunables-body", Static).update(self._body_text())

    def _body_text(self) -> str:
        return (
            f"score threshold   {self._threshold}\n"
            f"digest cap        {self._view.max_items}\n"
            f"always show top   {self._view.always_show_top}\n"
            f"cadence           {self._view.cadence}\n"
            "\n"
            f"measured applications per week: {self._view.applications_per_week}\n"
            f"{_misfire_note(self._view.gate_misfires)}"
            "\n"
            "up/down adjusts the threshold · esc writes and closes"
        )

    def action_raise_threshold(self) -> None:
        self._threshold = min(100, self._threshold + 1)
        self.query_one("#tunables-body", Static).update(self._body_text())

    def action_lower_threshold(self) -> None:
        self._threshold = max(0, self._threshold - 1)
        self.query_one("#tunables-body", Static).update(self._body_text())

    def action_close(self) -> None:
        if self._threshold == self._view.score_threshold:
            self.dismiss(None)
            return
        try:
            # Written back to profile.yaml so there stays exactly one source of
            # truth, comments and all.
            data.set_tunable(self._profile_path, "score_threshold", self._threshold)
        except OSError as error:
            # The deployed rubric is mounted read-only for the unattended units,
            # and an interactive session that loses its edit should say so
            # rather than take the queue down with it.
            self.dismiss(f"threshold not saved: {error.strerror or error}")
            return
        self.dismiss(None)


#: The three lists, in the order `v` cycles them.
_VIEWS = ("review", "working", "applied", "deferred")

_LABELS = {
    "review": "awaiting review",
    "working": "interested, not yet applied",
    "applied": "applied, awaiting a reply",
    "deferred": "deferred",
}

#: What to call a decision once it has been made.
_PAST_TENSE = {"interested": "interested:", "pass": "passed on", "defer": "deferred"}

_LISTS = {
    "review": data.queue,
    "working": data.working_set,
    "applied": data.applied,
    "deferred": data.deferred,
}


class ReviewApp(App[None]):
    """The review queue."""

    CSS = """
    #queue { height: 40%; }
    #revision-dialog { width: 90%; height: 80%; padding: 1 2; border: round $accent; }
    #revision { height: 1fr; }
    #detail { padding: 1 2; }
    #reason-dialog, #tunables-dialog, #confirm-dialog {
        width: 60; height: auto; padding: 1 2; border: round $accent; background: $surface;
    }
    """

    BINDINGS = [
        Binding("i", "interested", "interested"),
        Binding("p", "pass_on", "pass"),
        Binding("d", "defer", "defer"),
        Binding("o", "open_url", "open"),
        Binding("D", "draft", "draft"),
        Binding("s", "send", "send"),
        Binding("a", "applied", "applied"),
        Binding("e", "revise", "revise"),
        Binding("m", "merges", "merges"),
        Binding("t", "tunables", "tunables"),
        Binding("u", "undo", "undo"),
        Binding("A", "show_all", "show all"),
        Binding("v", "cycle_view", "view"),
        Binding("r", "reload", "reload"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, conn: sqlite3.Connection, profile: Profile, profile_path: Path) -> None:
        super().__init__()
        self._conn = conn
        self._profile = profile
        self._profile_path = profile_path
        self._rows: dict[str, int] = {}
        #: The last thing the app wanted to tell the operator. Kept as state
        #: rather than only painted, so it can be asserted on.
        self.status: str = ""
        self._view: str = _VIEWS[0]
        self._show_below_threshold: bool = False
        self._showing_all: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Horizontal(DataTable(id="queue"))
        yield Static(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#queue", DataTable)
        table.cursor_type = "row"
        table.add_columns("score", "title", "company", "comp", "source", "")
        self._rebuild()

    def action_cycle_view(self) -> None:
        """Move between the three lists a search actually has.

        New things to judge, things wanted but not yet applied for, and things
        waiting on an employer. Every key here works the same in all three, so
        a change of mind in the working set is the same keystroke it would have
        been in the queue.
        """
        self._view = _VIEWS[(_VIEWS.index(self._view) + 1) % len(_VIEWS)]
        self._rebuild()

    def action_reload(self) -> None:
        """Rebuild the current list, and say that it happened.

        The rebuild takes about eighty milliseconds, so the key is never slow —
        but on a queue that has not changed it produces no visible difference,
        which is indistinguishable from the key doing nothing. The timestamp
        moves whether or not the rows did.

        Only the keystroke reports. Every other action rebuilds too, and their
        own message is the more useful one.
        """
        self._rebuild()
        self.status = f"reloaded {datetime.now().strftime('%H:%M:%S')}"
        self.query_one("#detail", Static).update(self.status)

    def _rebuild(self) -> None:
        """Rebuild the current list from the store."""
        table = self.query_one("#queue", DataTable)
        table.clear()
        self._rows.clear()

        view = self._current_view()
        for row in view.rows:
            key = str(row.cluster_id)
            self._rows[key] = row.cluster_id
            table.add_row(
                str(row.score),
                row.title,
                row.company,
                row.comp,
                row.source,
                _age(row) or ("vetoed" if row.vetoed else ""),
                key=key,
            )

        note = " — all scored" if self._showing_all else _withheld_note(view)
        self.sub_title = f"{len(self._rows)} {_LABELS[self._view]}{note}"
        self._show_detail()

    def _current_view(self) -> data.ReviewLists:
        """The rows for the list being shown, and anything held back.

        Only the review queue filters on score. The other lists hold roles a
        decision has already been made about, and hiding one because the rubric
        has since been retuned would lose work rather than tidy it.
        """
        if self._view != "review":
            return data.ReviewLists(
                rows=_LISTS[self._view](self._conn), below_threshold=0, auto_rejected=0
            )
        self._showing_all = self._show_below_threshold
        return data.review_lists(
            self._conn,
            self._profile,
            include_below_threshold=self._show_below_threshold,
        )

    def action_show_all(self) -> None:
        """Toggle the below-threshold rows into the review queue."""
        self._show_below_threshold = not self._show_below_threshold
        self._rebuild()
        self.status = "showing everything scored" if self._show_below_threshold else "filtered"

    @on(DataTable.RowHighlighted)
    def _highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._show_detail()

    @property
    def selected_cluster(self) -> int | None:
        """The cluster under the cursor, if the queue is not empty."""
        table = self.query_one("#queue", DataTable)
        if not table.row_count:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return self._rows.get(str(row_key.value))

    def _show_detail(self) -> None:
        panel = self.query_one("#detail", Static)
        cluster_id = self.selected_cluster
        if cluster_id is None:
            panel.update("Queue empty. Nothing awaiting review.")
            return
        panel.update(render_detail(data.detail(self._conn, cluster_id)))

    def action_interested(self) -> None:
        self._decide("interested")

    def action_defer(self) -> None:
        self._decide("defer")

    def action_pass_on(self) -> None:
        cluster_id = self.selected_cluster
        if cluster_id is None:
            return

        def settled(reason: str | None) -> None:
            if reason is not None:
                self._decide("pass", reason, cluster_id)

        self.push_screen(ReasonScreen(), settled)

    def action_open_url(self) -> None:
        """Get the posting in front of a human, by whatever route exists.

        Review normally runs on the server, because that is where the database
        is, and the server has no browser and no display. ``webbrowser.open``
        there either fails or starts something nobody can see — which from the
        laptop is indistinguishable from a key that does nothing.

        So the browser is only tried where one could plausibly appear. Over SSH
        the link is written to the clipboard with OSC 52, which travels back
        down the connection to the terminal a human is actually looking at. The
        URL is shown either way: if the terminal refuses OSC 52 as well, it can
        still be selected off the screen.
        """
        cluster_id = self.selected_cluster
        if cluster_id is None:
            return

        url = data.detail(self._conn, cluster_id).url
        self.query_one("#detail", Static).update(url)

        if _has_a_display():
            self._open_in_browser(url)
            self.status = "opened"
            return

        self.copy_to_clipboard(url)
        self.status = "no display here — link copied to your clipboard"

    def _open_in_browser(self, url: str) -> None:
        """Hand a URL to the desktop. Separated so tests never launch one."""
        webbrowser.open(url)

    def action_draft(self) -> None:
        """Show this opportunity's draft, writing one if it has none.

        Re-opening rather than redrafting is the point: the panel is replaced
        the moment the cursor moves, and a letter that cannot be read again is
        one that would be sent unread. It also means this key never spends a
        second model call on work already done.
        """
        cluster_id = self.selected_cluster
        if cluster_id is None:
            return

        drafts = learning.drafts_for(self._conn, cluster_id)
        submitted = [d for d in drafts if d.submitted_at]
        if submitted:
            # `sent_at` only covers letters this process emailed. One posted by
            # hand and recovered from Sent has none, and offering to redraft it
            # invites a second application nobody meant to make.
            self.status = f"already applied {submitted[0].submitted_at[:10]} — press e to revise"
            self.query_one("#detail", Static).update(render_draft(submitted[0]))
            return

        if drafts:
            self.status = f"draft from {drafts[0].built_from.get('drafted_at', 'earlier')}"
            self.query_one("#detail", Static).update(render_draft(drafts[0]))
            return

        posting = store.canonical_posting(self._conn, cluster_id)
        if posting is None:
            return
        self.status = "drafting..."
        self.query_one("#detail", Static).update("drafting...")
        # Read here, not in the worker: the connection belongs to this thread.
        self._draft_worker(cluster_id, posting, learning.recent_sent_letters(self._conn))

    @work(thread=True)
    def _draft_worker(self, cluster_id: int, posting, sent_letters: tuple[str, ...]) -> None:
        """Do the model call off the UI thread; it takes tens of seconds.

        Network only. The SQLite connection belongs to the main thread, so both
        the past letters and the finished draft cross the boundary as values.
        """
        try:
            draft = draft_cover_letter(
                posting,
                load_assets(config.assets_path()),
                ClaudeDrafter(),
                sent_letters=sent_letters,
                candidate=self._profile.candidate_name,
            )
        except (DraftingError, RuntimeError) as error:
            self.call_from_thread(self._drafted, cluster_id, None, str(error))
            return
        self.call_from_thread(self._drafted, cluster_id, draft, None)

    def _drafted(self, cluster_id: int, draft, problem: str | None) -> None:
        if problem is not None:
            self.status = f"drafting failed: {problem}"
            self.query_one("#detail", Static).update(self.status)
            return
        learning.record_draft(self._conn, cluster_id, draft)
        self.status = f"drafted, resume variant {draft.resume_variant}"
        self.query_one("#detail", Static).update(render_draft(draft))

    def action_revise(self) -> None:
        """Record the version of this letter that was actually sent.

        Revision usually happens somewhere else — a real editor, or the mail
        client the letter goes out from — so this pane exists to receive a
        finished paste rather than to be a good place to write. What it is for
        is making sure the sent version survives at all.
        """
        cluster_id = self.selected_cluster
        if cluster_id is None:
            return

        drafts = learning.drafts_for(self._conn, cluster_id)
        if not drafts:
            self.status = "nothing drafted for this one yet — press D first"
            self.query_one("#detail", Static).update(self.status)
            return

        draft = drafts[0]

        def saved(body: str | None) -> None:
            if body is None or not body.strip():
                self.status = "revision discarded"
                return
            learning.record_final(self._conn, draft.id, body, source="pasted")
            self.status = "sent version recorded"
            self._rebuild()

        self.push_screen(RevisionScreen(draft.final_body or draft.body), saved)

    def action_applied(self) -> None:
        """Record that this opportunity's application was submitted.

        Most drafts are cover letters pasted into an employer's own form, which
        this process cannot observe. Pressing this is the only way that act
        enters the record, and calibration divides by it: an application nobody
        marked is an application that never happened, and its silence never
        counts against the score that produced it.
        """
        cluster_id = self.selected_cluster
        if cluster_id is None:
            return

        drafts = [d for d in learning.drafts_for(self._conn, cluster_id) if not d.submitted_at]
        if not drafts:
            self.status = "nothing drafted for this one yet — press D first"
            self.query_one("#detail", Static).update(self.status)
            return

        learning.mark_submitted(self._conn, drafts[0].id)
        self.status = "marked submitted"
        self._rebuild()

    def action_send(self) -> None:
        """Send the newest unsent draft for the selected opportunity."""
        cluster_id = self.selected_cluster
        if cluster_id is None:
            return
        pending = [d for d in learning.drafts_for(self._conn, cluster_id) if not d.submitted_at]
        if not pending:
            self.status = "nothing drafted for this one yet"
            self.query_one("#detail", Static).update(self.status)
            return

        draft = pending[0]
        if not draft.recipient:
            self.status = (
                "this draft has no recipient: it is a cover letter to paste into "
                "the employer's application form, not an email to send"
            )
            self.query_one("#detail", Static).update(self.status)
            return

        def answered(confirmed: bool) -> None:
            if not confirmed:
                self.status = "not sent"
                return
            try:
                message_id = data.send_draft(self._conn, draft, self._profile)
            except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
                self.status = f"send failed: {error}"
            else:
                self.status = f"sent as {message_id}"
            self.query_one("#detail", Static).update(self.status)

        self.push_screen(ConfirmScreen(f"Send to {draft.recipient}?\n\n{draft.subject}"), answered)

    def action_merges(self) -> None:
        pending = data.pending_merges(self._conn)
        panel = self.query_one("#detail", Static)
        if not pending:
            panel.update("No duplicate pairs awaiting confirmation.")
            return
        lines = [f"{pair.similarity:.2f}  {pair.left}  ~  {pair.right}" for pair in pending]
        panel.update("Pairs awaiting confirmation:\n" + "\n".join(lines))

    def action_tunables(self) -> None:
        view = data.tunables(self._conn, self._profile)

        def closed(problem: str | None) -> None:
            if problem:
                self.status = problem
                self.query_one("#detail", Static).update(problem)

        self.push_screen(TunablesScreen(view, self._profile_path), closed)

    def action_undo(self) -> None:
        """Take back the most recent decision.

        `d` for defer sits beside `p` for pass and reads as *dismiss*, so it
        gets pressed by mistake. The most recent decision anywhere is the right
        thing to undo: by the time the mistake is noticed the row has already
        left the list, so there is nothing to select.
        """
        undone = data.undo_last_decision(self._conn)
        if undone is None:
            self.status = "nothing to undo"
            self.query_one("#detail", Static).update(self.status)
            return

        self.status = f"undid {undone.decision}: {undone.company} — {undone.title}"
        self._rebuild()

    def _decide(
        self, decision: str, reason: str | None = None, cluster_id: int | None = None
    ) -> None:
        # Passed explicitly by the pass path: its reason prompt is a modal, and
        # re-reading the selection after it closes would decide about whatever
        # the cursor happens to be on by then.
        cluster_id = cluster_id if cluster_id is not None else self.selected_cluster
        if cluster_id is None:
            return

        row = next((r for r in _LISTS[self._view](self._conn) if r.cluster_id == cluster_id), None)
        data.decide(self._conn, cluster_id, decision, reason=reason)
        # Saying what happened is the whole of what was missing: a decision
        # that changes nothing visible gets made twice, and a decision made by
        # mistake goes unnoticed until the row is looked for later.
        named = f"{row.company} — {row.title}" if row else ""
        self.status = f"{_PAST_TENSE[decision]} {named} · u to undo"
        self._rebuild()


def _withheld_note(view: data.ReviewLists) -> str:
    """Say how many rows are being held back, if any.

    A list that quietly got shorter reads as a quiet day, and this project does
    not let a number do that.
    """
    notes = []
    if view.below_threshold:
        notes.append(f"{view.below_threshold} below threshold")
    if view.auto_rejected:
        notes.append(f"{view.auto_rejected} auto-rejected")
    return f" · {' · '.join(notes)}" if notes else ""


def _misfire_note(count: int) -> str:
    """Report gate misfires, and stay silent when there are none.

    Not a tunable but the evidence for one: each is a role the title anchors
    surfaced that was never the right kind of work. The anchors live in the
    rubric, so this is a number that points at an edit rather than at a job.
    """
    if not count:
        return ""
    return f"wrong-function passes (title anchors too loose): {count}\n"


def _has_a_display() -> bool:
    """Whether this process could plausibly put a browser in front of someone.

    An SSH session is decisive on its own: a display variable may still be set
    from the server's own graphical session, and opening a window there shows
    it to nobody.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _age(row: data.QueueRow) -> str:
    """How long this row has sat in its current state."""
    if row.age_days is None:
        return ""
    if row.age_days == 0:
        return "today"
    return f"{row.age_days}d"


def render_draft(draft) -> str:
    """Render a draft with what it was built from.

    Review is only a real check if it shows the material behind the letter, so
    the resume variant, the tailoring notes and any claim that could not be
    traced are shown above the text itself.
    """
    rule = "-" * 76
    lines = [
        f"DRAFT — resume variant {draft.resume_variant}",
        f"  {draft.built_from.get('variant_reason', '')}",
        rule,
    ]
    if draft.unverified:
        lines.append("!! claims that could not be traced to your own history:")
        for item in draft.unverified:
            missing = ", ".join(item.get("missing") or ())
            lines.append(f"   - {item.get('claim')}")
            if missing:
                lines.append(f"     nothing in your material mentions: {missing}")
        lines.append(rule)
    if draft.tailoring_notes:
        lines.append("tailoring:")
        lines.extend(f"   - {note}" for note in draft.tailoring_notes)
        lines.append(rule)
    lines += [f"Subject: {draft.subject}", "", draft.body]
    return "\n".join(lines)


def _drafts_line(detail: data.Detail) -> str:
    """Describe what has been drafted, so it is never invisible."""
    if not detail.drafts_pending and not detail.drafts_sent:
        return "none"
    parts = []
    if detail.drafts_pending:
        parts.append(f"{detail.drafts_pending} unsent")
    if detail.drafts_sent:
        parts.append(f"{detail.drafts_sent} sent")
    return ", ".join(parts)


def render_detail(detail: data.Detail) -> str:
    """Render one cluster's score as the reasoning behind it.

    Args:
        detail: The detail view.

    Returns:
        Plain text. Every weighted line carries the evidence that earned it.
    """
    rule = "─" * 76
    lines = [
        f"{detail.score}  {detail.title} — {detail.company}",
        rule,
    ]
    for line in detail.breakdown:
        if line.contribution == 0 and not line.evidence:
            continue
        evidence = f'"{line.evidence}"' if line.evidence else "—"
        lines.append(f"{line.contribution:>+5.0f}  {line.dimension:<26} {evidence}")
    lines += [
        rule,
        f" drafts {_drafts_line(detail):<26} "
        f"{'press D to read it' if detail.drafts_pending else 'press D to write one'}",
        f" comp   {detail.comp:<26} remote  {detail.remote}",
        f" posted {detail.posted:<26} reposts {detail.reposts}",
        f" gates  {detail.gates}",
        rule,
        detail.why_fits,
        f"⚠ {detail.concern}",
        detail.url,
    ]
    return "\n".join(lines)
