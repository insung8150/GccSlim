# Publish Checklist

Release version: `v2026.05.19`

- [x] Choose and add a license.
- [x] Keep the public command name as `gccslim`.
- [x] Review `README.md` and `README.ko.md`.
- [x] Run syntax checks.
- [x] Run secret and personal-path scans.
- [x] Initialize a fresh Git repository in this folder only.
- [x] Avoid copying private work logs from the internal GccSlim repository.
- [x] Confirm `rust/`, `src/`, `scripts/`, and `tests/` source folders are not present.
- [x] Confirm Rust implementation is present only as stripped binaries.
- [x] Confirm Codex helper files are included: `codex-slim-loop`, `codex-slim-now`, `gccfork_codex_slim_loop.py`.

Recommended scan:

Run the local release scan command from the internal release procedure before publishing. Do not commit any hit containing secrets, personal paths, hostnames, IP addresses, or private work notes.
