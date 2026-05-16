"""TTY curtain — claude TUI 가 붙어 있는 PTY 자체에 alt screen 직접 write.

이전 gccfork_screen.py (제거됨) 가 우리 stderr 에 alt screen 을 적용해 효과 없었던
교훈을 반영. 적용 위치 = **claude 의 /dev/pts/N**. terminal emulator (xterm.js)
가 화면 버퍼 교체.

사용:
    with tty_curtain(claude_pid):
        # /clear, /resume inject — alt screen 안에서만 textarea echo 발생
        ...
    # 빠져나오면 main screen 복귀 → 안에서 그려진 textarea 자동 사라짐
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Optional

ALT_ON = b"\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H"   # alt screen + cursor hide + clear + home
ALT_OFF = b"\x1b[?25h\x1b[?1049l"               # cursor show + main screen


def find_claude_tty(claude_pid: int) -> Optional[str]:
    """claude PID 의 controlling PTY 경로 찾기. /proc/<pid>/fd/1 readlink."""
    try:
        path = os.readlink(f"/proc/{claude_pid}/fd/1")
    except OSError:
        return None
    if path.startswith("/dev/pts/") or path.startswith("/dev/tty"):
        return path
    return None


def tty_write(tty_path: str, data: bytes) -> bool:
    try:
        fd = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return False
    try:
        os.write(fd, data)
        return True
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def wait_for_jsonl_idle(
    jsonl_path: str,
    *,
    idle_secs: float = 2.0,
    max_wait: float = 30.0,
    poll_interval: float = 0.2,
    require_change: bool = True,
    log_fn=None,
) -> dict:
    """jsonl 파일이 idle_secs 동안 size 변화 없으면 처리 완료로 간주.

    Args:
        jsonl_path: monitor 대상 jsonl 절대 경로
        idle_secs: 이 시간 동안 변경 없으면 idle 판정
        max_wait: 전체 최대 대기 시간 (안전 상한)
        poll_interval: 폴링 주기
        require_change: True 면 진입 후 최소 1번 변경 보기 전엔 idle 판정 X.
            본체 직후 wait 진입할 때 /resume hook 이 아직 시작도 안 했으면
            jsonl 이 stable 상태 → 즉시 idle 오판 가능. 이걸 막음.
        log_fn: 진단 로그 함수 (e.g. lambda msg: print(msg, file=sys.stderr))

    Returns:
        dict: {
            "elapsed": 실제 대기 시간,
            "saw_change": 변경 1회 이상 봤는지,
            "change_count": 변경 회수,
            "size_start": baseline,
            "size_end": 종료 시 크기,
            "reason": "idle" | "max_wait" | "no_change",
        }
    """
    log = log_fn or (lambda _msg: None)
    start = time.monotonic()
    try:
        baseline = os.path.getsize(jsonl_path)
    except OSError:
        baseline = -1
    last_size = baseline
    last_change = start
    saw_change = False
    change_count = 0
    log(f"  ⏳ jsonl-idle wait 시작: baseline={baseline:,}B path=...{jsonl_path[-40:]}")

    while time.monotonic() - start < max_wait:
        try:
            size = os.path.getsize(jsonl_path)
        except OSError:
            time.sleep(poll_interval)
            continue
        now = time.monotonic()
        if size != last_size:
            saw_change = True
            change_count += 1
            log(f"     +{now - start:5.2f}s  변경 #{change_count}: {last_size:,}B → {size:,}B (Δ{size - last_size:+,})")
            last_change = now
            last_size = size
        idle_for = now - last_change
        ready = idle_for >= idle_secs and (saw_change or not require_change)
        if ready:
            elapsed = now - start
            log(f"  ✅ jsonl-idle 감지: elapsed={elapsed:.2f}s saw_change={saw_change} changes={change_count}")
            return {
                "elapsed": elapsed,
                "saw_change": saw_change,
                "change_count": change_count,
                "size_start": baseline,
                "size_end": last_size,
                "reason": "idle",
            }
        time.sleep(poll_interval)

    elapsed = time.monotonic() - start
    reason = "no_change" if (require_change and not saw_change) else "max_wait"
    log(f"  ⚠ jsonl-idle 미달 (max_wait): elapsed={elapsed:.2f}s saw_change={saw_change} changes={change_count} reason={reason}")
    return {
        "elapsed": elapsed,
        "saw_change": saw_change,
        "change_count": change_count,
        "size_start": baseline,
        "size_end": last_size,
        "reason": reason,
    }


@contextlib.contextmanager
def tty_curtain(
    claude_pid: int,
    *,
    enabled: bool = True,
    jsonl_path: Optional[str] = None,
    idle_secs: float = 2.0,
    max_wait: float = 30.0,
    fallback_tail_wait: float = 1.5,
    post_idle_grace: float = 0.5,
    log_fn=None,
):
    """alt screen 진입 → yield → (jsonl idle 대기) → ALT_OFF.

    Args:
        claude_pid: claude TUI 의 PID.
        enabled: False 면 no-op.
        jsonl_path: 신호 polling target. None 이면 fallback_tail_wait sleep.
        idle_secs: jsonl 이 이 시간 동안 변경 없으면 처리 완료.
        max_wait: 전체 최대 대기 시간 (안전 상한).
        fallback_tail_wait: jsonl_path 없을 때 fallback sleep.
        post_idle_grace: idle 감지 후 추가 sleep — render 마무리 마진.
        log_fn: 진단 로그 함수.
    """
    log = log_fn or (lambda _msg: None)
    tty = find_claude_tty(claude_pid) if enabled else None
    yield_start = time.monotonic()
    if tty:
        tty_write(tty, ALT_ON)
        log(f"  🛡️ ALT_ON 인가 → {tty}")
    try:
        yield tty
    finally:
        body_done = time.monotonic()
        log(f"  🏁 본체 완료: 본체 소요 {body_done - yield_start:.2f}s")
        if tty:
            if jsonl_path:
                wait_for_jsonl_idle(
                    jsonl_path,
                    idle_secs=idle_secs,
                    max_wait=max_wait,
                    require_change=True,
                    log_fn=log,
                )
                if post_idle_grace > 0:
                    log(f"  💤 post-idle grace sleep {post_idle_grace}s")
                    time.sleep(post_idle_grace)
            elif fallback_tail_wait > 0:
                log(f"  💤 fallback sleep {fallback_tail_wait}s (jsonl_path 없음)")
                time.sleep(fallback_tail_wait)
            tty_write(tty, ALT_OFF)
            log(f"  🛡️ ALT_OFF 인가 — 총 {time.monotonic() - yield_start:.2f}s")
