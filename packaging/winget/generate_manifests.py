"""Generate winget manifests for a published Game Save Genie release.

Usage (after a release with GameSaveGenie-Setup.exe exists):

    python packaging/winget/generate_manifests.py 0.5.0

Downloads the installer for that version, computes its SHA-256, and writes
the three manifest files under ``packaging/winget/manifests/<version>/``.
Submit them to https://github.com/microsoft/winget-pkgs under
``manifests/v/Vasanthdev2004/GameSaveGenie/<version>/`` (fork + PR, or
``wingetcreate submit``).
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

REPO = "Vasanthdev2004/Game-Save-Genie"
IDENTIFIER = "Vasanthdev2004.GameSaveGenie"

VERSION_MANIFEST = """\
PackageIdentifier: {identifier}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: 1.6.0
"""

INSTALLER_MANIFEST = """\
PackageIdentifier: {identifier}
PackageVersion: {version}
InstallerType: inno
Scope: user
Installers:
  - Architecture: x64
    InstallerUrl: https://github.com/{repo}/releases/download/v{version}/GameSaveGenie-Setup.exe
    InstallerSha256: {sha256}
ManifestType: installer
ManifestVersion: 1.6.0
"""

LOCALE_MANIFEST = """\
PackageIdentifier: {identifier}
PackageVersion: {version}
PackageLocale: en-US
Publisher: Vasanthdev2004
PublisherUrl: https://github.com/Vasanthdev2004
PublisherSupportUrl: https://github.com/{repo}/issues
PackageName: Game Save Genie
PackageUrl: https://github.com/{repo}
License: MIT
LicenseUrl: https://github.com/{repo}/blob/main/LICENSE
ShortDescription: Steam Cloud for the games that don't have it — automatic, versioned, self-hosted cloud save sync.
Description: >-
  Game Save Genie automatically backs up PC game saves (19,000+ games via the
  Ludusavi database, plus any custom folder for emulators) to cloud storage
  you own — Google Drive, OneDrive, any S3 bucket, or a self-hosted server.
  Every play session becomes a restorable version, saves follow you between
  machines, and delta uploads only transfer the files that changed.
Tags:
  - backup
  - cloud-sync
  - game-saves
  - gaming
  - save-manager
ManifestType: defaultLocale
ManifestVersion: 1.6.0
"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    version = sys.argv[1].lstrip("v")
    url = f"https://github.com/{REPO}/releases/download/v{version}/GameSaveGenie-Setup.exe"

    print(f"Downloading {url} ...")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed https URL
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest().upper()
    print(f"SHA256: {sha256}")

    out = Path(__file__).parent / "manifests" / version
    out.mkdir(parents=True, exist_ok=True)
    fields = {"identifier": IDENTIFIER, "version": version, "repo": REPO, "sha256": sha256}
    (out / f"{IDENTIFIER}.yaml").write_text(VERSION_MANIFEST.format(**fields), encoding="utf-8")
    (out / f"{IDENTIFIER}.installer.yaml").write_text(
        INSTALLER_MANIFEST.format(**fields), encoding="utf-8"
    )
    (out / f"{IDENTIFIER}.locale.en-US.yaml").write_text(
        LOCALE_MANIFEST.format(**fields), encoding="utf-8"
    )
    print(f"Wrote manifests to {out}")
    print("Submit to microsoft/winget-pkgs under "
          f"manifests/v/Vasanthdev2004/GameSaveGenie/{version}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
