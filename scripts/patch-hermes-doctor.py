#!/usr/bin/env python3
"""Patch pinned Hermes provider diagnostics and isolated plugin-doctor deadline."""
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

PLUGIN_DEV_SHA256 = "337b410bc9ae757414e64278f81b545c5b55105c4c0fca956a99525629339ede"
PLUGIN_DEV_MARKER = '# QQBOT_HK_PLUGIN_DOCTOR_PATCH = "v1"'
PLUGIN_DEV_OLD = '        bundled = home / "bundled-plugins"\n'
PLUGIN_DEV_NEW = (
    f"        {PLUGIN_DEV_MARKER}\n"
    '        (home / "config.yaml").write_text("plugins:\\n  load_timeout_seconds: 60\\n", encoding="utf-8")\n'
    + PLUGIN_DEV_OLD
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


def patch_plugin_doctor(source: str) -> str:
    if PLUGIN_DEV_MARKER not in source:
        if (hashlib.sha256(source.encode("utf-8")).hexdigest() != PLUGIN_DEV_SHA256
                or source.count(PLUGIN_DEV_OLD) != 1):
            raise PatchError("pinned plugin doctor source or isolated-home seam mismatch")
        source = source.replace(PLUGIN_DEV_OLD, PLUGIN_DEV_NEW)
    if (source.count(PLUGIN_DEV_MARKER) != 1 or source.count(PLUGIN_DEV_NEW) != 1
            or hashlib.sha256(source.replace(PLUGIN_DEV_NEW, PLUGIN_DEV_OLD).encode("utf-8")).hexdigest()
            != PLUGIN_DEV_SHA256):
        raise PatchError("isolated plugin doctor deadline patch is incomplete")
    compile(source, "hermes_cli/plugin_dev.py", "exec")
    return source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/opt/hermes"))
    args = parser.parse_args()
    targets = (
        (args.root / "hermes_cli/doctor_config.py", patch_source),
        (args.root / "hermes_cli/plugin_dev.py", patch_plugin_doctor),
    )
    try:
        updates = []
        for path, patcher in targets:
            original = path.read_text(encoding="utf-8")
            updates.append((path, original, patcher(original)))
        for path, original, source in updates:
            if source != original:
                temporary = path.with_name(path.name + ".qqbot-hk.tmp")
                temporary.write_text(source, encoding="utf-8", newline="\n")
                os.chmod(temporary, path.stat().st_mode)
                os.replace(temporary, path)
    except (OSError, UnicodeError, SyntaxError, PatchError) as exc:
        print(f"ERROR: Hermes doctor patch failed: {exc}", file=sys.stderr)
        return 1
    print("HERMES_DOCTOR_PATCH=verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
