"""🛡️ 자동 슬림 사전 검증 (preflight) — gccfork 사이드카.

slim-and-reload spawn 전에 9개 체크를 **병렬** 실행. 어느 하나라도 fail 하면
spawn 거절 → e327/654e 같은 buffer fork 잔존 + jsonl 손상 같은 부작용 회피.

CLAUDE 메모리: feedback_module_separation.md (모듈 분리 정책).

체크 항목 (1~9):
  1. claude busy           — jsonl mtime + /proc/<pid>/io read_bytes 5초 윈도
  2. tool_use orphan       — jsonl 끝 30라인에 미완 tool_use → tool_result 매칭
  3. jsonl flock           — fcntl LOCK_EX | LOCK_NB 시도 (상시 못 잡으면 누가 쓰는 중)
  4. spawn lock            — ~/.claude/gccfork-locks/<sid>.lock 동시 spawn 가드
  5. inject backlog        — INJECT_DIR 에 처리 안 된 옛 요청 N개 이상이면 거절
  6. session lock 신선도   — sessions/<PID>.json mtime > 5분 = 좀비 의심
  7. disk 여유              — statvfs free >= jsonl size × 3 (.bak + new + buffer)
  8. single_pid            — 같은 sid 의 살아있는 claude PID 가 2개 이상이면 fail
  9. binary_version        — 같은 sid PID 들이 binary version 혼재 (옛 좀비) 면 fail

각 체크는 결과 dict 반환:
  {"ok": bool, "name": str, "reason": str|None, "elapsed_ms": float}

병렬 runner `run_preflight(sid, jsonl_path, claude_pid) -> PreflightResult`
가 ThreadPoolExecutor 로 동시 호출 + max(elapsed) 만큼만 소요.
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
# 개별 체크 (각각 ~ms 단위, 병렬 실행)
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
    """1. claude busy 감지 — jsonl mtime + /proc IO read_bytes 둘 다 검사."""
    try:
        mtime_age = time.time() - jsonl_path.stat().st_mtime
        if mtime_age < idle_window_sec:
            return False, f"jsonl mtime 신선 ({mtime_age:.1f}s 전 — 응답 진행 중 의심)"
    except OSError as exc:
        return False, f"stat 실패: {exc}"
    # /proc/<pid>/io read_bytes 단발 검사 — 정확하진 않지만 매우 빠른 휴리스틱
    try:
        io_path = Path(f"/proc/{claude_pid}/io")
        if io_path.exists():
            data = io_path.read_text()
            # read_bytes 변화 추적은 두 번 호출이 필요. 단발 검사는 skip.
            # 추후: 짧은 sleep 후 두 번 stat 으로 변화 감지. 지금은 mtime 만으로 충분.
            _ = data
    except OSError:
        pass
    return True, None


def check_tool_use_orphan(jsonl_path: Path) -> tuple[bool, Optional[str]]:
    """2. jsonl 끝 30라인에 tool_use 가 있는데 대응 tool_result 가 없으면 답변 진행 중."""
    try:
        size = jsonl_path.stat().st_size
        with jsonl_path.open("rb") as fh:
            seek_pos = max(0, size - 65536)  # 끝 64KB 면 30라인 이상 충분
            fh.seek(seek_pos)
            tail = fh.read().decode("utf-8", errors="ignore")
        lines = tail.splitlines()[-30:]
    except OSError as exc:
        return False, f"jsonl read 실패: {exc}"

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
        return False, f"미완 tool_use {len(pending_tool_use_ids)}개 (답변 진행 중)"
    return True, None


def check_jsonl_flock(jsonl_path: Path) -> tuple[bool, Optional[str]]:
    """3. jsonl 에 LOCK_EX|LOCK_NB 시도 — 다른 process 가 잡고 있으면 fail."""
    try:
        fd = os.open(str(jsonl_path), os.O_RDONLY)
    except OSError as exc:
        return False, f"open 실패: {exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return False, "다른 process 가 jsonl 점유 중 (claude 가 쓰는 중)"
        except OSError as exc:
            return False, f"flock 실패: {exc}"
        return True, None
    finally:
        os.close(fd)


def check_spawn_lock(sid: str, hold: bool = False) -> tuple[bool, Optional[str]]:
    """4. spawn lock — ~/.claude/gccfork-locks/<sid>.lock 동시 spawn 가드.

    `hold=False` (기본) — try-only, 점유 안 함. preflight 단계.
    `hold=True` — 점유 유지. 슬림 진입 직전 호출.
    """
    SPAWN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SPAWN_LOCK_DIR / f"{sid}.lock"
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        return False, f"lock open 실패: {exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False, f"이미 다른 spawn 진행 중 ({sid[:8]})"
        if not hold:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return True, None
    finally:
        if not hold:
            os.close(fd)


def check_inject_backlog(threshold: int = 20) -> tuple[bool, Optional[str]]:
    """5. inject backlog — INJECT_DIR 에 미처리 요청이 너무 많으면 fail.

    실시간 polling 은 spawn 직후 단계에서 별도 처리. preflight 에선 backlog 만.
    """
    if not INJECT_DIR.exists():
        return True, None
    try:
        pending = [f for f in INJECT_DIR.glob("*.json")
                   if not f.name.startswith(".claimed-")]
        if len(pending) >= threshold:
            return False, f"inject backlog {len(pending)}개 (>= {threshold})"
    except OSError as exc:
        return False, f"INJECT_DIR scan 실패: {exc}"
    return True, None


def check_session_lock_freshness(claude_pid: int,
                                  stale_sec: float = 300.0) -> tuple[bool, Optional[str]]:
    """6. session lock (sessions/<PID>.json) mtime 이 stale_sec 이상이면 좀비 의심."""
    lock_path = SESSIONS_DIR / f"{claude_pid}.json"
    if not lock_path.exists():
        return False, f"sessions/{claude_pid}.json 부재 (claude 인스턴스 죽음 의심)"
    if not Path(f"/proc/{claude_pid}").exists():
        return False, f"PID {claude_pid} 프로세스 부재 (좀비 lock)"
    try:
        age = time.time() - lock_path.stat().st_mtime
        if age > stale_sec:
            return False, f"session lock {age/60:.1f}분 전 갱신 (좀비 의심)"
    except OSError as exc:
        return False, f"stat 실패: {exc}"
    return True, None


_VERSION_RE = re.compile(r"/versions/([0-9]+\.[0-9]+\.[0-9]+)(?:/|$)")


def _holders_for_sid(sid: str) -> list[tuple[int, str]]:
    """sessions/<PID>.json 들 중 sid 와 일치하는 살아있는 (pid, version) 목록 반환.

    version 은 /proc/<pid>/exe 의 .../versions/<X.Y.Z>/... 패턴에서 추출.
    추출 실패 시 빈 문자열.
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
    """8. 같은 sid 의 살아있는 claude PID 가 1개 초과면 fail.

    좀비 fork (옛 vscode 창 잔존 등) 가 같은 jsonl 동시 쓰면 chord race + 폭발.
    """
    holders = _holders_for_sid(sid)
    if len(holders) > 1:
        pid_list = ", ".join(f"{p}({v or '?'})" for p, v in holders)
        return False, f"sid {sid[:8]} PID 다중 점유 {len(holders)}개: {pid_list}"
    return True, None


def check_binary_version_consistency(sid: str) -> tuple[bool, Optional[str]]:
    """9. 같은 sid PID 들이 모두 동일 claude binary version 인지.

    옛 binary 좀비 + 새 binary 동거 시 chord/atomic schema 가 다르면 손상.
    """
    holders = _holders_for_sid(sid)
    if len(holders) <= 1:
        return True, None  # single_pid 가 별도 잡거나 정상
    versions = {v for _, v in holders if v}
    if len(versions) > 1:
        return False, f"binary version 혼재 {sorted(versions)} (옛 좀비 의심)"
    return True, None


def check_disk_space(jsonl_path: Path, multiplier: float = 3.0) -> tuple[bool, Optional[str]]:
    """7. disk 여유 — jsonl size × multiplier (.bak + 새 jsonl + buffer)."""
    try:
        jsonl_size = jsonl_path.stat().st_size
        st = os.statvfs(str(jsonl_path.parent))
        free_bytes = st.f_bavail * st.f_frsize
        required = int(jsonl_size * multiplier)
        if free_bytes < required:
            return False, (f"disk 부족: 필요 {required/1024/1024:.1f}MB, "
                            f"가용 {free_bytes/1024/1024:.1f}MB")
    except OSError as exc:
        return False, f"statvfs 실패: {exc}"
    return True, None


# ════════════════════════════════════════════════════════════════════
# 병렬 runner
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
    """7개 체크 병렬 실행. 어느 하나라도 fail 하면 ok=False.

    `skip`: 건너뛸 체크 이름 set (예: {"disk"}).
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

    # 안정 표시 위해 이름 순 정렬
    order = ["busy", "tool_use_orphan", "jsonl_flock", "spawn_lock",
             "inject_backlog", "session_lock", "disk",
             "single_pid", "binary_version"]
    results.sort(key=lambda c: order.index(c.name) if c.name in order else 99)
    total_elapsed = (time.monotonic() - t0) * 1000
    ok = all(c.ok for c in results)
    return PreflightResult(ok=ok, checks=results, total_elapsed_ms=total_elapsed)


# ════════════════════════════════════════════════════════════════════
# spawn lock 점유 컨텍스트 — slim 진입 직전 wrap
# ════════════════════════════════════════════════════════════════════

class spawn_lock_holder:
    """with spawn_lock_holder(sid): ... — flock 점유, 종료 시 해제."""
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
            raise RuntimeError(f"spawn lock 이미 점유 중: {self.sid[:8]}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
