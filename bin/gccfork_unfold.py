"""gccfork_unfold — segmented compression sidecar.

Use Claude Code auto-compact markers (isCompactSummary or "Compacted from") as segment boundaries and slim each segment independently. Only the final segment receives keep-recent protection because it represents the active working area.

Background (verified by the 2026-05-04 ca09 experiment):
  - one regular slim pass :  21.9 MB → 20.5 MB  (-6.3%)
  - segmented unfold      :  21.9 MB →  5.27 MB (-75.9%)
  - **12x more efficient**

Reason: old segments have no keep-recent protected area, so strong slim can apply fully. The largest repeated tool_result entries are dropped.

Principles:
  P1. **Reject active sessions** — raise ActiveSessionUnfoldError when a live PID owns the session
  P2. **Automatic backup** — `.bak.<ts>.unfold.jsonl` preserves the pre-slim state
  P3. **Preserve boundaries** — keep the compact marker lines themselves for verification
  P4. **Protect the last segment** — use keep_recent_turns to protect the active work area
  P5. **idempotent** — no-op when there are no compactions (notify and exit)
  P6. **atomic** — tmp -> os.replace, rollback from backup on failure
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Textual imports — required by UnfoldConfirmScreen (modal class) below.
# Re-introduced 2026-05-07 after the `Segmented Unfold` button raised NameError on
# ModalScreen at module import time. The Phase E (2026-05-06) Python-archive
# pass had moved the algorithm body out of this file and inadvertently took
# these imports with it; the modal class itself stayed and still needs them.
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RadioButton, RadioSet, Static

from gccfork_sessions import (
    Session,
    all_active_sid_pid_map,
)


# ── errors ─────────────────────────────────────────────────────────────────
class ActiveSessionUnfoldError(ValueError):
    """Raised when trying to unfold an active Claude session; ask the user to quit first."""
    def __init__(self, sid: str):
        self.sid = sid
        super().__init__(
            f"Active Claude session {sid[:8]} cannot be unfolded. "
            f"Run /quit first, then try again."
        )


class NoCompactionFoundError(ValueError):
    """The jsonl file has no auto-compact markers, so there is nothing to unfold."""
    pass


# ── boundary detection ────────────────────────────────────────────────────────
@dataclass
class CompactBoundary:
    """Detected auto-compact event metadata."""
    line_idx: int            # 0-based line number in the jsonl file
    timestamp: str           # message timestamp, or an empty string
    detect_method: str       # "isCompactSummary" | "continuation_text" | "compacted_text"
    summary: str             # first 80 chars for UI preview


_CONT_PATTERNS = (
    "This session is being continued from a previous conversation",
    "Previous conversation that ran out of context",
)
_COMPACTED_PATTERNS = (
    "Compacted from",
    "compacted summary",
)


def _extract_text(obj: dict) -> str:
    """Extract user-visible text from a message dict.

    When content is a list, nested block text lists (for example tool_result content) are safely flattened to strings.
    """
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
                elif isinstance(t, list):
                    # nested — flatten one level
                    for sub in t:
                        if isinstance(sub, dict):
                            st = sub.get("text") or sub.get("content") or ""
                            if isinstance(st, str):
                                parts.append(st)
                        elif isinstance(sub, str):
                            parts.append(sub)
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    return ""


def detect_compact_boundaries(jsonl_path: Path) -> list[CompactBoundary]:
    """Detect auto-compact event lines and sort them by line number.

    Strict mode trusts only the `isCompactSummary: True` flag.
    Text matching (CONT / Compacted) was removed because it frequently produced false positives when search results happened to include those phrases (found 2026-05-05).

    Claude Code sets isCompactSummary for real auto-compact events, so this single signal is enough.
    """
    out: list[CompactBoundary] = []
    if not jsonl_path.exists():
        return out

    with jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
        for i, raw in enumerate(fh):
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("isCompactSummary") is not True:
                continue

            ts = obj.get("timestamp") or ""
            text_preview = _extract_text(obj).replace("\n", " ").strip()[:80]
            out.append(CompactBoundary(
                line_idx=i,
                timestamp=ts,
                detect_method="isCompactSummary",
                summary=text_preview,
            ))

    out.sort(key=lambda b: b.line_idx)
    return out


# ── segment split and slim ─────────────────────────────────────────────────
@dataclass
class SegmentStats:
    """Before/after slim statistics for one segment."""
    idx: int                 # 0-based segment number
    is_last: bool            # whether this is the protected final segment
    line_start: int          # start line in the source jsonl (inclusive)
    line_end: int            # end line (exclusive)
    bytes_before: int
    bytes_after: int
    lines_before: int
    lines_after: int
    drop_count: int
    keep_count: int

    @property
    def reduction_pct(self) -> float:
        if self.bytes_before == 0:
            return 0.0
        return (1 - self.bytes_after / self.bytes_before) * 100



# ─── Phase E archive (moved to unfold_python.py) ───────────────
# unfold_session + _slim_segment_lines + format_unfold_summary
# Calls now go through _call_rust_unfold_inplace() (Rust subprocess).

class UnfoldResult:
    """Segmented unfold result."""
    boundaries: list[CompactBoundary]
    segments: list[SegmentStats]
    bytes_before: int
    bytes_after: int
    backup_path: Optional[Path]
    elapsed_sec: float

    @property
    def total_reduction_pct(self) -> float:
        if self.bytes_before == 0:
            return 0.0
        return (1 - self.bytes_after / self.bytes_before) * 100


class UnfoldConfirmScreen(ModalScreen):
    """Segmented Unfold confirmation modal showing boundary count and action choices.

    UI:
      ┌─ Segmented Unfold ──────────────────────────────┐
      │ Session: ca09 (22.2 MB)                   │
      │ Found N auto-compact markers             │
      │                                        │
      │ ◉ Full unfold (fully slim all old segments)  │
      │ ○ Exclude last segment                          │
      │ ○ Cancel                                 │
      │                                        │
      │ [Cancel]                       [Run]    │
      └────────────────────────────────────────┘
    """

    DEFAULT_CSS = """
    UnfoldConfirmScreen {
        align: center middle;
    }
    #unfold-box {
        width: 92;
        height: auto;
        max-height: 90%;
        border: round $accent 35%;
        padding: 0;
        background: $surface;
    }
    #unfold-header {
        padding: 1 2;
        height: auto;
        border-bottom: hkey $accent 20%;
    }
    #unfold-title {
        color: $accent;
        text-style: bold;
    }
    #unfold-meta {
        color: $foreground 60%;
    }
    #unfold-body {
        padding: 1 2;
        height: auto;
    }
    #unfold-body RadioSet {
        background: transparent;
        border: none;
        height: auto;
    }
    #unfold-body RadioButton {
        background: $accent 5%;
        margin: 0 0 1 0;
        padding: 0 1;
        height: auto;
    }
    #unfold-body RadioButton:hover {
        background: $accent 10%;
    }
    #unfold-body RadioButton.-selected {
        background: $accent 16%;
    }
    #bundle-options {
        background: $accent 3%;
        border: round $accent 20%;
        padding: 1 2;
        margin: 0 0 1 0;
        height: auto;
    }
    #bundle-options.-disabled {
        background: $surface;
        border: round $accent 8%;
        opacity: 40%;
    }
    #bundle-options Static.opt-label {
        color: $foreground 70%;
        margin: 0 0 1 0;
    }
    #bundle-options.-disabled Static.opt-label {
        color: $foreground 35%;
    }
    #bundle-options RadioSet {
        margin: 0 0 1 0;
        height: auto;
    }
    #bundle-options RadioButton {
        background: $accent 5%;
        margin: 0 1 0 0;
        padding: 0 1;
    }
    #bundle-options.-disabled RadioButton {
        background: $accent 3%;
    }
    .desc {
        color: $foreground 55%;
        padding: 0 0 0 4;
    }
    #unfold-btn-row {
        padding: 1 2;
        height: auto;
        border-top: hkey $accent 20%;
    }
    #unfold-btn-row Button {
        margin: 0 1;
    }
    .spacer { width: 1fr; }
    """

    def __init__(self, session: Session, boundaries: list[CompactBoundary],
                 allow_bundle: bool = True) -> None:
        super().__init__()
        self.session = session
        self.boundaries = boundaries
        self.allow_bundle = allow_bundle

    def compose(self) -> ComposeResult:
        sz = self.session.jsonl_path.stat().st_size
        sz_mb = sz / 1024 / 1024
        n = len(self.boundaries)
        # Estimated reduction based on previous verification results
        est_inplace_mb = sz_mb * 0.24  # -76%
        est_inplace_protect_mb = sz_mb * 0.27  # -73% (S3 protected)
        est_bundle_mb = sz_mb * 0.04  # -96%

        with Vertical(id="unfold-box"):
            with Vertical(id="unfold-header"):
                yield Static(f"Segmented Unfold — {self.session.id[:8]}", id="unfold-title")
                yield Static(
                    f"{sz_mb:.1f} MB · {n} auto-compact markers · choose mode",
                    id="unfold-meta",
                )
            with Vertical(id="unfold-body"):
                with RadioSet(id="unfold-mode"):
                    yield RadioButton(
                        f"in-place full unfold  (estimated: {sz_mb:.1f}MB -> {est_inplace_mb:.1f}MB · -76%)",
                        value=True,
                        id="rb-all",
                    )
                    yield RadioButton(
                        f"in-place + protect last segment  (estimated: {sz_mb:.1f}MB -> {est_inplace_protect_mb:.1f}MB · -73%)",
                        id="rb-except-last",
                    )
                    yield RadioButton(
                        f"Bundle mode (new sid)  (estimated: {sz_mb:.1f}MB -> {est_bundle_mb:.2f}MB · -96%, recognition 96.8%)",
                        id="rb-bundle",
                    )

                # Mode descriptions
                yield Static(
                    "  • Full unfold: keep the same sid and markers; only the last segment is active context on resume\n"
                    "  • + protect last: same sid, keep recent 5 turns; clean without interrupting work (default)\n"
                    "  • Bundle: create a new child sid and inject all old work as an archive\n"
                    "      -> 96.8% recognition (verified with Opus 1M context), 40% context / 60% active headroom",
                    classes="desc",
                )

                # Bundle mode detail options
                with Vertical(id="bundle-options"):
                    yield Static("Bundle mode details (used when bundle is selected above):",
                                 classes="opt-label")
                    yield Static("Bundle size (token unit for grouped turns):", classes="opt-label")
                    with RadioSet(id="bundle-size"):
                        yield RadioButton("12K (small bundles, faster head reach)", id="bs-12k")
                        yield RadioButton("18K (recommended)", value=True, id="bs-18k")
                        yield RadioButton("25K (large bundles, richer context)", id="bs-25k")
                    yield Static("Recent turn protection:", classes="opt-label")
                    with RadioSet(id="recent-keep"):
                        yield RadioButton("3 turn", id="rk-3")
                        yield RadioButton("5 turns (recommended)", value=True, id="rk-5")
                        yield RadioButton("10 turns (mid-work)", id="rk-10")

            with Horizontal(id="unfold-btn-row"):
                yield Button("Cancel", id="btn-unfold-cancel")
                yield Static("", classes="spacer")
                yield Button("Run", id="btn-unfold-go", variant="warning")

    def on_mount(self) -> None:
        # Initial state: bundle mode is not selected, so detail options are disabled
        self._update_bundle_options_state("rb-all")

    def _update_bundle_options_state(self, choice_id: Optional[str]) -> None:
        """Toggle enabled/disabled state for bundle-mode detail options."""
        try:
            box = self.query_one("#bundle-options", Vertical)
        except Exception:
            return
        is_bundle = (choice_id == "rb-bundle")
        try:
            box.set_class(not is_bundle, "-disabled")
        except Exception:
            pass
        # Also set disabled on RadioSet / RadioButton widgets
        try:
            for rb in self.query("#bundle-options RadioButton"):
                rb.disabled = not is_bundle
        except Exception:
            pass

    def on_radio_set_changed(self, event) -> None:
        # Main mode radio changes toggle detail options
        if getattr(event.radio_set, "id", None) == "unfold-mode":
            pressed = event.radio_set.pressed_button
            choice = pressed.id if pressed else None
            self._update_bundle_options_state(choice)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-unfold-cancel":
            self.dismiss(None)
            return
        if bid == "btn-unfold-go":
            mode_set = self.query_one("#unfold-mode", RadioSet)
            pressed = mode_set.pressed_button
            choice = (pressed.id if pressed else "rb-all")

            # Bundle mode detail options
            bundle_size = 18_000
            recent_keep = 5
            try:
                bs_set = self.query_one("#bundle-size", RadioSet)
                bs_pressed = bs_set.pressed_button
                if bs_pressed:
                    bundle_size = {
                        "bs-12k": 12_000, "bs-18k": 18_000, "bs-25k": 25_000,
                    }.get(bs_pressed.id, 18_000)
            except Exception:
                pass
            try:
                rk_set = self.query_one("#recent-keep", RadioSet)
                rk_pressed = rk_set.pressed_button
                if rk_pressed:
                    recent_keep = {
                        "rk-3": 3, "rk-5": 5, "rk-10": 10,
                    }.get(rk_pressed.id, 5)
            except Exception:
                pass

            self.dismiss({
                "choice": choice,
                "bundle_size": bundle_size,
                "recent_keep": recent_keep,
            })


