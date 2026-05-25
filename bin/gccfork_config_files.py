"""gccfork_config_files.py — editor spawn module for config and memory files.

Sidecar module for opening session-injected files such as CLAUDE.md,
MEMORY.md, and GLOBAL_MEMORY.md in an external editor from the TUI with one
click. It follows the mono non-interactive policy: the main gccfork file only
imports the mixin and wires buttons/keys.

═══════════════════════════════════════════════════════════════════════════
Subprocess preconditions — violating these can lose user work
═══════════════════════════════════════════════════════════════════════════

This file is the external-editor spawn boundary. Keep these three rules:

[1] start_new_session=True — detach into a separate process group/session.
    Reason: subprocess.Popen defaults to a child of the TUI. If the TUI exits,
    child editors can receive SIGHUP and die too. Users must be able to close
    the TUI while keeping VSCode open.
    Verification: VSCode should survive after killing the TUI.

[2] stdout=DEVNULL, stderr=DEVNULL — discard both standard streams.
    Reason: VSCode startup can print GTK/GPU/dbus warnings to stderr. If those
    bytes hit the same terminal that Textual is drawing into, borders and
    widgets become corrupted until a full redraw.
    Verification: the TUI screen should remain intact after spawning.

[3] Explicitly catch FileNotFoundError and notify.
    Reason: if `code` is not installed, a silent FileNotFoundError leaves users
    with no explanation. The notification must identify the missing editor and
    the fallback result.
    Verification: configure a non-existent editor and confirm the notification.

Additional rules:
  - Do not pass cwd; spawned editors should use the user's current PWD.
  - Never use shell=True; paths with spaces or non-ASCII characters must not
    depend on shell escaping.

═══════════════════════════════════════════════════════════════════════════

## Editor selection priority

1. prefs `config_editor`
2. $EDITOR environment variable
3. code (VSCode)
4. cursor
5. nano (last fallback)

When set to `auto`, use the first command found in the priority order above.

## Discoverable files

| Emoji | Label | Path | Notes |
|---|---|---|---|
| 🌐 | Global CLAUDE.md | `~/.claude/CLAUDE.md` | Always |
| 📂 | Project CLAUDE.md | `<cwd>/CLAUDE.md` | Based on cwd |
| 🧠 | Project MEMORY.md | `~/.claude/projects/<slug>/memory/MEMORY.md` | Auto-discovered |
| 🌍 | GLOBAL_MEMORY.md | `~/.gccslim/memory/GLOBAL_MEMORY.md` | When present |
| 🖥 | system-apps.md | `~/.gccslim/memory/system-apps.md` | When present |
| 📁 | Memory folder | `~/.claude/projects/<slug>/memory/` (directory) | VSCode/cursor only |
| 📝 | Individual memory *.md | all .md files in the folder | Expanded dynamically |
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Textual is imported only inside the main gccfork PEP 723 environment. Unit
# tests that only exercise data helpers should avoid importing this module.
# There is no try/except guard here: if this sidecar cannot import, the main
# app should fall back explicitly.
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, Tree
from textual.widgets.tree import TreeNode

from gccfork_sessions import (
    PROJECTS_DIR,
    cwd_to_slug,
    pref_get,
)


# ─── Editor candidates and priority ─────────────────────────────────────
EDITOR_CANDIDATES = ["code", "cursor", "nano"]
EDITOR_DEFAULT = "auto"  # auto = prefs -> $EDITOR -> candidate order


def resolve_editor() -> tuple[Optional[str], str]:
    """Return the editor command plus the reason it was selected.

    Returns:
        (editor_cmd, reason). If editor_cmd is None, all fallbacks failed.
        reason is a short user-facing explanation such as "prefs", "$EDITOR",
        or "auto:code".
    """
    # 1. prefs (explicit user setting)
    pref = str(pref_get("config_editor", EDITOR_DEFAULT))
    if pref != EDITOR_DEFAULT and pref:
        if shutil.which(pref):
            return pref, f"prefs ({pref})"
        # If the configured command is not on PATH, fall back to auto.
        return _auto_resolve("prefs command missing -> auto fallback")

    # 2. auto mode
    return _auto_resolve("auto")


def _auto_resolve(prefix: str) -> tuple[Optional[str], str]:
    """Auto priority: $EDITOR, then EDITOR_CANDIDATES in order."""
    env_editor = os.environ.get("EDITOR")
    if env_editor and shutil.which(env_editor.split()[0]):
        return env_editor, f"{prefix}: $EDITOR={env_editor}"
    for cand in EDITOR_CANDIDATES:
        if shutil.which(cand):
            return cand, f"{prefix}: {cand}"
    return None, f"{prefix}: all candidates ({', '.join(EDITOR_CANDIDATES)}) not installed"


# ─── File metadata ──────────────────────────────────────────────────────
@dataclass
class ConfigFileEntry:
    """One editable config/memory file entry."""
    label: str
    emoji: str
    path: Path
    is_dir: bool = False
    exists: bool = False
    size_bytes: int = 0
    mtime_iso: str = ""

    @property
    def display_path(self) -> str:
        """Display paths under the home directory with a leading ~."""
        try:
            rel = self.path.relative_to(Path.home())
            return f"~/{rel}"
        except ValueError:
            return str(self.path)

    @property
    def short_meta(self) -> str:
        """Right-side card metadata, for example size and date."""
        if not self.exists:
            return "(none)"
        if self.is_dir:
            return "folder"
        kb = max(1, self.size_bytes // 1024)
        return f"{kb}KB · {self.mtime_iso[:10] if self.mtime_iso else '?'}"


def _file_entry(label: str, emoji: str, path: Path, is_dir: bool = False) -> ConfigFileEntry:
    """Build an entry with path metadata; missing files still get an entry."""
    entry = ConfigFileEntry(label=label, emoji=emoji, path=path, is_dir=is_dir)
    try:
        st = path.stat()
        entry.exists = True
        if not is_dir:
            entry.size_bytes = st.st_size
        # iso8601 yyyy-mm-dd
        from datetime import datetime, timezone
        entry.mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        pass
    return entry


def discover_config_files(current_cwd: Optional[str]) -> list[ConfigFileEntry]:
    """Discover editable files for the current cwd.

    If cwd is unavailable, project CLAUDE.md / MEMORY.md entries are still
    listed with exists=False.
    """
    home = Path.home()
    entries: list[ConfigFileEntry] = []

    # 1. Global CLAUDE.md
    entries.append(_file_entry("Global CLAUDE.md", "🌐", home / ".claude" / "CLAUDE.md"))

    # 2. Project CLAUDE.md based on cwd
    if current_cwd:
        proj_claude = Path(current_cwd) / "CLAUDE.md"
        entries.append(_file_entry("Project CLAUDE.md", "📂", proj_claude))

    # 3. Project MEMORY.md and individual memory .md files
    if current_cwd:
        try:
            slug = cwd_to_slug(current_cwd)
            mem_dir = PROJECTS_DIR / slug / "memory"
            mem_index = mem_dir / "MEMORY.md"
            entries.append(_file_entry("Project MEMORY.md", "🧠", mem_index))
            # Whole memory folder. Opening a directory only works well in
            # VSCode/cursor-like editors.
            entries.append(_file_entry(
                "Whole memory folder", "📁", mem_dir, is_dir=True,
            ))
            # Individual .md files except MEMORY.md.
            if mem_dir.exists():
                for md_path in sorted(mem_dir.glob("*.md")):
                    if md_path.name == "MEMORY.md":
                        continue
                    entries.append(_file_entry(
                        md_path.stem, "📝", md_path,
                    ))
        except Exception:
            pass

    # 4. GLOBAL_MEMORY.md
    entries.append(_file_entry(
        "GLOBAL_MEMORY.md", "🌍",
        home / ".gccslim/memory" / "GLOBAL_MEMORY.md",
    ))

    # 5. system-apps.md
    entries.append(_file_entry(
        "system-runtime-apps.md", "🖥",
        home / ".gccslim/memory" / "\uc2dc\uc2a4\ud15c\uad6c\ub3d9\uc11c\ubc84\uc571.md",
    ))

    return entries


# ─── Editor spawn helper ────────────────────────────────────────────────
def spawn_editor(path: Path) -> tuple[bool, str]:
    """Open a file/folder in the external editor.

    Returns (success, user_message). Applies the subprocess preconditions from
    the module docstring: detached session, DEVNULL streams, and explicit
    FileNotFoundError handling.
    """
    editor, reason = resolve_editor()
    if editor is None:
        return False, f"❌ Editor not found — {reason}"

    if not path.exists():
        return False, f"❌ File not found: {path}"

    # Split editor commands that include options, such as "code --new-window".
    parts = editor.split() if " " in editor else [editor]
    cmd = parts + [str(path)]

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,   # ⚠️ [2] protect TUI screen
            stderr=subprocess.DEVNULL,   # ⚠️ [2] protect TUI screen
            stdin=subprocess.DEVNULL,    # extra safety guard
            start_new_session=True,      # ⚠️ [1] keep alive after TUI exits
            close_fds=True,
        )
    except FileNotFoundError:
        # ⚠️ [3] resolve previously found the command on PATH, but a race may remove it before spawn; report it clearly.
        return False, f"❌ editor launch failed: '{editor}' command not found (resolve={reason})"
    except OSError as exc:
        return False, f"❌ editor spawn OSError: {exc} (editor={editor})"

    return True, f"📝 Opened {path.name} with {editor} (changes apply when saved)"


# ─── ConfigFilesScreen — modal ──────────────────────────────────────────
CONFIG_FILES_CSS = """
ConfigFilesScreen {
    align: center middle;
}
#cfg-box {
    width: 80%;
    max-width: 100;
    height: 80%;
    background: $surface;
    border: round $accent 35%;
    padding: 0;
}
#cfg-header {
    padding: 1 2;
    height: 4;
    background: $accent 30%;
    border-bottom: hkey $accent 20%;
}
#cfg-header-title {
    text-style: bold;
    color: $accent;
}
#cfg-header-meta {
    color: $foreground 70%;
}
#cfg-tree {
    height: 1fr;
    padding: 1 2;
    background: $surface;
    color: $foreground;
}
#cfg-tree:focus {
    background: $surface;
}
#cfg-tree > .tree--cursor {
    background: $accent 16%;
}
#cfg-tree > .tree--label {
    color: $foreground;
}
#cfg-tree > .tree--guides {
    color: $accent 35%;
}
#cfg-tree > .tree--guides-hover {
    color: $accent 60%;
}
#cfg-tree > .tree--guides-selected {
    color: $accent;
}
#cfg-scroll {
    padding: 1 2;
}
.cfg-card {
    padding: 0 2;
    margin: 0 0 1 0;
    background: $accent 5%;
    border: round $accent 20%;
    height: 4;
    color: $foreground;
}
.cfg-card:hover {
    background: $accent 10%;
    border: round $accent 35%;
}
.cfg-card:focus {
    background: $accent 16%;
    border: round $accent;
}
.cfg-card.-disabled {
    background: transparent;
    color: $foreground 30%;
}
.cfg-card-line1 {
    text-style: bold;
    color: $foreground;
    width: 100%;
    height: 1;
}
.cfg-card-line2 {
    color: $foreground 60%;
    width: 100%;
    height: 1;
}
#cfg-btn-row {
    padding: 0 2;
    height: 5;
    background: $accent 8%;
    border-top: hkey $accent 20%;
    layout: horizontal;
    align: left middle;
}
#cfg-btn-row Button {
    height: 3;
    min-width: 12;
    background: $accent 5%;
    border: round $accent 20%;
    color: $foreground;
    text-style: bold;
    margin: 0 1 0 0;
    padding: 0 2;
}
#cfg-btn-row Button:hover {
    background: $accent 10%;
    border: round $accent 35%;
    color: $accent;
}
#cfg-btn-row Button:focus {
    background: $accent 16%;
    border: round $accent;
    color: $accent;
}
#cfg-btn-row Static {
    color: $foreground 70%;
    height: 3;
    content-align: left middle;
}
#cfg-spacer {
    width: 1fr;
    height: 3;
}
#cfg-btn-cancel {
    min-width: 10;
}
#cfg-btn-advanced {
    min-width: 24;
}
"""


from textual.message import Message as _Message


class ConfigCard(Vertical):
    """File card — Vertical container + two-line Static + click handler.

    Wrap in a container to avoid Button single-line label limits.
    click/Enter/Space all emit an Activated message.
    """

    can_focus = True
    BINDINGS = [
        Binding("enter", "activate", "Open", show=False),
        Binding("space", "activate", "Open", show=False),
    ]

    class Activated(_Message):
        """Activate card (click/Enter/Space) — bubbles up so ConfigFilesScreen can catch it."""
        def __init__(self, entry: "ConfigFileEntry") -> None:
            super().__init__()
            self.entry = entry

    def __init__(self, entry: ConfigFileEntry, **kwargs) -> None:
        classes = "cfg-card" + (" -disabled" if not entry.exists else "")
        super().__init__(classes=classes, **kwargs)
        self.entry = entry
        if not entry.exists:
            self.can_focus = False

    def compose(self) -> ComposeResult:
        line1 = f"{self.entry.emoji}  {self.entry.label}"
        line2 = f"   {self.entry.display_path}  ·  {self.entry.short_meta}"
        yield Static(line1, classes="cfg-card-line1")
        yield Static(line2, classes="cfg-card-line2")

    def on_click(self) -> None:
        if self.entry.exists:
            self.post_message(self.Activated(self.entry))

    def action_activate(self) -> None:
        if self.entry.exists:
            self.post_message(self.Activated(self.entry))


class ConfigFilesScreen(ModalScreen[None]):
    """📝 Config/memory file editing modal.

    Spawn an external editor for the selected file. Editor priority is prefs `config_editor` →
    $EDITOR → code → cursor → nano in that order.
    """
    DEFAULT_CSS = CONFIG_FILES_CSS

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("q", "cancel", show=False),
    ]

    def __init__(self, current_cwd: Optional[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self.current_cwd = current_cwd
        self.entries: list[ConfigFileEntry] = []

    def on_mount(self) -> None:
        """Build Tree nodes from build_tracked_tree() and populate the textual Tree.

        On failure, write the full traceback to /tmp/gccfork-tree-error.log and notify the path.
        """
        import traceback as _tb
        log_path = Path("/tmp/gccfork-tree-error.log")
        try:
            tracked = build_tracked_tree(self.current_cwd)
            tree: Tree = self.query_one("#cfg-tree", Tree)
            self._populate_tree(tree, tracked)
            n_kids = len(tree.root.children)
            self.app.notify(
                f"📥 Tree build complete · {n_kids} categories  (on failure see {log_path})",
                timeout=4,
            )
        except Exception as exc:
            try:
                log_path.write_text(
                    f"=== {Path('/tmp').exists() and 'cfg tree build error'} ===\n"
                    f"current_cwd: {self.current_cwd}\n\n"
                    f"{_tb.format_exc()}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            try:
                self.app.notify(
                    f"❌ Tree build failed: {exc}\n→ full traceback: {log_path}",
                    severity="error", timeout=15,
                )
            except Exception:
                pass

    def _populate_tree(self, tree: Tree, tracked: dict) -> None:
        """Six categories -> Tree nodes. Simplification rules:
        - category label = `ICON NAME · count` (subtitle removed)
        - hook command -> basename only (full path is stored in node data)
        - source (Global/Project) = distinguished only by color ([label] prefix removed)
        - missing settings are grouped in one summary line
        - MEMORY individual files / reminders default to collapsed

        each category is isolated with try/except — other categories still render even if one fails.
        all labels are escaped for markup safety (protect hook commands containing `[` or `]`).
        """
        from rich.markup import escape as _esc
        root: TreeNode = tree.root
        root.expand()

        # source -> color mapping
        def src_color(src: str) -> str:
            if "Project" in src:
                return "cyan"
            return "white"

        def safe_add(parent: TreeNode, raw: str, *, expand: bool = False, **kwargs):
            """escape markup-unsafe chars before add. fallback to plain text on failure.

            expand argument is not passed to add(); call .expand() explicitly after node creation.
            (Textual versions differ in add(expand=...) support, so be explicit.)
            """
            try:
                node = parent.add(raw, **kwargs)
            except Exception:
                node = parent.add(_esc(raw), **kwargs)
            if expand:
                try:
                    node.expand()
                except Exception:
                    pass
            return node

        def safe_leaf(parent: TreeNode, raw: str, **kwargs):
            try:
                return parent.add_leaf(raw, **kwargs)
            except Exception:
                return parent.add_leaf(_esc(raw), **kwargs)

        def fail(name: str, exc: Exception) -> None:
            try:
                self.app.notify(f"[{name}] build failed: {exc}", severity="error", timeout=8)
            except Exception:
                pass

        # ── 1. SETTINGS — show existing entries and summarize missing ones ──────
        try:
            s = tracked["settings"]
            existing = [f for f in s["files"] if f["exists"]]
            missing = [f for f in s["files"] if not f["exists"]]
            n_settings = safe_add(root, f"[b]{s['icon']} SETTINGS · {len(existing)}[/b]", expand=True)
            for f in existing:
                label_short = _esc(f["label"].split("—")[0].strip())
                color = src_color(f["source_label"])
                hooks_n = _esc(f["label"].split("—")[1].strip() if "—" in f["label"] else "")
                safe_leaf(
                    n_settings,
                    f"[{color}]{label_short}[/{color}]  [dim]· {hooks_n}[/dim]",
                    data={"kind": "file", "path": f["path"], "exists": True},
                )
            if missing:
                safe_leaf(n_settings, f"[dim]({len(missing)} missing; recognized automatically when created)[/dim]")
        except Exception as exc:
            fail("SETTINGS", exc)

        # ── 2. HOOK — basename plus color distinguish source ──────────────
        try:
            h = tracked["hooks_registered"]
            total_hooks = sum(len(ev["items"]) for ev in h["events"])
            n_hooks = safe_add(root, f"[b]{h['icon']} HOOK · {total_hooks}[/b]", expand=True)
            if not h["events"]:
                safe_leaf(n_hooks, "[dim](none)[/dim]")
            for ev in h["events"]:
                ev_name = _esc(ev["label"].split(" (")[0])
                n_event = safe_add(n_hooks, f"[yellow]{ev_name}[/yellow] · {len(ev['items'])}", expand=True)
                for it in ev["items"]:
                    color = src_color(it["source"])
                    if it["script_path"]:
                        name = Path(it["script_path"]).name
                    else:
                        cmd = it["label"]
                        import re as _re3
                        m = _re3.search(r"(\S+\.(sh|mjs|js|py|ts))", cmd)
                        name = m.group(1).split("/")[-1] if m else cmd[:40] + ("…" if len(cmd) > 40 else "")
                    name_safe = _esc(name)
                    leaf_label = f"[{color}]{name_safe}[/{color}]"
                    if it["script_path"]:
                        safe_leaf(n_event, leaf_label, data={
                            "kind": "file",
                            "path": it["script_path"],
                            "exists": Path(it["script_path"]).exists(),
                        })
                    else:
                        safe_leaf(n_event, f"{leaf_label}  [dim](inline command)[/dim]")
        except Exception as exc:
            fail("HOOK", exc)

        # ── 3. actual injection evidence — counts only by matched source ─────────────
        try:
            e = tracked["evidence"]
            from collections import Counter
            sources_counter = Counter(r["matched_source"] for r in e["reminders"] if r["matched_source"])
            unmatched_count = sum(1 for r in e["reminders"] if not r["matched_source"])
            total_r = len(e["reminders"])
            n_ev = safe_add(root, f"[b]{e['icon']} actual evidence · {total_r} reminders[/b]", expand=True)
            if e["session_jsonl"]:
                sid_short = Path(e["session_jsonl"]).stem[:8]
                mt = _esc(e.get("mtime_iso", "")[5:16].replace("T", " "))
                safe_leaf(
                    n_ev,
                    f"[dim]session {sid_short} · {mt}[/dim]",
                    data={"kind": "file", "path": e["session_jsonl"], "exists": True},
                )
            for src, cnt in sorted(sources_counter.items(), key=lambda x: -x[1]):
                safe_leaf(n_ev, f"[green]✓[/green] [magenta]{_esc(src)}[/magenta] · {cnt} times")
            if unmatched_count:
                safe_leaf(n_ev, f"[dim]? unmatched · {unmatched_count} times[/dim]")
            if not e["reminders"]:
                safe_leaf(n_ev, "[dim](no reminders yet)[/dim]")
        except Exception as exc:
            fail("actual evidence", exc)

        # ── 4. CLAUDE.md ─────────────────────────────────────────
        try:
            cm = tracked["claude_md"]
            cm_existing = [f for f in cm["files"] if f["exists"]]
            n_cm = safe_add(root, f"[b]{cm['icon']} CLAUDE.md · {len(cm_existing)}[/b]", expand=True)
            for f in cm_existing:
                label_short = _esc(f["label"].replace(" CLAUDE.md", ""))
                safe_leaf(n_cm, f"[white]{label_short}[/white]", data={
                    "kind": "file", "path": f["path"], "exists": True,
                })
            cm_missing = len(cm["files"]) - len(cm_existing)
            if cm_missing:
                safe_leaf(n_cm, f"[dim]({cm_missing} missing)[/dim]")
        except Exception as exc:
            fail("CLAUDE.md", exc)

        # ── 5. MEMORY — collapsed by default, with index and individual files separated ───
        try:
            m = tracked["memory"]
            m_existing = [f for f in m["files"] if f["exists"]]
            index_files = [f for f in m_existing if "MEMORY.md" in f["label"] or "folder" in f["label"]]
            individual = [f for f in m_existing if f not in index_files]
            n_m = safe_add(
                root,
                f"[b]{m['icon']} MEMORY · index + {len(individual)} .md files[/b]",
                expand=False,
            )
            for f in index_files:
                safe_leaf(n_m, f"[white]{_esc(f['label'])}[/white]", data={
                    "kind": "file", "path": f["path"], "exists": True,
                })
            if individual:
                n_indv = safe_add(n_m, f"[dim]individual .md ({len(individual)})[/dim]", expand=False)
                for f in individual:
                    short = _esc(f["label"].replace("memory/", "").replace(".md", ""))
                    safe_leaf(n_indv, f"[white]{short}[/white]", data={
                        "kind": "file", "path": f["path"], "exists": True,
                    })
        except Exception as exc:
            fail("MEMORY", exc)

        # ── 6. external payload ─────────────────────────────────────
        try:
            ex = tracked["external_payload"]
            ex_existing = [f for f in ex["files"] if f["exists"]]
            n_ex = safe_add(root, f"[b]{ex['icon']} external payload · {len(ex_existing)}[/b]", expand=True)
            for f in ex_existing:
                safe_leaf(n_ex, f"[white]{_esc(f['label'])}[/white]", data={
                    "kind": "file", "path": f["path"], "exists": True,
                })
            ex_missing = len(ex["files"]) - len(ex_existing)
            if ex_missing:
                safe_leaf(n_ex, f"[dim]({ex_missing} missing)[/dim]")
        except Exception as exc:
            fail("external payload", exc)

    def on_tree_node_selected(self, event: "Tree.NodeSelected") -> None:
        """Leaf node click / Enter → spawn_editor.

        Modal does not close automatically — so the user can open multiple files in sequence.
        Esc / [Cancel] closes only when pressed.
        """
        node = event.node
        data = node.data
        if not data or not isinstance(data, dict):
            return
        if data.get("kind") != "file":
            return
        path_str = data.get("path", "")
        if not path_str:
            return
        path = Path(path_str)
        if not data.get("exists") or not path.exists():
            self.app.notify(f"File missing: {path}", severity="warning")
            return
        ok, msg = spawn_editor(path)
        sev = "information" if ok else "error"
        self.app.notify(msg, severity=sev, timeout=4)
        # intentionally do not dismiss — so the user can keep opening other files

    def compose(self) -> ComposeResult:
        # flat card data is no longer used — Tree widget shows everything
        editor, reason = resolve_editor()
        editor_label = f"Editor: {editor or 'none'}" if editor else "❌ Editor none"

        with Vertical(id="cfg-box"):
            with Vertical(id="cfg-header"):
                yield Static("📥 Auto injection — actual tracking", id="cfg-header-title")
                yield Static(
                    f"Leaf node Enter/click opens in VSCode · {reason}",
                    id="cfg-header-meta",
                )
            tree: Tree = Tree("📥 Claude Code auto injection (this project)", id="cfg-tree")
            tree.show_root = True
            tree.show_guides = True
            yield tree
            with Horizontal(id="cfg-btn-row"):
                yield Button("[Cancel]", id="cfg-btn-cancel")
                yield Static("", id="cfg-spacer")
                yield Button("🌐 Advanced (browser)", id="cfg-btn-advanced")
                yield Static("  " + editor_label)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-btn-cancel":
            self.dismiss()
            return
        if event.button.id == "cfg-btn-advanced":
            self._open_advanced_browser()
            return

    def on_config_card_activated(self, event: "ConfigCard.Activated") -> None:
        """Activate card (click/Enter/Space) → editor spawn. modal stays open (continuous editing)."""
        entry = event.entry
        ok, msg = spawn_editor(entry.path)
        sev = "information" if ok else "error"
        self.app.notify(msg, severity=sev, timeout=4)

    def _open_advanced_browser(self) -> None:
        """🌐 Advanced — spawn five-category tree HTML in browser. keep modal open."""
        try:
            tree = build_injection_tree(self.current_cwd)
            html = render_html_tree(tree)
            import time as _t
            sid = f"{Path(self.current_cwd or 'default').name}-{int(_t.time())}"
            ok, msg, _html_path = open_in_browser(html, sid_hint=sid)
            sev = "information" if ok else "error"
            self.app.notify(msg, severity=sev, timeout=5)
            # do not close modal — so the user can view the browser while keeping the TUI tree visible
        except Exception as exc:
            self.app.notify(f"❌ advanced browser spawn failed: {exc}", severity="error", timeout=8)

    def action_cancel(self) -> None:
        self.dismiss()


# ─── ConfigFilesMixin — App integration ─────────────────────────────────────
class ConfigFilesMixin:
    """Add these methods to main GCCForkApp:

      - action_show_config_files() — called by Ctrl+E or the edit button

    self.notify / self.push_screen / self.current_cwd are provided by the App body.
    """
    def action_show_config_files(self) -> None:
        try:
            current_cwd = getattr(self, "current_cwd", None)
            self.push_screen(ConfigFilesScreen(current_cwd=current_cwd))  # type: ignore[attr-defined]
        except Exception as exc:
            try:
                self.notify(f"📝 failed to open edit modal: {exc}", severity="error")  # type: ignore[attr-defined]
            except Exception:
                pass


__all__ = [
    "ConfigFileEntry",
    "ConfigFilesMixin",
    "ConfigFilesScreen",
    "EDITOR_CANDIDATES",
    "EDITOR_DEFAULT",
    "build_injection_tree",
    "build_tracked_tree",
    "discover_config_files",
    "extract_system_reminders",
    "open_in_browser",
    "parse_settings_files",
    "render_html_tree",
    "resolve_editor",
    "spawn_editor",
]


# ═══════════════════════════════════════════════════════════════════════
# actual tracking — settings.json parsing and jsonl reminder matching
# ═══════════════════════════════════════════════════════════════════════

def parse_settings_files(current_cwd: Optional[str]) -> dict:
    """Parse four settings files (Global + local + Project + Project local).

    Extract each file hooks section and record the source. **hook commands are not executed; static analysis only.**

    Returns:
        {
          "files": [{path, exists, hook_count, raw}, ...],   # 4 entries
          "merged_hooks": {event: [{source, command, type, matcher}]},
        }
    """
    import json as _json
    home = Path.home()

    candidates = [
        (home / ".claude" / "settings.json", "Global"),
        (home / ".claude" / "settings.local.json", "Global local"),
    ]
    if current_cwd:
        candidates.append((Path(current_cwd) / ".claude" / "settings.json", "Project"))
        candidates.append((Path(current_cwd) / ".claude" / "settings.local.json", "Project local"))

    files: list[dict] = []
    merged: dict[str, list[dict]] = {}

    for path, source_label in candidates:
        entry: dict = {
            "path": str(path),
            "source_label": source_label,
            "exists": path.exists(),
            "hook_count": 0,
            "events": [],
        }
        if path.exists():
            try:
                raw = _json.loads(path.read_text(encoding="utf-8"))
                hooks = raw.get("hooks", {}) or {}
                entry["events"] = list(hooks.keys())
                count = 0
                for event, items in hooks.items():
                    if event not in merged:
                        merged[event] = []
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        # Textual settings.json pattern: [{matcher, hooks: [{type, command}]}]
                        # or simple pattern: [{type, command}]
                        if isinstance(item, dict):
                            if "hooks" in item and isinstance(item["hooks"], list):
                                matcher = item.get("matcher", "*")
                                for h in item["hooks"]:
                                    if isinstance(h, dict):
                                        merged[event].append({
                                            "source": source_label,
                                            "matcher": matcher,
                                            "type": h.get("type", "?"),
                                            "command": h.get("command", h.get("prompt", "")),
                                        })
                                        count += 1
                            else:
                                merged[event].append({
                                    "source": source_label,
                                    "matcher": item.get("matcher", "*"),
                                    "type": item.get("type", "?"),
                                    "command": item.get("command", item.get("prompt", "")),
                                })
                                count += 1
                entry["hook_count"] = count
            except (OSError, _json.JSONDecodeError) as exc:
                entry["error"] = str(exc)
        files.append(entry)

    return {"files": files, "merged_hooks": merged}


# source candidates used for system-reminder matching — (label, source_path, content_signature)
def _build_source_signatures(current_cwd: Optional[str]) -> list[tuple[str, Path, str]]:
    """source candidates for matching — use the first 200 chars of each file as a signature."""
    sigs: list[tuple[str, Path, str]] = []
    home = Path.home()

    for label, path in [
        ("Global CLAUDE.md", home / ".claude" / "CLAUDE.md"),
        ("Project CLAUDE.md", Path(current_cwd) / "CLAUDE.md") if current_cwd else (None, None),
        ("GLOBAL_MEMORY.md", home / ".gccslim/memory" / "GLOBAL_MEMORY.md"),
        ("system-runtime-apps.md", home / ".gccslim/memory" / "\uc2dc\uc2a4\ud15c\uad6c\ub3d9\uc11c\ubc84\uc571.md"),
    ]:
        if label is None or path is None:
            continue
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8", errors="ignore")
                # meaningful first lines (ignoring whitespace)
                sig = "".join(c for c in content[:300] if c.strip())[:80]
                if sig:
                    sigs.append((label, path, sig))
        except OSError:
            pass

    # MEMORY.md separate
    if current_cwd:
        try:
            slug = cwd_to_slug(current_cwd)
            mem = PROJECTS_DIR / slug / "memory" / "MEMORY.md"
            if mem.exists():
                content = mem.read_text(encoding="utf-8", errors="ignore")
                sig = "".join(c for c in content[:300] if c.strip())[:80]
                if sig:
                    sigs.append(("Project MEMORY.md", mem, sig))
        except Exception:
            pass

    return sigs


def extract_system_reminders(
    current_cwd: Optional[str],
    max_bytes: int = 30_000_000,   # 30MB — full read (large jsonl files still include all reminders after SessionStart)
    max_reminders: int = 50,
) -> dict:
    """latest jsonl by mtime in the current cwd slug folder → system-reminder extraction and source matching.

    Returns:
        {
          "session_jsonl": str,   # absolute path of analyzed jsonl
          "session_id": str,      # ca099152...
          "mtime_iso": str,
          "reminders": [{
              "snippet": str,         # first 80 chars
              "matched_source": str|None,  # matched file label
              "matched_path": str|None,    # matched file path
          }],
          "total_count": int,     # total reminder count
        }
    """
    import re as _re
    result = {
        "session_jsonl": "",
        "session_id": "",
        "mtime_iso": "",
        "reminders": [],
        "total_count": 0,
    }

    if not current_cwd:
        return result

    try:
        slug = cwd_to_slug(current_cwd)
    except Exception:
        return result

    proj_dir = PROJECTS_DIR / slug
    if not proj_dir.exists():
        return result

    jsonls = sorted(
        [p for p in proj_dir.glob("*.jsonl") if not p.name.endswith(".bak.jsonl") and ".bak." not in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonls:
        return result

    latest = jsonls[0]
    result["session_jsonl"] = str(latest)
    result["session_id"] = latest.stem
    try:
        from datetime import datetime as _dt, timezone as _tz
        result["mtime_iso"] = _dt.fromtimestamp(latest.stat().st_mtime, tz=_tz.utc).isoformat()
    except OSError:
        pass

    try:
        with latest.open("rb") as fh:
            data = fh.read(max_bytes)
        text = data.decode("utf-8", errors="ignore")
    except OSError:
        return result

    # <system-reminder> block extraction — allows attributes and has no length limit
    blocks = _re.findall(r"<system-reminder[^>]*>([\s\S]*?)</system-reminder>", text)
    result["total_count"] = len(blocks)

    # build source signatures
    sigs = _build_source_signatures(current_cwd)

    # additional heuristic matching — specific keywords → hook source
    KEYWORD_HINTS = [
        ("openboard", "openboard hook"),
        ("CLAUDE SESSION INITIALIZED", "session-init.sh hook"),
        ("USER CONTEXT", "GLOBAL_MEMORY.md (hook injection)"),
        ("[SYNC]", "session-init.sh (git sync)"),
    ]

    seen_sigs: set[str] = set()
    for blk in blocks[:max_reminders * 2]:  # duplicate reminders are possible
        sig = blk.strip()[:60]
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        snippet = blk.strip().replace("\n", " ")[:120]

        matched_source = None
        matched_path = None

        # 1. keyword heuristic (hook source)
        for kw, hint in KEYWORD_HINTS:
            if kw in blk:
                matched_source = hint
                break

        # 2. file signature matching
        if matched_source is None:
            blk_compact = "".join(c for c in blk[:300] if c.strip())[:80]
            for label, path, file_sig in sigs:
                # signature prefix match (when first 30 chars match)
                if len(file_sig) >= 30 and file_sig[:30] in blk_compact:
                    matched_source = label
                    matched_path = str(path)
                    break

        result["reminders"].append({
            "snippet": snippet,
            "matched_source": matched_source,
            "matched_path": matched_path,
        })
        if len(result["reminders"]) >= max_reminders:
            break

    return result


def build_tracked_tree(current_cwd: Optional[str]) -> dict:
    """Six-category integration: settings + hooks + evidence + claude_md + memory + external.

    return a dict tree renderable by textual.widgets.Tree.
    """
    settings_data = parse_settings_files(current_cwd)
    reminders_data = extract_system_reminders(current_cwd)
    base_tree = build_injection_tree(current_cwd)

    # category 1 — SETTINGS files (static analysis)
    settings_node = {
        "key": "settings",
        "icon": "⚙",
        "label": f"SETTINGS files ({len(settings_data['files'])} items) [static analysis]",
        "subtitle": "hook registration locations; static parse only, no execution",
        "files": [],
    }
    for f in settings_data["files"]:
        suffix = f"active hooks {f['hook_count']} items" if f["exists"] else "(none)"
        settings_node["files"].append({
            "label": f"{f['source_label']} — {suffix}",
            "source_label": f["source_label"],   # used by _populate_tree for color mapping
            "path": f["path"],
            "exists": f["exists"],
            "is_dir": False,
            "size_bytes": Path(f["path"]).stat().st_size if f["exists"] else 0,
            "mtime_iso": "",
            "when": "master",
            "how": f["source_label"] + " settings",
        })

    # category 2 — registered HOOKs (by event)
    hooks_node = {
        "key": "hooks_registered",
        "icon": "🪝",
        "label": "registered HOOKs (by event)",
        "subtitle": "merged result of four settings files; which hook is registered to which event",
        "events": [],
    }
    for event, items in settings_data["merged_hooks"].items():
        event_node = {
            "label": f"{event} ({len(items)} items)",
            "items": [],
        }
        for h in items:
            cmd = h["command"][:80] + ("..." if len(h["command"]) > 80 else "")
            # try to extract .sh paths from commands (to make it editable)
            script_path = ""
            import re as _re2
            m = _re2.search(r"(\S+\.sh)\b", h["command"])
            if m:
                script_path = m.group(1).replace("~", str(Path.home()))
            event_node["items"].append({
                "label": cmd,
                "source": h["source"],
                "type": h["type"],
                "matcher": h.get("matcher", "*"),
                "script_path": script_path,
            })
        hooks_node["events"].append(event_node)

    # category 3 — actual injection evidence
    evidence_node = {
        "key": "evidence",
        "icon": "✅",
        "label": "actual injection evidence — recent session jsonl",
        "subtitle": (
            f"session: {reminders_data['session_id'][:8] if reminders_data['session_id'] else '(none)'}  ·  "
            f"mtime: {reminders_data['mtime_iso'][:19] if reminders_data['mtime_iso'] else '?'}  ·  "
            f"system-reminder: {reminders_data['total_count']} items"
        ),
        "session_jsonl": reminders_data["session_jsonl"],
        "reminders": reminders_data["reminders"],
    }

    return {
        "settings": settings_node,
        "hooks_registered": hooks_node,
        "evidence": evidence_node,
        "claude_md": next(c for c in base_tree["categories"] if c["key"] == "claude_md"),
        "memory": next(c for c in base_tree["categories"] if c["key"] == "memory"),
        "external_payload": next(c for c in base_tree["categories"] if c["key"] == "external_payload"),
        "meta": base_tree["meta"],
    }


# ═══════════════════════════════════════════════════════════════════════
# Advanced: auto-injection tree plus developer-friendly HTML
# ═══════════════════════════════════════════════════════════════════════

def build_injection_tree(current_cwd: Optional[str]) -> dict:
    """Return auto-injection items as a dict tree with five categories.

    Each leaf node: {label, path, exists, size_bytes, mtime_iso, when, how}
    categories: hooks / claude_md / memory / external_payload / skills
    """
    home = Path.home()
    claude_dir = home / ".claude"

    def file_node(label: str, path: Path, when: str, how: str) -> dict:
        node = {
            "label": label,
            "path": str(path),
            "exists": path.exists(),
            "is_dir": path.is_dir() if path.exists() else False,
            "size_bytes": 0,
            "mtime_iso": "",
            "when": when,
            "how": how,
        }
        try:
            st = path.stat()
            if not node["is_dir"]:
                node["size_bytes"] = st.st_size
            from datetime import datetime, timezone
            node["mtime_iso"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            pass
        return node

    # 1. HOOK infrastructure ⭐
    hook_files = []
    hook_files.append(file_node(
        "settings.json (master hook definition)",
        claude_dir / "settings.json",
        "every session + every prompt",
        "hook registration → inject stdout into context",
    ))
    hook_files.append(file_node(
        "settings.local.json (local override)",
        claude_dir / "settings.local.json",
        "every session + every prompt",
        "settings.json merged over settings.json (if present)",
    ))
    hooks_dir = claude_dir / "hooks"
    if hooks_dir.exists():
        for hook in sorted(hooks_dir.glob("*.sh")):
            hook_files.append(file_node(
                f"hooks/{hook.name}",
                hook,
                "according to settings.json trigger",
                "shell script, injected through stdout",
            ))

    # 2. CLAUDE.md (auto-discovered)
    claude_md_files = []
    claude_md_files.append(file_node(
        "Global CLAUDE.md",
        claude_dir / "CLAUDE.md",
        "every turn",
        "Claude Code auto-discovers and injects into context",
    ))
    if current_cwd:
        # traverse upward from cwd
        seen = set()
        cur = Path(current_cwd).resolve()
        while True:
            for cand in [cur / "CLAUDE.md", cur / ".claude" / "CLAUDE.md"]:
                if cand in seen:
                    continue
                seen.add(cand)
                if cand.exists():
                    rel = "Project" if cur == Path(current_cwd).resolve() else f"parent {cur.name}"
                    sub = " (.claude/)" if ".claude" in cand.parts else ""
                    claude_md_files.append(file_node(
                        f"{rel}{sub} CLAUDE.md",
                        cand,
                        "every turn",
                        "Claude Code finds it while traversing cwd -> root",
                    ))
            if cur.parent == cur:
                break
            cur = cur.parent

    # 3. MEMORY
    memory_files = []
    if current_cwd:
        try:
            slug = cwd_to_slug(current_cwd)
            mem_dir = PROJECTS_DIR / slug / "memory"
            memory_files.append(file_node(
                "MEMORY.md (index)",
                mem_dir / "MEMORY.md",
                "every session (200lines max)",
                "Claude Code auto-loads and injects into context",
            ))
            memory_files.append(file_node(
                "whole memory/ folder",
                mem_dir,
                "lazy",
                "model reads the index and then reads files as needed",
            ))
            if mem_dir.exists():
                for md in sorted(mem_dir.glob("*.md")):
                    if md.name == "MEMORY.md":
                        continue
                    memory_files.append(file_node(
                        f"memory/{md.name}",
                        md,
                        "lazy (when referenced by index)",
                        "model reads after consulting the index",
                    ))
        except Exception:
            pass

    # 4. external hook payload
    payload_files = []
    payload_files.append(file_node(
        "GLOBAL_MEMORY.md",
        home / ".gccslim/memory" / "GLOBAL_MEMORY.md",
        "each session start",
        "SessionStart hook cats the file into context",
    ))
    payload_files.append(file_node(
        "system-runtime-apps.md",
        home / ".gccslim/memory" / "\uc2dc\uc2a4\ud15c\uad6c\ub3d9\uc11c\ubc84\uc571.md",
        "reference only",
        "CLAUDE.md links it; model reads when needed",
    ))

    # 5. SKILLS
    skill_files = []
    skills_dir = claude_dir / "skills"
    if skills_dir.exists():
        for skill_dir in sorted(skills_dir.iterdir()):
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                skill_files.append(file_node(
                    f"skills/{skill_dir.name}/SKILL.md",
                    skill_md,
                    "on /skill call",
                    "user calls /skill; loaded then",
                ))

    return {
        "categories": [
            {
                "key": "hooks",
                "icon": "🪝",
                "label": "HOOK infrastructure",
                "subtitle": "master of auto injection; decides what is injected and when",
                "when": "every session + every prompt",
                "starred": True,
                "files": hook_files,
            },
            {
                "key": "claude_md",
                "icon": "📄",
                "label": "CLAUDE.md",
                "subtitle": "Auto-discovered while Claude Code traverses cwd -> root",
                "when": "every-turn context",
                "files": claude_md_files,
            },
            {
                "key": "memory",
                "icon": "🧠",
                "label": "MEMORY",
                "subtitle": "index auto-loaded; body is lazy-read by the model",
                "when": "every session (index) / lazy (body)",
                "files": memory_files,
            },
            {
                "key": "external_payload",
                "icon": "🌍",
                "label": "external hook payload",
                "subtitle": "SessionStart hook cats it into every session",
                "when": "each session start",
                "files": payload_files,
            },
            {
                "key": "skills",
                "icon": "🛠",
                "label": "SKILLS",
                "subtitle": "loaded on /skill call (not auto-injected)",
                "when": "on-demand",
                "files": skill_files,
            },
        ],
        "meta": {
            "current_cwd": current_cwd or "",
            "home": str(home),
        },
    }


# ── Developer-friendly HTML template (single file, zero dependencies) ─────────────────────
# Dark monospace. Keyboard: / = search, j/k = next/previous, Enter = VSCode Open,
# Esc = clear search. Detail trees use native <details>/<summary> expand/collapse.
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>📥 Claude Code auto-injection tree</title>
<style>
  :root {
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-hover: #1f2937;
    --fg: #c9d1d9;
    --fg-dim: #8b949e;
    --border: #30363d;
    --accent: #58a6ff;
    --accent-soft: #1f6feb33;
    --green: #56d364;
    --yellow: #e3b341;
    --red: #f85149;
    --purple: #bc8cff;
    --mono: 'JetBrains Mono', 'SF Mono', 'Menlo', 'Consolas', monospace;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--fg);
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.5;
    margin: 0;
    padding: 24px 32px;
    max-width: 1100px;
    margin: 0 auto;
  }
  h1 {
    font-size: 18px;
    font-weight: 600;
    margin: 0 0 4px 0;
    color: var(--fg);
  }
  .subtitle { color: var(--fg-dim); margin: 0 0 24px 0; font-size: 12px; }
  .meta-bar {
    display: flex; gap: 16px; padding: 8px 12px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; margin-bottom: 16px; font-size: 12px;
    color: var(--fg-dim);
  }
  .meta-bar span strong { color: var(--fg); }

  #search {
    width: 100%; padding: 10px 14px;
    background: var(--bg-card); color: var(--fg);
    border: 1px solid var(--border); border-radius: 6px;
    font-family: var(--mono); font-size: 13px;
    margin-bottom: 16px;
  }
  #search:focus { outline: none; border-color: var(--accent); }

  details {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 12px;
    overflow: hidden;
  }
  details[open] { border-color: var(--accent-soft); }
  summary {
    padding: 12px 16px;
    cursor: pointer;
    list-style: none;
    user-select: none;
    display: flex; align-items: baseline; gap: 12px;
    font-weight: 500;
  }
  summary::before {
    content: "▶"; color: var(--fg-dim); font-size: 10px; width: 12px;
    transition: transform 0.1s;
  }
  details[open] summary::before { transform: rotate(90deg); }
  summary:hover { background: var(--bg-hover); }
  .cat-icon { font-size: 16px; }
  .cat-label { color: var(--fg); }
  .cat-when {
    margin-left: auto; padding: 2px 8px;
    background: var(--accent-soft); color: var(--accent);
    border-radius: 10px; font-size: 11px;
  }
  .cat-star { color: var(--yellow); }
  .cat-sub {
    padding: 0 16px 8px 40px;
    color: var(--fg-dim); font-size: 12px;
  }
  .files { padding: 0 0 8px 0; }
  .file {
    display: grid;
    grid-template-columns: 1fr auto auto;
    gap: 12px;
    padding: 8px 16px 8px 40px;
    border-top: 1px solid var(--border);
    align-items: center;
  }
  .file:hover { background: var(--bg-hover); }
  .file.missing { opacity: 0.4; }
  .file-label { color: var(--fg); }
  .file-path {
    color: var(--fg-dim); font-size: 11px;
    margin-top: 2px; word-break: break-all;
  }
  .file-meta {
    color: var(--fg-dim); font-size: 11px; white-space: nowrap;
  }
  .file-actions { display: flex; gap: 6px; }
  .btn {
    padding: 4px 10px; border-radius: 4px;
    background: transparent; border: 1px solid var(--border);
    color: var(--fg-dim); cursor: pointer;
    font-family: var(--mono); font-size: 11px;
    text-decoration: none;
  }
  .btn:hover { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
  .btn-primary { color: var(--accent); border-color: var(--accent-soft); }
  .file-when {
    color: var(--purple); font-size: 11px;
    padding: 2px 6px; background: rgba(188, 140, 255, 0.1);
    border-radius: 3px;
  }
  .file-how {
    color: var(--fg-dim); font-size: 11px;
    margin-top: 2px;
  }
  .empty {
    padding: 16px 40px; color: var(--fg-dim);
    font-style: italic; font-size: 12px;
  }
  .help {
    margin-top: 24px; padding: 12px 16px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; color: var(--fg-dim); font-size: 11px;
  }
  .help kbd {
    background: var(--bg); border: 1px solid var(--border);
    border-radius: 3px; padding: 1px 6px; font-family: var(--mono);
    color: var(--fg); font-size: 10px;
  }
  .total-bar {
    display: flex; gap: 12px; margin-bottom: 16px;
    padding: 8px 12px; background: var(--bg-card);
    border: 1px solid var(--border); border-radius: 6px;
    font-size: 11px; color: var(--fg-dim);
  }
  .total-bar .stat strong { color: var(--green); }
</style>
</head>
<body>

<h1>📥 Claude Code auto-injection tree</h1>
<p class="subtitle">when / how / where -> click to edit</p>

<div class="meta-bar">
  <span><strong>cwd:</strong> __CURRENT_CWD__</span>
  <span><strong>home:</strong> __HOME__</span>
  <span><strong>generated:</strong> __GENERATED__</span>
</div>

<div class="total-bar" id="totals"></div>

<input id="search" type="text" placeholder="🔍  search file name/path  (/ shortcut · Esc clear)" autofocus>

<div id="categories"></div>

<div class="help">
  <strong>Keyboard</strong>:
  <kbd>/</kbd> search ·
  <kbd>Esc</kbd> search clear ·
  <kbd>j</kbd>/<kbd>k</kbd> next/previous ·
  <kbd>Enter</kbd> Editor Open ·
  <kbd>g</kbd>/<kbd>G</kbd> first/last
  <br><br>
  <strong>Editor</strong>: vscode:// URL scheme; clicking asks the OS to open VSCode (same window/new tab if already running).
  <br>
  <strong>📁 file manager</strong>: next to folder/file 📁 button = open location in the OS default file manager (file:// URL).
</div>

<script>
const TREE = __TREE_JSON__;

function fmtSize(b) {
  if (!b) return "-";
  if (b < 1024) return b + "B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + "KB";
  return (b / 1024 / 1024).toFixed(1) + "MB";
}
function fmtDate(iso) {
  if (!iso) return "-";
  return iso.slice(0, 10);
}
function fmtFromHome(path) {
  const home = TREE.meta.home;
  if (path.startsWith(home + "/")) return "~/" + path.slice(home.length + 1);
  return path;
}

function render() {
  const root = document.getElementById("categories");
  root.innerHTML = "";

  let totalFiles = 0, totalExists = 0, totalSize = 0;

  for (const cat of TREE.categories) {
    const det = document.createElement("details");
    det.dataset.catKey = cat.key;
    det.open = (cat.key === "hooks" || cat.key === "claude_md" || cat.key === "memory");

    const summary = document.createElement("summary");
    summary.innerHTML = `
      <span class="cat-icon">${cat.icon}</span>
      <span class="cat-label">${cat.label}${cat.starred ? ' <span class="cat-star">★</span>' : ''}</span>
      <span class="cat-when">${cat.when}</span>
    `;
    det.appendChild(summary);

    const sub = document.createElement("div");
    sub.className = "cat-sub";
    sub.textContent = cat.subtitle;
    det.appendChild(sub);

    const filesDiv = document.createElement("div");
    filesDiv.className = "files";

    if (!cat.files.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "(none)";
      filesDiv.appendChild(empty);
    }

    for (const f of cat.files) {
      totalFiles++;
      if (f.exists) { totalExists++; totalSize += f.size_bytes || 0; }

      const row = document.createElement("div");
      row.className = "file" + (f.exists ? "" : " missing");
      row.dataset.search = (f.label + " " + f.path).toLowerCase();

      const main = document.createElement("div");
      main.innerHTML = `
        <div class="file-label">${f.label}</div>
        <div class="file-path">${fmtFromHome(f.path)}</div>
        <div class="file-how">↳ ${f.how}</div>
      `;
      row.appendChild(main);

      const meta = document.createElement("div");
      meta.className = "file-meta";
      meta.innerHTML = `
        <div><span class="file-when">${f.when}</span></div>
        <div style="margin-top:4px;">${fmtSize(f.size_bytes)} · ${fmtDate(f.mtime_iso)}</div>
      `;
      row.appendChild(meta);

      const acts = document.createElement("div");
      acts.className = "file-actions";
      if (f.exists) {
        const editUrl = "vscode://file" + encodeURI(f.path);
        const fileUrl = "file://" + encodeURI(f.is_dir ? f.path : f.path.substring(0, f.path.lastIndexOf("/")));
        acts.innerHTML = `
          <a class="btn btn-primary" href="${editUrl}" data-edit>📝 Edit</a>
          <a class="btn" href="${fileUrl}" target="_blank">📁</a>
        `;
      } else {
        acts.innerHTML = `<span class="btn" style="cursor:default;opacity:0.5;">none</span>`;
      }
      row.appendChild(acts);

      filesDiv.appendChild(row);
    }

    det.appendChild(filesDiv);
    root.appendChild(det);
  }

  // total bar
  const t = document.getElementById("totals");
  t.innerHTML = `
    <span class="stat"><strong>${totalExists}</strong> / ${totalFiles} files exist</span>
    <span class="stat">total <strong>${fmtSize(totalSize)}</strong></span>
    <span class="stat">${TREE.categories.length} categories</span>
  `;
}

// search — instant filter
function applySearch(q) {
  q = (q || "").toLowerCase().trim();
  const rows = document.querySelectorAll(".file");
  let visible = 0;
  rows.forEach(r => {
    const match = !q || r.dataset.search.includes(q);
    r.style.display = match ? "" : "none";
    if (match) visible++;
  });
  document.querySelectorAll("details").forEach(d => {
    if (q) d.open = true;
    const cat = d.querySelectorAll(".file");
    const anyVisible = Array.from(cat).some(r => r.style.display !== "none");
    d.style.display = (q && !anyVisible) ? "none" : "";
  });
}

const search = document.getElementById("search");
search.addEventListener("input", e => applySearch(e.target.value));

// keyboard — vim-like
let cursor = -1;
function visibleRows() {
  return Array.from(document.querySelectorAll(".file")).filter(r => r.style.display !== "none");
}
function moveCursor(delta) {
  const rows = visibleRows();
  if (!rows.length) return;
  cursor = Math.max(0, Math.min(rows.length - 1, cursor + delta));
  rows.forEach((r, i) => {
    r.style.outline = (i === cursor) ? "2px solid var(--accent)" : "";
    r.style.outlineOffset = (i === cursor) ? "-2px" : "";
  });
  rows[cursor].scrollIntoView({ block: "nearest" });
}
document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT") {
    if (e.key === "Escape") { search.value = ""; applySearch(""); search.blur(); }
    return;
  }
  if (e.key === "/") { e.preventDefault(); search.focus(); }
  else if (e.key === "j") { e.preventDefault(); moveCursor(1); }
  else if (e.key === "k") { e.preventDefault(); moveCursor(-1); }
  else if (e.key === "g") { cursor = -1; moveCursor(1); }
  else if (e.key === "G") { cursor = visibleRows().length; moveCursor(-1); }
  else if (e.key === "Enter") {
    const rows = visibleRows();
    if (cursor >= 0 && rows[cursor]) {
      const link = rows[cursor].querySelector("a[data-edit]");
      if (link) link.click();
    }
  }
});

render();
</script>
</body>
</html>
"""


def render_html_tree(tree: dict) -> str:
    """Render build_injection_tree result as a single HTML string.

    Zero dependencies: all CSS/JS is inline. JSON is safely escaped to protect </script>.
    """
    import json as _json
    from datetime import datetime as _dt
    json_str = _json.dumps(tree, ensure_ascii=False).replace("</", "<\\/")
    html = HTML_TEMPLATE
    html = html.replace("__TREE_JSON__", json_str)
    html = html.replace("__CURRENT_CWD__", _html_escape(tree["meta"].get("current_cwd", "(none)")))
    html = html.replace("__HOME__", _html_escape(tree["meta"].get("home", "")))
    html = html.replace("__GENERATED__", _dt.now().strftime("%Y-%m-%d %H:%M:%S"))
    return html


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def open_in_browser(html_content: str, sid_hint: str = "current") -> tuple[bool, str, Optional[Path]]:
    """Write HTML to /tmp and spawn the OS default browser.

    Same sid overwrites the file to avoid /tmp accumulation; the OS cleans /tmp on boot.

    Applies subprocess preconditions [1][2][3], same as spawn_editor.

    Returns:
        (success flag, user message, html_path)
    """
    sid_safe = "".join(c for c in sid_hint if c.isalnum() or c in "-_")[:32] or "current"
    html_path = Path("/tmp") / f"gccfork-tree-{sid_safe}.html"
    try:
        html_path.write_text(html_content, encoding="utf-8")
    except OSError as exc:
        return False, f"❌ HTML write failed: {exc}", None

    # browser spawn — Linux=xdg-open, macOS=open, fallback=$BROWSER
    opener = None
    for cand in ("xdg-open", "open"):
        if shutil.which(cand):
            opener = cand
            break
    if opener is None:
        env_browser = os.environ.get("BROWSER")
        if env_browser and shutil.which(env_browser.split()[0]):
            opener = env_browser
    if opener is None:
        return False, (
            f"❌ browser opener not found (xdg-open / open / $BROWSER all unavailable). "
            f"manual: file://{html_path}"
        ), html_path

    try:
        subprocess.Popen(
            opener.split() + [str(html_path)],
            stdout=subprocess.DEVNULL,   # ⚠️[2] protect TUI screen
            stderr=subprocess.DEVNULL,   # ⚠️[2]
            stdin=subprocess.DEVNULL,
            start_new_session=True,      # ⚠️[1] keep alive after TUI exits
            close_fds=True,
        )
    except FileNotFoundError:
        return False, f"❌ '{opener}' launch failed (race condition)", html_path
    except OSError as exc:
        return False, f"❌ browser spawn OSError: {exc}", html_path

    return True, f"🌐 opened tree in browser ({html_path.name})", html_path
