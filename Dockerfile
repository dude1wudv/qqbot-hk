FROM node@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS trusted-node
FROM nousresearch/hermes-agent@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7

LABEL org.opencontainers.image.title="qqbot-hk Hermes policy image" \
      io.qqbot-hk.hermes-base-digest="sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7" \
      io.qqbot-hk.audio-patch="v2" \
      io.qqbot-hk.chat-reasoning-patch="v7" \
      io.qqbot-hk.model-routing-patch="v1" \
      io.qqbot-hk.aux-budget-patch="v2" \
      io.qqbot-hk.qq-context-patch="v2" \
      io.qqbot-hk.qq-help-patch="v1" \
      io.qqbot-hk.qq-output-patch="v1" \
      io.qqbot-hk.qq-recovery-patch="v1" \
      io.qqbot-hk.doctor-patch="v3" \
      io.qqbot-hk.dependency-pins="2026-10-21"

# Preserve the CA/Node remediation already applied to the live QQ container.
COPY --from=trusted-node /usr/local/bin/node /usr/local/bin/node
COPY --chmod=0755 scripts/patch-hermes-trust-store.py /opt/hermes/patch-hermes-trust-store.py
RUN python /opt/hermes/patch-hermes-trust-store.py
LABEL security.emudhra.mitigated="2026-10-22"

COPY --chmod=0755 scripts/patch-hermes-dependencies.py /tmp/patch-hermes-dependencies.py
COPY --chmod=0644 overrides/hermes-package-lock.json /tmp/hermes-package-lock.json
RUN python /tmp/patch-hermes-dependencies.py --lockfile /tmp/hermes-package-lock.json \
    && npm ci --prefix /opt/hermes --ignore-scripts --no-audit --no-fund \
    && rm -f /tmp/patch-hermes-dependencies.py /tmp/hermes-package-lock.json

COPY --chmod=0755 scripts/patch-hermes-doctor.py /tmp/patch-hermes-doctor.py
RUN python /tmp/patch-hermes-doctor.py \
    && rm -f /tmp/patch-hermes-doctor.py


COPY --chmod=0755 scripts/patch-hermes-audio.py /tmp/patch-hermes-audio.py
RUN python /tmp/patch-hermes-audio.py \
    && rm -f /tmp/patch-hermes-audio.py

COPY --chmod=0755 scripts/patch-hermes-chat-reasoning.py /tmp/patch-hermes-chat-reasoning.py
RUN python /tmp/patch-hermes-chat-reasoning.py \
    && rm -f /tmp/patch-hermes-chat-reasoning.py

COPY --chmod=0755 scripts/patch-hermes-aux-budget.py /tmp/patch-hermes-aux-budget.py
RUN python /tmp/patch-hermes-aux-budget.py \
    && rm -f /tmp/patch-hermes-aux-budget.py


COPY --chmod=0755 scripts/patch-hermes-qq-help.py /tmp/patch-hermes-qq-help.py
RUN python /tmp/patch-hermes-qq-help.py \
    && rm -f /tmp/patch-hermes-qq-help.py

COPY --chmod=0755 scripts/patch-hermes-qq-output.py /tmp/patch-hermes-qq-output.py
RUN python /tmp/patch-hermes-qq-output.py \
    && rm -f /tmp/patch-hermes-qq-output.py

COPY --chmod=0755 scripts/patch-hermes-qq-recovery.py /tmp/patch-hermes-qq-recovery.py
RUN python /tmp/patch-hermes-qq-recovery.py \
    && rm -f /tmp/patch-hermes-qq-recovery.py

COPY --chmod=0644 runtime/qqbot_context.py /opt/hermes/qqbot_context.py
COPY --chmod=0644 runtime/qqbot_request_deadline.py /opt/hermes/qqbot_request_deadline.py
COPY --chmod=0644 plugins/smart_group_qq/llm_policy.py /opt/hermes/qqbot_llm_policy.py
COPY --chmod=0644 plugins/smart_group_qq/media.py /opt/hermes/qqbot_hk_media.py
COPY --chmod=0755 scripts/patch-hermes-qq-context.py /tmp/patch-hermes-qq-context.py
RUN python /tmp/patch-hermes-qq-context.py \
    && rm -f /tmp/patch-hermes-qq-context.py

COPY --chmod=0644 runtime/qqbot_model_routing.py /opt/hermes/qqbot_model_routing.py
COPY --chmod=0755 scripts/patch-hermes-model-routing.py /tmp/patch-hermes-model-routing.py
RUN python /tmp/patch-hermes-model-routing.py \
    && rm -f /tmp/patch-hermes-model-routing.py


COPY --chmod=0755 scripts/verify-hermes-audio.py /opt/hermes/verify-hermes-audio.py
COPY --chmod=0755 scripts/verify-hermes-chat-reasoning.py /opt/hermes/verify-hermes-chat-reasoning.py
COPY --chmod=0755 scripts/verify-hermes-model-routing.py /opt/hermes/verify-hermes-model-routing.py
COPY --chmod=0755 scripts/verify-live-models.py /opt/hermes/verify-live-models.py
COPY --chmod=0755 scripts/verify-hermes-aux-budget.py /opt/hermes/verify-hermes-aux-budget.py
COPY --chmod=0755 scripts/verify-hermes-qq-context.py /opt/hermes/verify-hermes-qq-context.py
COPY --chmod=0755 scripts/verify-hermes-request-deadline.py /opt/hermes/verify-hermes-request-deadline.py
COPY --chmod=0755 scripts/verify-hermes-qq-approvals.py /opt/hermes/verify-hermes-qq-approvals.py
COPY --chmod=0755 scripts/verify-hermes-qq-commands.py /opt/hermes/verify-hermes-qq-commands.py
COPY --chmod=0755 scripts/verify-hermes-qq-recovery.py /opt/hermes/verify-hermes-qq-recovery.py
COPY --chmod=0755 scripts/verify-hermes-qq-output.py /opt/hermes/verify-hermes-qq-output.py
COPY --chmod=0755 scripts/verify-hermes-qq-lifecycle.py /opt/hermes/verify-hermes-qq-lifecycle.py
COPY --chmod=0755 scripts/verify-hermes-dependencies.py /opt/hermes/verify-hermes-dependencies.py
COPY --chmod=0644 plugins/smart_group_qq /opt/hermes/qqbot-hk/plugins/smart_group_qq
COPY --chmod=0644 config/hermes-config.yaml /opt/hermes/qqbot-hk/hermes-config.yaml

RUN python /opt/hermes/verify-hermes-audio.py \
    && python /opt/hermes/patch-hermes-trust-store.py --check \
    && python /opt/hermes/verify-hermes-chat-reasoning.py \
    && python /opt/hermes/verify-hermes-model-routing.py \
    && python /opt/hermes/verify-hermes-aux-budget.py \
    && python /opt/hermes/verify-hermes-qq-context.py \
    && python /opt/hermes/verify-hermes-request-deadline.py \
    && python /opt/hermes/verify-hermes-qq-approvals.py \
    && python /opt/hermes/verify-hermes-qq-commands.py \
    && python /opt/hermes/verify-hermes-qq-output.py \
    && python /opt/hermes/verify-hermes-qq-recovery.py \
    && python /opt/hermes/verify-hermes-qq-lifecycle.py \
    && python /opt/hermes/verify-hermes-dependencies.py \
    && QQ_CLIENT_SECRET=offline-synthetic-member-secret python -m hermes_cli.main plugins doctor /opt/hermes/qqbot-hk/plugins/smart_group_qq --ci
