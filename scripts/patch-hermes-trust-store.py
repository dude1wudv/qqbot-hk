#!/usr/bin/env python3
"""Keep the existing four-certificate distrust fix in the QQ container image.

No network access. Default paths are fixed; --check is strictly read-only.
The DER fingerprints and PEM filtering contract come from the workspace's
ops/security/emudhra_20261020.py. Certificate names are never matching criteria.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys


BLOCKED_ROOTS = frozenset({
    "7DD78D5F4F13459A83DFF9ABBBA62EDBAF6F2D102BF257FD712F4D9F2746ED8D",
    "86A1ECBA089C4A8D3BBE2734C612BA341D813E043CF9E8A862CD5C57A36BBE6B",
    "D7A7A0FB5D7E2731D771E9484EBCDEF71D5F0C3E0A2948782BC83EE0EA699EF4",
    "F2EDAAFF3D9B701FB4D14C9F285F8C0BAD90FC3BCC1FA07D47F4718EDABA34A5",
})
HERMES_ROOT = Path("/opt/hermes")
SOURCE_ROOT = Path("/usr/share/ca-certificates")
SYSTEM_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----[ \t\r\n]*"
    rb"(?P<body>[A-Za-z0-9+/= \t\r\n]+?)"
    rb"[ \t\r\n]*-----END CERTIFICATE-----"
)
_PEM_MARKER = re.compile(rb"-----\s*(?:BEGIN|END)\b", re.IGNORECASE)
_PRIVATE_KEY = re.compile(rb"-----\s*BEGIN\s+[^\r\n]*PRIVATE\s+KEY\s*-----", re.IGNORECASE)


def der_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest().upper()


def _validate_der(der: bytes) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))


def filter_pem_bytes(data: bytes, *, allow_empty: bool = False) -> tuple[bytes, dict]:
    """Remove only matching PEM blocks; retain every other byte unchanged."""
    if not isinstance(data, bytes) or not data or b"\x00" in data:
        raise ValueError("invalid PEM input")
    data.decode("utf-8-sig")
    if _PRIVATE_KEY.search(data):
        raise ValueError("private-key PEM is forbidden")
    chunks, removed = [], []
    count = last = 0
    for match in _CERTIFICATE.finditer(data):
        gap = data[last:match.start()]
        if _PEM_MARKER.search(gap):
            raise ValueError("malformed or unsupported PEM block")
        der = base64.b64decode(re.sub(rb"[ \t\r\n]", b"", match.group("body")), validate=True)
        _validate_der(der)
        fingerprint = der_sha256(der)
        count += 1
        chunks.append(gap)
        if fingerprint in BLOCKED_ROOTS:
            removed.append(fingerprint)
        else:
            chunks.append(match.group(0))
        last = match.end()
    trailing = data[last:]
    if _PEM_MARKER.search(trailing) or not count:
        raise ValueError("malformed or missing PEM certificates")
    if count == len(removed) and not allow_empty:
        raise ValueError("refusing an empty trust bundle")
    chunks.append(trailing)
    return b"".join(chunks), {"certificates_before": count,
                               "certificates_after": count - len(removed), "removed": removed}


def locate_bundles(hermes_root: Path, system_bundle: Path) -> list[Path]:
    if not system_bundle.is_file():
        raise ValueError("system CA bundle not found")
    resolved_root = hermes_root.resolve(strict=True)
    bundles = [system_bundle.resolve(strict=True)]
    for package in ("certifi", "botocore"):
        candidates = sorted((hermes_root / ".venv").glob(f"lib*/python*/site-packages/{package}/cacert.pem"))
        if not candidates:
            raise ValueError(f"Hermes {package} CA bundle not found")
        for path in candidates:
            resolved = path.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(resolved_root):
                raise ValueError(f"invalid Hermes {package} CA bundle path")
            if resolved not in bundles:
                bundles.append(resolved)
    return bundles


def source_plan(source_root: Path) -> list[tuple[Path, bytes, dict]]:
    if not source_root.is_dir():
        raise ValueError("system CA source directory not found")
    resolved_root = source_root.resolve(strict=True)
    plan = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.suffix.lower() not in {".crt", ".pem", ".cer"} and b"-----BEGIN CERTIFICATE-----" not in data:
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise ValueError("CA source escapes source directory")
        filtered, summary = filter_pem_bytes(data, allow_empty=True)
        plan.append((resolved, filtered, summary))
    if not plan:
        raise ValueError("no system CA source certificates found")
    return plan


def patch_trust_store(*, hermes_root: Path = HERMES_ROOT, source_root: Path = SOURCE_ROOT,
                      system_bundle: Path = SYSTEM_BUNDLE, check_only: bool = False,
                      runner=None) -> dict:
    # Resolve and validate every required bundle before making any changes.
    bundles = locate_bundles(hermes_root, system_bundle)
    sources = source_plan(source_root)
    bundle_plan = [(path, *filter_pem_bytes(path.read_bytes())) for path in bundles]
    remaining = sum(len(summary["removed"]) for _, _, summary in sources + bundle_plan)
    if check_only:
        if remaining:
            raise ValueError("blocked DER fingerprints remain in CA sources or bundles")
        return {"ok": True, "mode": "check", "bundles": [str(path) for path in bundles],
                "source_files_checked": len(sources), "blocked_certificates_remaining": 0}

    source_removed = sum(len(summary["removed"]) for _, _, summary in sources)
    for path, filtered, summary in sources:
        if not summary["removed"]:
            continue
        if summary["certificates_after"] == 0:
            path.unlink()
        else:
            path.write_bytes(filtered)
    # Source removal makes Debian regeneration persistent. Captured output is
    # deliberately not printed: only the bounded JSON result leaves the script.
    (runner or subprocess.run)(["update-ca-certificates", "--fresh"], check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    bundle_removed = 0
    for path in locate_bundles(hermes_root, system_bundle):
        original = path.read_bytes()
        filtered, summary = filter_pem_bytes(original)
        bundle_removed += len(summary["removed"])
        if filtered != original:
            path.write_bytes(filtered)
    verification = patch_trust_store(hermes_root=hermes_root, source_root=source_root,
                                    system_bundle=system_bundle, check_only=True)
    return {**verification, "mode": "patch", "source_certificates_removed": source_removed,
            "bundle_certificates_removed": bundle_removed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check all fixed paths without writing or regenerating.")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux":
            raise ValueError("this entry point only targets the Linux Hermes image")
        report = patch_trust_store(check_only=args.check)
        node = json.loads(subprocess.check_output([
            "node", "-e", "const tls=require('node:tls'),{X509Certificate}=require('node:crypto');"
            "console.log(JSON.stringify({version:process.version,roots:tls.rootCertificates.map("
            "c=>new X509Certificate(c).fingerprint256.replaceAll(':',''))}));",
        ], text=True, timeout=30))
        if BLOCKED_ROOTS.intersection(node["roots"]):
            raise ValueError("blocked DER fingerprints remain in Node defaults")
        report["node_version"] = node["version"]
        report["node_blocked_certificates_remaining"] = 0
    except (OSError, ValueError, ssl.SSLError, subprocess.SubprocessError) as exc:
        # Exception class is sufficient for build failure; never dump PEM or
        # captured child output. Missing bundles have explicit safe messages.
        report = {"ok": False, "mode": "check" if args.check else "patch",
                  "error_type": type(exc).__name__}
        if isinstance(exc, ValueError) and str(exc).endswith("CA bundle not found"):
            report["error"] = str(exc)
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
