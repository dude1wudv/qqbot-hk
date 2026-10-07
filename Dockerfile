FROM nousresearch/hermes-agent@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7

LABEL org.opencontainers.image.title="qqbot-hk Hermes policy image" \
      io.qqbot-hk.hermes-base-digest="sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7" \
      io.qqbot-hk.audio-patch="v2" \
      io.qqbot-hk.chat-reasoning-patch="v5" \
      io.qqbot-hk.qq-context-patch="v1" \
      io.qqbot-hk.qq-help-patch="v1" \
      io.qqbot-hk.qq-output-patch="v1" \
      io.qqbot-hk.doctor-patch="v3" \
      io.qqbot-hk.dependency-pins="2026-10-06"

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


COPY --chmod=0755 scripts/patch-hermes-qq-help.py /tmp/patch-hermes-qq-help.py
RUN python /tmp/patch-hermes-qq-help.py \
    && rm -f /tmp/patch-hermes-qq-help.py

COPY --chmod=0755 scripts/patch-hermes-qq-output.py /tmp/patch-hermes-qq-output.py
RUN python /tmp/patch-hermes-qq-output.py \
    && rm -f /tmp/patch-hermes-qq-output.py

COPY --chmod=0644 runtime/qqbot_context.py /opt/hermes/qqbot_context.py
COPY --chmod=0644 plugins/smart_group_qq/media.py /opt/hermes/qqbot_hk_media.py
COPY --chmod=0755 scripts/patch-hermes-qq-context.py /tmp/patch-hermes-qq-context.py
RUN python /tmp/patch-hermes-qq-context.py \
    && rm -f /tmp/patch-hermes-qq-context.py


COPY --chmod=0755 scripts/verify-hermes-audio.py /opt/hermes/verify-hermes-audio.py
COPY --chmod=0755 scripts/verify-hermes-chat-reasoning.py /opt/hermes/verify-hermes-chat-reasoning.py
COPY --chmod=0755 scripts/verify-hermes-qq-context.py /opt/hermes/verify-hermes-qq-context.py
COPY --chmod=0755 scripts/verify-hermes-qq-commands.py /opt/hermes/verify-hermes-qq-commands.py
COPY --chmod=0755 scripts/verify-hermes-qq-output.py /opt/hermes/verify-hermes-qq-output.py
COPY --chmod=0755 scripts/verify-hermes-qq-lifecycle.py /opt/hermes/verify-hermes-qq-lifecycle.py
COPY --chmod=0755 scripts/verify-hermes-dependencies.py /opt/hermes/verify-hermes-dependencies.py
COPY --chmod=0644 plugins/smart_group_qq /opt/hermes/qqbot-hk/plugins/smart_group_qq
COPY --chmod=0644 config/hermes-config.yaml /opt/hermes/qqbot-hk/hermes-config.yaml

RUN python /opt/hermes/verify-hermes-audio.py \
    && python /opt/hermes/verify-hermes-chat-reasoning.py \
    && python /opt/hermes/verify-hermes-qq-context.py \
    && python /opt/hermes/verify-hermes-qq-commands.py \
    && python /opt/hermes/verify-hermes-qq-output.py \
    && python /opt/hermes/verify-hermes-qq-lifecycle.py \
    && python /opt/hermes/verify-hermes-dependencies.py \
    && QQ_CLIENT_SECRET=offline-synthetic-member-secret python -m hermes_cli.main plugins doctor /opt/hermes/qqbot-hk/plugins/smart_group_qq --ci
