"""TTY curtain — write the alternate screen directly to Claude's PTY.

This captures the lesson from the removed gccfork_screen.py: applying the
alternate screen to our stderr has no effect. The write must target Claude's
own /dev/pts/N so the terminal emulator (xterm.js) swaps the screen buffer.

Usage:
    with tty_curtain(claude_pid):
        # /clear, /resume injection echoes only inside the alternate screen.
        ...
    # Returning to the main screen hides the temporary textarea echo.
"""
from __future__ import annotations

import contextlib
import os
import time
from typing import Optional

ALT_ON = b"\x1b[?1049h\x1b[?25l\x1b[2J\x1b[H"   # alt screen + cursor hide + clear + home
ALT_OFF = b"\x1b[?25h\x1b[?1049l"               # cursor show + main screen


def find_claude_tty(claude_pid: int) -> Optional[str]:
    """Return the controlling PTY path for a Claude PID via /proc/<pid>/fd/1."""
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
    """Treat the JSONL as complete after its size is stable for idle_secs.

    Args:
        jsonl_path: absolute path to the JSONL being monitored.
        idle_secs: stable duration required before declaring idle.
        max_wait: total wait ceiling.
        poll_interval: polling interval.
        require_change: if True, do not declare idle until at least one size
            change has been seen after entry. This avoids a false idle when the
            /resume hook has not started writing yet.
        log_fn: diagnostic logger, e.g. lambda msg: print(msg, file=sys.stderr).

    Returns:
        dict: {
            "elapsed": actual wait time,
            "saw_change": whether at least one change was observed,
            "change_count": number of changes,
            "size_start": baseline,
            "size_end": final size,
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
    log(f"  ⏳ jsonl-idle wait start: baseline={baseline:,}B path=...{jsonl_path[-40:]}")

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
            log(f"     +{now - start:5.2f}s  change #{change_count}: {last_size:,}B → {size:,}B (Δ{size - last_size:+,})")
            last_change = now
            last_size = size
        idle_for = now - last_change
        ready = idle_for >= idle_secs and (saw_change or not require_change)
        if ready:
            elapsed = now - start
            log(f"  ✅ jsonl-idle detected: elapsed={elapsed:.2f}s saw_change={saw_change} changes={change_count}")
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
    log(f"  ⚠ jsonl-idle not reached (max_wait): elapsed={elapsed:.2f}s saw_change={saw_change} changes={change_count} reason={reason}")
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
    """Enter alternate screen → yield → wait for JSONL idle → ALT_OFF.

    Args:
        claude_pid: Claude TUI PID.
        enabled: if False, this is a no-op.
        jsonl_path: signal polling target; if None, fallback_tail_wait is used.
        idle_secs: stable duration before treating JSONL processing as done.
        max_wait: total wait ceiling.
        fallback_tail_wait: fallback sleep when jsonl_path is unavailable.
        post_idle_grace: extra sleep after idle to let rendering finish.
        log_fn: diagnostic logger.
    """
    log = log_fn or (lambda _msg: None)
    tty = find_claude_tty(claude_pid) if enabled else None
    yield_start = time.monotonic()
    if tty:
        tty_write(tty, ALT_ON)
        log(f"  🛡️ ALT_ON applied → {tty}")
    try:
        yield tty
    finally:
        body_done = time.monotonic()
        log(f"  🏁 body complete: elapsed {body_done - yield_start:.2f}s")
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
                log(f"  💤 fallback sleep {fallback_tail_wait}s (jsonl_path missing)")
                time.sleep(fallback_tail_wait)
            tty_write(tty, ALT_OFF)
            log(f"  🛡️ ALT_OFF applied — total {time.monotonic() - yield_start:.2f}s")
