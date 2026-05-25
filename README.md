# GccSlim

Current release: `v2026.05.26.1`

GccSlim is a local session management and session-slimming distribution for Claude Code and Codex CLI workflows.

This staging folder is a **binary distribution**, not the internal development source tree. Rust implementation sources, regression fixtures, private work logs, local session files, hostnames, IP addresses, and personal paths are intentionally excluded.

![GccSlim usage guide](assets/gccslim-guide-en.png)

## QR

Scan this QR code to open the GitHub repository.

![GccSlim GitHub QR](assets/gccslim-github-qr.png)

## Choose your language

GccSlim ships separate **Korean** and **English** distributions. Pick the asset matching your language and platform from the [latest release](https://github.com/insung8150/GccSlim/releases/latest):

| Language | Platform | Asset |
|---|---|---|
| English | Linux x86_64 | `gccslim-en-linux-x86_64-<version>.tar.gz` |
| English | macOS arm64 | `gccslim-en-macos-arm64-<version>.tar.gz` |
| Korean  | Linux x86_64 | `gccslim-ko-linux-x86_64-<version>.tar.gz` |
| Korean  | macOS arm64 | `gccslim-ko-macos-arm64-<version>.tar.gz` |

```bash
# English on Linux
tar xzf gccslim-en-linux-x86_64-*.tar.gz
cd gccslim-en-linux-x86_64-*
bash install.sh
gccslim    # English UI

# Korean on Linux
tar xzf gccslim-ko-linux-x86_64-*.tar.gz
cd gccslim-ko-linux-x86_64-*
bash install.sh
gccslim    # Korean UI
```

**Switching languages later**

Both language tarballs install into the same `~/.local/bin/gccslim`. To switch, download the other language tarball and run `install.sh` again — it overwrites the binary and updates `~/.local/share/gccslim/default-language` accordingly.

The settings panel has a `Language` radio that refreshes a few labels in place, but a complete UI switch still requires installing the matching tarball.

한국어 안내는 [README.ko.md](README.ko.md) 참고.

## Run

Without installing:

```bash
./bin/gccslim
```

Install into `~/.local/bin`:

```bash
./install.sh
gccslim
```

Direct slim command:

```bash
gccslim-now
```

Codex helper commands installed by this release:

```bash
codex-slim-loop
codex-slim-now
```

## Included

- `bin/gccslim`: public TUI entrypoint.
- `bin/gccslim-now`: direct slim wrapper for the active Claude session.
- `bin/gccslim-slim`: platform-selecting slim wrapper.
- `bin/gccslim-claude-patch`: platform-selecting Claude patch wrapper.
- `bin/codex-slim-loop`: Codex same-terminal slim/restart wrapper.
- `bin/codex-slim-now`: Codex active-session slim request helper.
- `bin/gccfork_codex_slim_loop.py`: Codex wrapper loop sidecar.
- `bin/gccfork_codex_slim_reload.py`: Codex JSONL slim plan/apply sidecar.
- `bin/linux-x86_64/`: stripped Linux x86_64 Rust binaries.
- `bin/macos-arm64/`: stripped macOS arm64 Rust binaries.
- `bin/gccfork_*.py`: Python sidecar modules kept under compatibility names.
- `share/gccslim/brain-system-prompt.md`: sanitized runtime prompt.

Compatibility wrappers named `gccfork-slim` and `gccfork-claude-patch` are included because some internal dispatch paths still call those legacy names.

## Not Included

- Rust source code.
- Rust tests or internal regression fixtures.
- Private session logs.
- Local `.claude`, `.codex`, `.gccfork` state.
- Internal Korean work logs and migration notes.
- Machine-specific SSH, hostname, IP, and absolute-path notes.

## License

MIT License. See `LICENSE`.
