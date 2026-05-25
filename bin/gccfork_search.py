"""gccfork — deep full-text session search sidecar.

This file contains the search feature split out of `gccfork.py`. Import it from main with:

    from gccfork_search import DeepModeIndicator, DeepSearchMixin

and apply it to the App class as a mixin:

    class GCCForkApp(DeepSearchMixin, App):
        ...

Call `self._init_deep_search_state()` once at the end of App `__init__`.

Search modes:
  1. exact substring (lowercase)
  2. whitespace-insensitive substring  ("markerdetect" ↔ "marker detect")
  3. token AND  ("machine learning" -> lines containing both tokens)
  4. fuzzy partial_ratio ≥ 80  (rapidfuzz, handles English typos)

UI effects:
  - Knight Rider progress banner
  - matched session rows get a light red background
  - preview shows ±2 turns (5 total) with alternating backgrounds
  - pre-compaction turns are marked "🚨 pre-compact" with a red background (not necessarily remembered after resume)
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from typing import Optional

from collections import defaultdict

from rich.cells import cell_len
from rich.style import Style

from textual import events
from textual._text_area_theme import TextAreaTheme
from textual.color import Color
from textual.containers import VerticalScroll
from textual.widgets import Input, Static, TextArea


class DeepBlockTextArea(TextArea):
    """🔬 Match block TextArea with drag-select and delegated right-click copy menu.

    Importing SelectableTextArea (the main module class) from this mixin would create a circular dependency, so this small copy keeps the same behavior locally.
    """
    ALLOW_SELECT = False

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            event.prevent_default()
            if hasattr(self.app, "prepare_context_copy"):
                self.app.prepare_context_copy(self.id, self.selected_text)
            if hasattr(self.app, "remember_active_copy_widget"):
                self.app.remember_active_copy_widget(self.id)
            event.stop()
            return
        if hasattr(self.app, "remember_active_copy_widget"):
            self.app.remember_active_copy_widget(self.id)
        await super()._on_mouse_down(event)

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 3:
            event.prevent_default()
            if hasattr(self.app, "open_copy_context_menu"):
                self.app.open_copy_context_menu(
                    widget_id=self.id,
                    text=self.selected_text,
                    screen_x=int(event.screen_x or 0),
                    screen_y=int(event.screen_y or 0),
                )
            event.stop()
            return
        await super()._on_mouse_up(event)


# ─── helper — copy of the main helper to avoid a circular import ─────────────────
def _extract_text_from_message(message) -> str:
    """Return combined text from Claude `message.content` (str or list[block])."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


# ─── widget — deep-search indicator ───────────────────────────────────────────
class DeepModeIndicator(Static, can_focus=True):
    """🔬 Deep-search indicator toggled by click, Enter, or Space.

    Flow:
      1. click enables deep mode (red visual state) and focuses the input
      2. user enters the query (incremental filtering does not run)
      3. Enter starts a full-body scan
      4. click again disables deep mode and returns to normal filtering
    """

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        event.prevent_default()
        event.stop()
        self.focus()

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button != 1:
            return
        event.prevent_default()
        event.stop()
        if hasattr(self.app, "toggle_deep_search"):
            self.app.toggle_deep_search()

    async def _on_key(self, event: events.Key) -> None:
        if event.key not in {"enter", "space"}:
            return
        event.prevent_default()
        event.stop()
        if hasattr(self.app, "toggle_deep_search"):
            self.app.toggle_deep_search()


# ─── Mixin — attach search methods to the App ────────────────────────────
class DeepSearchMixin:
    """🔬 Deep search mixin for GCCForkApp.

    Mixin state is initialized by `_init_deep_search_state()`. Call it once at the end of main `__init__`.
    """

    # State (the mixin has no __init__, so attributes are set externally)
    _deep_mode: bool
    _deep_scan_done: bool
    _deep_search_query: str
    _deep_search_results: set[str]
    _deep_search_in_progress: bool

    def _init_deep_search_state(self) -> None:
        """Call once at the end of App `__init__`."""
        self._deep_mode = False
        self._deep_scan_done = False
        self._deep_search_query = ""
        self._deep_search_results = set()
        self._deep_search_in_progress = False
        # Block cache: (sid, query) -> blocks list. Re-selecting the same session can use it immediately.
        # Cleared on a new scan or when leaving the mode.
        self._deep_blocks_cache: dict[tuple[str, str], list] = {}

    # ─── mode toggle ───────────────────────────────────────────────────
    def toggle_deep_search(self) -> None:
        """🔬 Deep indicator click toggles the mode.

        OFF to ON: only updates the visual state (red) and focuses the input; it does not scan yet.
        ON to OFF: return to normal filtering.
        """
        if self._deep_search_in_progress:
            return
        if self._deep_mode:
            self._exit_deep_mode()
        else:
            self._enter_deep_mode()

    def _enter_deep_mode(self) -> None:
        self._deep_mode = True
        self._deep_scan_done = False
        self._deep_search_results = set()
        self._deep_blocks_cache = {}  # entering a new mode — reset cache
        # A previous scan may have failed and left in_progress stuck, so always reset it
        self._deep_search_in_progress = False
        try:
            inp = self.query_one("#filter-input", Input)
            inp.add_class("deep-scan")
            ind = self.query_one("#filter-mode-indicator", DeepModeIndicator)
            ind.add_class("deep-scan")
            inp.focus()
        except Exception:
            pass
        self.refresh_list()
        self.notify("🔬 Deep mode — enter a query and press Enter to scan full session bodies")

    def _exit_deep_mode(self) -> None:
        self._deep_mode = False
        self._deep_scan_done = False
        self._deep_search_query = ""
        self._deep_search_results = set()
        self._deep_blocks_cache = {}  # leaving mode — clear cache
        self._deep_search_in_progress = False  # clean state for next entry
        try:
            inp = self.query_one("#filter-input", Input)
            inp.remove_class("deep-scan")
            ind = self.query_one("#filter-mode-indicator", DeepModeIndicator)
            ind.remove_class("deep-scan")
        except Exception:
            pass
        self._hide_deep_preview()
        self.refresh_list()

    # ─── Textual event handlers ────────────────────────────────────────
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Filter input Enter starts a full-body scan only in deep mode."""
        if event.input.id != "filter-input":
            return
        if not self._deep_mode:
            return
        query = event.value.strip()
        if not query:
            self.notify("Enter a query, then press Enter.", severity="warning")
            return
        if self._deep_search_in_progress:
            return
        self._start_deep_search(query)

    # ─── background scan ──────────────────────────────────────────────
    def _start_deep_search(self, query: str) -> None:
        """Match all session jsonl bodies in the background with multiple matchers. Knight Rider UI."""
        self._deep_search_in_progress = True
        self._deep_search_query = query
        self._deep_blocks_cache = {}  # new query — invalidate cache
        candidates = list(self.sessions)
        n = len(candidates)
        # Snapshot the five noise-filter prefs at worker start
        from gccfork_settings import get_deep_prefs_snapshot, get_scannable_text
        prefs = get_deep_prefs_snapshot()
        self._show_progress_banner(f"🔬 Full-body scan — {n} sessions / '{query}' / multiple matchers")
        self._start_scanner_animation()

        def _worker() -> None:
            matched: set[str] = set()
            prebuilt: dict = {}
            try:
                # The fuzzy matcher is the sixth noise filter and defaults to OFF because false positives are common.
                fuzz = None
                if prefs.get("deep_include_fuzzy", False):
                    from rapidfuzz import fuzz as _fuzz
                    fuzz = _fuzz
                qlow = query.lower()
                qstripped = re.sub(r"\s+", "", qlow)
                qtokens = [t for t in qlow.split() if len(t) >= 2]

                def line_match(line_lower: str) -> bool:
                    if not line_lower:
                        return False
                    if qlow in line_lower:
                        return True
                    if qstripped and qstripped in re.sub(r"\s+", "", line_lower):
                        return True
                    if len(qtokens) >= 2 and all(t in line_lower for t in qtokens):
                        return True
                    if fuzz is not None and len(line_lower) < 1500:
                        if fuzz.partial_ratio(qlow, line_lower) >= 80:
                            return True
                    return False

                # When a line matches, build the excerpt block immediately and cache it.
                for idx, sess in enumerate(candidates, 1):
                    try:
                        p = sess.jsonl_path
                        if not p.exists():
                            continue
                        with p.open("r", encoding="utf-8", errors="ignore") as fh:
                            for raw in fh:
                                # Apply the five noise filters. If get_scannable_text returns an empty string, skip that line (for disabled attachment/file-history/tool_result/tool_use/system categories).
                                try:
                                    obj = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                scannable = get_scannable_text(obj, prefs)
                                if scannable and line_match(scannable):
                                    matched.add(sess.id)
                                    break
                        if sess.id in matched:
                            try:
                                blocks = self._build_deep_match_blocks(sess, query, max_hits=6)
                                if blocks:
                                    prebuilt[(sess.id, query)] = blocks
                            except Exception:
                                pass
                    except OSError:
                        continue
                    except Exception:
                        continue
                    if idx % 5 == 0:
                        try:
                            self.call_from_thread(
                                self._update_stream_text,
                                f"  progress {idx}/{n}  ·  matches {len(matched)}  (multiple matchers)",
                            )
                        except Exception:
                            pass
            except Exception as exc:
                # Even whole-worker failures such as rapidfuzz import errors notify completion in finally
                try:
                    self.call_from_thread(
                        self.notify,
                        f"🔬 Scan error: {type(exc).__name__}: {exc}",
                        severity="error",
                    )
                except Exception:
                    pass
            finally:
                # Always call _finish_deep_search to clear in_progress and refresh the list.
                # Even when an exception leaves matched empty, finish with scan_done=True so the user can retry.
                try:
                    self.call_from_thread(self._finish_deep_search, query, matched, prebuilt)
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_deep_search(self, query: str, matched: set[str], prebuilt: dict | None = None) -> None:
        self._deep_search_in_progress = False
        self._stop_scanner_animation()
        self._hide_progress_banner()
        if not self._deep_mode or self._deep_search_query != query:
            return
        self._deep_search_results = matched
        self._deep_scan_done = True
        if prebuilt:
            self._deep_blocks_cache.update(prebuilt)
        self.notify(f"🔬 Full-body scan complete — {len(matched)} matches", timeout=4)
        self.refresh_list()

    # ─── excerpt extraction and preview highlighting ───────────────────────────────
    def _extract_deep_match_snippet(
        self, session, query: str, max_hits: int = 6,
    ) -> tuple[str, list[tuple[int, int, bool]]]:
        """Excerpt block for ±2 turns around a matched turn (5 total).

        Return: (full text, [(start_row, end_row, is_pre_compact), ...])

        Match policy is the same multi-matcher used by the worker.
        Compaction boundary: turns before the last isCompactSummary in the jsonl are pre-compact.
        """
        if not query or not session.jsonl_path.exists():
            return "", []

        from gccfork_settings import get_deep_prefs_snapshot, get_scannable_text
        prefs = get_deep_prefs_snapshot()
        fuzz = None
        if prefs.get("deep_include_fuzzy", False):
            from rapidfuzz import fuzz as _fuzz
            fuzz = _fuzz
        qlow = query.lower()
        qstripped = re.sub(r"\s+", "", qlow)
        qtokens = [t for t in qlow.split() if len(t) >= 2]

        def line_matches(line_low: str) -> bool:
            if not line_low: return False
            if qlow in line_low: return True
            if qstripped and qstripped in re.sub(r"\s+", "", line_low): return True
            if len(qtokens) >= 2 and all(t in line_low for t in qtokens): return True
            if fuzz is not None and len(line_low) < 1500:
                if fuzz.partial_ratio(qlow, line_low) >= 80: return True
            return False

        turns: list[dict] = []
        last_compact_turn_idx = -1
        cur_turn: Optional[dict] = None
        try:
            with session.jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # Five noise filters: scannable == empty means this is not a match candidate
                    scannable = get_scannable_text(obj, prefs)
                    line_has_match = line_matches(scannable) if scannable else False
                    if obj.get("isCompactSummary") is True:
                        last_compact_turn_idx = len(turns) - 1
                        continue
                    typ = obj.get("type")
                    is_meta = obj.get("isMeta") or obj.get("isSidechain")
                    msg = obj.get("message") or {}
                    role = msg.get("role") or typ
                    text = _extract_text_from_message(msg)
                    if role == "user" and not is_meta:
                        cur_turn = {
                            "idx": len(turns),
                            "lines": [("user", text)] if text else [],
                            "match": False,
                        }
                        turns.append(cur_turn)
                    elif role == "assistant" and not is_meta and cur_turn is not None and text:
                        cur_turn["lines"].append(("assistant", text))
                    if line_has_match and cur_turn is not None:
                        cur_turn["match"] = True
        except OSError:
            return "", []

        match_turns = [t["idx"] for t in turns if t["match"]]
        if not match_turns:
            return "", []
        match_turns = match_turns[:max_hits]

        # Build ±2-turn windows (5 total) and merge overlaps
        windows: list[tuple[int, int, int]] = []
        for mi in match_turns:
            lo = max(0, mi - 2)
            hi = min(len(turns) - 1, mi + 2)
            if windows and lo <= windows[-1][1] + 1:
                prev_lo, _, prev_mi = windows[-1]
                windows[-1] = (prev_lo, hi, prev_mi)
            else:
                windows.append((lo, hi, mi))

        # Render with preserved newlines, 6 lines / 500 chars per message, PAD_WIDTH 200 cells
        lines: list[str] = []
        block_meta: list[tuple[int, int, bool]] = []
        type_marks = {"user": "🧑", "assistant": "🤖"}
        MAX_LINES_PER_MSG = 6
        MAX_CHARS_PER_MSG = 500
        PAD_WIDTH = 200

        def pad(line: str) -> str:
            cur = cell_len(line)
            if cur >= PAD_WIDTH:
                return line
            return line + " " * (PAD_WIDTH - cur)

        for bi, (lo, hi, primary_mi) in enumerate(windows, 1):
            is_pre = (last_compact_turn_idx >= 0 and primary_mi <= last_compact_turn_idx)
            header = f"─── match #{bi}/{len(windows)} (turn {primary_mi+1}{' · 🚨 pre-compact' if is_pre else ''}) ─────"
            block_start = len(lines)
            lines.append(pad(header))
            for ti in range(lo, hi + 1):
                t = turns[ti]
                marker = "▸" if ti == primary_mi else " "
                for role, text in t["lines"]:
                    mk = type_marks.get(role, "·")
                    label = f"{marker} [t{ti+1:>3}] {mk} "
                    indent = " " * cell_len(label)
                    msg = text[:MAX_CHARS_PER_MSG]
                    if len(text) > MAX_CHARS_PER_MSG:
                        msg = msg + "…"
                    msg_lines = msg.split("\n")
                    truncated = msg_lines[:MAX_LINES_PER_MSG]
                    omitted = len(msg_lines) - MAX_LINES_PER_MSG
                    for i, ml in enumerate(truncated):
                        prefix = label if i == 0 else indent
                        lines.append(pad(prefix + ml.rstrip()))
                    if omitted > 0:
                        lines.append(pad(f"{indent}… ({omitted}more lines)"))
                    marker = " "
            lines.append(pad(""))
            block_end = len(lines) - 1
            block_meta.append((block_start, block_end, is_pre))
        if lines and lines[-1].strip() == "":
            lines.pop()
        return "\n".join(lines), block_meta

    def _apply_deep_snippet_highlight(self, preview: TextArea, text: str, query: str) -> None:
        """Highlight query substrings inside preview excerpt blocks."""
        if not query:
            return
        try:
            if preview.theme != "gccfork-preview-ai":
                app_theme = self.current_theme
                surface = Color.parse(app_theme.surface or app_theme.background or "#202020")
                foreground = Color.parse(app_theme.foreground or "#f3f3f3")
                error_color = Color.parse(getattr(app_theme, "error", None) or "#e01b24")
                deep_match_bg = surface.blend(error_color, 0.55)
                theme = TextAreaTheme(
                    name="gccfork-preview-deep",
                    syntax_styles={
                        "deep-search-match": Style(
                            bgcolor=deep_match_bg.rich_color,
                            color=foreground.rich_color,
                            bold=True,
                        ),
                    },
                )
                preview.register_theme(theme)
                preview.theme = "gccfork-preview-deep"
            qlow = query.lower()
            qlen = len(query)
            lines = text.split("\n")
            in_block = False
            highlights = preview._highlights or defaultdict(list)
            for row, line in enumerate(lines):
                if "🔬 body match excerpt" in line:
                    in_block = True
                    continue
                if in_block and line.startswith("───"):
                    in_block = False
                    continue
                if not in_block:
                    continue
                line_lower = line.lower()
                pos = 0
                while True:
                    idx = line_lower.find(qlow, pos)
                    if idx < 0:
                        break
                    s = len(line[:idx].encode("utf-8"))
                    e = len(line[:idx + qlen].encode("utf-8"))
                    highlights[row].append((s, e, "deep-search-match"))
                    pos = idx + qlen
            preview._highlights = highlights
            preview._line_cache.clear()
            preview.refresh()
        except Exception:
            pass

    # ─── Option D render — one DeepBlockTextArea per block (supports drag-select) ──
    def _render_deep_preview(self, session, query: str) -> bool:
        """Mount one DeepBlockTextArea per block inside deep-preview VerticalScroll.

        Cache hit mounts immediately; cache miss builds and stores the result.
        Return True when rendering succeeded with matches; False when there are no matches.
        """
        try:
            container = self.query_one("#deep-preview", VerticalScroll)
            normal = self.query_one("#preview-text")
        except Exception:
            return False

        # Prefer the cache. The same (sid, query) skips rebuilds and jsonl reparsing.
        cache_key = (session.id, query)
        blocks = self._deep_blocks_cache.get(cache_key)
        if blocks is None:
            blocks = self._build_deep_match_blocks(session, query, max_hits=6)
            self._deep_blocks_cache[cache_key] = blocks
        if not blocks:
            return False

        normal.styles.display = "none"
        container.styles.display = "block"
        try:
            for ch in list(container.children):
                ch.remove()
        except Exception:
            pass

        # Query highlight theme: create once and share across all block widgets
        match_theme = self._make_deep_block_theme()

        for block in blocks:
            css_class = "deep-block deep-block-pre-compact" if block["is_pre"] else (
                "deep-block deep-block-dim" if block["dim"] else "deep-block deep-block-normal"
            )
            widget = DeepBlockTextArea(
                block["text"],
                classes=css_class,
                read_only=True,
                soft_wrap=True,
                compact=True,
                show_line_numbers=False,
                show_cursor=False,
                highlight_cursor_line=False,
            )
            try:
                container.mount(widget)
                if match_theme is not None:
                    widget.register_theme(match_theme)
                    widget.theme = match_theme.name
                widget._highlights = block["highlights"]
                widget._line_cache.clear()
                widget.refresh()
            except Exception:
                pass
        return True

    def _make_deep_block_theme(self):
        """TextArea theme used to highlight query hits in block widgets."""
        try:
            app_theme = self.current_theme
            surface = Color.parse(app_theme.surface or app_theme.background or "#202020")
            foreground = Color.parse(app_theme.foreground or "#f3f3f3")
            error_color = Color.parse(getattr(app_theme, "error", None) or "#e01b24")
            match_bg = surface.blend(error_color, 0.55)
            return TextAreaTheme(
                name="gccfork-deep-block",
                syntax_styles={
                    "deep-search-match": Style(
                        bgcolor=match_bg.rich_color,
                        color=foreground.rich_color,
                        bold=True,
                    ),
                },
            )
        except Exception:
            return None

    def _hide_deep_preview(self) -> None:
        """Hide deep-preview and show the normal preview again.

        This is called on every update_preview, so return immediately when already hidden.
        """
        try:
            container = self.query_one("#deep-preview", VerticalScroll)
            # Already hidden with no children is a no-op, common during normal-mode selection changes
            if container.styles.display == "none" and not container.children:
                return
            normal = self.query_one("#preview-text")
            container.styles.display = "none"
            normal.styles.display = "block"
            for ch in list(container.children):
                ch.remove()
        except Exception:
            pass

    def _build_deep_match_blocks(
        self, session, query: str, max_hits: int = 6,
    ) -> list[dict]:
        """Return excerpt blocks for ±2 turns around each matched turn.

        Each block dict:
          - "text": plain text (header + body lines), loaded directly by TextArea
          - "highlights": defaultdict(row → [(start_byte, end_byte, "deep-search-match")])
          - "is_pre": bool (whether this is pre-compact; CSS red background and border)
          - "dim": bool (the dim phase of alternating backgrounds)
        """
        if not query or not session.jsonl_path.exists():
            return []
        from gccfork_settings import get_deep_prefs_snapshot, get_scannable_text
        prefs = get_deep_prefs_snapshot()
        fuzz = None
        if prefs.get("deep_include_fuzzy", False):
            from rapidfuzz import fuzz as _fuzz
            fuzz = _fuzz
        qlow = query.lower()
        qstripped = re.sub(r"\s+", "", qlow)
        qtokens = [t for t in qlow.split() if len(t) >= 2]

        def line_matches(line_low: str) -> bool:
            if not line_low: return False
            if qlow in line_low: return True
            if qstripped and qstripped in re.sub(r"\s+", "", line_low): return True
            if len(qtokens) >= 2 and all(t in line_low for t in qtokens): return True
            if fuzz is not None and len(line_low) < 1500:
                if fuzz.partial_ratio(qlow, line_low) >= 80: return True
            return False

        turns: list[dict] = []
        last_compact_turn_idx = -1
        cur_turn: Optional[dict] = None
        try:
            with session.jsonl_path.open("r", encoding="utf-8", errors="ignore") as fh:
                for raw in fh:
                    try:
                        obj = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    # Five noise filters, same as the worker
                    scannable = get_scannable_text(obj, prefs)
                    line_has_match = line_matches(scannable) if scannable else False
                    if obj.get("isCompactSummary") is True:
                        last_compact_turn_idx = len(turns) - 1
                        continue
                    typ = obj.get("type")
                    is_meta = obj.get("isMeta") or obj.get("isSidechain")
                    msg = obj.get("message") or {}
                    role = msg.get("role") or typ
                    text = _extract_text_from_message(msg)
                    if role == "user" and not is_meta:
                        cur_turn = {
                            "idx": len(turns),
                            "lines": [("user", text)] if text else [],
                            "match": False,
                        }
                        turns.append(cur_turn)
                    elif role == "assistant" and not is_meta and cur_turn is not None and text:
                        cur_turn["lines"].append(("assistant", text))
                    if line_has_match and cur_turn is not None:
                        cur_turn["match"] = True
        except OSError:
            return []

        match_turns = [t["idx"] for t in turns if t["match"]][:max_hits]
        if not match_turns:
            return []

        # ±2 windows + merge overlapping
        windows: list[tuple[int, int, int]] = []
        for mi in match_turns:
            lo = max(0, mi - 2)
            hi = min(len(turns) - 1, mi + 2)
            if windows and lo <= windows[-1][1] + 1:
                prev_lo, _, prev_mi = windows[-1]
                windows[-1] = (prev_lo, hi, prev_mi)
            else:
                windows.append((lo, hi, mi))

        type_marks = {"user": "🧑", "assistant": "🤖"}
        MAX_LINES_PER_MSG = 6
        MAX_CHARS_PER_MSG = 500
        qlow = query.lower()
        qlen = len(query)
        blocks: list[dict] = []
        for bi, (lo, hi, primary_mi) in enumerate(windows, 1):
            is_pre = (last_compact_turn_idx >= 0 and primary_mi <= last_compact_turn_idx)
            header_text = (
                f"━━ match #{bi}/{len(windows)} · turn {primary_mi+1}"
                + (" · 🚨 pre-compact (not remembered by AI after resume)" if is_pre else "")
                + " ━━"
            )
            text_lines: list[str] = [header_text]
            for ti in range(lo, hi + 1):
                t = turns[ti]
                marker = "▸ " if ti == primary_mi else "  "
                for role, text_msg in t["lines"]:
                    mk = type_marks.get(role, "·")
                    label = f"{marker}[t{ti+1:>3}] {mk} "
                    indent = " " * cell_len(label)
                    msg = text_msg[:MAX_CHARS_PER_MSG]
                    if len(text_msg) > MAX_CHARS_PER_MSG:
                        msg = msg + "…"
                    msg_lines = msg.split("\n")
                    truncated = msg_lines[:MAX_LINES_PER_MSG]
                    omitted = len(msg_lines) - MAX_LINES_PER_MSG
                    for i, ml in enumerate(truncated):
                        prefix = label if i == 0 else indent
                        text_lines.append(prefix + ml.rstrip())
                    if omitted > 0:
                        text_lines.append(f"{indent}… ({omitted}more lines)")
                    marker = "  "
            full_text = "\n".join(text_lines)

            # Map query substring positions to byte ranges (TextArea highlight format)
            highlights: defaultdict = defaultdict(list)
            for row, line in enumerate(text_lines):
                line_lower = line.lower()
                pos = 0
                while True:
                    idx = line_lower.find(qlow, pos)
                    if idx < 0:
                        break
                    s = len(line[:idx].encode("utf-8"))
                    e = len(line[:idx + qlen].encode("utf-8"))
                    highlights[row].append((s, e, "deep-search-match"))
                    pos = idx + qlen

            blocks.append({
                "text": full_text,
                "highlights": highlights,
                "is_pre": is_pre,
                "dim": (bi % 2 == 0),
            })
        return blocks

    def _apply_deep_block_bg(
        self, preview: TextArea, text: str, block_meta: list[tuple[int, int, bool]],
    ) -> None:
        """Each excerpt block gets alternating backgrounds; pre-compact blocks get a red background."""
        if not block_meta:
            return
        try:
            app_theme = self.current_theme
            surface = Color.parse(app_theme.surface or app_theme.background or "#202020")
            foreground = Color.parse(app_theme.foreground or "#f3f3f3")
            error_color = Color.parse(getattr(app_theme, "error", None) or "#e01b24")
            dim_bg = surface.blend(foreground, 0.07)
            pre_compact_bg = surface.blend(error_color, 0.20)
            cur_theme_name = preview.theme
            theme = preview._themes.get(cur_theme_name)
            if theme is None:
                return
            ss = dict(theme.syntax_styles)
            ss["deep-block-dim"] = Style(
                bgcolor=dim_bg.rich_color,
                color=foreground.rich_color,
            )
            ss["deep-block-pre-compact"] = Style(
                bgcolor=pre_compact_bg.rich_color,
                color=foreground.rich_color,
            )
            new_theme = TextAreaTheme(
                name=cur_theme_name,
                cursor_line_style=theme.cursor_line_style,
                syntax_styles=ss,
            )
            preview.register_theme(new_theme)
            preview.theme = cur_theme_name

            highlights = preview._highlights or defaultdict(list)
            lines = text.split("\n")
            for bi, (start, end, is_pre) in enumerate(block_meta):
                style_name = "deep-block-pre-compact" if is_pre else (
                    "deep-block-dim" if bi % 2 == 1 else None
                )
                if style_name is None:
                    continue
                for row in range(start, end + 1):
                    if row >= len(lines):
                        break
                    line_end = len(lines[row].encode("utf-8"))
                    if line_end > 0:
                        highlights[row].insert(0, (0, line_end, style_name))
            preview._highlights = highlights
            preview._line_cache.clear()
            preview.refresh()
        except Exception:
            pass
