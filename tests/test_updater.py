from __future__ import annotations

from actl.core import updater


def test_update_version_parsing_and_order():
    assert updater._version("v0.1.2") > updater._version("0.1.1")
    assert updater._version("0.1.1") == updater._version("v0.1.1")


def test_release_requires_verified_assets(monkeypatch):
    monkeypatch.setattr(updater, "_json_get", lambda _url: {
        "tag_name": "v0.1.2",
        "assets": [
            {"name": "JuActl-Setup.exe", "browser_download_url": "https://example/setup"},
            {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/hash"},
        ],
        "html_url": "https://github.com/ju0o/JuActl/releases/tag/v0.1.2",
    })
    result = updater.check_latest()
    assert result["version"] == "0.1.2"
    assert result["installer"].endswith("/setup")


def test_update_rejects_missing_checksum_asset(monkeypatch):
    monkeypatch.setattr(updater, "_json_get", lambda _url: {"tag_name": "v0.1.2", "assets": []})
    try:
        updater.check_latest()
    except ValueError as exc:
        assert "SHA256SUMS" in str(exc)
    else:
        raise AssertionError("unsigned release must be rejected")
