#!/usr/bin/env python3
"""Use the resolved provider namespace for pinned Hermes model diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

EXPECTED_SHA256 = "26bb1a43611d7487878a02cfcc9a33171f7524776e4815e4c170d19e5b1092c8"
MARKER = '# QQBOT_HK_DOCTOR_PATCH = "v2"'
OLD = '    policy_id = str(runtime_provider or catalog_provider or "").strip().lower()\n'
NEW = (
    f"    {MARKER}\n"
    '    policy_id = str(catalog_provider or runtime_provider or "").strip().lower()\n'
)
SEAMS = (
    (
        '    runtime_provider = catalog_provider = provider\n',
        '    runtime_provider = catalog_provider = provider\n    provider_def = None\n',
    ),
    (OLD, NEW),
    (
        '    accepts_vendor_slug = policy_id in _VENDOR_SLUG_PROVIDERS or policy_id == "custom" or policy_id.startswith("custom:")\n',
        '    accepts_vendor_slug = (policy_id in _VENDOR_SLUG_PROVIDERS or policy_id == "custom" or policy_id.startswith("custom:")\n'
        '                           or getattr(provider_def, "source", "") == "user-config")\n',
    ),
)


class PatchError(RuntimeError):
    pass


def patch_source(source: str, expected_sha256: str = EXPECTED_SHA256) -> str:
    if MARKER not in source:
        actual = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if actual != expected_sha256 or any(source.count(old) != 1 for old, _ in SEAMS):
            raise PatchError("pinned doctor config source or policy seam mismatch")
        for old, new in SEAMS:
            source = source.replace(old, new)
    if source.count(MARKER) != 1 or any(source.count(new) != 1 for _, new in SEAMS) or OLD in source:
        raise PatchError("doctor provider policy patch is incomplete")
    compile(source, "hermes_cli/doctor_config.py", "exec")
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/hermes"))
    args = parser.parse_args()
    path = args.root / "hermes_cli/doctor_config.py"
    try:
        original = path.read_text(encoding="utf-8")
        patched = patch_source(original)
        if patched != original:
            temporary = path.with_name(path.name + ".qqbot-hk.tmp")
            temporary.write_text(patched, encoding="utf-8", newline="\n")
            os.chmod(temporary, path.stat().st_mode)
            os.replace(temporary, path)
    except (OSError, UnicodeError, SyntaxError, PatchError) as exc:
        print(f"ERROR: Hermes doctor patch failed: {exc}", file=sys.stderr)
        return 1
    print("HERMES_DOCTOR_PATCH=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
