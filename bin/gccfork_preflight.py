"""Automatic slim preflight checks for gccfork.

Runs safety checks in parallel before spawning slim-and-reload. If any check
fails, spawning is denied to avoid stale buffer forks and JSONL corruption.

Related internal design note: feedback_module_separation.md.

Checks:
  1. claude busy           — JSONL mtime plus /proc/<pid>/io heuristic
  2. tool_use orphan       — unfinished tool_use without matching tool_result
  3. jsonl flock           — fcntl LOCK_EX | LOCK_NB probe
  4. spawn lock            — ~/.claude/gccfork-locks/<sid>.lock guard
  5. inject backlog        — too many unprocessed inject requests
  6. session lock freshness — stale sessions/<PID>.json suggests a zombie
  7. disk space            — free bytes >= jsonl size × 3
  8. single_pid            — fail if more than one live claude PID owns sid
  9. binary_version        — fail if live PIDs for sid use mixed versions

Each check returns:
  {"ok": bool, "name": str, "reason": str|None, "elapsed_ms": float}

The parallel runner `run_preflight(sid, jsonl_path, claude_pid)` uses
ThreadPoolExecutor, so total time is close to the slowest single check.
"""
from __future__ import annotations
import json
import os
import re
import time
import fcntl
from pathlib import Path
from typing import NamedTuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed


SPAWN_LOCK_DIR = Path.home() / ".claude" / "gccfork-locks"
INJECT_DIR = Path.home() / ".claude" / "gccfork-inject-requests"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"


class CheckResult(NamedTuple):
    ok: bool
    name: str
    reason: Optional[str]
    elapsed_ms: float


class PreflightResult(NamedTuple):
    ok: bool
    checks: list[CheckResult]
    total_elapsed_ms: float

    def fail_reasons(self) -> list[str]:
        return [f"{c.name}: {c.reason}" for c in self.checks if not c.ok]

    def summary(self) -> str:
        if self.ok:
            ps = ", ".join(f"{c.name}({c.elapsed_ms:.1f}ms)" for c in self.checks)
            return f"✓ preflight OK ({self.total_elapsed_ms:.1f}ms) — {ps}"
        fails = [c for c in self.checks if not c.ok]
        return (f"❌ preflight FAIL ({self.total_elapsed_ms:.1f}ms) — "
                + " | ".join(f"{c.name}: {c.reason}" for c in fails))


# ════════════════════════════════════════════════════════════════════
# Individual checks. Each is normally millisecond-scale and runs in parallel.
# ════════════════════════════════════════════════════════════════════

def _timed(name: str, fn, *args, **kwargs) -> CheckResult:
    t0 = time.monotonic()
    try:
        ok, reason = fn(*args, **kwargs)
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    return CheckResult(ok, name, reason, (time.monotonic() - t0) * 1000)


def check_busy(jsonl_path: Path, claude_pid: int,
               idle_window_sec: float = 5.0) -> tuple[bool, Optional[str]]:
    """Detect active Claude output using JSONL mtime and a /proc IO heuristic."""
    try:
        mtime_age = time.time() - jsonl_path.stat().st_mtime
        if mtime_age < idle_window_sec:
            return False, f"jsonl mtime is fresh ({mtime_age:.1f}s ago; response may be active)"
    except OSError as exc:
        return False, f"stat failed: {exc}"
    # One-shot /proc/<pid>/io read_bytes probe. It is not exact but very cheap.
    try:
        io_path = Path(f"/proc/{claude_pid}/io")
        if io_path.exists():
            data = io_path.read_text()
            # Tracking read_bytes changes needs two samples. Keep mtime as the
            # main cheap signal for now.
            _ = data
    except OSError:
        pass
    return True, None


def check_tool_use_orphan(jsonl_path: Path) -> tuple[bool, Optional[str]]:
    """Detect unfinished tool_use blocks near the JSONL tail."""
    try:
        size = jsonl_path.stat().st_size
        with jsonl_path.open("rb") as fh:
            seek_pos = max(0, size - 65536)  # Tail 64KB is enough for 30+ lines.
            fh.seek(seek_pos)
            tail = fh.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()[-30:]
    except OSError as exc:
        return False, f"jsonl read failed: {exc}"

    pending_tool_use_ids: set[str] = set()
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use":
                    tu_id = block.get("id")
                    if tu_id:
                        pending_tool_use_ids.add(tu_id)
                elif btype == "tool_result":
                    tu_id = block.get("tool_use_id")
                    if tu_id:
                        pending_tool_use_ids.discard(tu_id)
    if pending_tool_use_ids:
        return False, f"{len(pending_tool_use_ids)} unfinished tool_use block(s); response may be active"
    return True, None


def check_jsonl_flock(jsonl_path: Path) -> tuple[bool, Optional[str]]:
    """Try LOCK_EX|LOCK_NB on the JSONL; fail if another process holds it."""
    try:
        fd = os.open(str(jsonl_path), os.O_RDONLY)
    except OSError as exc:
        return False, f"open failed: {exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return False, "another process holds the JSONL; Claude may be writing"
        except OSError as exc:
            return False, f"flock failed: {exc}"
        return True, None
    finally:
        os.close(fd)


def check_spawn_lock(sid: str, hold: bool = False) -> tuple[bool, Optional[str]]:
    """Acquire the per-session spawn lock.

    `hold=False` is try-only for preflight. `hold=True` keeps the lock and is
    used immediately before entering slim.
    """
    SPAWN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SPAWN_LOCK_DIR / f"{sid}.lock"
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        return False, f"lock open failed: {exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, f"another spawn is already in progress ({sid[:8]})"
        if not hold:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return True, None
    finally:
        if not hold:
            os.close(fd)


def check_inject_backlog(threshold: int = 20) -> tuple[bool, Optional[str]]:
    """Fail if too many stale inject requests are queued.

    Real-time polling is handled after spawn; preflight only checks backlog.
    """
    if not INJECT_DIR.exists():
        return True, None
    try:
        pending = [f for f in INJECT_DIR.glob("*.json")
                   if not f.name.startswith(".claimed-")]
        if len(pending) >= threshold:
            return False, f"inject backlog {len(pending)} (>= {threshold})"
    except OSError as exc:
        return False, f"INJECT_DIR scan failed: {exc}"
    return True, None


def check_session_lock_freshness(claude_pid: int,
                                  stale_sec: float = 300.0) -> tuple[bool, Optional[str]]:
    """Check sessions/<PID>.json freshness; stale files suggest zombies."""
    lock_path = SESSIONS_DIR / f"{claude_pid}.json"
    if not lock_path.exists():
        return False, f"sessions/{claude_pid}.json is missing; Claude may be dead"
    if not Path(f"/proc/{claude_pid}").exists():
        return False, f"PID {claude_pid} is missing; zombie lock"
    try:
        age = time.time() - lock_path.stat().st_mtime
        if age > stale_sec:
            return False, f"session lock was updated {age/60:.1f} minutes ago; zombie suspected"
    except OSError as exc:
        return False, f"stat failed: {exc}"
    return True, None


_VERSION_RE = re.compile(r"/versions/([0-9]+\.[0-9]+\.[0-9]+)(?:/|$)")


def _holders_for_sid(sid: str) -> list[tuple[int, str]]:
    """Return live (pid, version) holders for sid from sessions/<PID>.json.

    The version is extracted from the .../versions/<X.Y.Z>/... segment in
    /proc/<pid>/exe. If extraction fails, version is an empty string.
    """
    if not SESSIONS_DIR.exists():
        return []
    sid_prefix = sid[:8]
    holders: list[tuple[int, str]] = []
    try:
        sess_files = list(SESSIONS_DIR.glob("*.json"))
    except OSError:
        return []
    for sess_file in sess_files:
        try:
            pid = int(sess_file.stem)
        except ValueError:
            continue
        if not Path(f"/proc/{pid}").exists():
            continue
        try:
            data = json.loads(sess_file.read_text())
        except Exception:
            continue
        sess_id = data.get("sessionId") or ""
        if not sess_id.startswith(sid_prefix):
            continue
        version = ""
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
            m = _VERSION_RE.search(exe)
            if m:
                version = m.group(1)
        except OSError:
            pass
        holders.append((pid, version))
    return holders


def check_single_pid_per_sid(sid: str) -> tuple[bool, Optional[str]]:
    """Fail if more than one live Claude PID owns the same sid.

    Stale forks from old VSCode windows can write the same JSONL concurrently,
    causing chord races and corruption.
    """
    holders = _holders_for_sid(sid)
    if len(holders) > 1:
        pid_list = ", ".join(f"{p}({v or '?'})" for p, v in holders)
        return False, f"sid {sid[:8]} is held by {len(holders)} PIDs: {pid_list}"
    return True, None


def check_binary_version_consistency(sid: str) -> tuple[bool, Optional[str]]:
    """Check that all live PIDs for sid use the same Claude binary version.

    Mixed old/new binaries can disagree on chord or atomic schemas.
    """
    holders = _holders_for_sid(sid)
    if len(holders) <= 1:
        return True, None  # single_pid catches this separately, or it is normal.
    versions = {v for _, v in holders if v}
    if len(versions) > 1:
        return False, f"mixed binary versions {sorted(versions)}; stale zombie suspected"
    return True, None


def check_disk_space(jsonl_path: Path, multiplier: float = 3.0) -> tuple[bool, Optional[str]]:
    """Check free disk space for backup, new JSONL, and buffer."""
    try:
        jsonl_size = jsonl_path.stat().st_size
        st = os.statvfs(str(jsonl_path.parent))
        free_bytes = st.f_bavail * st.f_frsize
        required = int(jsonl_size * multiplier)
        if free_bytes < required:
            return False, (f"insufficient disk: need {required/1024/1024:.1f}MB, "
                            f"available {free_bytes/1024/1024:.1f}MB")
    except OSError as exc:
        return False, f"statvfs failed: {exc}"
    return True, None


# ════════════════════════════════════════════════════════════════════
# Parallel runner
# ════════════════════════════════════════════════════════════════════

def run_preflight(
    sid: str,
    jsonl_path: Path,
    claude_pid: int,
    *,
    skip: Optional[set[str]] = None,
    inject_backlog_threshold: int = 20,
    busy_idle_window_sec: float = 5.0,
    session_lock_stale_sec: float = 300.0,
    disk_multiplier: float = 3.0,
) -> PreflightResult:
    """Run checks in parallel. ok=False if any check fails.

    `skip` is a set of check names to skip, for example {"disk"}.
    """
    skip = skip or set()
    jobs = []
    if "busy" not in skip:
        jobs.append(("busy", check_busy, (jsonl_path, claude_pid, busy_idle_window_sec)))
    if "tool_use_orphan" not in skip:
        jobs.append(("tool_use_orphan", check_tool_use_orphan, (jsonl_path,)))
    if "jsonl_flock" not in skip:
        jobs.append(("jsonl_flock", check_jsonl_flock, (jsonl_path,)))
    if "spawn_lock" not in skip:
        jobs.append(("spawn_lock", check_spawn_lock, (sid, False)))
    if "inject_backlog" not in skip:
        jobs.append(("inject_backlog", check_inject_backlog, (inject_backlog_threshold,)))
    if "session_lock" not in skip:
        jobs.append(("session_lock", check_session_lock_freshness,
                      (claude_pid, session_lock_stale_sec)))
    if "disk" not in skip:
        jobs.append(("disk", check_disk_space, (jsonl_path, disk_multiplier)))
    if "single_pid" not in skip:
        jobs.append(("single_pid", check_single_pid_per_sid, (sid,)))
    if "binary_version" not in skip:
        jobs.append(("binary_version", check_binary_version_consistency, (sid,)))

    t0 = time.monotonic()
    results: list[CheckResult] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {pool.submit(_timed, name, fn, *args): name
                for name, fn, args in jobs}
        for fut in as_completed(futs):
            results.append(fut.result())

    # Sort by stable display order.
    order = ["busy", "tool_use_orphan", "jsonl_flock", "spawn_lock",
             "inject_backlog", "session_lock", "disk",
             "single_pid", "binary_version"]
    results.sort(key=lambda c: order.index(c.name) if c.name in order else 99)
    total_elapsed = (time.monotonic() - t0) * 1000
    ok = all(c.ok for c in results)
    return PreflightResult(ok=ok, checks=results, total_elapsed_ms=total_elapsed)


# ════════════════════════════════════════════════════════════════════
# Spawn lock context used immediately before entering slim.
# ════════════════════════════════════════════════════════════════════

class spawn_lock_holder:
    """Hold the per-session flock and release it on exit."""
    def __init__(self, sid: str):
        self.sid = sid
        self.fd: Optional[int] = None

    def __enter__(self) -> "spawn_lock_holder":
        SPAWN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = SPAWN_LOCK_DIR / f"{self.sid}.lock"
        self.fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f"spawn lock is already held: {self.sid[:8]}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
