# Changelog

## 0.6.2 — 2026-08-05

### Fixed
- **Backups to Google Drive no longer pay an API call per directory.** Cloud saves are content-addressed, stored at `blobs/<hh>/<hash>`, which spreads one version across many directories; rclone walked source and destination in step and listed each one. Against a store holding 5000 blobs, a backup with nothing new to send went from 138 requests to 6. This is what made a first upload sit for minutes before moving any data. ([#26](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/26))
- Restores and uploads run 16 transfers instead of rclone's default 4. A save is many small files, so the time goes into round trips, not bandwidth.
- **A Ludusavi you installed yourself is now used instead of being ignored.** `gsg` checked PATH for rclone but not for Ludusavi, so a copy from your distro, Homebrew, or your own build was passed over in favour of a download. That matters most where the download cannot work: Ludusavi publishes one Linux build (x86_64) and one macOS build (Apple Silicon), so ARM Linux and Intel Macs have no official binary and previously had no way to supply their own.
- **`gsg` stops downloading a Ludusavi it cannot run.** Ludusavi's release assets carry no architecture in their names, so the wrong one downloaded happily and failed only when executed — then stayed cached, failing identically on every later run. Unsupported architectures now refuse before downloading and say to install Ludusavi yourself.

## 0.6.1 — 2026-08-04



Three ways a new install could fail before it ever backed anything up. All three were reported by people trying to use it, none were reachable from Windows, which is where it was written.



### Fixed

- **`gsg` could not install itself on Linux.** The rclone downloader asked for an asset named `linux-amd64.tar.gz`. rclone publishes `.zip` for every platform it supports and never has published a Linux tarball, so first run ended in `Could not find rclone asset` on every Linux machine. The architecture was hardcoded to `amd64` besides, so arm64 — Apple Silicon, Raspberry Pi, ARM handhelds — would have been handed an Intel binary even once the extension was right. Both are derived from the running platform now. ([#1](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/1))

- **Self-hosted S3 could not be connected at all.** `gsg setup-s3` wrote `force_path_style = false`, which addresses a bucket as a subdomain: `game-saves.myserver.lan`. Nothing self-hosted resolves that, and nothing behind a reverse proxy has a certificate for it, so MinIO, Garage, Ceph and every TLS-terminated endpoint were unreachable regardless of what the user typed. Path style is the default now, and `--no-path-style` is there for hosted providers that need subdomains. ([#24](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/24))

- **An endpoint without `http://` silently became HTTPS.** rclone assumes TLS for a scheme-less endpoint, so `myserver:9000` produced `server gave HTTP response to HTTPS client` — an error that tells you nothing about what to type instead. Setup now tries HTTPS first and falls back to HTTP, and says plainly when it lands on plaintext and what that exposes.

- **A wrong S3 endpoint took 2.5 minutes to say so.** The credential check ran with rclone's default ten attempts and backoff, which is transfer-grade patience applied to a config check. Nobody iterates on setup at that speed. It fails in about 20 seconds now, or 40 if it has to try both schemes.

- **`gsg` reported a missing dependency at runtime instead of install time.** `click` was imported but never declared, so a plain `pip install game-save-genie` produced `ModuleNotFoundError` on first run. CI could not have caught it: every job installed `.[dev]`, and `black` pulls in `click`. There is now a job that installs runtime dependencies only and imports every module. Thanks to [@EnduringGuerila](https://github.com/EnduringGuerila) for the fix. ([#20](https://github.com/Vasanthdev2004/Game-Save-Genie/pull/20), [#22](https://github.com/Vasanthdev2004/Game-Save-Genie/pull/22))

- `gsg setup-s3` no longer announces "Railway S3 configured and verified" when connecting a homelab server.



## 0.6.0 — 2026-07-27

The visible release: the app finally has a face, and the background watcher can tell you when something is wrong.

### Added
- **`gsg set <game-id> --exe <name.exe>` / `--clear-exe`** — change how a game is detected. The watcher learns a game's executable by watching it run and can learn the wrong one; until now the only repairs were hand-editing `games.yaml` or removing and re-adding the game, which is one flag away from deleting its saves.
- **Running `gsg` opens the dashboard.** Once cloud storage is configured, a bare `gsg` launches the interactive dashboard instead of printing command-line help. This matters most where it isn't obvious: the Windows Start Menu shortcut runs a bare `gsg`, so clicking "Game Save Genie" used to open a black console showing help text. First run still gets the setup wizard, `gsg --help` is still help, and a non-interactive `gsg` (piped or redirected) still prints help so scripts are unaffected.
- **`gsg ui` — an interactive dashboard.** Restoring a save was the one genuinely browse-and-select task in the product, and the CLI made you do it by eye: read a version id out of `gsg versions`, retype it into `gsg pull --version`. Copying a timestamp between two commands is a bad thing to ask of someone who has just lost progress. Now you arrow onto the version you want and press `r`. Games on the left with their version count, last backup and real cloud target; versions on the right, local or cloud (`c` toggles); a log pane for results. `b` backs up, `r` restores, `F5` refreshes.
  - Restores always confirm first, and run the **same** code path as `gsg restore` / `gsg pull` — verification, the pre-restore safety backup, and the refusal to restore under a running game all live in shared helpers, so they cannot drift between the two front ends.
  - Every rclone and Ludusavi call runs on a worker thread, so the interface never freezes during an upload.
- **The app has a face.** A proper lamp icon now ships as the executable icon, the setup wizard icon, the Add/Remove Programs entry, and the Start Menu shortcut — all of which previously fell back to a generic default. Generated from a reviewable script (`packaging/make_icon.py`), not a committed mystery binary.
- **`gsg restore --force`**, matching `gsg pull --force`, for the rare case where the running-game check is wrong.
- **System tray icon for `gsg auto`.** The watcher runs hidden by design, which meant it had no way to tell you anything. It now sits in the notification area, colour-coded — blue when everything is backed up, amber when a tracked game has never been backed up, red when a backup or upload failed — with the last event in the tooltip. Right-click for **Back up now**, **Show status**, **Open log folder**, and **Quit**. Disable with `gsg auto --no-tray`. Entirely optional: if the tray can't be created (headless Linux, no notification area, missing dependency) the watcher runs exactly as before.

### Fixed
- **Game detection no longer matches the wrong game, or gives up on short titles.** Titles were reduced to words of four letters or more, so `GTA V`, `F1 24` and `NHL 25` reduced to *nothing* and could never be detected — while still being counted in "Watching N game(s)". Meanwhile `EA Sports FC 26` reduced to the single word `sports`, which matched `EA SPORTS WRC`. Matching now drops publisher and edition words, keeps version numbers, and weighs evidence per path segment against whole tokens rather than substrings anywhere in the path — so FC 26 matches its own folder and neither WRC nor FC 25.
- **The watcher stops learning launchers as game executables.** It learned on the *start* transition, guarded by "exactly one process matches" — precisely the tick where only the earliest-spawned process of the tree exists, which selects *for* launchers and crash handlers. Cyberpunk 2077 ended up identified by `REDEngineErrorReporter.exe`. Learning now happens on close, from every process seen during the session, skipping launchers/updaters/anti-cheat, and additively.
- **A learned executable is no longer a permanent trap.** It used to disable title matching entirely, so one bad guess made a game undetectable forever with nothing to indicate why. Learned names are now a fast path that title matching still runs alongside; an explicit `--exe` still narrows deliberately.
- `gsg auto` reports games it cannot detect at all, and games identified only by a launcher or crash handler, instead of counting them as watched.
- **Restores refuse to run while the game is running — in every front end.** `gsg pull` already checked. `gsg restore` did not, and the dashboard inherited that gap: alt-tabbing out of a running game and restoring would overwrite save files under the live process, which then writes its in-memory state on the next autosave and half-undoes the restore. The check now lives in the shared restore helpers that all three paths go through.
- The dashboard survives an unreachable cloud, a missing rclone or Ludusavi binary, a corrupt `games.yaml`, and a locked database — each is now a red line in the Activity pane instead of a full-screen traceback that ended the app.
- The dashboard can no longer start two concurrent restores against the same staging directory, keeps your selected game across a refresh, and no longer lets log output paint over the running interface.
- A declined or failed setup wizard is no longer buried under the dashboard, which made an unconfigured install look like a working one.
- **Backup failures are no longer invisible.** Windows autostart launches the watcher with a hidden console, so every failure message went to a screen nobody could see — and notifications only fired on *success*. A failing backup now produces a desktop notification, an `ERROR` log line, and a red tray icon. Weeks of failures used to look identical to everything working.
- **A failed cloud upload is reported as one.** `_cloud_upload` returned nothing, so the watcher announced "Save backed up" whether or not the upload succeeded. It now reports upload failures distinctly from local-backup failures.
- `gsg auto` names games that have never been backed up at startup, instead of leaving them silently unprotected.
- The watcher can be stopped cleanly (`GameWatcher.stop()`), so tray Quit and Ctrl+C both shut down immediately rather than waiting out the poll interval.
- **`gsg list` / `gsg status` now show where saves actually go.** The Cloud column printed a per-game provider label that was stored when the game was added and never updated — so after switching cloud providers it reported the old one (e.g. `s3`) for saves that were sitting safely in the new provider all along. It is now a **Cloud Target** column showing the real destination (`gdrive:game-save-genie`), derived on every render so it cannot go stale.
- **Cross-machine sync is no longer silently one-way.** A game with no explicit per-game provider was uploaded by `gsg auto` but ignored by the cloud-restore check, so its saves went up and could never come back down. Every command now resolves a game's provider and remote the same way: per-game setting if present, otherwise the global config.
- **`gsg backup` uploads what `gsg auto` uploads.** The two disagreed about whether a game was cloud-enabled.
- **`gsg remove --purge` deletes the right thing, and says so honestly.** It resolved the remote from the global config while uploads used the per-game one, so it could purge a *different* remote than the game's saves were written to; it skipped cloud deletion entirely for games without a per-game provider (leaving orphaned data no command could reach); and it printed "Deleted cloud saves" unconditionally because the rclone exit code was discarded. It now targets the game's own remote, reports failures, warns when no remote is configured, and asks for confirmation before deleting.
- `gsg remove --purge` also clears the game's rows from the version database, so re-adding the same title no longer resurrects history pointing at deleted snapshots.
- `gsg status` flags games that have never been backed up instead of quietly showing "never", and counts only real backups in the Versions column (safety snapshots were inflating it while Last Backup still read "never").
- `gsg status` / `gsg versions` mark a version as `stale (<remote>)` when it was uploaded to a remote you no longer use, instead of claiming it is synced.

## 0.5.0 — 2026-07-20

The launch release: easy install, Linux support, and honest docs.

### Added
- **Windows installer** — `GameSaveGenie-Setup.exe` attached to every release: per-user install (no admin), adds `gsg` to your PATH, Start Menu entry, and offers to run first-time setup. Cleanly removes itself — including the PATH entry — on uninstall.
- **Linux support (beta)** — the full pipeline runs on Linux: `gsg auto --install` sets up a systemd user service, notifications via `notify-send`, and Steam detection across native/Steam Deck/Flatpak paths (so Steam games aren't double-backed-up). Backup/restore/`pull` already worked cross-platform.
- **winget tooling** — a generator for the winget manifests so `winget install` can follow.

### Fixed
- systemd autostart hardening: benign service outcomes (another watcher already running, no games yet) exit cleanly so the service never crash-loops; `ExecStart` is quoted for install paths with spaces; retries are bounded.

### Docs
- Corrected the comparison table (Game Backup Monitor *does* have auto-restore; its real difference is native cloud) and added an honest beta-status note. Nothing overstated.

## 0.4.0 — 2026-07-20

### Added
- **Custom-path games (`gsg add --path`)** — back up any folder or file directly, bypassing Ludusavi's save database. This covers emulator saves (RetroArch, PCSX2, Dolphin, memcards, save states) and any game Ludusavi doesn't know. Pass `--path` one or more times; changes are detected by content hash so unchanged saves aren't re-backed-up, and everything flows through the same snapshot / delta-cloud-upload / versioning / safety-backup pipeline as Ludusavi games. Restores write back to each machine's own configured paths, so cross-machine sync works and a tampered backup can't redirect a restore.

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
