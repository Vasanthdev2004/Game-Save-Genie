# The cloud format

What Game Save Genie stores in your bucket, written down so something other
than `gsg` can read and write it.

This exists because `gsg` cannot run everywhere saves live. It is Python, and
a Miyoo Mini+ running OnionOS has 128 MB of RAM and a BusyBox userland
([#32](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/32)). But
rclone runs there, and everything below is reachable with rclone, `sha256sum`
and a POSIX shell.

Treat this as a contract. Changes to it will be versioned, not silent.

## What this does and does not get you today

An earlier version of this page said a device writing this layout "gets
restores on the desktop for free, because `gsg` cannot tell the difference".
That was wrong, and it is corrected here rather than quietly deleted, because
someone may have built against it.

`gsg` can tell the difference. Every restore is gated on
`_staged_backup_has_content`, which requires a Ludusavi `mapping.yaml` inside
the restored tree for a normal game, or the custom-game manifest for a custom
one. A backup written from this document alone carries neither, so the blobs
and the manifest land in the bucket correctly and `gsg pull` then refuses
them.

So what a device gets right now is **storage that gsg understands**: the
objects are valid, deduplicated against everything gsg has uploaded, listed by
`gsg cloud-list`, and safe from the garbage collector. What it does not yet
get is a one-command restore on the desktop.

Making a CAS manifest sufficient on its own is tracked in
[#43](https://github.com/Vasanthdev2004/Game-Save-Genie/issues/43). It is the
right end state — `cas.reconstruct` already verifies every blob against its
hash, which is a stronger check than the file-presence test that currently
gates restores — but it is not done, and this page should not have implied it
was.

## Layout

Everything for one game lives under a single prefix:

```
<remote>:<remote-root>/<game-id>/
├── manifests/
│   └── 20260814-093000-123456.json
└── blobs/
    ├── 3f/
    │   └── 3f786850e387550fdab836ed7e6dc881de23001b...
    └── 89/
        └── 89e01536ac207279409d4de1e5253e01f4a1769e...
```

A **blob** is one file's exact bytes, stored once under its own SHA-256. Two
saves that share a file share the blob. Nothing is ever rewritten in place,
because a blob's name is derived from its content.

A **manifest** is one backup: a small JSON file listing which blobs, under
which paths, make up that version.

`<hh>` is the first two lowercase hex characters of the digest, and the full
digest is repeated as the filename: `blobs/3f/3f7868...`.

## Manifest

```json
{
  "format": "gsg-cas-1",
  "version_id": "20260814-093000-123456",
  "game_id": "stardew-valley",
  "created_at": "2026-08-14T09:30:00.123456+00:00",
  "source_machine": "miyoo-mini",
  "files": [
    { "path": "Saves/Farm_123/Farm_123", "sha256": "3f786850e387...", "size": 48213 },
    { "path": "Saves/Farm_123/SaveGameInfo", "sha256": "89e01536ac20...", "size": 1044 }
  ]
}
```

| field | meaning |
| --- | --- |
| `format` | Always `gsg-cas-1`. A reader that does not recognise the value should refuse the manifest rather than guess. |
| `version_id` | See below. Must match the filename without `.json`. |
| `game_id` | The prefix this manifest lives under. |
| `created_at` | ISO 8601, UTC, timezone-aware. |
| `source_machine` | Free text, or `null`. Shown when restoring so you can tell which device wrote it. |
| `files[].path` | Relative to the backup root, forward slashes, never absolute and never containing `..`. Restores reject anything that would escape the destination. |
| `files[].sha256` | Lowercase hex. Restores verify this and refuse a mismatch. |
| `files[].size` | Bytes. |

## Version IDs order everything

A version ID is a UTC timestamp formatted `%Y%m%d-%H%M%S-%f`. That format is
chosen so lexicographic order equals chronological order, and the rest of the
system relies on it: `gsg` sorts version IDs as strings to decide which backup
is newest, and `gsg prune --keep N` deletes the ones that sort lowest.

**A device with a wrong clock is therefore a data-loss risk, not a cosmetic
problem.** If it writes version IDs stamped in 2005, those sort first, and the
next prune deletes real backups while keeping the mis-stamped one. Set the
clock before the first upload. Handhelds without an RTC need this seen to
explicitly — there is no way for `gsg` to detect it after the fact, because a
timestamp that is merely wrong is indistinguishable from one that is old.

## Writing a backup

Order matters. Blobs first, manifest last.

1. Hash every file you intend to store.
2. Upload each file to `blobs/<hh>/<digest>`. Skip any that already exist —
   the name is the hash, so a name match is a content match, and there is
   nothing to compare beyond that.
3. Only once every blob is present, upload the manifest to
   `manifests/<version-id>.json`.

The manifest going last is what makes an interrupted upload safe. A version
becomes visible at the moment its manifest appears, so a half-finished upload
leaves reusable blobs and no version pointing at missing data. Reversing the
order produces a backup that looks restorable and is not.

## Reading a backup

1. List `manifests/` to find versions. Strip `.json` for the version ID.
2. Fetch the manifest you want.
3. Fetch each `blobs/<hh>/<digest>` it references.
4. Verify every blob against its `sha256` before writing anything, and reject
   any `path` that is absolute or climbs out of the destination.

## Reserved names

- Objects whose name starts with `_` are not versions. Skip them.
- `manifests/` and `blobs/` are structure, not versions.
- `<game-id>/<version-id>.zip` is the pre-CAS layout: one zip per backup.
  `gsg` still restores these. New writers should not produce them.

## Unreferenced blobs

Deleting a manifest does not delete its blobs — other versions may share them.
`gsg gc` removes blobs no manifest references, and skips anything written in
the last hour so that an upload still in flight is never collected. A
third-party writer does not need to implement this; running `gsg gc` from a
desktop cleans up after every device.

## Notes for constrained devices

Content addressing suits flash storage. An unchanged file is never re-sent,
so a repeat backup transfers nothing, and the device only ever *reads* its
saves to hash them.

The one thing to avoid is a staging directory. `gsg` stages blobs into a temp
tree before uploading because on a desktop that is free; on an SD card it
doubles the writes for no benefit. Upload straight from the save path — the
format does not care where the bytes came from.
