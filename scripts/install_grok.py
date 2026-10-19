"""Install the pinned Linux Grok binary without running a remote installer."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
from urllib.request import urlopen


ARTIFACT_HOSTS = (
    "https://storage.googleapis.com/grok-build-public-artifacts/cli",
    "https://x.ai/cli",
)
LOCK_PATH = Path(__file__).with_name("grok-cli.lock.json")


def install(destination: Path, lock_path: Path = LOCK_PATH) -> str:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    target = "linux-" + architecture
    if platform.system() != "Linux" or target not in lock["platforms"]:
        raise ValueError("Grok image supports Linux amd64 and arm64 only")
    expected = lock["platforms"][target]
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="grok-install-", dir=destination.parent) as directory:
        binary = Path(directory) / "grok"
        for index, host in enumerate(ARTIFACT_HOSTS):
            digest = hashlib.sha256()
            size = 0
            try:
                url = f"{host}/grok-{lock['version']}-{target}.gz"
                with urlopen(url, timeout=60) as response, gzip.GzipFile(fileobj=response) as stream, binary.open("wb") as output:
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        if size > expected["size"]:
                            raise ValueError("Grok download exceeds pinned size")
                        digest.update(chunk)
                        output.write(chunk)
                if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise ValueError("Grok binary checksum mismatch")
            except (OSError, EOFError, ValueError):
                if index == len(ARTIFACT_HOSTS) - 1:
                    raise
                continue
            binary.chmod(0o755)
            os.replace(binary, destination)
            return lock["version"]
    raise RuntimeError("No Grok download source configured")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("/usr/local/bin/grok"))
    args = parser.parse_args()
    print("Installed Grok " + install(args.destination))
