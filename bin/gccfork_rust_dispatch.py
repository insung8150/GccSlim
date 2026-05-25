"""Rust binary delegation switch.

Selected subcommands delegate to Rust binaries when available
(`~/.local/bin/gccfork-slim`, `~/.local/bin/gccfork-claude-patch`).
If Rust is unavailable or disabled, execution falls back to Python.

Switch priority:
    1. `GCCFORK_DISABLE_RUST=1` forces Python.
    2. `GCCFORK_FORCE_RUST=1` disables fallback even if Rust is missing/fails.
    3. Module constant `RUST_DEFAULT` provides the default.

Usage:
    from gccfork_rust_dispatch import (
        try_rust_slim_inplace,
        try_rust_slim_and_reload,
        try_rust_patch_claude,
    )

    if (rc := try_rust_slim_inplace(args)) is not None:
        return rc
    # ... Python fallback
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ─── Internal switch default ────────────────────────────────────────────
RUST_DEFAULT: bool = True  # True = prefer Rust / False = force Python

RUST_SLIM_BIN = Path.home() / ".local" / "bin" / "gccfork-slim"
RUST_PATCH_BIN = Path.home() / ".local" / "bin" / "gccfork-claude-patch"


def rust_enabled() -> bool:
    """Return whether Rust delegation is currently enabled."""
    if os.environ.get("GCCFORK_DISABLE_RUST") == "1":
        return False
    if os.environ.get("GCCFORK_FORCE_RUST") == "1":
        return True
    return RUST_DEFAULT


def _rust_bin_ok(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _force_rust() -> bool:
    return os.environ.get("GCCFORK_FORCE_RUST") == "1"


def _spawn(cmd: list[str], *, json_out: bool) -> int:
    """Invoke a Rust binary and return its exit code.

    stdout/stderr are inherited so user-facing output stays consistent.
    """
    try:
        if json_out:
            r = subprocess.run(cmd, check=False)
            return r.returncode
        return subprocess.call(cmd)
    except OSError as exc:
        print(f"⚠️ Failed to spawn Rust binary: {exc}", file=sys.stderr)
        return -1


# ─── slim-inplace delegation ────────────────────────────────────────────
def try_rust_slim_inplace(args) -> Optional[int]:
    """Try Rust delegation for `gccfork slim-inplace <sid> ...`.

    Returns an exit code when Rust handled the command. Returns None when Rust
    is disabled or unavailable so the Python fallback can run.
    """
    if not rust_enabled():
        return None
    if not _rust_bin_ok(RUST_SLIM_BIN):
        if _force_rust():
            print("❌ GCCFORK_FORCE_RUST=1 but gccfork-slim is missing", file=sys.stderr)
            return 5
        return None

    # Resolve sid to a JSONL path directly, similar to Python scan_sessions.
    sid = getattr(args, "sid", None)
    if not sid:
        return None
    jsonl_path = _find_jsonl_for_sid(sid)
    if jsonl_path is None:
        # Python scan_sessions can handle smarter prefix matching; fall back.
        return None

    cmd = [
        str(RUST_SLIM_BIN),
        "--jsonl", str(jsonl_path),
        "--mode", getattr(args, "mode", "strong"),
        # Skip Rust preflight by default because Python intentionally handles it.
        # Avoid silent no-op slim cases when Rust preflight fails.
        "--no-preflight",
    ]
    if getattr(args, "keep_recent", None) is not None:
        cmd.extend(["--keep-recent", str(args.keep_recent)])
    if getattr(args, "no_backup", False):
        cmd.append("--no-backup")
    if getattr(args, "json", False):
        cmd.append("--json")
    if getattr(args, "quiet", False):
        cmd.append("--quiet")

    # Anti-fragmentation is handled directly by Rust.
    af, dyn_cap, krt = _resolve_anti_frag_args(args)
    if af:
        cmd.append("--anti-frag")
        if not dyn_cap:
            cmd.append("--no-dynamic-cap")
        if krt is not None:
            cmd.extend(["--keep-recent-turns", str(krt)])
        # GCCFORK_VISIBLE_CAP or args.visible_cap controls hidden-region bundling.
        # Default 230 follows the Claude Code message-count cap. Without this
        # cap, bundles can become large enough to time out on claude resume.
        vc = getattr(args, "visible_cap", None)
        if vc is None:
            import os as _os
            v_env = _os.environ.get("GCCFORK_VISIBLE_CAP", "").strip()
            if v_env.isdigit():
                vc = int(v_env)
            else:
                vc = 230  # default when env is unset
        if vc and vc > 0:
            cmd.extend(["--visible-cap", str(vc)])

    rc = _spawn(cmd, json_out=getattr(args, "json", False))
    if rc < 0 and not _force_rust():
        return None
    return rc


# ─── slim-and-reload delegation ─────────────────────────────────────────
def try_rust_slim_and_reload(args) -> Optional[int]:
    """Try Rust delegation for `gccfork slim-and-reload --self|--sid <sid>`."""
    if not rust_enabled():
        return None
    if not _rust_bin_ok(RUST_SLIM_BIN):
        if _force_rust():
            print("❌ GCCFORK_FORCE_RUST=1 but gccfork-slim is missing", file=sys.stderr)
            return 5
        return None

    cmd = [str(RUST_SLIM_BIN)]
    if getattr(args, "self_session", False) or getattr(args, "self", False):
        cmd.append("--self")
    elif getattr(args, "sid", None):
        cmd.extend(["--sid", args.sid])
    else:
        return None  # unknown mode; delegate to Python fallback

    cmd.extend(["--mode", getattr(args, "mode", "strong")])
    if getattr(args, "keep_recent", None) is not None:
        cmd.extend(["--keep-recent", str(args.keep_recent)])
    if getattr(args, "no_clear", False):
        cmd.append("--no-clear")
    if getattr(args, "no_resume", False):
        cmd.append("--no-resume")
    if getattr(args, "no_phantom_trash", False):
        cmd.append("--no-phantom-trash")
    if getattr(args, "no_preflight", False):
        cmd.append("--no-preflight")
    if getattr(args, "no_backup", False):
        cmd.append("--no-backup")
    if getattr(args, "json", False):
        cmd.append("--json")
    if getattr(args, "quiet", False):
        cmd.append("--quiet")

    # Anti-fragmentation is handled directly by Rust.
    af, dyn_cap, krt = _resolve_anti_frag_args(args)
    if af:
        cmd.append("--anti-frag")
        if not dyn_cap:
            cmd.append("--no-dynamic-cap")
        if krt is not None:
            cmd.extend(["--keep-recent-turns", str(krt)])
        # GCCFORK_VISIBLE_CAP or args.visible_cap controls hidden-region bundling.
        # Default 230 follows the Claude Code message-count cap. Without this
        # cap, bundles can become large enough to time out on claude resume.
        vc = getattr(args, "visible_cap", None)
        if vc is None:
            import os as _os
            v_env = _os.environ.get("GCCFORK_VISIBLE_CAP", "").strip()
            if v_env.isdigit():
                vc = int(v_env)
            else:
                vc = 230  # default when env is unset
        if vc and vc > 0:
            cmd.extend(["--visible-cap", str(vc)])

    rc = _spawn(cmd, json_out=getattr(args, "json", False))
    if rc < 0 and not _force_rust():
        return None
    return rc


# ─── patch-claude delegation ────────────────────────────────────────────
def try_rust_patch_claude(args) -> Optional[int]:
    """Try Rust delegation for `gccfork patch-claude`."""
    if not rust_enabled():
        return None
    if not _rust_bin_ok(RUST_PATCH_BIN):
        if _force_rust():
            print("❌ GCCFORK_FORCE_RUST=1 but gccfork-claude-patch is missing",
                  file=sys.stderr)
            return 5
        return None

    cmd = [str(RUST_PATCH_BIN)]
    if getattr(args, "auto", False):
        cmd.append("--auto")
    if getattr(args, "strict", False):
        cmd.append("--strict")
    if getattr(args, "force", False):
        cmd.append("--force")
    if getattr(args, "binary", None):
        cmd.extend(["--binary", str(args.binary)])
    if getattr(args, "json", False):
        cmd.append("--json")

    rc = _spawn(cmd, json_out=getattr(args, "json", False))
    if rc < 0 and not _force_rust():
        return None
    return rc


# ─── Helpers ────────────────────────────────────────────────────────────
def _resolve_anti_frag_args(args) -> tuple[bool, bool, Optional[int]]:
    """Resolve (anti_frag, dynamic_cap, keep_recent_turns) from args + prefs.

    Rust directly supports anti-fragmentation, so Python fallback is unnecessary.
    """
    # anti-frag flag: args first, then settings pref
    af_explicit = getattr(args, "anti_fragmentation", None)
    if af_explicit is None:
        try:
            from gccfork import pref_get
            af = bool(pref_get("slim_default_anti_fragmentation", False))
        except Exception:
            af = False
    else:
        af = bool(af_explicit)

    # dynamic_cap pref (default ON)
    dyn_explicit = getattr(args, "dynamic_cap", None)
    if dyn_explicit is None:
        try:
            from gccfork import pref_get
            dyn_cap = bool(pref_get("slim_default_dynamic_cap", True))
        except Exception:
            dyn_cap = True
    else:
        dyn_cap = bool(dyn_explicit)

    krt = getattr(args, "keep_recent_turns", None) or None
    return af, dyn_cap, krt


def _find_jsonl_for_sid(sid: str) -> Optional[Path]:
    """Resolve a sid or prefix to one JSONL path when uniquely matched."""
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    matches: list[Path] = []
    for proj in projects.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            stem = f.stem
            if stem == sid or stem.startswith(sid):
                matches.append(f)
    if len(matches) == 1:
        return matches[0]
    return None


def status_summary() -> str:
    """Return a diagnostic summary for Rust delegation status."""
    enabled = rust_enabled()
    slim_ok = _rust_bin_ok(RUST_SLIM_BIN)
    patch_ok = _rust_bin_ok(RUST_PATCH_BIN)
    flag = (
        "DISABLE" if os.environ.get("GCCFORK_DISABLE_RUST") == "1"
        else "FORCE" if os.environ.get("GCCFORK_FORCE_RUST") == "1"
        else "default"
    )
    return (
        f"Rust delegation: {'ON' if enabled else 'OFF'}  (switch: {flag}, RUST_DEFAULT={RUST_DEFAULT})\n"
        f"  gccfork-slim         : {'✅' if slim_ok else '❌'} {RUST_SLIM_BIN}\n"
        f"  gccfork-claude-patch : {'✅' if patch_ok else '❌'} {RUST_PATCH_BIN}"
    )
