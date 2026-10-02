#!/usr/bin/env python3
"""Verify installed security pins and require successful, zero-finding npm audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path("/opt/hermes")
LOCK_SHA256 = "8b39377f88447e93f4526948ba646b0c20c75ceb3e692374d08fd49713c42691"
PINS = {"brace-expansion": {"5.0.12"}, "undici": {"6.28.1", "7.29.1"},
        "vitest": {"4.1.11"}, "@vitest/mocker": {"4.1.11"}, "@xmldom/xmldom": {"0.9.12"}}


def main() -> None:
    content = (ROOT / "package-lock.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != LOCK_SHA256:
        raise SystemExit("Hermes security lockfile checksum mismatch")
    lock = json.loads(content)
    found = set()
    for name, data in lock["packages"].items():
        package = next((key for key in PINS if name.endswith("node_modules/" + key)), None)
        if package is None:
            continue
        manifest_path = ROOT / name / "package.json"
        if not manifest_path.is_file():
            raise SystemExit(f"Pinned package missing from installed tree: {package}")
        installed = json.loads(manifest_path.read_text())["version"]
        if installed != data["version"] or installed not in PINS[package]:
            raise SystemExit(f"Unsafe or inconsistent installed package: {package}")
        found.add(package)
    if found != set(PINS):
        raise SystemExit("Security pins are missing from the installed workspace")
    for scope, args in [("root", ["--workspaces=false"]),
                        ("web", ["--workspace", "web"]),
                        ("ui-tui", ["--workspace", "ui-tui"]),
                        ("all-workspaces", [])]:
        result = subprocess.run(["npm", "audit", "--json", *args], cwd=ROOT,
                                capture_output=True, text=True, timeout=90)
        report = json.loads(result.stdout)
        counts = report.get("metadata", {}).get("vulnerabilities")
        if result.returncode or report.get("error") or not isinstance(counts, dict):
            raise SystemExit(f"Security audit failed or unavailable: {scope}")
        if counts.get("total") != 0:
            raise SystemExit(f"Security audit has unresolved findings: {scope}")
        print(f"HERMES_NPM_AUDIT={scope} EXIT=0 VULNERABILITIES=0")
    subprocess.run([str(ROOT / "node_modules/.bin/esbuild"), "--version"],
                   check=True, capture_output=True, text=True, timeout=15)
    print("HERMES_DEPENDENCY_PINS=installed LOCK=verified ESBUILD=executable")


if __name__ == "__main__":
    main()
