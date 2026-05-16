# Publish Checklist

- [x] Choose and add a license.
- [ ] Keep the public command name as `gccslim`.
- [ ] Review `README.md` and `README.ko.md`.
- [ ] Run syntax checks.
- [ ] Run secret and personal-path scans.
- [ ] Initialize a fresh Git repository in this folder only.
- [ ] Avoid copying private work logs from the internal GccSlim repository.
- [ ] Confirm `rust/`, `src/`, `scripts/`, and `tests/` source folders are not present.
- [ ] Confirm Rust implementation is present only as stripped binaries.

Recommended scan:

Run the local release scan command from the internal release procedure before publishing. Do not commit any hit containing secrets, personal paths, hostnames, IP addresses, or private work notes.
