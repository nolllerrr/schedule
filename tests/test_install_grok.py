from __future__ import annotations

import gzip
import hashlib
import io
import json

import pytest

from scripts import install_grok


@pytest.fixture
def pinned_binary(tmp_path, monkeypatch):
    binary = b"verified CLI binary"
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"version": "1.0.62", "platforms": {
        "linux-x86_64": {"sha256": hashlib.sha256(binary).hexdigest(), "size": len(binary)},
    }}), encoding="utf-8")
    monkeypatch.setattr(install_grok.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install_grok.platform, "machine", lambda: "x86_64")
    return binary, lock


def test_verified_download_can_fall_back_to_second_official_host(tmp_path, monkeypatch, pinned_binary):
    binary, lock = pinned_binary
    urls = []

    def download(url, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            raise OSError("Primary host unavailable")
        return io.BytesIO(gzip.compress(binary))

    monkeypatch.setattr(install_grok, "urlopen", download)
    target = tmp_path / "bin/grok"
    assert install_grok.install(target, lock) == "1.0.62"
    assert target.read_bytes() == binary
    assert len(urls) == 2


@pytest.mark.parametrize("payload", [b"corrupt CLI binary!", b"oversized" * 100, b"short"])
def test_bad_download_never_replaces_existing_binary(tmp_path, monkeypatch, pinned_binary, payload):
    _, lock = pinned_binary
    monkeypatch.setattr(install_grok, "urlopen", lambda *args, **kwargs: io.BytesIO(gzip.compress(payload)))
    target = tmp_path / "grok"
    target.write_bytes(b"old working CLI")
    with pytest.raises(ValueError):
        install_grok.install(target, lock)
    assert target.read_bytes() == b"old working CLI"
    assert not list(tmp_path.glob("grok-install-*"))
