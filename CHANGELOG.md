# Changelog

## 0.3.0 — 2026-07-18

### Added
- **Delta cloud uploads (content-addressed storage).** Cloud backups now store each file once by its SHA-256 and describe each version with a small manifest, so a new backup only uploads the save files that actually changed — an unchanged 40 MB slot is never re-sent. Dramatically reduces cloud usage for games with large or many saves. Existing full-zip cloud versions remain fully readable and restorable; retention now also garbage-collects unreferenced blobs (grace-guarded so a concurrent upload's data is never collected).

### Security
- CAS reconstruction validates every manifest path (rejecting absolute, drive-rooted, and `..` paths) so a tampered manifest from a shared bucket cannot write outside the restore directory.

## 0.2.0 — 2026-07-18

The trust release: everything the README promises now actually happens.

### Added
- **Real per-version snapshots** — every backup is frozen into an immutable zip with a SHA-256 checksum; `gsg restore --version` restores the version you picked.
- **`gsg pull`** — cross-machine restore: any cloud version on any machine, `--all` to catch a machine up, with automatic path remapping when the save was made under a different Windows username.
- **First-run wizard** — bare `gsg` walks through cloud setup (Google Drive / OneDrive via browser sign-in, or Railway S3) and start-at-boot.
- **Cloud retention** — `max_versions` now prunes remote objects too (fail-safe: never the newest, nothing on listing errors), with a storage meter and quota warning in `gsg status`.
- **Standalone `gsg.exe`** — single-file build via `packaging/build_exe.ps1`; no Python needed.
- `gsg pause` / `gsg resume`, `gsg --version`, `gsg setup-drive`, `gsg setup-onedrive`.

### Fixed
- Auto-restore only ever runs while the game is **not** running (startup sweep + idle checks) — never underneath a live process.
- A failed download or restore changes nothing and retries cleanly; safety backups can no longer lock out cloud restores.
- Cloud downloads verify layout via listing instead of trusting rclone exit codes (S3 returns success for nonexistent prefixes).
- `--dry-run` is actually dry; `--no-auto-sync` is honored; `gsg init` no longer wipes tracked games; `gsg scan` shows real sizes; `gsg config` is named correctly.
- Watcher: multi-process games (launcher + game) no longer trigger spurious close backups; callbacks can't crash the background daemon; single-instance lock.

### Security
- All archive extraction is path-traversal-safe and CRC-verified; Railway S3 credentials are verified before setup is declared successful; binary downloads fail loudly on HTTP errors.

## 0.1.0 — 2026-07-13

Initial release: Ludusavi + rclone wrapper with game scanning, launcher filtering, process watcher, Railway S3 upload, and Windows autostart.
