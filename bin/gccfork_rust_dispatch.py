"""Rust 바이너리 위임 — 코드 내부 스위치.

세 가지 subcommand 가 Rust 바이너리 (`~/.local/bin/gccfork-slim`,
`~/.local/bin/gccfork-claude-patch`) 가 있으면 위임, 없거나 비활성이면
Python fallback 으로 떨어짐.

스위치 우선순위:
    1. 환경변수 `GCCFORK_DISABLE_RUST=1` — Python 강제
    2. 환경변수 `GCCFORK_FORCE_RUST=1` — Rust 가 없거나 실패해도 fallback 안 함 (디버그)
    3. 모듈 상수 `RUST_DEFAULT` — 기본값 (현재 True = Rust 우선)

사용:
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


# ─── 코드 내부 스위치 (기본값) ────────────────────────────────
RUST_DEFAULT: bool = True  # True = Rust 우선 / False = Python 강제

RUST_SLIM_BIN = Path.home() / ".local" / "bin" / "gccfork-slim"
RUST_PATCH_BIN = Path.home() / ".local" / "bin" / "gccfork-claude-patch"


def rust_enabled() -> bool:
    """현재 Rust 위임이 활성화되어 있는가."""
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
    """subprocess 로 Rust 바이너리 호출. exit code 반환.

    stdout/stderr 는 그대로 부모로 흘림 (사용자 안내 일관성).
    json_out 이면 stdout 만 캡처해서 부모 stdout 에 그대로 출력.
    """
    try:
        if json_out:
            r = subprocess.run(cmd, check=False)
            return r.returncode
        return subprocess.call(cmd)
    except OSError as exc:
        print(f"⚠️ Rust 바이너리 spawn 실패: {exc}", file=sys.stderr)
        return -1


# ─── slim-inplace 위임 ────────────────────────────────────────
def try_rust_slim_inplace(args) -> Optional[int]:
    """`gccfork slim-inplace <sid> ...` 의 Rust 위임 시도.

    Rust 가 활성이고 호출 성공이면 exit code 반환.
    Rust 비활성 또는 spawn 실패면 None 반환 → Python fallback.
    """
    if not rust_enabled():
        return None
    if not _rust_bin_ok(RUST_SLIM_BIN):
        if _force_rust():
            print("❌ GCCFORK_FORCE_RUST=1 인데 gccfork-slim 바이너리 없음", file=sys.stderr)
            return 5
        return None

    # sid → jsonl 경로 직접 찾기 (Python 의 scan_sessions 와 동등하게)
    sid = getattr(args, "sid", None)
    if not sid:
        return None
    jsonl_path = _find_jsonl_for_sid(sid)
    if jsonl_path is None:
        # Python 의 scan_sessions 가 더 똑똑할 수 있음 (prefix 매칭) → fallback
        return None

    cmd = [
        str(RUST_SLIM_BIN),
        "--jsonl", str(jsonl_path),
        "--mode", getattr(args, "mode", "strong"),
        # preflight default skip — Python 측에서 이미 의도적 호출.
        # Rust preflight 가 silent fail 하면 슬림 무처리되는 사례 회피 (2026-05-13).
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

    # 단편화 방지 — Rust 가 직접 처리 (Python fallback 우회 불필요)
    af, dyn_cap, krt = _resolve_anti_frag_args(args)
    if af:
        cmd.append("--anti-frag")
        if not dyn_cap:
            cmd.append("--no-dynamic-cap")
        if krt is not None:
            cmd.extend(["--keep-recent-turns", str(krt)])
        # GCCFORK_VISIBLE_CAP 또는 args.visible_cap → 안 보이는 영역 압축본화
        # (선택 A — Claude Code cap (~115 turn) 안의 영역만 bundle).
        # default 230 (Claude Code 의 메시지 카운트 cap). visible_cap 미적용 시
        # bundle 이 거대해져 claude resume 시 timeout. (2026-05-13 cfb593e0 사건 회고)
        vc = getattr(args, "visible_cap", None)
        if vc is None:
            import os as _os
            v_env = _os.environ.get("GCCFORK_VISIBLE_CAP", "").strip()
            if v_env.isdigit():
                vc = int(v_env)
            else:
                vc = 230  # 기본값 (env 미설정 시)
        if vc and vc > 0:
            cmd.extend(["--visible-cap", str(vc)])

    rc = _spawn(cmd, json_out=getattr(args, "json", False))
    if rc < 0 and not _force_rust():
        return None
    return rc


# ─── slim-and-reload 위임 ─────────────────────────────────────
def try_rust_slim_and_reload(args) -> Optional[int]:
    """`gccfork slim-and-reload --self|--sid <sid>` 의 Rust 위임 시도."""
    if not rust_enabled():
        return None
    if not _rust_bin_ok(RUST_SLIM_BIN):
        if _force_rust():
            print("❌ GCCFORK_FORCE_RUST=1 인데 gccfork-slim 바이너리 없음", file=sys.stderr)
            return 5
        return None

    cmd = [str(RUST_SLIM_BIN)]
    if getattr(args, "self_session", False) or getattr(args, "self", False):
        cmd.append("--self")
    elif getattr(args, "sid", None):
        cmd.extend(["--sid", args.sid])
    else:
        return None  # 모드 불명 → Python 으로 위임

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

    # 단편화 방지 — Rust 가 직접 처리
    af, dyn_cap, krt = _resolve_anti_frag_args(args)
    if af:
        cmd.append("--anti-frag")
        if not dyn_cap:
            cmd.append("--no-dynamic-cap")
        if krt is not None:
            cmd.extend(["--keep-recent-turns", str(krt)])
        # GCCFORK_VISIBLE_CAP 또는 args.visible_cap → 안 보이는 영역 압축본화
        # (선택 A — Claude Code cap (~115 turn) 안의 영역만 bundle).
        # default 230 (Claude Code 의 메시지 카운트 cap). visible_cap 미적용 시
        # bundle 이 거대해져 claude resume 시 timeout. (2026-05-13 cfb593e0 사건 회고)
        vc = getattr(args, "visible_cap", None)
        if vc is None:
            import os as _os
            v_env = _os.environ.get("GCCFORK_VISIBLE_CAP", "").strip()
            if v_env.isdigit():
                vc = int(v_env)
            else:
                vc = 230  # 기본값 (env 미설정 시)
        if vc and vc > 0:
            cmd.extend(["--visible-cap", str(vc)])

    rc = _spawn(cmd, json_out=getattr(args, "json", False))
    if rc < 0 and not _force_rust():
        return None
    return rc


# ─── patch-claude 위임 ────────────────────────────────────────
def try_rust_patch_claude(args) -> Optional[int]:
    """`gccfork patch-claude` 의 Rust 위임 시도."""
    if not rust_enabled():
        return None
    if not _rust_bin_ok(RUST_PATCH_BIN):
        if _force_rust():
            print("❌ GCCFORK_FORCE_RUST=1 인데 gccfork-claude-patch 바이너리 없음",
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


# ─── 헬퍼 ─────────────────────────────────────────────────────
def _resolve_anti_frag_args(args) -> tuple[bool, bool, Optional[int]]:
    """args + prefs 로부터 (anti_frag, dynamic_cap, keep_recent_turns) 결정.

    Rust 가 anti-frag 직접 지원 (Phase D, 2026-05-06) — Python fallback 불필요.
    """
    # anti-frag flag — args 우선, 없으면 settings pref
    af_explicit = getattr(args, "anti_fragmentation", None)
    if af_explicit is None:
        try:
            from gccfork import pref_get
            af = bool(pref_get("slim_default_anti_fragmentation", False))
        except Exception:
            af = False
    else:
        af = bool(af_explicit)

    # dynamic_cap pref (기본 ON)
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
    """sid (또는 8자 prefix) → jsonl 경로. 1개만 매치 시 반환."""
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
    """현재 위임 상태 요약 — `--rust-status` 같은 진단용."""
    enabled = rust_enabled()
    slim_ok = _rust_bin_ok(RUST_SLIM_BIN)
    patch_ok = _rust_bin_ok(RUST_PATCH_BIN)
    flag = (
        "DISABLE" if os.environ.get("GCCFORK_DISABLE_RUST") == "1"
        else "FORCE" if os.environ.get("GCCFORK_FORCE_RUST") == "1"
        else "default"
    )
    return (
        f"Rust 위임: {'ON' if enabled else 'OFF'}  (스위치: {flag}, RUST_DEFAULT={RUST_DEFAULT})\n"
        f"  gccfork-slim         : {'✅' if slim_ok else '❌'} {RUST_SLIM_BIN}\n"
        f"  gccfork-claude-patch : {'✅' if patch_ok else '❌'} {RUST_PATCH_BIN}"
    )
