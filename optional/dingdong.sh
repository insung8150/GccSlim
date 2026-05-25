#!/usr/bin/env bash
# Notification chime — solfege "sol (G5) – mi (E5) – do (C5)" arpeggio (~1.4 s).
#
# Designed for Claude Code's Stop hook:
#   { "type": "command", "command": "bash ~/.local/share/gccslim/dingdong.sh" }
#
# Cross-platform:
#   - Linux:  python3 + numpy + aplay (ALSA)
#   - macOS:  afplay built-in system sound (no python3/numpy needed)
#
# Silent on failure — never prints to stdout/stderr or blocks the hook.

set -u

# macOS branch — afplay with a built-in chime, then exit.
if command -v afplay >/dev/null 2>&1; then
    # Try a sequence of pleasant built-in sounds; fall back to the first that exists.
    for snd in \
        /System/Library/Sounds/Glass.aiff \
        /System/Library/Sounds/Tink.aiff \
        /System/Library/Sounds/Pop.aiff
    do
        if [ -f "$snd" ]; then
            afplay "$snd" >/dev/null 2>&1
            exit 0
        fi
    done
    exit 0
fi

# Linux branch — python3 + numpy + aplay. If any dep is missing, exit silently.
if ! command -v aplay >/dev/null 2>&1; then
    exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
    exit 0
fi

python3 - <<'PY' 2>/dev/null
try:
    import numpy as np
    import subprocess
except Exception:
    raise SystemExit(0)

sr = 44100

def make_note(freq: float, duration: float) -> "np.ndarray":
    t = np.linspace(0, duration, int(sr * duration), False)
    envelope = np.exp(-t * 5)
    tone = np.sin(2 * np.pi * freq * t) * envelope
    tone += np.sin(2 * np.pi * freq * 2 * t) * envelope * 0.3
    tone += np.sin(2 * np.pi * freq * 3 * t) * envelope * 0.1
    return tone * 0.5

ding = make_note(784, 0.4)   # G5 sol
dong = make_note(659, 0.4)   # E5 mi
deng = make_note(523, 0.6)   # C5 do
gap = np.zeros(int(sr * 0.05))

audio = np.concatenate([ding, gap, dong, gap, deng])
audio = (audio * 32767).astype(np.int16)

try:
    proc = subprocess.Popen(
        ['aplay', '-f', 'S16_LE', '-r', '44100', '-c', '1'],
        stdin=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
    )
    proc.communicate(audio.tobytes(), timeout=5)
except Exception:
    raise SystemExit(0)
PY

exit 0
