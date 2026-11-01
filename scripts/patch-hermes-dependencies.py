#!/usr/bin/env python3
"""Apply reproducible security pins to the immutable Hermes Node workspace."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ORIGINAL_LOCK_SHA256 = 'd439feaa570d5098f5479f2af154c001f48d5c2d95a938977cc235384978e37c'
SAFE_LOCK_SHA256 = 'c50acc447e56bc2aad63dd295199ac26c648ba102fd7ea517cb7ea7a1d2045a3'
MANIFESTS = {'package.json': ('1f2378a4d91f99fa1f407d017a27a09298d509664f8353261c3579d2ecf4d852',
                  (('overrides', 'undici@^6', '6.29.0', '6.29.1'),
                   ('overrides', 'undici@^7', '7.30.0', '7.30.1'),
                   ('overrides', '@xmldom/xmldom', None, '0.9.12'),
                   ('overrides', 'sanitize-html', None, '2.18.1'),
                   ('overrides', 'vite', None, '8.3.5'),
                   ('overrides', 'zod', None, '4.6.6'),
                   ('overrides', 'fast-uri', None, '3.1.9'),
                   ('overrides', 'follow-redirects', None, '1.16.2'),
                   ('overrides', 'joi', None, '18.3.1'),
                   ('overrides', 'glob', None, '14.0.3'),
                   ('overrides', 'minimatch', None, '11.0.3'),
                   ('overrides', '@isaacs/nonrecursive-glob-match', None, '1.0.3'),
                   ('overrides', 'ink', None, '7.1.1'))),
 'web/package.json': ('30b1a6e95b603e736b7e06056248e93fd0369a6ec4540449e0c52461efa788d0',
                      (('devDependencies', 'vite', '8.2.4', '8.3.5'),)),
 'ui-tui/package.json': ('03af8c8c2914f72ee91b1b5afe4f942c6d1614442d501d11d57e812750f30f34',
                         (('dependencies', 'undici', '6.29.0', '6.29.1'),)),
 'tests-js/package.json': ('ba875b0aad7c4d555793f062e75ccf7bc9c90be6304ffc0f1cd44fc24ebd9cba',
                           (('dependencies', 'zod', '4.4.3', '4.6.6'),))}


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
    print("HERMES_DEPENDENCIES=xmldom:0.9.12,source-map-js:1.2.2,brace-expansion:5.0.12,undici:6.29.1/7.29.1,vitest:4.1.12,sanitize-html:2.18.1,vite:8.3.5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
