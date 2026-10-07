#!/usr/bin/env python3
"""Apply reproducible security pins to the immutable Hermes Node workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ORIGINAL_LOCK_SHA256 = "193a3a3703499eb7c4b4ce48d3ba1e9dae2be62a48315c1e7ea9545e8be4a067"
SAFE_LOCK_SHA256 = "dabb346fe733b1d967ce92d44187a0a3dc430ee3601f744315987da836f93484"
MANIFESTS = {
    "package.json": (
        "8b5b2ea9721e8d4bdedea457f5b5bab038e7f3e11ea4e1381bd43f2e87ce04cd",
        (("overrides", "@xmldom/xmldom", None, "0.9.12"),
         ("overrides", "source-map-js", None, "1.2.2"),
         ("overrides", "brace-expansion", "5.0.9", "5.0.12"),
         ("overrides", "undici@^6", "6.28.0", "6.28.1"),
         ("overrides", "undici@^7", "7.29.0", "7.29.1")),
    ),
    "web/package.json": (
        "884fec6f6a1a3c4293be5192ed492a1aaac7acfe7cd9e6f8938ca5453d31a0e9",
        (("devDependencies", "vitest", "4.1.10", "4.1.11"),),
    ),
    "ui-tui/package.json": (
        "4753204cb018d78f203e4d2c9c1a8ca6f710563052c86df3c41f718b840f5418",
        (("dependencies", "undici", "6.28.0", "6.28.1"),
         ("devDependencies", "vitest", "4.1.10", "4.1.11")),
    ),
    "tests-js/package.json": (
        "08657cd581bcaacec53c834c703cd6254579db0223c45d2d9a21b629060acec5",
        (("devDependencies", "vitest", "4.1.10", "4.1.11"),),
    ),
}


class PatchError(RuntimeError):
    pass


def checked_bytes(path: Path, expected: str) -> bytes:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise PatchError(f"pinned dependency input mismatch: {path.name}")
    return content


def patch_dependencies(root: Path, lockfile: Path) -> None:
    # Validate every input before changing any upstream file.
    safe_lock = checked_bytes(lockfile, SAFE_LOCK_SHA256)
    checked_bytes(root / "package-lock.json", ORIGINAL_LOCK_SHA256)
    replacements: list[tuple[Path, str]] = []
    for name, (expected, edits) in MANIFESTS.items():
        path = root / name
        manifest = json.loads(checked_bytes(path, expected))
        for section, key, old, new in edits:
            if manifest.get(section, {}).get(key) != old:
                raise PatchError(f"dependency pin seam mismatch: {name}:{key}")
            manifest[section][key] = new
        replacements.append((path, json.dumps(manifest, indent=2) + "\n"))
    for path, content in replacements:
        path.write_text(content, encoding="utf-8", newline="\n")
    (root / "package-lock.json").write_bytes(safe_lock)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/hermes"))
    parser.add_argument("--lockfile", type=Path, required=True)
    args = parser.parse_args()
    try:
        patch_dependencies(args.root, args.lockfile)
    except (OSError, UnicodeError, ValueError, PatchError) as exc:
        print(f"ERROR: Hermes dependency patch failed: {exc}", file=sys.stderr)
        return 1
    print("HERMES_DEPENDENCIES=xmldom:0.9.12,source-map-js:1.2.2,brace-expansion:5.0.12,undici:6.28.1/7.29.1,vitest:4.1.11")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
