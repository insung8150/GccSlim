#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SHARE_DIR="${HOME}/.local/share/gccslim"
# Integration assets — copied verbatim so the in-TUI advisor can locate
# patch-settings.py / slim-reload-intercept.sh / slim*.md / dingdong.sh
# without depending on the unpacked install tree.
INTEGRATION_DIR="$SHARE_DIR/integration"

mkdir -p "$BIN_DIR" "$SHARE_DIR" "$INTEGRATION_DIR"
mkdir -p "$BIN_DIR/linux-x86_64" "$BIN_DIR/macos-arm64"
mkdir -p "$SHARE_DIR/i18n"

install -m 0755 "$ROOT/bin/gccslim" "$BIN_DIR/gccslim"
install -m 0755 "$ROOT/bin/gccslim-now" "$BIN_DIR/gccslim-now"
install -m 0755 "$ROOT/bin/gccslim-slim" "$BIN_DIR/gccslim-slim"
install -m 0755 "$ROOT/bin/gccslim-claude-patch" "$BIN_DIR/gccslim-claude-patch"
if [[ -x "$ROOT/bin/codex-slim-loop" ]]; then
  install -m 0755 "$ROOT/bin/codex-slim-loop" "$BIN_DIR/codex-slim-loop"
fi
if [[ -x "$ROOT/bin/codex-slim-now" ]]; then
  install -m 0755 "$ROOT/bin/codex-slim-now" "$BIN_DIR/codex-slim-now"
fi

# Compatibility names used by legacy internal dispatch paths.
install -m 0755 "$ROOT/bin/gccfork-slim" "$BIN_DIR/gccfork-slim"
install -m 0755 "$ROOT/bin/gccfork-claude-patch" "$BIN_DIR/gccfork-claude-patch"

if [[ -x "$ROOT/bin/linux-x86_64/gccslim-slim" ]]; then
  install -m 0755 "$ROOT/bin/linux-x86_64/gccslim-slim" "$BIN_DIR/linux-x86_64/gccslim-slim"
fi
if [[ -x "$ROOT/bin/linux-x86_64/gccslim-claude-patch" ]]; then
  install -m 0755 "$ROOT/bin/linux-x86_64/gccslim-claude-patch" "$BIN_DIR/linux-x86_64/gccslim-claude-patch"
fi
if [[ -x "$ROOT/bin/macos-arm64/gccslim-slim" ]]; then
  install -m 0755 "$ROOT/bin/macos-arm64/gccslim-slim" "$BIN_DIR/macos-arm64/gccslim-slim"
fi
if [[ -x "$ROOT/bin/macos-arm64/gccslim-claude-patch" ]]; then
  install -m 0755 "$ROOT/bin/macos-arm64/gccslim-claude-patch" "$BIN_DIR/macos-arm64/gccslim-claude-patch"
fi

for f in "$ROOT"/bin/gccfork_*.py; do
  install -m 0755 "$f" "$BIN_DIR/$(basename "$f")"
done

if [[ -f "$ROOT/share/gccslim/brain-system-prompt.md" ]]; then
  install -m 0644 "$ROOT/share/gccslim/brain-system-prompt.md" "$SHARE_DIR/brain-system-prompt.md"
fi
if [[ -f "$ROOT/share/gccslim/default-language" ]]; then
  install -m 0644 "$ROOT/share/gccslim/default-language" "$SHARE_DIR/default-language"
fi
if [[ -d "$ROOT/share/i18n" ]]; then
  for f in "$ROOT"/share/i18n/*.json; do
    [[ -f "$f" ]] || continue
    install -m 0644 "$f" "$SHARE_DIR/i18n/$(basename "$f")"
  done
fi

# Integration assets — Claude /slim hook + slash commands + optional dingdong.
# The install advisor (gccfork_install_advisor.py) reads from this directory.
if [[ -d "$ROOT/claude-integration" ]]; then
  install -d -m 0755 "$INTEGRATION_DIR/claude-integration"
  for f in slim.md dry.md slim-reload-intercept.sh patch-settings.py; do
    if [[ -f "$ROOT/claude-integration/$f" ]]; then
      mode=0644
      case "$f" in
        *.sh|*.py) mode=0755 ;;
      esac
      install -m "$mode" "$ROOT/claude-integration/$f" \
        "$INTEGRATION_DIR/claude-integration/$f"
    fi
  done
fi
if [[ -d "$ROOT/optional" ]]; then
  install -d -m 0755 "$INTEGRATION_DIR/optional"
  if [[ -f "$ROOT/optional/dingdong.sh" ]]; then
    install -m 0755 "$ROOT/optional/dingdong.sh" \
      "$INTEGRATION_DIR/optional/dingdong.sh"
    install -m 0755 "$ROOT/optional/dingdong.sh" \
      "$SHARE_DIR/dingdong.sh"
  fi
fi

echo "Installed GccSlim to $BIN_DIR"
echo "Integration assets in $INTEGRATION_DIR"
echo "Run: gccslim"
