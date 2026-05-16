#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/gccslim"

mkdir -p "$BIN_DIR" "$SHARE_DIR"

install -m 0755 "$ROOT/bin/gccslim" "$BIN_DIR/gccslim"
install -m 0755 "$ROOT/bin/gccslim-now" "$BIN_DIR/gccslim-now"
install -m 0755 "$ROOT/bin/gccslim-slim" "$BIN_DIR/gccslim-slim"
install -m 0755 "$ROOT/bin/gccslim-claude-patch" "$BIN_DIR/gccslim-claude-patch"

# Compatibility names used by legacy internal dispatch paths.
install -m 0755 "$ROOT/bin/gccfork-slim" "$BIN_DIR/gccfork-slim"
install -m 0755 "$ROOT/bin/gccfork-claude-patch" "$BIN_DIR/gccfork-claude-patch"

for f in "$ROOT"/bin/gccfork_*.py; do
  install -m 0644 "$f" "$BIN_DIR/$(basename "$f")"
done

if [[ -f "$ROOT/share/gccslim/brain-system-prompt.md" ]]; then
  install -m 0644 "$ROOT/share/gccslim/brain-system-prompt.md" "$SHARE_DIR/brain-system-prompt.md"
fi

echo "Installed GccSlim to $BIN_DIR"
echo "Run: gccslim"
