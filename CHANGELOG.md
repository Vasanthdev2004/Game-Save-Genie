# Changelog

## 0.8.0 — 2026-08-31

### Added

- **`gsg scan` now shows which stores sync each game themselves.** A new Native
  Cloud column reports whether Steam, GOG, Epic, Origin or Uplay provide their
  own save sync for a game, read from Ludusavi's manifest:

  ```
  | Title            | Source | Native Cloud     | Files | Size      |
  | Apex Legends     | steam  | origin, steam    | 3     | 12.29 KiB |
  | Cyberpunk 2077   | other  | epic, gog, steam | 66    | 79.09 MiB |
  | Human: Fall Flat | steam  | -                | 17    | 4.24 MiB  |
  ```

  It is shown whether or not you ask to filter on it, because knowing Steam
  already covers a game is useful even when you want gsg to cover it too.
  ([#51](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/51), requested
  by [@Tudzer](https://github.com/Tudzer))

- **`gsg scan --skip-cloud-synced steam,gog`** hides the games those stores
  already cover, and reports how many it hid. An unrecognised store name is
  rejected before the scan runs rather than silently filtering nothing.

  This is opt-in and will stay opt-in. The manifest records what the **game**
  supports, not what **your copy** is covered by — a repack, a GOG offline
  installer or a Hydra install of a Steam Cloud game is synced by nothing, and
  those are exactly the saves gsg exists to protect. Filtering on capability by
  default would drop precisely them, without saying so.

### Notes

Implemented by reading Ludusavi's manifest rather than by driving Ludusavi's
own cloud filter, which would have meant writing to a config file you own and
leaving it modified if gsg died mid-scan. gsg already reads that manifest for
the save-tag check added in 0.7.0, so this is the same pass over the same file.

## 0.7.0 — 2026-08-29

A correctness release. No new features: this is the result of auditing the
paths that can lose a save rather than the paths that fail to write one, after
four consecutive patch releases fixed bugs that a single external user found
and CI did not.

Most of these were silent. That is the common thread, and the reason for a
minor version rather than another patch — a backup tool that reports success
while doing nothing is worse than one that crashes.

### Fixed — data loss

- **Cloud retention could delete the version it had just uploaded.** Version
  ids are wall-clock timestamps and retention sorts them as strings, so an
  upload from a machine whose clock runs behind sorted oldest and was deleted
  by the prune that ran immediately after its own upload. The tray went green,
  `gsg versions` reported `Cloud = yes`, and the object was gone. Permanent for
  anyone with an RTC offset — a Steam Deck, a dual-boot PC, a VM. The local
  prune had always protected the version being written; the cloud prune now
  does too. ([#36](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/36))
- **A save location that had gone missing was recorded silently.** An unmounted
  drive or a moved folder became a metadata note, while the backup reported
  `Backed up 40 file(s)` in green and covered less than you asked for.
  Retention then pruned the versions that still held the missing folder — at
  ten versions and a backup every ten minutes of play, that does not take long.
  It is now named in the result, logged, and shown.
  ([#37](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/37))
- **Two backups running at once could produce a snapshot that verified clean
  and restored a half-written save tree.** Only `gsg watch` and `gsg auto` took
  the instance lock; `gsg backup`, `gsg restore`, `gsg pull` and the dashboard
  — which runs in its own process — took nothing. The archive's checksum is
  taken after it is written, so it certifies the archive, not the consistency
  of what went into it. Every backup path now goes through one guard. A
  snapshot that fails also no longer records a version at all: it used to store
  a row still pointing at the live directory, so several "distinct" restore
  points all restored the newest content.
  ([#38](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/38))
- **One machine with a wrong clock could stall cross-machine sync
  permanently.** A version stamped years ahead became "the newest save" on
  every other machine forever, so `gsg pull` reported "already up to date" with
  no way back short of editing the bucket by hand. Cloud versions stamped more
  than a day ahead are refused now, with the reason logged.
  ([#39](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/39))

### Fixed — Linux

- **The watcher was inverted.** System processes were excluded only by a
  Windows path check, so on Linux nothing was excluded at all:
  `/usr/libexec/xdg-desktop-portal` matched a game called *Portal*, and that
  match was then learned and written to `games.yaml` permanently — leaving the
  game reported as forever running, which silently disabled its cloud restore.
  In the other direction, the process name was collected and never read, so
  under Proton, where the executable path is the Wine loader, no game was
  detected at all. Native Linux games worked, which made the failure look
  random rather than systematic. Both halves are fixed.
  ([#40](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/40))
- **Cross-machine restore did not remap Linux or macOS home directories.** Only
  Windows `C:/Users/<name>` paths were rewritten, so a Steam Deck restoring
  onto a desktop reported success and wrote the save under the other machine's
  home. ([#42](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/42))

### Fixed — silent failures

- **A game whose saves had moved reported "No changes detected since last
  backup".** Indistinguishable from success, every session, indefinitely. An
  empty scan is a failure now, and says what probably happened.
  ([#41](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/41))
- **An error during the close-backup was logged and swallowed** while the tray
  still showed the green "Playing" state. It turns the tray red and fires a
  notification now.
  ([#41](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/41))

### Changed

- **Games with no save data of their own are no longer added automatically.**
  Ludusavi tags each path it knows as `save`, `config` and so on, and gsg
  ignored the tags — so a game like Roblox, whose only known files are graphics
  and control preferences because progress lives on the account, became a
  tracked game with ten retained versions of a 4 KiB settings file. Explicit
  `gsg add` is unaffected.
  ([#47](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/47))
- **Launcher ownership is re-checked at every scan, not only when a game is
  first seen.** Epic records a manifest per installed game, so a title that
  happened to be uninstalled during the first scan was invisible to the check
  and stayed tracked forever, duplicating Epic's own cloud sync. gsg reports it
  and leaves the decision to you.
  ([#48](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/48))

### Fixed — tests and CI

- **CI could not catch a regression in any function that deletes a cloud
  save.** The end-to-end cloud tests skip when rclone is missing and no job
  installed it, so `upload_save_cas`, `download_save_cas`,
  `prune_remote_versions` and `gc_blobs` had never run on a runner. rclone is
  installed on every leg now, and a skip in that module is a hard failure.
  ([#44](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/44))
- **Every fix shipped in 0.6.1 through 0.6.3 was pinned at the helper and not
  at the call site**, so reverting the production wiring left the suite green
  and a refactor could have silently undone any of them. All are pinned where
  they are used now, and each was mutation-tested by reverting it and
  confirming the suite goes red.
  ([#44](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/44))

### Documentation

- `CLOUD_FORMAT.md` claimed that a device writing the documented layout "gets
  restores on the desktop for free". It does not: every restore is gated on a
  Ludusavi `mapping.yaml` or the custom-game manifest, and a content-addressed
  upload carries neither. The claim is corrected in place rather than deleted,
  because it was published and may have been acted on. What a device does get
  — valid, deduplicated, GC-safe storage — is stated plainly now.
  ([#43](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/43))
- The README no longer describes cross-machine remapping as Windows-only,
  because it no longer is.

### Notes

The test suite went from 229 tests to 295.

Nothing here changes how a working install behaves. If your backups have been
running and restoring correctly, this release is insurance rather than repair
— but the failures it fixes were, by construction, ones you would not have
noticed.

## 0.6.3 — 2026-08-15

### Fixed
- **Autostart could fail at logon, or silently start nothing.** The Windows startup script was written as UTF-8, which Windows Script Host does not read. A UTF-8 byte order mark makes WSH refuse the file outright (`Invalid character`, 800A0408, line 1, char 1). Without a BOM it is read as the system ANSI codepage, which agrees with UTF-8 only while every character is ASCII — so a profile like `C:\Users\José` produced a startup script pointing at a path that does not exist, and because the script opens with `On Error Resume Next` it failed silently at every logon, with nothing logged. Now written as UTF-16LE with a BOM, which survives both cases.

### Added
- **[CLOUD_FORMAT.md](CLOUD_FORMAT.md) documents the cloud layout** so something other than `gsg` can read and write it: content-addressed blobs at `blobs/<hh>/<sha256>`, one JSON manifest per backup, every field, and the ordering rule that makes an interrupted upload safe. All of it reachable with rclone, `sha256sum` and a POSIX shell — for devices `gsg` itself cannot run on, such as a handheld with 128 MB of RAM. ([#32](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/32))

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
