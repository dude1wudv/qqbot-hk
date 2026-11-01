#!/usr/bin/env python3
"""Verify installed security pins and require successful, zero-finding npm audits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

ROOT = Path("/opt/hermes")
LOCK_SHA256 = '5aef655b889628002cc870a1e339b50afe688e1d67f71d5f0becfa5d51d6e0d8'
PINS = {'undici': {'7.30.1', '6.29.1'},
 'minipass': {'3.3.6', '7.1.3'},
 'brace-expansion': {'5.0.12'},
 'vitest': {'4.1.12'},
 '@vitest/mocker': {'4.1.12'},
 '@xmldom/xmldom': {'0.9.12'},
 'source-map-js': {'1.2.2'},
 'sanitize-html': {'2.18.1'},
 'vite': {'8.3.5'},
 'zod': {'4.6.6'},
 'fast-uri': {'3.1.9'},
 'follow-redirects': {'1.16.2'},
 'joi': {'18.3.1'},
 'glob': {'14.0.3'},
 'minimatch': {'11.0.3'},
 '@isaacs/nonrecursive-glob-match': {'1.0.3'},
 'path-scurry': {'3.0.0'}}

DESKTOP_ONLY_PATHS = {
    "node_modules/fast-uri",
    "node_modules/follow-redirects",
    "node_modules/joi",
    "node_modules/node-gyp/node_modules/undici",
}


def deployed_dependency_paths(lock, root):
    packages = lock["packages"]
    pending = [
        name for name in packages
        if "node_modules" not in PurePosixPath(name).parts
        and (root / name / "package.json").is_file()
    ]
    found = set()
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        data = packages[name]
        if data.get("link"):
            target = data["resolved"]
            if target in packages:
                pending.append(target)
            continue
        sections = ["dependencies", "optionalDependencies", "peerDependencies"]
        if "node_modules" not in PurePosixPath(name).parts:
            sections.append("devDependencies")
        for section in sections:
            for dependency in data.get(section, {}):
                prefix = PurePosixPath(name)
                while True:
                    candidate = str(prefix / "node_modules" / dependency)
                    if candidate in packages:
                        pending.append(candidate)
                        break
                    if prefix == PurePosixPath("."):
                        break
                    prefix = prefix.parent
    return found


def verify_installed_pins(lock, root):
    deployed = deployed_dependency_paths(lock, root)
    found = set()
    absent_desktop = []
    for name, data in lock["packages"].items():
        package = next((key for key in PINS if name.endswith("node_modules/" + key)), None)
        if package is None:
            continue
        if data["version"] not in PINS[package]:
            raise SystemExit(f"Unsafe locked package: {package}")
        manifest_path = root / name / "package.json"
        if not manifest_path.is_file():
            if (name in DESKTOP_ONLY_PATHS and name not in deployed
                    and "apps/desktop" in lock["packages"]
                    and not (root / "apps/desktop/package.json").exists()):
                absent_desktop.append(name)
                found.add(package)
                continue
            raise SystemExit(f"Pinned package missing from installed tree: {package}")
        installed = json.loads(manifest_path.read_text())["version"]
        if installed != data["version"] or installed not in PINS[package]:
            raise SystemExit(f"Unsafe or inconsistent installed package: {package}")
        found.add(package)
    if found != set(PINS):
        raise SystemExit("Security pins are missing from the installed workspace")
    print("HERMES_DESKTOP_ONLY_PINS=locked-safe NOT_DEPLOYED=" + str(len(absent_desktop)))


def main() -> None:
    content = (ROOT / "package-lock.json").read_bytes()
    if hashlib.sha256(content).hexdigest() != LOCK_SHA256:
        raise SystemExit("Hermes security lockfile checksum mismatch")
    lock = json.loads(content)
    verify_installed_pins(lock, ROOT)
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
