"""Cleanup period check + modal — alert user when ~/.claude/settings.json's
cleanupPeriodDays is too small (would silently delete archive/merge variants).

Sidecar pattern (per project policy: feature-per-module separation).

Modal flow:
  1. Read ~/.claude/settings.json's cleanupPeriodDays (default 30 if missing)
  2. If value < THRESHOLD_DAYS → push CleanupConfirmScreen
  3. User chooses: [Later] (keep current) or [Set permanent (9999)] → atomic write

The threshold protects users from claude's silent purge of archive/merge work
that they intend to keep indefinitely.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

# textual imports — runtime-only (PEP 723 inline deps activate via uv)
from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, Button


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Threshold — values below this trigger the warning modal.
# 1825 days = 5 years. Below this, user's archive/merge work is at risk
# of silent deletion within a reasonable horizon.
THRESHOLD_DAYS = 1825

# Recommended replacement value — effectively forever.
# 9999 days ≈ 27 years. Lets claude's cleanup remain "active" formally
# while never actually firing in practical usage.
RECOMMENDED_DAYS = 9999

# Default applied by claude when key is missing (matches official docs).
DEFAULT_DAYS = 30


def read_cleanup_period_days() -> Optional[int]:
    """Read cleanupPeriodDays from settings.json.

    Returns:
        int     — explicit value
        DEFAULT_DAYS — key missing (claude's default)
        None    — settings.json missing or unreadable (cannot warn)
    """
    if not SETTINGS_PATH.exists():
        return None
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("cleanupPeriodDays")
    if value is None:
        # Key missing → claude defaults to 30
        return DEFAULT_DAYS
    if isinstance(value, bool):
        # bool is a subclass of int — treat as missing
        return DEFAULT_DAYS
    if isinstance(value, int) and value > 0:
        return value
    return DEFAULT_DAYS


def update_cleanup_period_days(new_value: int) -> tuple[bool, str]:
    """Atomically update cleanupPeriodDays in settings.json.

    Preserves all other keys + JSON formatting style (indent=2).
    Backs up original to settings.json.bak-cleanup-<timestamp>.

    Returns:
        (success, message) — message contains backup path on success,
                             error description on failure.
    """
    if not SETTINGS_PATH.exists():
        return False, f"settings.json not found at {SETTINGS_PATH}"
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"settings.json parse error: {e}"
    if not isinstance(data, dict):
        return False, "settings.json root is not an object"

    # Backup
    import time
    ts = int(time.time())
    backup_path = SETTINGS_PATH.with_suffix(f".json.bak-cleanup-{ts}")
    try:
        shutil.copy2(SETTINGS_PATH, backup_path)
    except Exception as e:
        return False, f"backup failed: {e}"

    data["cleanupPeriodDays"] = new_value

    # Atomic write — tmp file + rename
    try:
        with NamedTemporaryFile(
            "w",
            delete=False,
            dir=SETTINGS_PATH.parent,
            encoding="utf-8",
            suffix=".tmp",
        ) as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            tmp_path = Path(fh.name)
        os.replace(tmp_path, SETTINGS_PATH)
    except Exception as e:
        return False, f"write failed: {e}"

    return True, str(backup_path)


def needs_warning() -> Optional[int]:
    """Return current value if below threshold (warning needed), else None."""
    current = read_cleanup_period_days()
    if current is None:
        return None
    if current < THRESHOLD_DAYS:
        return current
    return None


class CleanupConfirmScreen(ModalScreen[bool]):
    """Modal to alert user about small cleanupPeriodDays + offer one-click fix.

    Returns True if user chose to apply RECOMMENDED_DAYS, False otherwise.
    Also writes the change immediately on confirmation.
    """

    DEFAULT_CSS = """
    CleanupConfirmScreen {
        align: center middle;
    }
    #cleanup-box {
        width: 80;
        height: auto;
        max-height: 30;
        background: $panel-darken-2;
        border: round $accent 35%;
        padding: 0;
    }
    #cleanup-header {
        height: 4;
        padding: 1 2;
        border-bottom: hkey $accent 20%;
        background: $accent 30%;
    }
    #cleanup-title {
        text-style: bold;
        color: $accent;
        text-align: left;
    }
    #cleanup-body {
        padding: 1 2;
        height: auto;
    }
    #cleanup-body Static {
        margin: 0 0 1 0;
    }
    .cleanup-warn {
        color: $error;
        text-style: bold;
    }
    .cleanup-info {
        color: $accent;
    }
    .cleanup-dim {
        color: $text-muted;
    }
    #cleanup-footer {
        height: 3;
        padding: 0 2;
        border-top: hkey $accent 20%;
        align: right middle;
    }
    #cleanup-footer Button {
        margin-left: 2;
    }
    .cleanup-spacer {
        width: 1fr;
    }
    """

    BINDINGS = [("escape", "dismiss_later", "Later")]

    def __init__(self, current_value: int) -> None:
        super().__init__()
        self.current_value = current_value

    def compose(self) -> ComposeResult:
        with Vertical(id="cleanup-box"):
            with Vertical(id="cleanup-header"):
                yield Static(
                    "⚠  Claude 자동 세션 삭제 경고",
                    id="cleanup-title",
                )
                yield Static(
                    "~/.claude/settings.json — cleanupPeriodDays",
                    classes="cleanup-dim",
                )
            with Vertical(id="cleanup-body"):
                yield Static(
                    f"현재 값: [b]{self.current_value}일[/b]  →  {self.current_value}일 "
                    "지난 세션 jsonl 이 claude 시작 시 [b]자동 삭제[/b]됩니다.",
                    markup=True,
                    classes="cleanup-warn",
                )
                yield Static(
                    "GccSlim 의 archive / merge variant 도 jsonl 이라 같이 삭제됩니다. "
                    "사용자가 수년 후 옛 작업을 꺼내려 할 때 [b]복구 불가능[/b].",
                    markup=True,
                )
                yield Static(
                    f"권장: [b]{RECOMMENDED_DAYS}일[/b] (~27년) 으로 변경 → 사실상 영구 보관.",
                    markup=True,
                    classes="cleanup-info",
                )
                yield Static(
                    "[Esc / Later] 누르면 다음 시작 시 다시 안내합니다.",
                    classes="cleanup-dim",
                )
            with Horizontal(id="cleanup-footer"):
                yield Static("", classes="cleanup-spacer")
                yield Button("Later", id="cleanup-later", variant="default")
                yield Button(
                    f"영구 보관 ({RECOMMENDED_DAYS}일)",
                    id="cleanup-apply",
                    variant="primary",
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "cleanup-apply":
            ok, msg = update_cleanup_period_days(RECOMMENDED_DAYS)
            if ok:
                try:
                    self.app.notify(
                        f"✓ cleanupPeriodDays = {RECOMMENDED_DAYS}일 적용. backup: {msg}",
                        severity="information",
                        timeout=8,
                    )
                except Exception:
                    pass
                self.dismiss(True)
            else:
                try:
                    self.app.notify(
                        f"✗ 변경 실패: {msg}",
                        severity="error",
                        timeout=10,
                    )
                except Exception:
                    pass
                self.dismiss(False)
        else:
            self.dismiss(False)

    def action_dismiss_later(self) -> None:
        self.dismiss(False)


__all__ = [
    "THRESHOLD_DAYS",
    "RECOMMENDED_DAYS",
    "DEFAULT_DAYS",
    "SETTINGS_PATH",
    "read_cleanup_period_days",
    "update_cleanup_period_days",
    "needs_warning",
    "CleanupConfirmScreen",
]
