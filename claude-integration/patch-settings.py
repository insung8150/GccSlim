#!/usr/bin/env python3
"""Idempotent patcher for ~/.claude/settings.json — adds the GccSlim
UserPromptSubmit hook entry for /slim and /slim:dry interception.

No external dependencies (stdlib only). Safe to re-run.

Usage:
    python3 patch-settings.py                  # apply
    python3 patch-settings.py --remove         # uninstall
    python3 patch-settings.py --dry-run        # show diff only
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
HOOK_SCRIPT = Path.home() / ".claude" / "hooks" / "slim-reload-intercept.sh"
HOOK_COMMAND = f"bash {HOOK_SCRIPT}"
EVENT = "UserPromptSubmit"


def _load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: failed to parse {SETTINGS_PATH}: {exc}", file=sys.stderr)
        sys.exit(2)


def _backup() -> Path:
    """Take a timestamped backup of settings.json before mutating."""
    ts = int(time.time())
    bak = SETTINGS_PATH.with_suffix(f".json.bak-gccslim-{ts}")
    shutil.copy2(SETTINGS_PATH, bak)
    return bak


def _save(settings: dict) -> None:
    """Atomic write — tmp file + rename to avoid partial-write corruption."""
    tmp = SETTINGS_PATH.with_suffix(".json.tmp-gccslim")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def _hooks_for_event(settings: dict, event: str) -> list:
    return settings.setdefault("hooks", {}).setdefault(event, [])


def _entry_exists(settings: dict, event: str, command_substring: str) -> bool:
    for matcher_block in settings.get("hooks", {}).get(event, []):
        for hook in matcher_block.get("hooks", []):
            cmd = hook.get("command", "")
            if command_substring in cmd:
                return True
    return False


def install(*, dry_run: bool = False) -> int:
    if not HOOK_SCRIPT.exists():
        print(f"ERROR: hook script not found at {HOOK_SCRIPT}", file=sys.stderr)
        print("  → copy release/claude-integration/slim-reload-intercept.sh first.", file=sys.stderr)
        return 1

    settings = _load_settings()
    if _entry_exists(settings, EVENT, "slim-reload-intercept.sh"):
        print(f"OK: hook entry already present in {SETTINGS_PATH}")
        return 0

    if dry_run:
        print(f"DRY-RUN: would add hook entry to {SETTINGS_PATH}:")
        print(f"  event: {EVENT}")
        print(f"  command: {HOOK_COMMAND}")
        return 0

    # Real write — backup first
    if SETTINGS_PATH.exists():
        bak = _backup()
        print(f"  backup: {bak}")

    event_list = _hooks_for_event(settings, EVENT)
    # Match any prompt — empty matcher (Claude Code convention)
    target_matcher = None
    for block in event_list:
        if block.get("matcher", "") == "":
            target_matcher = block
            break
    if target_matcher is None:
        target_matcher = {"matcher": "", "hooks": []}
        event_list.append(target_matcher)

    target_matcher.setdefault("hooks", []).append(
        {"type": "command", "command": HOOK_COMMAND}
    )

    _save(settings)
    print(f"OK: added hook entry to {SETTINGS_PATH}")
    return 0


def uninstall(*, dry_run: bool = False) -> int:
    settings = _load_settings()
    if not _entry_exists(settings, EVENT, "slim-reload-intercept.sh"):
        print(f"OK: no GccSlim hook entry in {SETTINGS_PATH} (already removed or never installed)")
        return 0

    if dry_run:
        print(f"DRY-RUN: would remove hook entry from {SETTINGS_PATH}")
        return 0

    bak = _backup()
    print(f"  backup: {bak}")

    removed = 0
    for matcher_block in settings.get("hooks", {}).get(EVENT, []):
        before = len(matcher_block.get("hooks", []))
        matcher_block["hooks"] = [
            h for h in matcher_block.get("hooks", [])
            if "slim-reload-intercept.sh" not in h.get("command", "")
        ]
        removed += before - len(matcher_block["hooks"])
    # Drop empty matcher blocks
    settings.setdefault("hooks", {})[EVENT] = [
        b for b in settings.get("hooks", {}).get(EVENT, [])
        if b.get("hooks")
    ]
    # Drop empty event key
    if not settings["hooks"].get(EVENT):
        settings["hooks"].pop(EVENT, None)
    # Drop empty hooks key
    if not settings["hooks"]:
        settings.pop("hooks", None)

    _save(settings)
    print(f"OK: removed {removed} hook entry from {SETTINGS_PATH}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--remove", action="store_true", help="uninstall the hook entry")
    p.add_argument("--dry-run", action="store_true", help="show what would change without writing")
    args = p.parse_args()

    if args.remove:
        return uninstall(dry_run=args.dry_run)
    return install(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
