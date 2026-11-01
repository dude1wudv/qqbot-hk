# Hermes 0.22.5 upgrade candidate (2026-11-01)

## Version and image provenance

- Stable upstream tag `v0.22.5` resolves to `bf0203a794a07aac3ff764972ab9087baa4e4f4e`. The Docker Hub multi-arch index was read through the required proxy and independently hashed as `sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d`; amd64 manifest is `sha256:fd822bae0a61bf023450737edbeaeea5fc6460f17cb4ee240a7e67b306ed63fb`, created `2026-10-23T02:28:32.579290321Z`.
- The official QQ adapter remains bundled at `plugins/platforms/qqbot/adapter.py`; no third-party plugin migration was performed.
- Docker Desktop Linux daemon is unavailable on this workstation, so no local image build, root-owned Chromium install, CA regeneration, UID10000 browser run, or container smoke was claimed.

## Rebased policy

The source tree was checked out at the immutable tag and all policy scripts were re-anchored to real 0.22.5 source hashes before writing. The chain is recorded in `overrides/hermes-v0.22.5-patch-chain.json`. It covers audio policy, Chat reasoning, auxiliary budget, QQ help/output/recovery, plugin credential scope, doctor diagnostics, model routing, context/deadline hooks, and the new `run_turn_prepare.py` / `run_turn_persistence.py` layout. New upstream native behavior was retained where it superseded a local hook; the summary path already uses the interruptible request lifecycle, so the old redundant hook was removed from the rebased chain.

CA/Node trust remediation, Playwright 1.64.0 with the fixed Node executable, group/private isolation, model routing, deadline, memory and reset behavior remain represented in the image verification scripts.

## Dependencies

The 0.22.5 package tree was regenerated through `npm@11.17.0` with the proxy. Exact security overrides include `undici` 6.29.1/7.30.1, `fast-uri` 3.1.9, `follow-redirects` 1.16.2, `joi` 18.3.1, `glob` 14.0.3, `minimatch` 11.0.3, `@isaacs/nonrecursive-glob-match` 1.0.3, `zod` 4.6.6, `vite` 8.3.5 and the previously reviewed pins. Package-lock-only `npm audit` reports 0 vulnerabilities (exit 0) for the regenerated workspace. `overrides/hermes-package-lock.json` contains that generated lock and the chain records its SHA-256.

## Verification

- `python -m unittest discover -s tests -p 'test_hermes_*patch.py' -q`: 41/41 passed after fixture and patch-chain updates.
- `python -m unittest discover -s tests -q`: **534/534 passed**, exit 0, 53.3 seconds. This includes the updated plugin-scope/dependency/model-routing fixtures against the 0.22.5 seams.
- Docker build and image verification remain pending because Docker Desktop is unavailable. Server-side isolated build must execute `docker build --progress=plain`, all Dockerfile `verify-*` scripts, `plugins doctor --ci`, four npm audits, and UID10000 Playwright before deployment.

This repository is a candidate source update only; no commit, push, SSH, or deployment was performed.
