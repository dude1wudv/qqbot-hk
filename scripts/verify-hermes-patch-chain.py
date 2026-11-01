#!/usr/bin/env python3
"""Replay the image's source patches from local immutable upstream Git objects."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile


PROJECT = Path(__file__).resolve().parents[1]


def load_patch(name):
    spec = importlib.util.spec_from_file_location(
        "hermes_chain_" + name.replace("-", "_"),
        PROJECT / "scripts" / f"patch-hermes-{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def upstream_bytes(source_root, revision, relative):
    return subprocess.check_output(
        ["git", "show", f"{revision}:{relative}"], cwd=source_root,
        stderr=subprocess.PIPE,
    )


def apply_patch(module, name, relative, source, expected):
    if name == "doctor":
        return (module.patch_plugin_doctor(source) if relative.endswith("plugin_dev.py")
                else module.patch_source(source))
    if name in {"audio", "chat-reasoning", "qq-help"}:
        return module.patch_source(source, expected)
    if name == "qq-output":
        return module.patch_source(Path(relative), source, expected)
    if name in {"qq-context", "model-routing"}:
        digest, replacements = module.PATCHES[relative]
        if digest != expected:
            raise ValueError(f"Patch/chain input hash differs: {name}:{relative}")
        return module.patch_source(source, digest, replacements)
    return module.patch_source(source)


def verify(source_root):
    record = json.loads((PROJECT / "overrides/hermes-v0.22.5-patch-chain.json").read_text())
    chain = record["chain"]
    recorded_order = list(dict.fromkeys(entry["patch"] for entry in chain))
    image_order = re.findall(
        r"RUN python /tmp/patch-hermes-([a-z-]+)\.py",
        (PROJECT / "Dockerfile").read_text(),
    )
    image_order = [name for name in image_order if name != "dependencies"]
    if recorded_order != image_order:
        raise ValueError("Recorded source patch order differs from Dockerfile")
    modules, sources = {}, {}
    for entry in chain:
        name, relative = entry["patch"], entry["path"]
        if name not in modules:
            modules[name] = load_patch(name)
        if relative not in sources:
            sources[relative] = upstream_bytes(
                source_root, record["upstream_commit"], relative,
            ).decode("utf-8")
        source = sources[relative]
        if hashlib.sha256(source.encode()).hexdigest() != entry["input_sha256"]:
            raise ValueError(f"Chain input mismatch: {name}:{relative}")
        output = apply_patch(modules[name], name, relative, source, entry["input_sha256"])
        if hashlib.sha256(output.encode()).hexdigest() != entry["output_sha256"]:
            raise ValueError(f"Chain output mismatch: {name}:{relative}")
        if apply_patch(modules[name], name, relative, output, entry["input_sha256"]) != output:
            raise ValueError(f"Patch is not idempotent: {name}:{relative}")
        sources[relative] = output
        print(f"HERMES_PATCH_CHAIN={name}:{relative} SHA=verified IDEMPOTENT=yes")

    dependency_patch = load_patch("dependencies")
    with tempfile.TemporaryDirectory(prefix="hermes-dependency-replay-") as temporary:
        target = Path(temporary)
        for relative in (*dependency_patch.MANIFESTS, "package-lock.json"):
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(upstream_bytes(source_root, record["upstream_commit"], relative))
        dependency_patch.patch_dependencies(target, PROJECT / "overrides/hermes-package-lock.json")
        if hashlib.sha256((target / "package-lock.json").read_bytes()).hexdigest() != record["dependency_lock_sha256"]:
            raise ValueError("Recorded dependency lock differs from replay")
    print(f"HERMES_PATCH_CHAIN=passed TARGETS={len(chain)} FILES={len(sources)} "
          "ORDER=Dockerfile DEPENDENCY_MANIFESTS=4")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True,
                        help="Local upstream Git checkout containing the recorded immutable commit")
    args = parser.parse_args()
    verify(args.source_root)


if __name__ == "__main__":
    main()
