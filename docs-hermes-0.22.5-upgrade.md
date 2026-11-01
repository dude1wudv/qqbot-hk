# Hermes 0.22.5 upgrade candidate (2026-11-01)

## Version and image provenance

- Stable upstream tag `v0.22.5` resolves to `bf0203a794a07aac3ff764972ab9087baa4e4f4e`. The Docker Hub multi-arch index was read through the required proxy and independently hashed as `sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d`; amd64 manifest is `sha256:fd822bae0a61bf023450737edbeaeea5fc6460f17cb4ee240a7e67b306ed63fb`, created `2026-10-23T02:28:32.579290321Z`.
- The official QQ adapter remains bundled at `plugins/platforms/qqbot/adapter.py`; no third-party plugin migration was performed.
- Docker Desktop Linux daemon is unavailable on this workstation, so no local image build, root-owned Chromium install, CA regeneration, UID10000 browser run, or container smoke was claimed.

## Rebased policy

The source tree was checked out at the immutable tag and all policy scripts were re-anchored to real 0.22.5 source hashes before writing. The chain is recorded in `overrides/hermes-v0.22.5-patch-chain.json`. It covers audio policy, Chat reasoning, auxiliary budget, QQ help/output/recovery, plugin credential scope, doctor diagnostics, model routing, context/deadline hooks, and the new `run_turn_prepare.py` / `run_turn_persistence.py` layout. New upstream native behavior was retained where it superseded a local hook; the summary path already uses the interruptible request lifecycle, so the old redundant hook was removed from the rebased chain.

CA/Node trust remediation, Playwright 1.64.0 with the fixed Node executable, group/private isolation, model routing, deadline, memory and reset behavior remain represented in the image verification scripts.

## Dependencies

The initial npm 11 lock was generated with legacy peer resolution and failed the real base image's npm 12.0.2 frozen install. It has been replaced by a strict `npm@12.0.2 install --package-lock-only --ignore-scripts --no-audit --no-fund` lock, generated through the required proxy without legacy/force flags. The original upstream manifests plus declared security edits were used; Babel 7.29.7 at the root and Babel 8.0.1 in desktop/web now have separate correct placements. The Dockerfile checks the immutable base's npm version before its unchanged frozen `npm ci`.

Exact security overrides include `undici` 6.29.1/7.30.1, `fast-uri` 3.1.9, `follow-redirects` 1.16.2, `joi` 18.3.1, `glob` 14.0.3, `minimatch` 11.0.3, `@isaacs/nonrecursive-glob-match` 1.0.3, `zod` 4.6.6, `vite` 8.3.5 and the previously reviewed pins. All installed-version allowlists remain unchanged. Package-lock-only `npm audit` reports 0 vulnerabilities (exit 0) independently for root, web, ui-tui and all workspaces. `overrides/hermes-package-lock.json` contains the strict 1,557-entry lock and the chain records SHA-256 `5aef655b889628002cc870a1e339b50afe688e1d67f71d5f0becfa5d51d6e0d8`. The upstream 14-day age policy was retained during resolution with only reviewed security-package exceptions.

## Verification

- `python -m unittest discover -s tests -p 'test_hermes_*patch.py' -q`: 41/41 passed after fixture and patch-chain updates.
- `python -m unittest discover -s tests -q`: **534/534 passed**, exit 0, 53.3 seconds. This includes the updated plugin-scope/dependency/model-routing fixtures against the 0.22.5 seams.
- npm 12.0.2 frozen `ci --dry-run --ignore-scripts --no-audit --no-fund`: exit 0 against the strict regenerated lock. This validates lock consistency, not a Linux installed tree; server isolated build remains required.
- Strict-lock follow-up: dependency patch tests **3/3**, exit 0; replay from the four original upstream manifests and original lock produces exactly the manifests and strict lock used by the npm checks. Frozen dry-run also passes with the base's `npm_config_install_links=false`, exit 0.
- The server build passed frozen npm installation, then exposed a stale quoted `FailoverReason` annotation anchor in the model-routing patch. The anchor now matches the actual 0.22.5 unquoted signature; no input/output checksum or recorded chain was changed. `python scripts/verify-hermes-patch-chain.py --source-root <local-upstream-checkout>` now calls every real patcher against immutable upstream Git objects in Dockerfile order: **26/26 targets, 20 files**, original input/output SHA-256 and idempotence all pass, plus the four dependency manifests (exit 0). The routing regression uses an independent literal upstream signature rather than generating its fixture from the patch definition; **18/18 routing tests** pass, exit 0.
- The next server build completed source patching and exposed the upstream YAML migration: Python 3.14 uses the shipped `hermes_yaml` API backed by ruamel, not PyYAML. Inspection confirmed both `python` and `/opt/hermes/.venv/bin/python` use the same correct 3.14.9 environment and can import `hermes_yaml`/`ruamel.yaml`; interpreter selection was retained. Five image verifiers and server acceptance now use `hermes_yaml`, and the cron reconciler prefers that API while retaining PyYAML only for standalone tests without Hermes. No parser or duplicate environment was installed into the image. The no-PyYAML cron regression and existing cron tests pass **12/12**, exit 0; image validation must continue on the server.
- Follow-up full local suite: **535/535 tests**, exit 0, 54.7 seconds after the YAML compatibility fix. Image label checks retain `security.emudhra.mitigated=2026-10-22` as the original remediation date, followed by the actual CA/Node revalidation; Hermes version/revision are also checked against the pinned base.
- Docker build and image verification remain pending because Docker Desktop is unavailable. Server-side isolated build must execute `docker build --progress=plain`, all Dockerfile `verify-*` scripts, `plugins doctor --ci`, four npm audits, and UID10000 Playwright before deployment.

This repository is a candidate source update only; no commit, push, SSH, or deployment was performed.

## Final runtime compatibility fixes

The startup bridge now snapshots owner-tagged subscriptions from the active profile plugin manager and awaits callbacks on the gateway loop. Removed subscriptions are rechecked by identity before invocation. The former get_plugin_subscriptions API does not exist in 0.22.5.

The immutable server base omits apps/desktop, while its full npm lock retains that workspace. Four pinned lock paths (fast-uri, follow-redirects, joi and nested node-gyp/undici) are desktop-only and absent from the installed tree. Their exact safe locked versions remain mandatory; an exemption additionally requires the desktop manifest to be absent and the path to be unreachable from every deployed workspace dependency graph. Missing runtime dependencies still fail. All installed versions, exact lock digest and four zero-finding npm audits remain required. The real isolated installed-tree check passed with exactly four such absent paths. Three scope regressions and three maintenance tests passed.

The first production candidate passed all offline checks but was automatically rolled back when verify-server reported a disconnected gateway. Inspection proved that the candidate WebSocket had connected; the synthetic ACL probe itself created a GatewayRunner in the live home and published a new starting/empty-platform status record (its writer argv was the stdin probe). The probe now uses both an isolated process home and context-local home override, plus a temporary sessions directory, while testing the loaded production ACL. It passed with the production profile mounted read-only. The original gateway-connected assertion remains unchanged. The rollback restored the matched old image and profile, retaining failed-candidate data separately on the server.

Final local regression at runtime commit 70710d4: 544 tests passed, exit 0. Both GitHub jobs in run 41295837402 passed. The server image built successfully, ran all four zero-vulnerability npm audits, all source/runtime verifiers, plugin doctor and real UID10000 Chromium. Production verification skips only the explicitly paid verify-live-models call; this remains an unverified area rather than an implied pass.
