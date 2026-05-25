"""gccfork settings modal and section registry sidecar module.

Main module usage:
    from gccfork_settings import SettingsScreen, get_deep_prefs_snapshot, get_scannable_text

    # Action-bar settings button handler:
    self.push_screen(SettingsScreen())

    # Deep-search worker:
    prefs = get_deep_prefs_snapshot()
    scannable = get_scannable_text(obj, prefs)   # '' means the line is noise and skipped
    if scannable and line_match(scannable):
        ...

Adding a new settings section:
    1. Add one dict to the list returned by `get_settings_sections()`.
       - "type": "checkboxes" uses items: [{key, label, hint, default}, ...]
       - "type": "text" uses content: "<read-only body>"
    2. Checkbox keys are prefs keys; read them with `pref_get(key, default)`.
    3. SettingsScreen.compose automatically renders the new section.

Five deep-search noise categories added on 2026-04-27:
    - attachment            : Claude Code auto-attachment metadata such as file names/paths
    - file-history-snapshot : cwd file snapshots
    - tool_result           : tool output such as ls/find/git
    - tool_use args         : tool_use input such as command/path
    - system/internal       : system, isMeta, isSidechain, <system-reminder>, /command input, thinking
"""
from __future__ import annotations

import json
import re
import sys

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RadioButton, RadioSet, Static, TextArea

from gccfork_i18n import LANGUAGE_PREF_KEY, current_language, set_language_pref, tr


# Same values as main.INTERNAL_USER_PREFIXES; parse_session uses the same strings.
# User messages starting with these prefixes are treated as automatic system text.
_INTERNAL_USER_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<bash-stdout>",
    "<bash-stderr>",
    "Caveat: The messages below were generated",
    "<system-reminder>",
)


class SettingsBrainButton(Static, can_focus=True):
    """One-line brain button for settings panes."""

    def __init__(self, target: str) -> None:
        super().__init__("🧠 AI", id=f"btn-settings-brain-{target}")
        self.target = target

    async def on_click(self) -> None:
        screen = self.screen
        if hasattr(screen, "_open_settings_brain_agent"):
            screen._open_settings_brain_agent(self.target)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"enter", "space"}:
            event.prevent_default()
            event.stop()
            screen = self.screen
            if hasattr(screen, "_open_settings_brain_agent"):
                screen._open_settings_brain_agent(self.target)


# ─── Deep-search noise filters ───────────────────────────────────────────────
# default=False means unchecked, noise filtering on, excluded from matcher.
# Checking a category includes it in matching and restores the old behavior.
DEEP_SEARCH_ITEMS: list[dict] = [
    {
        "key": "deep_include_attachment",
        "label": "Include [attachment] lines in matching",
        "hint": "File metadata auto-attached by Claude Code. File names and paths often create noise.",
        "default": False,
    },
    {
        "key": "deep_include_file_history",
        "label": "Include [file-history-snapshot] in matching",
        "hint": "cwd file snapshot on session start/resume. File-name substring matches are common.",
        "default": False,
    },
    {
        "key": "deep_include_tool_result",
        "label": "Include tool_result body in matching (ls/find/git/cat output, etc.)",
        "hint": "Tool output often contains file names and directory listings that cause unrelated matches.",
        "default": False,
    },
    {
        "key": "deep_include_tool_use_args",
        "label": "Include tool_use arguments (command/path/...) in matching",
        "hint": "Tool-call metadata such as Bash command or Read/Edit file_path.",
        "default": False,
    },
    {
        "key": "deep_include_system_internal",
        "label": "Include system / <system-reminder> / internal messages in matching",
        "hint": "Automatic system text, /command input, isMeta/isSidechain, and thinking blocks.",
        "default": False,
    },
    {
        "key": "deep_include_fuzzy",
        "label": "Allow fuzzy matching (rapidfuzz partial_ratio >= 80)",
        "hint": "Similar words can match. Example: searching 'altera' can match 'alternate-screen'. Default OFF.",
        "default": False,
    },
]


# ─── Slim modes: three presets, strong / medium / weak ───────────────────────
# Each mode times five categories creates 15 prefs keys. KEEP=True preserves
# that category line or a stub; False drops it.
#
# Default policy:
#   strict:   current structure; all five false, text only, smallest slim
#   balanced: text plus one-line tool_use, partial system_internal, error tool_result
#   loose:    balanced plus first attachment and short tool_result output
SLIM_CATEGORIES: list[dict] = [
    {
        "cat": "attachment",
        "label": "Preserve [attachment] as a one-line stub for the first attachment",
        "hint": "Shows what the user first put into context. Repeated reattachments are always excluded.",
    },
    {
        "cat": "file_history",
        "label": "Preserve [file-history-snapshot]",
        "hint": "cwd file-change snapshot. Usually noise; preserved as a one-line stub when kept.",
    },
    {
        "cat": "tool_result",
        "label": "Preserve tool_result (errors first, short output)",
        "hint": "Tool output from Bash/ls/cat, etc. Errors are always kept; successful output is kept only up to 200 chars.",
    },
    {
        "cat": "tool_use_args",
        "label": "Preserve tool_use arguments (name plus short input)",
        "hint": "Trace what the AI called. One-line summary with name plus the first 80 input chars.",
    },
    {
        "cat": "system_internal",
        "label": "Preserve system / thinking / slash commands",
        "hint": "First thinking sentence, explicit user slash commands such as /compact, and compact summaries.",
    },
]

SLIM_MODE_DEFAULTS: dict[str, dict[str, bool]] = {
    "strong": {"attachment": False, "file_history": False, "tool_result": False, "tool_use_args": False, "system_internal": False},
    "medium": {"attachment": False, "file_history": False, "tool_result": True,  "tool_use_args": True,  "system_internal": True},
    "weak":   {"attachment": True,  "file_history": True,  "tool_result": True,  "tool_use_args": True,  "system_internal": True},
}

SLIM_MODE_LABELS: dict[str, tuple[str, str]] = {
    "strong": ("🔻 Slim (strong)", "Text only; smallest output, about 3% of original. Default."),
    "medium": ("🔻 Slim (medium)", "Text plus tool-call trace and first thinking sentence; readable flow, about 15% of original."),
    "weak":   ("🔻 Slim (weak)",   "medium plus attachments and file history; conservative slim, about 50% of original."),
}

CODEX_SLIM_MODE_LABELS: dict[str, tuple[str, str]] = {
    "safe": (
        "Safe mode",
        "Keeps all previous compact summaries, the current slim body, and the latest 10 raw turns. Prioritizes recovery/review.",
    ),
    "strong": (
        "Strong mode",
        "Keeps all previous compact summaries, the current slim body, and only the latest 3 raw turns. Prioritizes context space.",
    ),
}

CODEX_SLIM_MODE_DEFAULT_KEEP: dict[str, int] = {
    "safe": 10,
    "strong": 3,
}

# Legacy key migration aliases for CLI and old registry compatibility.
SLIM_MODE_ALIASES: dict[str, str] = {
    "strict": "strong",
    "balanced": "medium",
    "loose": "weak",
}

# Defaults used when the claude `/slim` slash command delegates to gccfork TUI.
# Users can change these in the SettingsScreen [Slim] tab.
#
# Protected turns vary by mode, forming a reasonable strength gradient:
#   strong -> 5 turns, aggressive trim and short protection
#   medium -> 10 turns, balanced
#   weak   -> 30 turns, more preservation and longer protection
# claude `/slim` reads the turns key for the mode pointed to by `slim_default_mode`.
SLIM_DEFAULT_PREFS: dict[str, "str | int | bool"] = {
    "slim_default_mode": "strong",                # strong | medium | weak
    "slim_default_reload": True,                  # True auto-resumes after slim; False only slims on disk
    "slim_strong_keep_recent_turns": 5,
    "slim_medium_keep_recent_turns": 10,
    "slim_weak_keep_recent_turns": 30,
    "slim_default_anti_fragmentation": True,      # True bundles included-context lines in place; recommended for Claude recognition
    "slim_default_dynamic_cap": True,             # True auto-adjusts cap by jsonl size to fit 1M context
    "slim_default_visible_cap_compact": True,     # True moves non-context region before native compact marker on cap overflow
    "slim_default_send_other_env": False,         # True defaults to sending to another environment, such as VSCode bridge or gnome-terminal
    "slim_default_newtab": False,                 # True opens in a new tab; False opens a new window
    "codex_slim_default_mode": "strong",          # safe | strong
    "codex_slim_keep_recent": 3,                  # Default number of latest Codex user turns preserved by /slim
    "codex_slim_include_compact_summaries": True, # Include previous compact summaries in the new context
    "codex_slim_trim_recent_tools": True,         # 제거 tool/plumbing rows even from recent raw protected turns
    "codex_slim_compact_event_types": "",         # Extra event_msg.payload.type values treated as compact summary sources
    "codex_slim_compact_text_keys": "",           # Extra payload keys searched for compact summary text
    "codex_slim_default_clone": False,            # True creates a slim clone while preserving the original
    "codex_slim_default_reload": True,            # True reopens/restarts after slim
    "codex_slim_default_send_other_env": False,
    "codex_slim_default_newtab": False,
}


def _slim_items_for_mode(mode: str) -> list[dict]:
    """Build one mode's prefs items from the 3-mode by 5-category matrix."""
    defaults = SLIM_MODE_DEFAULTS[mode]
    return [
        {
            "key": f"slim_{mode}_keep_{c['cat']}",
            "label": c["label"],
            "hint": c["hint"],
            "default": defaults[c["cat"]],
        }
        for c in SLIM_CATEGORIES
    ]



# ─── Phase A archive (2026-05-06) ───────────────────────────────────────
# get_slim_mode_prefs, slim_line_verdict, and _stub_* helpers moved to
# bin/_archive_2026-05-06_phase_a_python/verdict_python.py.
# Rust is the single processing path, routed through ~/.local/bin/gccfork
# _call_rust_slim_general() via subprocess.

def get_settings_sections() -> list[dict]:
    """Build the section registry at call time so HelpScreen import failures are safe.

    Section dict keys:
      - id: identifier for CSS and future routing
      - title: visible header text
      - type: "checkboxes" | "text"
      - intro: optional one-line description below the header
      - items: (checkboxes) [{key, label, hint, default}, ...]
      - content: read-only text body
    """
    out: list[dict] = [
        {
            "id": "deep-search",
            "title": "🔬 Deep Search — Body Matcher Noise Filters",
            "type": "checkboxes",
            "intro": (
                "Checked = include in matching and allow noise. Unchecked = exclude from matcher.\n"
                "Default: all five unchecked, so matches come only from user-entered body text."
            ),
            "items": DEEP_SEARCH_ITEMS,
        },
    ]
    # Three slim modes, each with five categories, generate 15 checkboxes.
    for mode in ("strong", "medium", "weak"):
        title, intro = SLIM_MODE_LABELS[mode]
        out.append({
            "id": f"slim-{mode}",
            "title": title,
            "type": "checkboxes",
            "intro": (
                f"{intro}\n"
                "Checked = preserve that category, possibly as a stub. Unchecked = drop it completely."
            ),
            "items": _slim_items_for_mode(mode),
        })
    # Archive section: read-only current prefs display; RadioSet UI follows below.
    out.append({
        "id": "archive",
        "title": "🗂 Archive / Merge — Current Settings",
        "type": "text",
        "content": _build_archive_settings_text(),
    })
    out.append({
        "id": "help",
        "title": "❓ Help",
        "type": "text",
        "content": _load_help_text(),
    })
    return out


# ─── Archive option specs used by settings UI ────────────────────────────────
# Each option:
#   key    — prefs key, matching gccfork_archive.ARCHIVE_DEFAULTS
#   kind   — "radio" (enum) | "bool" (Checkbox)
#   label  — visible UI label
#   hint   — optional explanation
#   choices — for radio: [(value, label), ...]
ARCHIVE_OPTIONS: list[dict] = [
    {
        "key": "archive_preview_mode",
        "kind": "radio",
        "label": "Preview integration mode",
        "hint": "How archived children are shown in the parent preview",
        "choices": [
            ("tail_sections", "📜 Append child sections at the end (default)"),
            ("interleave", "⏱ Interleave by timestamp"),
            ("headers_only", "📋 Headers only (compact)"),
            ("split", "↕ Split picker"),
        ],
    },
    {
        "key": "archive_search_includes_children",
        "kind": "bool",
        "label": "Include child bodies in deep search",
    },
    {
        "key": "archive_important_handling",
        "kind": "radio",
        "label": "When archiving sessions marked important (★)",
        "choices": [
            ("confirm", "Ask for one more confirmation (safe)"),
            ("auto_include", "Include automatically"),
            ("reject", "Reject until ★ is removed"),
        ],
    },
    {
        "key": "archive_restore_enabled",
        "kind": "radio",
        "label": "Restore behavior",
        "choices": [
            ("trash_pattern", "🗑 Trash pattern (restorable)"),
            ("permanent", "⛔ Permanent (not restorable)"),
        ],
    },
    {
        "key": "archive_trigger_mode",
        "kind": "radio",
        "label": "Trigger entry point",
        "choices": [
            ("both", "🗂 Button + Ctrl+Shift+M"),
            ("button", "🗂 Button only"),
            ("keybinding", "Ctrl+Shift+M only"),
        ],
    },
    {
        "key": "archive_lazy_load",
        "kind": "bool",
        "label": "Lazy load, showing only the first 5KB of child bodies to protect heavy jsonl files",
    },
    {
        "key": "archive_child_color_distinction",
        "kind": "bool",
        "label": "Distinguish children by color using each child's root color",
    },
    {
        "key": "archive_section_header_format",
        "kind": "radio",
        "label": "Child section header format",
        "choices": [
            ("simple", "▶ short_id  name"),
            ("verbose", "▶ short_id  name  ·  N turns  ·  KB  ·  archived_at"),
        ],
    },
    {
        "key": "archive_child_sort_order",
        "kind": "radio",
        "label": "Child sort order",
        "choices": [
            ("mtime", "Recently modified first"),
            ("branch_order", "Branch time order (archived_at)"),
            ("alphabetic", "Alphabetical by name"),
        ],
    },
    {
        "key": "archive_folder_layout",
        "kind": "radio",
        "label": "Archive folder layout",
        "choices": [
            ("per_project", "Per project — <P>/archive/"),
            ("central", "Central — ~/.claude/gccfork-archive/<P>/"),
        ],
    },
    # ── True Merge options, model B / Phase 6 ────────────────────────────────
    {
        "key": "merge_stitching_method",
        "kind": "radio",
        "label": "🗂 Merge stitching method for active jsonl",
        "hint": "Which integrated view to show in the newly merged session",
        "choices": [
            ("interleave",   "interleave — common + unique messages sorted by timestamp + origin prefix [sid HH:MM] (default)"),
            ("linear",       "linear — common + each unique tail as a sequential chain"),
            ("parallel",     "parallel — common + keep branches intact"),
            ("common-only",  "common-only — common only; drop unique tails"),
            ("as-sections",  "as-sections — common + section dividers + each unique tail"),
        ],
    },
]


def _build_archive_settings_text() -> str:
    """Show current archive option values and how to change them.

    Dotted option keys such as `archive.preview_mode` can collide with textual ids,
    so older versions used no checkbox UI here. The current UI keeps ids underscore-based.
    """
    try:
        from gccfork_archive import ARCHIVE_DEFAULTS, get_archive_pref
    except ImportError:
        return "(failed to import archive module)"

    LABELS = {
        "archive_preview_mode": ("Preview integration mode", "interleave / tail_sections / headers_only / split"),
        "archive_search_includes_children": ("Include child bodies in search", "true / false"),
        "archive_important_handling": ("When archiving important sessions", "auto_include / confirm / reject"),
        "archive_restore_enabled": ("Restore behavior", "trash_pattern / permanent"),
        "archive_trigger_mode": ("Trigger entry point", "keybinding / button / both"),
        "archive_lazy_load": ("Lazy load child body snippets", "true / false"),
        "archive_child_color_distinction": ("Distinguish children by color", "true / false"),
        "archive_section_header_format": ("Child header format", "simple / verbose"),
        "archive_child_sort_order": ("Child sort order", "mtime / branch_order / alphabetic"),
        "archive_folder_layout": ("Archive folder layout", "per_project / central"),
        # ── True Merge (Phase 6 model B) ─────────────────────────────────
        "merge_stitching_method": ("🗂 Merge stitching method", "linear / interleave / parallel / common-only / as-sections"),
    }
    # Include MERGE_DEFAULTS as well; on lazy-import failure, show archive only.
    DEFAULTS_ALL = dict(ARCHIVE_DEFAULTS)
    try:
        from gccfork_merge import MERGE_DEFAULTS
        DEFAULTS_ALL.update(MERGE_DEFAULTS)
    except Exception:
        pass

    def _get_pref(key):
        # Keys remain underscore-based (archive_X, merge_X); read them directly with pref_get.
        if key.startswith("archive_"):
            return get_archive_pref(key)
        if key.startswith("merge_"):
            from gccfork_sessions import pref_get as _pref_get
            return _pref_get(key, DEFAULTS_ALL.get(key))
        return None

    lines: list[str] = [
        "Current archive + merge option values (●=default, ◆=changed):",
        "",
    ]
    for key in DEFAULTS_ALL:
        label, choices = LABELS.get(key, (key, ""))
        default = DEFAULTS_ALL[key]
        current = _get_pref(key)
        marker = "●" if current == default else "◆"
        lines.append(f"  {marker} {label}")
        lines.append(f"     key:     {key}")
        lines.append(f"     current: {current!r}  (default: {default!r})")
        lines.append(f"     choices: {choices}")
        lines.append("")

    lines += [
        "How to change manually:",
        "  1. Add or edit prefs in ~/.claude/gccfork-registry.json",
            "     example: \"prefs\": { \"archive_preview_mode\": \"headers_only\" }",
        "  2. python3 -c \"from gccfork_sessions import pref_set; pref_set('archive.preview_mode', 'headers_only')\"",
        "  3. RadioSet-based UI is available in this settings pane",
        "",
        "🗂 Trigger after multi-select:",
        "  - [🗂 Merge] button in the multi-action bar (trigger_mode = button / both)",
        "  - Ctrl+Shift+M shortcut (trigger_mode = keybinding / both)",
    ]
    return "\n".join(lines)


def _load_help_text() -> str:
    """Load gccfork.HelpScreen.HELP_TEXT so help stays a single source of truth."""
    for module_name in ("gccfork", "__main__"):
        module = sys.modules.get(module_name)
        help_screen = getattr(module, "HelpScreen", None) if module else None
        help_text = getattr(help_screen, "HELP_TEXT", None)
        if isinstance(help_text, str) and help_text.strip():
            return help_text
    try:
        from gccfork import HelpScreen
        return HelpScreen.HELP_TEXT
    except Exception as exc:
        return (
            "[bold red]Failed to load help[/]\n\n"
            f"Could not find HelpScreen.HELP_TEXT: {type(exc).__name__}: {exc}"
        )


def _help_text_for_text_area(text: str) -> str:
    """제거 Rich markup because TextArea does not interpret it."""
    return re.sub(r"\[(?:/?[a-zA-Z][^\]]*|/)\]", "", str(text))


def _settings_items_text(title: str, intro: str | None, items: list[dict]) -> str:
    lines = [_help_text_for_text_area(title)]
    if intro:
        lines += ["", _help_text_for_text_area(intro)]
    for item in items:
        lines.append("")
        lines.append(f"- {_help_text_for_text_area(str(item.get('label', '')))}")
        hint = item.get("hint")
        if hint:
            lines.append(f"  {_help_text_for_text_area(str(hint))}")
    return "\n".join(lines).strip()


def _archive_options_text() -> str:
    lines = [
        "🗂 Archive / Merge Options",
        "",
        "Changes are saved to prefs immediately and apply from the next archive operation.",
    ]
    for opt in ARCHIVE_OPTIONS:
        lines.append("")
        lines.append(f"- {_help_text_for_text_area(str(opt.get('label', '')))}")
        hint = opt.get("hint")
        if hint:
            lines.append(f"  {_help_text_for_text_area(str(hint))}")
        choices = opt.get("choices") or []
        for value, label in choices:
            lines.append(f"  - {value}: {_help_text_for_text_area(str(label))}")
    return "\n".join(lines)


def _editor_options_text() -> str:
    return "\n".join(
        [
            "📝 Editor",
            "",
            "Open settings/memory files in an external editor with Ctrl+E or the edit button.",
            "auto resolves in this order: $EDITOR -> code -> cursor -> nano.",
        ]
    )


def _slim_options_text() -> str:
    lines = [
        "🪴 /slim Defaults",
        "",
        "Defaults for Claude/Codex /slim calls and the GccSlim slim button.",
        "",
        "Claude:",
        "- default mode: strong / medium / weak",
        "- bundling: condense included-context lines into large bundles",
        "- non-context compacting: move cap-overflow lines before the native compact boundary",
        "- auto-open after slim",
        "- send to another environment",
        "- open in a new tab",
        "- dynamic cap: measure jsonl size and auto-fit within 1M context",
        "- recent raw protected turns: preserve the latest user turns per mode",
        "",
        "Codex:",
        "- default mode: safe / strong",
        "- include previous compact summaries in chronological order before the new context",
        "- result structure: compact summaries #1..N -> current slim body -> recent raw protected turns",
        "- mode summary:",
    ]
    for mode in ("safe", "strong"):
        title, intro = CODEX_SLIM_MODE_LABELS[mode]
        keep = CODEX_SLIM_MODE_DEFAULT_KEEP[mode]
        lines.append(f"  - {title}: preserves {keep} recent user turns as raw by default. {_help_text_for_text_area(intro)}")
    lines.extend([
        "- keep: number of recent user turns preserved as raw",
        "- include previous compact summaries: collect compacted.payload.message values into the new context",
        "- preserve original: create a slim clone",
        "- auto-open after slim",
        "- send to another environment",
        "- open in a new tab",
        "",
        "General slim detail-preservation options:",
    ])
    for mode in ("strong", "medium", "weak"):
        title, intro = SLIM_MODE_LABELS[mode]
        lines.append("")
        lines.append(f"{_help_text_for_text_area(title)}")
        lines.append(_help_text_for_text_area(intro))
        for item in _slim_items_for_mode(mode):
            lines.append(f"- {_help_text_for_text_area(str(item.get('label', '')))}")
            hint = item.get("hint")
            if hint:
                lines.append(f"  {_help_text_for_text_area(str(hint))}")
    return "\n".join(lines)


# ─── External API used by deep-search worker ─────────────────────────────────
def get_deep_prefs_snapshot() -> dict:
    """Read all deep_include_* prefs from current prefs as one dict.

    Worker threads run asynchronously from main prefs, so use a start-time snapshot.
    """
    from gccfork import pref_get
    return {item["key"]: pref_get(item["key"], item["default"]) for item in DEEP_SEARCH_ITEMS}


def get_scannable_text(obj: dict, prefs: dict) -> str:
    """Convert one parsed jsonl object into lowercase body text for the matcher.

    Based on the deep_include_* flags:
      - if a category is disabled, return an empty string so matcher skips it
      - if enabled, return that category's body text lowercased

    Empty string means this line is not a match candidate.
    """
    if not isinstance(obj, dict):
        return ""

    typ = obj.get("type", "")

    # ── 1. attachment ─────────────────────────────────────────────────
    if typ == "attachment":
        return _full_obj_text(obj) if prefs.get("deep_include_attachment", False) else ""

    # ── 2. file-history-snapshot ──────────────────────────────────────
    if typ == "file-history-snapshot":
        return _full_obj_text(obj) if prefs.get("deep_include_file_history", False) else ""

    # ── 5-a. metadata types such as system / summary / permission-mode ──────
    if typ in ("system", "summary", "permission-mode", "last-prompt", "custom-title"):
        return _full_obj_text(obj) if prefs.get("deep_include_system_internal", False) else ""

    # ── 5-b. isMeta / isSidechain, even on user/assistant messages ─────────
    if obj.get("isMeta") or obj.get("isSidechain"):
        return _full_obj_text(obj) if prefs.get("deep_include_system_internal", False) else ""

    # ── user/assistant: inspect by content block ────────────────────────────
    msg = obj.get("message") or {}
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")

    # 5-c. user messages that start with internal prefixes such as <command-name>
    if typ == "user" and isinstance(content, str):
        if not prefs.get("deep_include_system_internal", False):
            stripped = content.lstrip()
            for prefix in _INTERNAL_USER_PREFIXES:
                if stripped.startswith(prefix):
                    return ""

    parts: list[str] = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                # Real user/assistant body text is always included.
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif bt == "tool_use":
                # 4. tool_use arguments such as command/path.
                if prefs.get("deep_include_tool_use_args", False):
                    name = b.get("name")
                    if isinstance(name, str):
                        parts.append(name)
                    inp = b.get("input")
                    if inp is not None:
                        try:
                            parts.append(json.dumps(inp, ensure_ascii=False))
                        except (TypeError, ValueError):
                            parts.append(str(inp))
            elif bt == "tool_result":
                # 3. tool_result body such as ls/find/git/cat output.
                if prefs.get("deep_include_tool_result", False):
                    tr = b.get("content")
                    if isinstance(tr, str):
                        parts.append(tr)
                    elif isinstance(tr, list):
                        for x in tr:
                            if isinstance(x, dict):
                                t = x.get("text")
                                if isinstance(t, str):
                                    parts.append(t)
            elif bt == "thinking":
                # 5-d. thinking blocks are controlled by the system/internal toggle.
                if prefs.get("deep_include_system_internal", False):
                    th = b.get("thinking")
                    if isinstance(th, str):
                        parts.append(th)

    return " ".join(p for p in parts if p).lower()


def _full_obj_text(obj: dict) -> str:
    """Return full raw json lowercased for enabled categories, matching old worker behavior."""
    try:
        return json.dumps(obj, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(obj).lower()


# ─── SettingsScreen modal ───────────────────────────────────────────────────
class SettingsScreen(ModalScreen[None]):
    """gccfork settings modal with the same visual tone as the slim modal.

    Layout follows the slim modal pattern:
      ┌─ #settings-box (round $accent 50%) ─────────────┐
      │ ┌─ #settings-header (brand left, title center, version right)
      │ ├─ #settings-tabs ([Search] [Slim] [Help])
      │ ├─ #settings-content                             │
      │ │   ─ #pane-search   : scrolls inside the tab
      │ │   ─ #pane-slim     : scrolls inside the tab
      │ │   ─ #pane-help     : TextArea direct scroll/select
      │ └─ #settings-btn-row (changed count, spacer, close)
      └────────────────────────────────────────────────┘

    Tab switching displays only the active tab.
    Slim mode switching uses ModeCard clicks and _select_mode(), matching the slim modal.
    """
    BINDINGS = [
        Binding("escape", "close", "닫기", show=False),
        Binding("q", "close", "닫기", show=False),
        Binding("1", "switch_tab('search')", "검색 탭", show=False),
        Binding("2", "switch_tab('slim')", "슬림 탭", show=False),
        Binding("3", "switch_tab('archive')", "병합 탭", show=False),
        Binding("4", "switch_tab('editor')", "편집기 탭", show=False),
        Binding("5", "switch_tab('advisor')", "권고 설치 탭", show=False),
        Binding("6", "switch_tab('help')", "도움말 탭", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._active_tab: str = "search"
        self._slim_sub_tab: str = "claude"
        self._slim_mode: str = "strong"
        self._slim_advanced_open: bool = False

    def compose(self) -> ComposeResult:
        from gccfork import (
            pref_get,
            GCCFORK_VERSION,
            CopyMenuOverlay,
            ModeCard,
            SelectableTextArea,
        )

        # Cache section dicts by id.
        sections = {s.get("id", ""): s for s in get_settings_sections()}
        deep_section = sections.get("deep-search", {})
        help_section = sections.get("help", {})
        archive_section = sections.get("archive", {})
        slim_sections = {
            mode: sections.get(f"slim-{mode}", {})
            for mode in ("strong", "medium", "weak")
        }

        with Vertical(id="settings-box"):
            # ── Header ─────────────────────────────────────────────────────
            with Horizontal(id="settings-header"):
                yield Static("[b]GccForK[/]", id="settings-brand", markup=True)
                yield Static(f"[b]{tr('settings.title', '⚙ Settings')}[/]", id="settings-title", markup=True)
                yield Static(
                    f"[dim]v{GCCFORK_VERSION}[/]",
                    id="settings-meta", markup=True,
                )

            # ── Scope toggle removed on 2026-05-08 ─────────────────────────
            # gccfork always launches from a project cwd, so a global/project
            # choice is not meaningful to users. All prefs automatically save to
            # <cwd>/.gccfork/ccfork-prefs.json under Policy B. Backend support is
            # kept with default="project"; only the UI toggle is removed.

            # ── Tab bar ───────────────────────────────────────────────────
            with Horizontal(id="settings-tabs"):
                yield Button(tr("settings.tabs.search", "Search"), id="tab-search", classes="settings-tab -active")
                yield Button(tr("settings.tabs.slim", "Slim"), id="tab-slim", classes="settings-tab")
                yield Button(tr("settings.tabs.merge", "Merge"), id="tab-archive", classes="settings-tab")
                yield Button(tr("settings.tabs.edit", "Edit"), id="tab-editor", classes="settings-tab")
                yield Button(tr("settings.tabs.guide", "Guide"), id="tab-advisor", classes="settings-tab")
                yield Button(tr("settings.tabs.help", "Help"), id="tab-help", classes="settings-tab")

            # ── Body: each tab owns its own viewport; no shared parent scroll. ─
            with Vertical(id="settings-content"):
                # ─── Search pane ───────────────────────────────────────────
                with VerticalScroll(id="pane-search", classes="settings-pane settings-pane-scroll"):
                    yield Static(
                        deep_section.get("title", "🔬 Deep Search"),
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _settings_items_text(
                            deep_section.get("title", "🔬 Deep Search"),
                            deep_section.get("intro"),
                            deep_section.get("items", []),
                        ),
                        id="settings-search-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    intro = deep_section.get("intro")
                    if intro:
                        yield Static(intro, classes="settings-intro")
                    for item in deep_section.get("items", []):
                        value = pref_get(item["key"], item["default"])
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                item["label"],
                                value=bool(value),
                                id=f"chk-{item['key']}",
                                classes="settings-checkbox",
                            )
                            hint = item.get("hint")
                            if hint:
                                yield Static(
                                    f"↳ {hint}",
                                    classes="settings-item-hint",
                                )
                    with Horizontal(classes="settings-section-actions"):
                        yield Button(
                            "↻ Reset to defaults",
                            id="btn-reset-deep-search",
                            classes="settings-reset-btn",
                        )

                # ─── Slim pane ─────────────────────────────────────────────
                with VerticalScroll(id="pane-slim", classes="settings-pane settings-pane-scroll"):
                    # ─── /slim defaults, separated for Claude and Codex ───────
                    with Horizontal(classes="settings-pane-head"):
                        yield Static(
                            "[b]🪴 /slim Defaults[/]  [dim]· automatically applied to Claude / Codex calls[/]",
                            classes="settings-pane-title", markup=True,
                        )
                        yield Static("", classes="settings-spacer")
                        yield SettingsBrainButton("slim")
                    yield SelectableTextArea(
                        _slim_options_text(),
                        id="settings-slim-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    with Horizontal(id="settings-slim-subtabs"):
                        yield Button("Claude", id="btn-slim-sub-claude", classes="settings-tab -active")
                        yield Button("Codex", id="btn-slim-sub-codex", classes="settings-tab")

                    with Vertical(id="slim-pane-claude", classes="settings-slim-subpane"):
                        yield Static(
                            "Claude's `/slim` command delegates to gccfork without extra options. "
                            "The items below match the default selections in the slim button modal.",
                            classes="settings-intro", markup=True,
                        )
                        cur_def_mode = str(pref_get("slim_default_mode", "strong"))
                        yield Static(
                            "[b]Default mode[/]",
                            classes="settings-radio-label", markup=True,
                        )
                        with RadioSet(id="rs-slim_default_mode", classes="settings-radioset"):
                            for m in ("strong", "medium", "weak"):
                                yield RadioButton(
                                    m,
                                    value=(cur_def_mode == m),
                                    id=f"rb-slim_default_mode-{m}",
                                    classes="settings-radio",
                                )
                        yield Static(
                            "[b]Application structure[/]\n"
                            "Full session\n"
                            "├─ compact summaries / archive   preserved\n"
                            "└─ active region\n"
                            "   ├─ non-context region         compacted\n"
                            "   └─ context-included region    bundled\n"
                            "      └─ latest N raw turns      preserved as raw",
                            classes="settings-intro",
                            markup=True,
                        )
                        # Same order and wording as the modal.
                        # 1) Bundling.
                        cur_anti_frag = bool(pref_get("slim_default_anti_fragmentation", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Bundle processing (condense included-context region into larger bundles)",
                                value=cur_anti_frag,
                                id="chk-slim_default_anti_fragmentation",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Applies in-place by grouping old turns into bundle structures. "
                                "Cleans the context-included region while preserving the latest N user turns raw.",
                                classes="settings-item-hint",
                            )

                        # 2) Non-context compacting. The modal shows this only on
                        # cap_overflow, but settings expose the default policy always.
                        cur_cap_compact = bool(pref_get("slim_default_visible_cap_compact", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Compact non-context region (outside cap)",
                                value=cur_cap_compact,
                                id="chk-slim_default_visible_cap_compact",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Moves the non-context region outside the Claude Code cap (~230 messages) "
                                "before the native compact boundary. Raw data remains in jsonl.",
                                classes="settings-item-hint",
                            )

                        # 3) Auto-open after slim, a user-facing name for hot reload.
                        cur_def_reload = bool(pref_get("slim_default_reload", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Auto-open after slim",
                                value=cur_def_reload,
                                id="chk-slim_default_reload",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Automatically resumes the same sid in a new terminal/tab after slim. "
                                "If off, only slims on disk and applies on the next resume.",
                                classes="settings-item-hint",
                            )

                        # 3-└) Send to another environment; equivalent to modal's environment-specific send option.
                        cur_other_env = bool(pref_get("slim_default_send_other_env", False))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ Send to another environment (VSCode / gnome-terminal)",
                                value=cur_other_env,
                                id="chk-slim_default_send_other_env",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Enabled when auto-open is on. The modal shows the currently detected environment name.",
                                classes="settings-item-hint",
                            )

                        # 3-└) Open in a new tab.
                        cur_newtab = bool(pref_get("slim_default_newtab", False))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ Open in a new tab (unchecked = new window)",
                                value=cur_newtab,
                                id="chk-slim_default_newtab",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Enabled when auto-open is on and the target is not VSCode.",
                                classes="settings-item-hint",
                            )

                        # 4) Dynamic cap, settings-only automatic policy.
                        cur_dyn_cap = bool(pref_get("slim_default_dynamic_cap", True))
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "📏 Dynamic cap (measure jsonl size and auto-fit within 1M context)",
                                value=cur_dyn_cap,
                                id="chk-slim_default_dynamic_cap",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Large sessions automatically shrink trim cap; small sessions preserve raw. "
                                "Applies only when bundle processing is ON. If OFF, mode-specific fixed caps apply (200/500/1000). "
                                "Not exposed in the modal because this is an automatic system policy.",
                                classes="settings-item-hint",
                            )
                        yield Static(
                            "[b]Recent raw protected turns[/]",
                            classes="settings-radio-label", markup=True,
                        )
                        for mode in ("strong", "medium", "weak"):
                            turns_key = f"slim_{mode}_keep_recent_turns"
                            cur_turns = int(
                                pref_get(turns_key, SLIM_DEFAULT_PREFS.get(turns_key, 5)) or 5
                            )
                            with Horizontal(classes="settings-turn-row"):
                                yield Static(
                                    f"{mode:<7}",
                                    classes="settings-radio-label",
                                )
                                yield Input(
                                    value=str(cur_turns),
                                    id=f"input-{turns_key}",
                                    type="integer",
                                    classes="settings-input",
                                )
                                yield Static(
                                    "user turns preserved raw",
                                    classes="settings-item-hint",
                                )
                        yield Static(
                            "↳ Counted by user messages. Preserves this much of the latest conversation flow as raw.",
                            classes="settings-item-hint",
                        )
                        with Horizontal(classes="settings-section-actions"):
                            yield Button(
                                "↻ Reset /slim defaults",
                                id="btn-reset-slim-defaults",
                                classes="settings-reset-btn",
                            )

                        yield Button(
                            "▶ General slim detail-preservation options",
                            id="btn-toggle-slim-advanced",
                            classes="settings-reset-btn",
                        )
                        with Vertical(id="slim-advanced-pane", classes="settings-slim-sub"):
                            yield Static(
                                "[b][Advanced][/] General slim detail-preservation options",
                                classes="settings-pane-title", markup=True,
                            )
                            yield Static(
                                "Legacy detail options that matter when bundle processing is off or the general slim path is used.",
                                classes="settings-intro",
                            )
                            with Horizontal(id="settings-slim-mode-row"):
                                for mode in ("strong", "medium", "weak"):
                                    sec = slim_sections.get(mode, {})
                                    mode_title = sec.get("title", mode)
                                    mode_intro = sec.get("intro", "").split("\n")[0]
                                    badge = " [b](default)[/]" if mode == "strong" else ""
                                    card = ModeCard(
                                        f"[b]{mode_title}[/]{badge}\n[dim]{mode_intro[:48]}[/]",
                                        id=f"setting-mode-{mode}",
                                        classes="settings-mode-card",
                                        markup=True,
                                    )
                                    if mode == self._slim_mode:
                                        card.add_class("-selected")
                                    yield card

                            # Five checkboxes for the selected mode, one display-toggled subpane per mode.
                            for mode in ("strong", "medium", "weak"):
                                sec = slim_sections.get(mode, {})
                                with Vertical(
                                    id=f"slim-sub-{mode}",
                                    classes="settings-slim-sub",
                                ):
                                    sub_intro = sec.get("intro")
                                    if sub_intro:
                                        yield Static(sub_intro, classes="settings-intro")
                                    for item in sec.get("items", []):
                                        value = pref_get(item["key"], item["default"])
                                        with Vertical(classes="settings-checkbox-row"):
                                            yield Checkbox(
                                                item["label"],
                                                value=bool(value),
                                                id=f"chk-{item['key']}",
                                                classes="settings-checkbox",
                                            )
                                            hint = item.get("hint")
                                            if hint:
                                                yield Static(
                                                    f"↳ {hint}",
                                                    classes="settings-item-hint",
                                                )
                                    with Horizontal(classes="settings-section-actions"):
                                        yield Button(
                                            "↻ Reset this mode",
                                            id=f"btn-reset-slim-{mode}",
                                            classes="settings-reset-btn",
                                        )


                    with Vertical(id="slim-pane-codex", classes="settings-slim-subpane"):
                        yield Static(
                            "Defaults for the Codex TUI `/slim` command and the GccSlim Codex slim button. "
                            "The Codex wrapper reads the current project's .gccfork/ccfork-prefs.json.",
                            classes="settings-intro", markup=True,
                        )
                        cur_codex_mode = str(pref_get("codex_slim_default_mode", "strong"))
                        if cur_codex_mode not in CODEX_SLIM_MODE_LABELS:
                            cur_codex_mode = "strong"
                        yield Static("[b]Default mode[/]", classes="settings-radio-label", markup=True)
                        with RadioSet(id="rs-codex_slim_default_mode", classes="settings-radioset"):
                            for m in ("safe", "strong"):
                                yield RadioButton(
                                    m,
                                    value=(cur_codex_mode == m),
                                    id=f"rb-codex_slim_default_mode-{m}",
                                    classes="settings-radio",
                                )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Static("[b]Mode summary[/]", classes="settings-radio-label", markup=True)
                            for m in ("safe", "strong"):
                                title, intro = CODEX_SLIM_MODE_LABELS[m]
                                keep = CODEX_SLIM_MODE_DEFAULT_KEEP[m]
                                yield Static(
                                    f"[b]{title}[/]  [dim]preserves {keep} recent user turns as raw by default[/]\n"
                                    f"   {intro}",
                                    classes="settings-item-hint",
                                    markup=True,
                                )
                        with Horizontal(classes="settings-turn-row"):
                            yield Static("keep", classes="settings-radio-label")
                            yield Input(
                                value=str(int(pref_get("codex_slim_keep_recent", 3) or 3)),
                                id="input-codex_slim_keep_recent",
                                type="integer",
                                classes="settings-input",
                            )
                            yield Static("recent user turns preserved raw", classes="settings-item-hint")
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Include previous compact summaries in the new context",
                                value=bool(pref_get("codex_slim_include_compact_summaries", True)),
                                id="chk-codex_slim_include_compact_summaries",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ If compacted three times, summaries #1, #2, and #3 are collected chronologically before the current slim body.",
                                classes="settings-item-hint",
                            )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Trim tool calls/results even inside recent raw protected turns",
                                value=bool(pref_get("codex_slim_trim_recent_tools", True)),
                                id="chk-codex_slim_trim_recent_tools",
                                classes="settings-checkbox",
                            )
                            yield Static(
                                "↳ Recent conversation text remains raw; old tool outputs, token logs, and internal plumbing rows are removed.",
                                classes="settings-item-hint",
                            )
                        with Horizontal(classes="settings-turn-row"):
                            yield Static("compact event types +", classes="settings-radio-label")
                            yield Input(
                                value=str(pref_get("codex_slim_compact_event_types", "") or ""),
                                id="input-codex_slim_compact_event_types",
                                classes="settings-input",
                                placeholder="comma-separated extra event types",
                            )
                        yield Static(
                            "↳ Defaults already include context_compacted, compact_summary, conversation_compacted, auto_compacted.",
                            classes="settings-item-hint",
                        )
                        with Horizontal(classes="settings-turn-row"):
                            yield Static("compact text keys +", classes="settings-radio-label")
                            yield Input(
                                value=str(pref_get("codex_slim_compact_text_keys", "") or ""),
                                id="input-codex_slim_compact_text_keys",
                                classes="settings-input",
                                placeholder="comma-separated extra text keys",
                            )
                        yield Static(
                            "↳ Defaults already include message, summary, text, content, compact_summary.",
                            classes="settings-item-hint",
                        )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Preserve original: create a slim clone",
                                value=bool(pref_get("codex_slim_default_clone", False)),
                                id="chk-codex_slim_default_clone",
                                classes="settings-checkbox",
                            )
                            yield Static("↳ When ON, keeps the original JSONL and creates a new slim Codex SID.", classes="settings-item-hint")
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "Auto-open after slim",
                                value=bool(pref_get("codex_slim_default_reload", True)),
                                id="chk-codex_slim_default_reload",
                                classes="settings-checkbox",
                            )
                            yield Static("↳ For active wrapper sessions, uses the same-terminal restart marker.", classes="settings-item-hint")
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ Send to another environment (VSCode / gnome-terminal)",
                                value=bool(pref_get("codex_slim_default_send_other_env", False)),
                                id="chk-codex_slim_default_send_other_env",
                                classes="settings-checkbox",
                            )
                        with Vertical(classes="settings-checkbox-row"):
                            yield Checkbox(
                                "    └ Open in a new tab (unchecked = new window)",
                                value=bool(pref_get("codex_slim_default_newtab", False)),
                                id="chk-codex_slim_default_newtab",
                                classes="settings-checkbox",
                            )
                        with Horizontal(classes="settings-section-actions"):
                            yield Button(
                                "↻ Reset Codex /slim defaults",
                                id="btn-reset-codex-slim-defaults",
                                classes="settings-reset-btn",
                            )

                # ─── Merge pane with real RadioSet/Checkbox UI ──────────────
                with VerticalScroll(id="pane-archive", classes="settings-pane settings-pane-scroll"):
                    with Horizontal(classes="settings-pane-head"):
                        yield Static(
                            "[b]🗂 Archive / Merge — Options[/]",
                            classes="settings-pane-title", markup=True,
                        )
                        yield Static("", classes="settings-spacer")
                        yield SettingsBrainButton("archive")
                    yield SelectableTextArea(
                        _archive_options_text(),
                        id="settings-archive-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    yield Static(
                        "Changes are saved to prefs immediately and apply from the next archive operation.",
                        classes="settings-intro",
                    )
                    # Sidecar imports; combine archive and merge defaults.
                    try:
                        from gccfork_archive import ARCHIVE_DEFAULTS
                    except ImportError:
                        ARCHIVE_DEFAULTS = {}
                    try:
                        from gccfork_merge import MERGE_DEFAULTS
                    except ImportError:
                        MERGE_DEFAULTS = {}
                    DEFAULTS_COMBINED = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}

                    for opt in ARCHIVE_OPTIONS:
                        key = opt["key"]
                        default = DEFAULTS_COMBINED.get(key)
                        current = pref_get(key, default)
                        if opt["kind"] == "bool":
                            with Vertical(classes="settings-checkbox-row"):
                                yield Checkbox(
                                    opt["label"],
                                    value=bool(current),
                                    id=f"chk-{key}",
                                    classes="settings-checkbox",
                                )
                                hint = opt.get("hint")
                                if hint:
                                    yield Static(f"↳ {hint}", classes="settings-item-hint")
                        elif opt["kind"] == "radio":
                            yield Static(
                                f"[b]{opt['label']}[/]",
                                classes="settings-radio-label", markup=True,
                            )
                            hint = opt.get("hint")
                            if hint:
                                yield Static(f"↳ {hint}", classes="settings-item-hint")
                            radio_id = f"rs-{key}"
                            with RadioSet(id=radio_id, classes="settings-radioset"):
                                for value, choice_label in opt["choices"]:
                                    is_current = (str(current) == str(value))
                                    yield RadioButton(
                                        choice_label,
                                        value=is_current,
                                        id=f"rb-{key}-{value}",
                                        classes="settings-radio",
                                    )
                    with Horizontal(classes="settings-section-actions"):
                        yield Button(
                            "↻ Reset all to defaults",
                            id="btn-reset-archive",
                            classes="settings-reset-btn",
                        )

                # ─── Editor pane: config_editor RadioSet ───────────────────
                with VerticalScroll(id="pane-editor", classes="settings-pane settings-pane-scroll"):
                    yield Static(
                        "[b]📝 Editor — Ctrl+E or edit button[/]",
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _editor_options_text(),
                        id="settings-editor-copy",
                        classes="settings-select-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    yield Static(
                        "Click settings/memory files such as CLAUDE.md or MEMORY.md to spawn an external editor.",
                        classes="settings-intro",
                    )
                    try:
                        from gccfork_config_files import EDITOR_CANDIDATES, EDITOR_DEFAULT, resolve_editor
                    except ImportError:
                        EDITOR_CANDIDATES = ["code", "cursor", "nano"]
                        EDITOR_DEFAULT = "auto"
                        resolve_editor = None
                    cur_editor = pref_get("config_editor", EDITOR_DEFAULT)
                    yield Static(
                        "[b]Editor selection[/]",
                        classes="settings-radio-label", markup=True,
                    )
                    yield Static(
                        "↳ auto resolves in this order: $EDITOR -> code -> cursor -> nano",
                        classes="settings-item-hint",
                    )
                    with RadioSet(id="rs-config_editor", classes="settings-radioset"):
                        for value in [EDITOR_DEFAULT] + EDITOR_CANDIDATES:
                            label = f"{value} (automatic priority)" if value == EDITOR_DEFAULT else value
                            yield RadioButton(
                                label,
                                value=(str(cur_editor) == value),
                                id=f"rb-config_editor-{value}",
                                classes="settings-radio",
                            )
                    if resolve_editor is not None:
                        eff, reason = resolve_editor()
                        eff_text = f"Current effective editor: [b]{eff or '(none)'}[/]  [dim]· {reason}[/]"
                        yield Static(eff_text, classes="settings-intro", markup=True)
                    with Horizontal(classes="settings-section-actions"):
                        yield Button(
                            "↻ Restore auto",
                            id="btn-reset-editor",
                            classes="settings-reset-btn",
                        )

                # ─── Recommended install pane ──────────────────────────────
                # Cards for cleanup, stale instances, Claude/Codex /slim, and dingdong.
                # Users who dismissed or missed the automatic guide modal can revisit
                # and reinstall from here. Card bodies use SelectableTextArea so mouse
                # drag selection/copy works.
                with VerticalScroll(id="pane-advisor", classes="settings-pane settings-pane-scroll"):
                    yield Static(
                        f"[b]{tr('settings.advisor.title', '🪴 Recommended 설치 Options')}[/]  "
                        f"[dim]· {tr('settings.advisor.subtitle', 'Claude · Codex · notifications')}[/]",
                        classes="settings-pane-title", markup=True,
                    )
                    yield Static(tr("settings.language.title", "🌐 Language"), classes="settings-radio-label", markup=True)
                    yield SelectableTextArea(
                        tr(
                            "settings.language.intro",
                            "Choose the UI language for GccSlim. The setting is stored in the current project's .gccfork preferences when project prefs are active.",
                        ),
                        id="settings-language-intro",
                        classes="settings-select-text advisor-intro-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    active_lang = current_language()
                    with RadioSet(id=f"rs-{LANGUAGE_PREF_KEY}", classes="settings-radioset"):
                        yield RadioButton(
                            tr("settings.language.en", "English"),
                            id=f"rb-{LANGUAGE_PREF_KEY}-en",
                            value=(active_lang == "en"),
                            classes="settings-radio",
                        )
                        yield RadioButton(
                            tr("settings.language.ko", "Korean"),
                            id=f"rb-{LANGUAGE_PREF_KEY}-ko",
                            value=(active_lang == "ko"),
                            classes="settings-radio",
                        )
                    yield SelectableTextArea(
                        tr(
                            "settings.advisor.intro",
                            "권고 설치 옵션입니다. 내부 검사는 자동으로 처리하며, 필요한 항목만 설치하거나 제거할 수 있습니다.",
                        ),
                        id="advisor-intro",
                        classes="settings-select-text advisor-intro-text",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )
                    yield Vertical(id="advisor-cards-host")

                # ─── Help pane ─────────────────────────────────────────────
                with Vertical(id="pane-help", classes="settings-pane"):
                    yield Static(
                        help_section.get("title", tr("help.title", "❓ Help")),
                        classes="settings-pane-title", markup=True,
                    )
                    yield SelectableTextArea(
                        _help_text_for_text_area(help_section.get("content", "")),
                        id="settings-help-content",
                        read_only=True,
                        soft_wrap=True,
                        compact=True,
                        show_line_numbers=False,
                        highlight_cursor_line=True,
                    )

            # ── Footer, edge-aligned ───────────────────────────────────────
            with Horizontal(id="settings-btn-row"):
                yield Static("", id="settings-status")
                yield Static("", id="settings-btn-spacer")
                yield Button(tr("settings.close", "Esc 닫기"), id="btn-settings-close", variant="primary")
        yield CopyMenuOverlay(id="copy-menu")

    def on_mount(self) -> None:
        # Initial tab: show search and hide the rest.
        self._apply_tab_visibility()
        self._apply_slim_mode_visibility()
        self._apply_slim_subtab_visibility()
        try:
            self.query_one("#tab-search", Button).focus()
        except Exception:
            pass
        # Advisor tab: set intro/cards-host height to auto so #pane-advisor
        # VerticalScroll scrolls correctly.
        try:
            self.query_one("#advisor-intro").styles.height = "auto"
        except Exception:
            pass
        try:
            self.query_one("#advisor-cards-host", Vertical).styles.height = "auto"
        except Exception:
            pass
        self._refresh_status()

    # _compose_scope_toggle removed (2026-05-08): gccfork is always launched
    # in a project's cwd, so a "global vs project" choice is meaningless to
    # the user. All prefs auto-save to <cwd>/.gccfork/ccfork-prefs.json
    # via the active project scope set on TUI mount. Backend infrastructure
    # (set_active_pref_scope) is preserved for power users who edit JSON
    # manually, but no UI exposes the toggle.

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        cid = event.checkbox.id or ""
        if cid.startswith("chk-"):
            from gccfork import pref_set
            pref_set(cid[4:], bool(event.value))
            self._refresh_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Persist input changes by extracting key from id="input-{key}".
        Empty or invalid integer inputs are ignored, preserving the previous value.
        """
        iid = event.input.id or ""
        if not iid.startswith("input-"):
            return
        key = iid[len("input-"):]
        raw = (event.value or "").strip()
        if not raw:
            return
        try:
            value: object = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                value = raw
        from gccfork import pref_set
        pref_set(key, value)
        self._refresh_status()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        """Persist RadioSet changes by extracting key/value from widget ids.

        (Scope toggle removed 2026-05-08 — see _compose_scope_toggle comment.)
        """
        rs_id = event.radio_set.id or ""
        if not rs_id.startswith("rs-"):
            return
        key = rs_id[3:]
        rb_id = event.pressed.id or ""
        prefix = f"rb-{key}-"
        if not rb_id.startswith(prefix):
            return
        value = rb_id[len(prefix):]
        if key == LANGUAGE_PREF_KEY:
            lang = set_language_pref(value)
            self.notify(
                tr("settings.language.changed", "Language -> {language}. Reopen the settings window to refresh all labels.", language=lang),
                severity="information",
            )
            self._refresh_status()
            return
        from gccfork import pref_set
        pref_set(key, value)
        self._refresh_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-settings-close":
            self.dismiss(None)
            return
        if bid.startswith("tab-"):
            self._select_tab(bid[len("tab-"):])
            return
        if bid.startswith("btn-slim-sub-"):
            self._select_slim_sub_tab(bid[len("btn-slim-sub-"):])
            return
        if bid == "btn-toggle-slim-advanced":
            self._slim_advanced_open = not self._slim_advanced_open
            self._apply_slim_mode_visibility()
            return
        if bid.startswith("btn-reset-"):
            self._reset_section(bid[len("btn-reset-"):])
            return
        if bid.startswith("advisor-"):
            self._handle_advisor_action(bid[len("advisor-"):])
            return

    def action_close(self) -> None:
        self.dismiss(None)

    def action_switch_tab(self, tab: str) -> None:
        self._select_tab(tab)

    # ─── Tab switching ──────────────────────────────────────────────────────
    def _select_tab(self, tab: str) -> None:
        if tab not in ("search", "slim", "archive", "editor", "advisor", "help"):
            return
        self._active_tab = tab
        self._apply_tab_visibility()
        if tab == "advisor":
            self._refresh_advisor_cards()

    def _apply_tab_visibility(self) -> None:
        for tab in ("search", "slim", "archive", "editor", "advisor", "help"):
            try:
                pane = self.query_one(f"#pane-{tab}")
                pane.display = (tab == self._active_tab)
            except Exception:
                pass
            try:
                btn = self.query_one(f"#tab-{tab}", Button)
                if tab == self._active_tab:
                    btn.add_class("-active")
                else:
                    btn.remove_class("-active")
            except Exception:
                pass

    def _select_slim_sub_tab(self, tab: str) -> None:
        if tab not in ("claude", "codex"):
            return
        self._slim_sub_tab = tab
        self._apply_slim_subtab_visibility()

    def _apply_slim_subtab_visibility(self) -> None:
        for tab in ("claude", "codex"):
            try:
                pane = self.query_one(f"#slim-pane-{tab}", Vertical)
                pane.display = (tab == self._slim_sub_tab)
            except Exception:
                pass
            try:
                btn = self.query_one(f"#btn-slim-sub-{tab}", Button)
                if tab == self._slim_sub_tab:
                    btn.add_class("-active")
                else:
                    btn.remove_class("-active")
            except Exception:
                pass

    # ─── Slim mode switching, called by ModeCard ────────────────────────────
    def _select_mode(self, mode: str) -> None:
        if mode not in ("strong", "medium", "weak"):
            return
        self._slim_mode = mode
        # Toggle the card .-selected class.
        for m in ("strong", "medium", "weak"):
            try:
                card = self.query_one(f"#setting-mode-{m}")
                if m == mode:
                    card.add_class("-selected")
                else:
                    card.remove_class("-selected")
            except Exception:
                pass
        self._apply_slim_mode_visibility()

    def _apply_slim_mode_visibility(self) -> None:
        try:
            pane = self.query_one("#slim-advanced-pane", Vertical)
            pane.display = self._slim_advanced_open
        except Exception:
            pass
        try:
            btn = self.query_one("#btn-toggle-slim-advanced", Button)
            btn.label = (
                "▼ General slim detail-preservation options"
                if self._slim_advanced_open
                else "▶ General slim detail-preservation options"
            )
        except Exception:
            pass
        for m in ("strong", "medium", "weak"):
            try:
                sub = self.query_one(f"#slim-sub-{m}", Vertical)
                sub.display = (self._slim_advanced_open and m == self._slim_mode)
            except Exception:
                pass

    def _slim_settings_system_prompt(self) -> str:
        return (
            "You are an AI explainer dedicated to the Slim tab in the GccSlim settings screen.\n"
            "Your goal is to help the user quickly understand how slim settings relate to the slim button modal.\n"
            "Claude Code session structure handled by GccSlim slim:\n"
            "- Sessions are JSONL files at ~/.claude/projects/<cwd-slug>/<sid>.jsonl.\n"
            "- Claude Code mainly reconstructs context from conversation after the native compact marker (isCompactSummary / compact_boundary).\n"
            "- There is also a user/assistant message-count cap, so old active messages may be excluded from context even when the 1M token budget has space.\n"
            "- Existing compact summaries/archive remain in raw JSONL, but they are before the native compact boundary and are not normally injected into resume context.\n"
            "- The active region is the raw conversation after the latest native compact marker.\n"
            "- The non-context region is inside active but outside the Claude message cap.\n"
            "- The context-included region is inside the current cap and is the bundle target.\n"
            "- Recent raw protected turns preserve the exact JSONL lines for the last flow the user was viewing.\n"
            "Slim application structure:\n"
            "- compact summaries/archive: preserved\n"
            "- non-context region: moved before native compact boundary, keeping it outside context\n"
            "- context-included region: bundled to reduce context load\n"
            "- latest N raw turns: preserved exactly\n"
            "Mode meanings:\n"
            "- strong: prioritizes context space; trims most aggressively and preserves 5 recent raw turns by default.\n"
            "- medium: balanced; trades off flow readability and reduction, preserving 10 recent raw turns by default.\n"
            "- weak: conservative; preserves more and reduces less, preserving 30 recent raw turns by default.\n"
            "Codex mode meanings:\n"
            "- safe: preserves all previous compact summaries, the current slim body, and 10 recent raw turns; prioritizes recovery/review.\n"
            "- strong: preserves all previous compact summaries, the current slim body, and only 3 recent raw turns; prioritizes context space.\n"
            "Codex slim result structure:\n"
            "- compacted.payload.message summaries #1..N\n"
            "- current session slim body\n"
            "- recent raw protected turns\n"
            "Explanation rules:\n"
            "- Answer in Korean.\n"
            "- Base the answer only on current settings.\n"
            "- Distinguish default settings from advanced settings.\n"
            "- Explain that 'non-context compacting' does not delete raw data; it moves it before the native compact boundary.\n"
            "- Briefly explain how 'bundle processing' and 'recent raw protected turns' affect actual slim execution.\n"
            "- End with a one-line recommendation for settings users should usually keep."
        )

    def _slim_settings_user_prompt(self) -> str:
        from gccfork import pref_get
        mode = str(pref_get("slim_default_mode", "strong"))
        vals = {
            "bundle processing": bool(pref_get("slim_default_anti_fragmentation", True)),
            "non-context compacting": bool(pref_get("slim_default_visible_cap_compact", True)),
            "auto-open after slim": bool(pref_get("slim_default_reload", True)),
            "send to another environment": bool(pref_get("slim_default_send_other_env", False)),
            "open in a new tab": bool(pref_get("slim_default_newtab", False)),
            "dynamic cap": bool(pref_get("slim_default_dynamic_cap", True)),
        }
        turns = {
            m: int(pref_get(f"slim_{m}_keep_recent_turns", SLIM_DEFAULT_PREFS[f"slim_{m}_keep_recent_turns"]) or 5)
            for m in ("strong", "medium", "weak")
        }
        advanced = {}
        for m in ("strong", "medium", "weak"):
            advanced[m] = {}
            for item in _slim_items_for_mode(m):
                advanced[m][item["label"]] = bool(pref_get(item["key"], item["default"]))
        option_lines = "\n".join(
            f"- {k}: {'ON' if v else 'OFF'}" for k, v in vals.items()
        )
        turns_lines = "\n".join(f"- {m}: {n} user turns preserved raw" for m, n in turns.items())
        return (
            "Explain the current Slim tab state in the GccSlim settings screen to the user.\n\n"
            f"[Default mode]\n- {mode}\n\n"
            "[Application structure]\n"
            "Full session\n"
            "├─ compact summaries / archive   preserved\n"
            "└─ active region\n"
            "   ├─ non-context region         compacted\n"
            "   └─ context-included region    bundle target\n"
            "      └─ latest N raw turns      preserved as raw\n\n"
            f"[Default options]\n{option_lines}\n\n"
            f"[Recent raw protected turns]\n{turns_lines}\n\n"
            f"[Advanced settings open]\n- {'open' if self._slim_advanced_open else 'closed'}\n\n"
            f"[Advanced detail-preservation options]\n{json.dumps(advanced, ensure_ascii=False, indent=2)}\n\n"
            "[Codex defaults]\n"
            f"- mode: {pref_get('codex_slim_default_mode', 'strong')}\n"
            f"- keep_recent: {pref_get('codex_slim_keep_recent', 3)}\n"
            f"- include_compact_summaries: {pref_get('codex_slim_include_compact_summaries', True)}\n"
            f"- trim_recent_tools: {pref_get('codex_slim_trim_recent_tools', True)}\n"
            f"- extra_compact_event_types: {pref_get('codex_slim_compact_event_types', '')}\n"
            f"- extra_compact_text_keys: {pref_get('codex_slim_compact_text_keys', '')}\n"
            f"- clone: {pref_get('codex_slim_default_clone', False)}\n"
            f"- reload: {pref_get('codex_slim_default_reload', True)}\n"
            f"- other_env: {pref_get('codex_slim_default_send_other_env', False)}\n"
            f"- newtab: {pref_get('codex_slim_default_newtab', False)}\n\n"
            "Briefly explain how these settings connect to the default selections in the slim button modal."
        )

    def _archive_settings_system_prompt(self) -> str:
        return (
            "You are an AI explainer dedicated to the Merge tab in the GccSlim settings screen.\n"
            "Your goal is to help the user quickly understand how Archive/Merge settings affect actual session merge, preview, search, and restore behavior.\n"
            "Basic GccSlim merge concepts:\n"
            "- Merge creates a new integrated session from multiple sessions that share a common ancestor.\n"
            "- Archive options control child-session display, search inclusion, restore policy, and storage location around merge/archive workflows.\n"
            "- Merge stitching controls how multiple session flows are woven together in the merged JSONL result.\n"
            "Main option meanings:\n"
            "- preview_mode: controls how archived child contents appear in parent/merged previews.\n"
            "- search_includes_children: controls whether deep search includes archived child bodies.\n"
            "- important_handling: controls automatic include, confirmation, or rejection for sessions marked ★ during archive/merge.\n"
            "- restore_enabled: controls whether archived sessions can be restored and whether the trash pattern is used.\n"
            "- trigger_mode: controls whether merge entry points are shown as shortcut, button, or both.\n"
            "- lazy_load: reads child bodies only when needed to reduce load on large session lists.\n"
            "- child_color_distinction: applies visual distinction per archived child.\n"
            "- section_header_format: controls compact or detailed child-section headers.\n"
            "- child_sort_order: controls archived child sorting.\n"
            "- folder_layout: controls whether archive storage is per-project or centralized.\n"
            "- merge_stitching_method: controls the conversation weaving method in the merged result.\n"
            "Explanation rules:\n"
            "- Answer in Korean.\n"
            "- Base the answer only on current settings.\n"
            "- End with a one-line suggestion about which value the user should keep or change.\n"
            "- Focus on user-visible effects rather than internal file paths or implementation function names."
        )

    def _archive_settings_user_prompt(self) -> str:
        from gccfork import pref_get
        try:
            from gccfork_archive import ARCHIVE_DEFAULTS
        except ImportError:
            ARCHIVE_DEFAULTS = {}
        try:
            from gccfork_merge import MERGE_DEFAULTS
        except ImportError:
            MERGE_DEFAULTS = {}
        defaults = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}
        values = {}
        for opt in ARCHIVE_OPTIONS:
            key = opt["key"]
            default = defaults.get(key)
            values[key] = {
                "label": opt.get("label", key),
                "kind": opt.get("kind", ""),
                "current": pref_get(key, default),
                "default": default,
                "hint": opt.get("hint", ""),
            }
        return (
            "Explain the current Merge tab state in the GccSlim settings screen to the user.\n\n"
            "[Current Merge / Archive settings]\n"
            f"{json.dumps(values, ensure_ascii=False, indent=2)}\n\n"
            "Briefly explain how each setting affects actual merge, preview, search, and restore flows."
        )

    def _open_settings_brain_agent(self, target: str) -> None:
        if target == "slim":
            source_key = "settings-slim"
            system_prompt = self._slim_settings_system_prompt()
            user_prompt = self._slim_settings_user_prompt()
        elif target == "archive":
            source_key = "settings-archive"
            system_prompt = self._archive_settings_system_prompt()
            user_prompt = self._archive_settings_user_prompt()
        else:
            return
        app = self.app
        if not hasattr(app, "_spawn_settings_brain_agent"):
            try:
                app.notify("Could not find the settings AI agent launcher.", severity="error", timeout=5)
            except Exception:
                pass
            return
        app._spawn_settings_brain_agent(
            source_key=source_key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    # ─── helpers ────────────────────────────────────────────────────
    def _reset_section(self, section_id: str) -> None:
        """Reset every checkbox/RadioSet in a section to defaults.

        Checkbox.value changes automatically fire on_checkbox_changed and pref_set.
        RadioSet selected changes also fire on_radio_set_changed.
        """
        # /slim defaults section: reset mode, reload, options, and protected turns.
        # Mode-specific buttons also call into this reset path for their own detail options.
        if section_id == "slim-defaults":
            from gccfork import pref_set
            for k in ("slim_default_mode", "slim_default_reload",
                      "slim_default_anti_fragmentation",
                      "slim_default_dynamic_cap",
                      "slim_default_visible_cap_compact",
                      "slim_default_send_other_env",
                      "slim_default_newtab",
                      "slim_strong_keep_recent_turns",
                      "slim_medium_keep_recent_turns",
                      "slim_weak_keep_recent_turns"):
                pref_set(k, SLIM_DEFAULT_PREFS[k])
            try:
                rb = self.query_one(
                    f"#rb-slim_default_mode-{SLIM_DEFAULT_PREFS['slim_default_mode']}",
                    RadioButton,
                )
                if not rb.value:
                    rb.value = True
            except Exception:
                pass
            for chk_id, key in (
                ("#chk-slim_default_reload", "slim_default_reload"),
                ("#chk-slim_default_anti_fragmentation", "slim_default_anti_fragmentation"),
                ("#chk-slim_default_dynamic_cap", "slim_default_dynamic_cap"),
                ("#chk-slim_default_visible_cap_compact", "slim_default_visible_cap_compact"),
                ("#chk-slim_default_send_other_env", "slim_default_send_other_env"),
                ("#chk-slim_default_newtab", "slim_default_newtab"),
            ):
                try:
                    cb = self.query_one(chk_id, Checkbox)
                    cb.value = bool(SLIM_DEFAULT_PREFS[key])
                except Exception:
                    pass
            for mode in ("strong", "medium", "weak"):
                turns_key = f"slim_{mode}_keep_recent_turns"
                try:
                    inp = self.query_one(f"#input-{turns_key}", Input)
                    inp.value = str(SLIM_DEFAULT_PREFS[turns_key])
                except Exception:
                    pass
            self.notify("↻ /slim defaults restored: mode, options, and protected turns")
            self._refresh_status()
            return

        if section_id == "codex-slim-defaults":
            from gccfork import pref_set
            keys = (
                "codex_slim_default_mode",
                "codex_slim_keep_recent",
                "codex_slim_include_compact_summaries",
                "codex_slim_trim_recent_tools",
                "codex_slim_compact_event_types",
                "codex_slim_compact_text_keys",
                "codex_slim_default_clone",
                "codex_slim_default_reload",
                "codex_slim_default_send_other_env",
                "codex_slim_default_newtab",
            )
            for key in keys:
                pref_set(key, SLIM_DEFAULT_PREFS[key])
            try:
                rb = self.query_one(
                    f"#rb-codex_slim_default_mode-{SLIM_DEFAULT_PREFS['codex_slim_default_mode']}",
                    RadioButton,
                )
                if not rb.value:
                    rb.value = True
            except Exception:
                pass
            try:
                inp = self.query_one("#input-codex_slim_keep_recent", Input)
                inp.value = str(SLIM_DEFAULT_PREFS["codex_slim_keep_recent"])
            except Exception:
                pass
            for input_id, key in (
                ("#input-codex_slim_compact_event_types", "codex_slim_compact_event_types"),
                ("#input-codex_slim_compact_text_keys", "codex_slim_compact_text_keys"),
            ):
                try:
                    inp = self.query_one(input_id, Input)
                    inp.value = str(SLIM_DEFAULT_PREFS[key])
                except Exception:
                    pass
            for chk_id, key in (
                ("#chk-codex_slim_include_compact_summaries", "codex_slim_include_compact_summaries"),
                ("#chk-codex_slim_trim_recent_tools", "codex_slim_trim_recent_tools"),
                ("#chk-codex_slim_default_clone", "codex_slim_default_clone"),
                ("#chk-codex_slim_default_reload", "codex_slim_default_reload"),
                ("#chk-codex_slim_default_send_other_env", "codex_slim_default_send_other_env"),
                ("#chk-codex_slim_default_newtab", "codex_slim_default_newtab"),
            ):
                try:
                    cb = self.query_one(chk_id, Checkbox)
                    cb.value = bool(SLIM_DEFAULT_PREFS[key])
                except Exception:
                    pass
            self.notify("↻ Codex /slim defaults restored")
            self._refresh_status()
            return

        # Editor section: restore config_editor to auto.
        if section_id == "editor":
            try:
                from gccfork_config_files import EDITOR_DEFAULT
            except ImportError:
                EDITOR_DEFAULT = "auto"
            from gccfork import pref_set
            try:
                rb_default = self.query_one(f"#rb-config_editor-{EDITOR_DEFAULT}", RadioButton)
                if not rb_default.value:
                    rb_default.value = True
                    self.notify(f"↻ Editor -> {EDITOR_DEFAULT}")
                else:
                    self.notify(f"Already {EDITOR_DEFAULT}", severity="information")
            except Exception:
                pass
            pref_set("config_editor", EDITOR_DEFAULT)
            self._refresh_status()
            return

        # Archive section: reset according to ARCHIVE_OPTIONS, including merge options.
        if section_id == "archive":
            try:
                from gccfork_archive import ARCHIVE_DEFAULTS
            except ImportError:
                ARCHIVE_DEFAULTS = {}
            try:
                from gccfork_merge import MERGE_DEFAULTS
            except ImportError:
                MERGE_DEFAULTS = {}
            DEFAULTS_COMBINED = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}
            from gccfork import pref_set
            reset_count = 0
            for opt in ARCHIVE_OPTIONS:
                key = opt["key"]
                default = DEFAULTS_COMBINED.get(key)
                if opt["kind"] == "bool":
                    try:
                        cb = self.query_one(f"#chk-{key}", Checkbox)
                        if bool(cb.value) != bool(default):
                            cb.value = bool(default)
                            reset_count += 1
                    except Exception:
                        pass
                elif opt["kind"] == "radio":
                    # Select the default RadioButton when a non-default one is selected.
                    try:
                        rb_default = self.query_one(f"#rb-{key}-{default}", RadioButton)
                        if not rb_default.value:
                            rb_default.value = True  # RadioSet disables the other radio itself.
                            reset_count += 1
                    except Exception:
                        pass
                    # Also reset prefs directly in case RadioSet.changed does not fire.
                    pref_set(key, None)
            if reset_count:
                self.notify(f"↻ '🗂 Archive' — restored {reset_count} defaults")
            else:
                self.notify("'🗂 Archive' is already at defaults", severity="information")
            self._refresh_status()
            return

        # Existing checkbox sections.
        target = next(
            (s for s in get_settings_sections() if s.get("id") == section_id),
            None,
        )
        if not target or target.get("type") != "checkboxes":
            return
        reset_count = 0
        for item in target.get("items", []):
            try:
                cb = self.query_one(f"#chk-{item['key']}", Checkbox)
                if bool(cb.value) != bool(item["default"]):
                    cb.value = bool(item["default"])
                    reset_count += 1
            except Exception:
                pass
        # slim-{mode} sections also reset protected-turn Input to the mode default.
        if section_id.startswith("slim-"):
            mode = section_id[len("slim-"):]
            if mode in ("strong", "medium", "weak"):
                turns_key = f"slim_{mode}_keep_recent_turns"
                turns_default = SLIM_DEFAULT_PREFS.get(turns_key)
                if turns_default is not None:
                    from gccfork import pref_set
                    pref_set(turns_key, turns_default)
                    try:
                        inp = self.query_one(f"#input-{turns_key}", Input)
                        if inp.value != str(turns_default):
                            inp.value = str(turns_default)
                            reset_count += 1
                    except Exception:
                        pass
        title = target.get("title", section_id)
        if reset_count:
            self.notify(f"↻ '{title}' — restored {reset_count} defaults")
        else:
            self.notify(f"'{title}' is already at defaults", severity="information")

    # ─── Advisor install tab: card rendering and handlers ───────────────────
    def _refresh_advisor_cards(self) -> None:
        """Re-render advisor cards after entering or operating the advisor tab.

        Each card body is rendered as SelectableTextArea, matching the help tab,
        so users can left-drag to select/copy. TextArea does not interpret Rich
        markup, so only plain text is used.
        """
        try:
            host = self.query_one("#advisor-cards-host", Vertical)
        except Exception:
            return
        from gccfork import SelectableTextArea
        # 제거 existing children and rebuild quickly.
        for child in list(host.children):
            try:
                child.remove()
            except Exception:
                pass
        try:
            from gccfork_install_advisor import install_status_summary
            snap = install_status_summary()
        except Exception as exc:
            host.mount(Static(
                f"[dim]Could not read recommendation status: {exc}[/]",
                classes="settings-intro", markup=True,
            ))
            return

        def _badge(installed: bool, dismissed: bool, blocked: str = "") -> str:
            # Plain text only; TextArea has no markup.
            if blocked:
                return f"· {blocked}"
            if installed:
                return "✓ installed"
            if dismissed:
                return "· hidden by user"
            return "· not installed"

        def _mount_card(text: str) -> None:
            """Mount card body text as SelectableTextArea.

            Do not assign an id. When `_refresh_advisor_cards()` is called rapidly,
            textual `remove()` is async, and duplicate ids can temporarily coexist,
            causing DuplicateIds. Cards do not need identity.

            Force height to auto because the TextArea default can clip body text.
            """
            ta = SelectableTextArea(
                text,
                classes="settings-select-text advisor-card-text",
                read_only=True,
                soft_wrap=True,
                compact=True,
                show_line_numbers=False,
                highlight_cursor_line=True,
            )
            ta.styles.height = "auto"
            host.mount(ta)

        # 1) cleanup: current value / recommended value, with apply button if too low.
        try:
            from gccfork_cleanup_check import (
                read_cleanup_period_days, RECOMMENDED_DAYS, DEFAULT_DAYS,
            )
            cur = read_cleanup_period_days()
            effective = DEFAULT_DAYS if cur is None else cur
            cur_s = f"(미설정 -> {DEFAULT_DAYS}일)" if cur is None else f"{cur}일"
            _mount_card(
                f"🧹 cleanupPeriodDays\n"
                f"현재: {cur_s}\n"
                f"권고: {RECOMMENDED_DAYS}일",
            )
            row = Horizontal(classes="settings-section-actions")
            host.mount(row)
            if effective < RECOMMENDED_DAYS:
                row.mount(Button(
                    f"권고값 적용 ({RECOMMENDED_DAYS})",
                    id="advisor-cleanup-apply",
                    variant="primary",
                ))
            else:
                noop_btn = Button("✓ 이미 권고값 이상")
                noop_btn.disabled = True
                row.mount(noop_btn)
        except Exception as exc:
            _mount_card(f"🧹 cleanupPeriodDays\n읽기 실패: {exc}")

        # 2) stale: old TUI detection result only.
        try:
            from gccfork import _find_other_gccfork_tuis  # type: ignore[attr-defined]
            stale = [i for i in (_find_other_gccfork_tuis() or []) if i.get("is_old")]
            n = len(stale)
            _mount_card(f"🪦 이전 TUI 인스턴스\n감지: {n}")
        except Exception:
            _mount_card("🪦 이전 TUI 인스턴스\n확인 불가")

        # 3) Claude /slim
        cs = snap.get("claude_slash", {})
        cs_badge = _badge(cs.get("installed", False), cs.get("dismissed", False))
        cs_lines = [
            f"🔻 Claude `/slim` 통합   {cs_badge}",
            "Claude 안의 `/slim`, `/slim:dry`를 GccSlim에 연결합니다.",
        ]
        _mount_card("\n".join(cs_lines))
        row = Horizontal(classes="settings-section-actions")
        host.mount(row)
        if cs.get("installed", False):
            row.mount(Button("제거", id="advisor-claude-slash-remove", classes="settings-reset-btn"))
        else:
            row.mount(Button("설치", id="advisor-claude-slash-install", variant="primary"))

        # 4) Codex /slim integration
        cw = snap.get("codex_wrapper", {})
        cw_available = cw.get("available", False)
        cw_installed = bool(cw.get("installed", False))
        cw_block = "" if (cw_installed or cw_available) else "자동 설치 자산 없음"
        cw_badge = _badge(cw.get("installed", False), cw.get("dismissed", False), cw_block)
        cw_lines = [
            f"🔻 Codex `/slim` 통합   {cw_badge}",
            "Codex에서 `/slim` 후 같은 터미널로 재시작하도록 연결합니다.",
        ]
        _mount_card("\n".join(cw_lines))
        row = Horizontal(classes="settings-section-actions")
        host.mount(row)
        if cw_installed:
            row.mount(Button("제거", id="advisor-codex-wrapper-remove", classes="settings-reset-btn"))
        else:
            btn = Button("설치", id="advisor-codex-wrapper-install", variant="primary")
            btn.disabled = not cw_available
            row.mount(btn)

        # 5) Claude dingdong
        dd = snap.get("dingdong", {})
        dd_badge = _badge(dd.get("installed", False), dd.get("dismissed", False))
        dd_lines = [
            f"🔔 Claude 작업 완료 알림음   {dd_badge}",
            "Claude 응답이 끝나면 알림음을 재생합니다.",
        ]
        if not dd.get("deps_ok", False):
            dd_lines.append("필요한 오디오 의존성이 아직 준비되지 않았습니다.")
        _mount_card("\n".join(dd_lines))
        row = Horizontal(classes="settings-section-actions")
        host.mount(row)
        if dd.get("installed", False):
            row.mount(Button("제거", id="advisor-dingdong-remove", classes="settings-reset-btn"))
        else:
            btn = Button("설치", id="advisor-dingdong-install", variant="primary")
            btn.disabled = not dd.get("deps_ok", False)
            row.mount(btn)

        # 6) Codex dingdong
        cdd = snap.get("codex_dingdong", {})
        cdd_badge = _badge(cdd.get("installed", False), cdd.get("dismissed", False))
        cdd_lines = [
            f"🔔 Codex 작업 완료 알림음   {cdd_badge}",
            "Codex 응답이 끝나면 알림음을 재생합니다.",
        ]
        if not cdd.get("deps_ok", False):
            cdd_lines.append("필요한 오디오 의존성이 아직 준비되지 않았습니다.")
        _mount_card("\n".join(cdd_lines))
        row = Horizontal(classes="settings-section-actions")
        host.mount(row)
        if cdd.get("installed", False):
            row.mount(Button("제거", id="advisor-codex-dingdong-remove", classes="settings-reset-btn"))
        else:
            btn = Button("설치", id="advisor-codex-dingdong-install", variant="primary")
            btn.disabled = not cdd.get("deps_ok", False)
            row.mount(btn)

        # 7) VS Code 터미널 히스토리
        vs = snap.get("vscode_scrollback", {})
        vs_installed = bool(vs.get("installed", False))
        vs_value = vs.get("value")
        vs_recommended = int(vs.get("recommended") or 100000)
        vs_current = "(미설정)" if vs_value is None else f"{vs_value}줄"
        vs_badge = "✓ 권고값 적용됨" if vs_installed else "· 권고값 미만"
        vs_lines = [
            f"🧾 VS Code 터미널 히스토리   {vs_badge}",
            f"current: {vs_current}",
            f"권고: {vs_recommended}줄",
            "하드 복제 세션의 재생 대화 시작 부분이 보존되도록 충분한 스크롤백을 유지합니다.",
        ]
        _mount_card("\n".join(vs_lines))
        row = Horizontal(classes="settings-section-actions")
        host.mount(row)
        if vs_installed:
            noop_btn = Button("✓ 이미 권고값")
            noop_btn.disabled = True
            row.mount(noop_btn)
        else:
            row.mount(Button(f"{vs_recommended}줄로 설정", id="advisor-vscode-scrollback-apply", variant="primary"))

    def _handle_advisor_action(self, action: str) -> None:
        """Handle advisor card button clicks by calling install_advisor functions and re-rendering."""
        from gccfork_install_advisor import (
            apply_claude_slash_install, uninstall_claude_slash,
            apply_codex_wrapper_install, uninstall_codex_wrapper,
            apply_dingdong_install, uninstall_dingdong,
            apply_codex_dingdong_install, uninstall_codex_dingdong,
            apply_vscode_scrollback_install,
        )
        ok, msg = (False, "")
        if action == "cleanup-apply":
            from gccfork_cleanup_check import update_cleanup_period_days, RECOMMENDED_DAYS
            ok, info = update_cleanup_period_days(RECOMMENDED_DAYS)
            msg = (
                f"cleanupPeriodDays = {RECOMMENDED_DAYS} days 적용됨. {info}"
                if ok else f"적용 실패: {info}"
            )
        elif action == "claude-slash-install":
            ok, msg = apply_claude_slash_install()
        elif action == "claude-slash-remove":
            ok, msg = uninstall_claude_slash()
        elif action == "codex-wrapper-install":
            ok, msg = apply_codex_wrapper_install()
        elif action == "codex-wrapper-remove":
            ok, msg = uninstall_codex_wrapper()
        elif action == "dingdong-install":
            ok, msg = apply_dingdong_install()
        elif action == "dingdong-remove":
            ok, msg = uninstall_dingdong()
        elif action == "codex-dingdong-install":
            ok, msg = apply_codex_dingdong_install()
        elif action == "codex-dingdong-remove":
            ok, msg = uninstall_codex_dingdong()
        elif action == "vscode-scrollback-apply":
            ok, msg = apply_vscode_scrollback_install()
        else:
            return
        try:
            status = self.query_one("#settings-status", Static)
            prefix = "✓" if ok else "✗"
            status.update(f"{prefix} {msg}")
        except Exception:
            pass
        self._refresh_advisor_cards()

    def _refresh_status(self) -> None:
        """Footer status showing number of prefs different from defaults, including archive options."""
        from gccfork import pref_get
        changed = 0
        for section in get_settings_sections():
            if section.get("type") != "checkboxes":
                continue
            for item in section.get("items", []):
                cur = pref_get(item["key"], item["default"])
                if bool(cur) != bool(item["default"]):
                    changed += 1
        # Count archive + merge bool/enum options.
        try:
            from gccfork_archive import ARCHIVE_DEFAULTS
        except ImportError:
            ARCHIVE_DEFAULTS = {}
        try:
            from gccfork_merge import MERGE_DEFAULTS
        except ImportError:
            MERGE_DEFAULTS = {}
        DEFAULTS_COMBINED = {**ARCHIVE_DEFAULTS, **MERGE_DEFAULTS}
        for opt in ARCHIVE_OPTIONS:
            key = opt["key"]
            default = DEFAULTS_COMBINED.get(key)
            cur = pref_get(key, default)
            if cur != default:
                changed += 1
        for key in (
            "slim_default_mode",
            "slim_default_reload",
            "slim_default_anti_fragmentation",
            "slim_default_dynamic_cap",
            "slim_default_visible_cap_compact",
            "slim_default_send_other_env",
            "slim_default_newtab",
            "slim_strong_keep_recent_turns",
            "slim_medium_keep_recent_turns",
            "slim_weak_keep_recent_turns",
            "codex_slim_default_mode",
            "codex_slim_keep_recent",
            "codex_slim_include_compact_summaries",
            "codex_slim_trim_recent_tools",
            "codex_slim_compact_event_types",
            "codex_slim_compact_text_keys",
            "codex_slim_default_clone",
            "codex_slim_default_reload",
            "codex_slim_default_send_other_env",
            "codex_slim_default_newtab",
        ):
            if pref_get(key, SLIM_DEFAULT_PREFS.get(key)) != SLIM_DEFAULT_PREFS.get(key):
                changed += 1
        try:
            status = self.query_one("#settings-status", Static)
            if changed == 0:
                status.update("· all defaults")
            else:
                status.update(f"· {changed} changed")
        except Exception:
            pass
