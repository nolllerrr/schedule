"""Regenerate lock exports, or verify committed exports with --check.

Run through the locked development environment so uv itself is pinned.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["uv", "lock", *( ["--check"] if args.check else [])], cwd=root, check=True)
    with tempfile.TemporaryDirectory(prefix="schedule-locks-") as temporary:
        for name, extras in (("requirements.lock", ["--no-dev"]), ("dev-requirements.lock", ["--extra", "dev"])):
            output = Path(temporary) / name
            subprocess.run(["uv", "export", "--locked", "--no-header", "--no-emit-project", *extras, "--output-file", str(output), "--quiet"], cwd=root, check=True)
            if args.check:
                if output.read_bytes() != (root / name).read_bytes():
                    raise SystemExit(f"Stale dependency export: {name}")
            else:
                (root / name).write_bytes(output.read_bytes())
        output = Path(temporary) / "build-requirements.lock"
        subprocess.run(["uv", "pip", "compile", "scripts/build-requirements.in", "--python-version", "3.12", "--generate-hashes", "--universal", "--no-header", "--output-file", str(output), "--quiet"], cwd=root, check=True)
        if args.check:
            if output.read_bytes() != (root / "build-requirements.lock").read_bytes():
                raise SystemExit("Stale dependency export: build-requirements.lock")
        else:
            (root / "build-requirements.lock").write_bytes(output.read_bytes())


if __name__ == "__main__":
    main()
