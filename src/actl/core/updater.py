"""Safe GitHub Release updater for the Windows GUI."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

REPOSITORY = "ju0o/JuActl"
CURRENT_VERSION = "0.1.1"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


def _version(value: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", value or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def _json_get(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "JuActl-Updater"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub release response is not an object")
    return payload


def check_latest() -> dict:
    release = _json_get(API_URL)
    tag = str(release.get("tag_name", ""))
    assets = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in release.get("assets", [])
        if isinstance(asset, dict) and asset.get("name") and asset.get("browser_download_url")
    }
    installer = assets.get("JuActl-Setup.exe")
    checksums = assets.get("SHA256SUMS.txt")
    if not tag or not installer or not checksums:
        raise ValueError("latest release is missing installer or SHA256SUMS.txt")
    return {"tag": tag, "version": tag.removeprefix("v"), "installer": installer,
            "checksums": checksums, "url": str(release.get("html_url", ""))}


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "JuActl-Updater"})
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)


def download_verified(release: dict) -> Path:
    directory = Path(tempfile.mkdtemp(prefix="juactl-update-"))
    installer = directory / "JuActl-Setup.exe"
    checksums = directory / "SHA256SUMS.txt"
    try:
        _download(release["checksums"], checksums)
        _download(release["installer"], installer)
        expected = None
        for line in checksums.read_text(encoding="ascii", errors="replace").splitlines():
            digest, _, name = line.strip().partition("  ")
            if name == "JuActl-Setup.exe":
                expected = digest.lower()
                break
        actual = hashlib.sha256(installer.read_bytes()).hexdigest().lower()
        if not expected or expected != actual:
            raise ValueError("installer SHA256 verification failed")
        return installer
    except Exception:
        for path in (installer, checksums):
            path.unlink(missing_ok=True)
        directory.rmdir()
        raise


def launch_installer(installer: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("in-app installer is supported on Windows only")
    subprocess.Popen([str(installer), "/SILENT", "/NORESTART"], close_fds=True)
