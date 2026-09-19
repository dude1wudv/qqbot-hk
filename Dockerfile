FROM nousresearch/hermes-agent@sha256:9469b3e78b9545b6d576eb8887a95352e9a0ea83730eaf31431cf862ca1010e1

LABEL org.opencontainers.image.title="qqbot-hk Hermes policy image" \
      io.qqbot-hk.hermes-base-digest="sha256:9469b3e78b9545b6d576eb8887a95352e9a0ea83730eaf31431cf862ca1010e1" \
      io.qqbot-hk.audio-patch="v2" \
      io.qqbot-hk.chat-reasoning-patch="v1" \
      io.qqbot-hk.compression-recovery-patch="v1" \
      io.qqbot-hk.qq-help-patch="v1" \
      io.qqbot-hk.qq-output-patch="v1"


COPY --chmod=0755 scripts/patch-hermes-audio.py /tmp/patch-hermes-audio.py
RUN python /tmp/patch-hermes-audio.py \
    && rm -f /tmp/patch-hermes-audio.py

COPY --chmod=0755 scripts/patch-hermes-chat-reasoning.py /tmp/patch-hermes-chat-reasoning.py
RUN python /tmp/patch-hermes-chat-reasoning.py \
    && rm -f /tmp/patch-hermes-chat-reasoning.py

COPY --chmod=0755 scripts/patch-hermes-compression-recovery.py /tmp/patch-hermes-compression-recovery.py
RUN python /tmp/patch-hermes-compression-recovery.py \
    && rm -f /tmp/patch-hermes-compression-recovery.py

COPY --chmod=0755 scripts/patch-hermes-qq-help.py /tmp/patch-hermes-qq-help.py
RUN python /tmp/patch-hermes-qq-help.py \
    && rm -f /tmp/patch-hermes-qq-help.py

COPY --chmod=0755 scripts/patch-hermes-qq-output.py /tmp/patch-hermes-qq-output.py
RUN python /tmp/patch-hermes-qq-output.py \
    && rm -f /tmp/patch-hermes-qq-output.py


COPY --chmod=0755 scripts/verify-hermes-audio.py /opt/hermes/verify-hermes-audio.py
COPY --chmod=0755 scripts/verify-hermes-chat-reasoning.py /opt/hermes/verify-hermes-chat-reasoning.py
COPY --chmod=0755 scripts/verify-hermes-compression-recovery.py /opt/hermes/verify-hermes-compression-recovery.py
COPY --chmod=0755 scripts/verify-hermes-qq-commands.py /opt/hermes/verify-hermes-qq-commands.py
COPY --chmod=0755 scripts/verify-hermes-qq-output.py /opt/hermes/verify-hermes-qq-output.py
COPY --chmod=0644 plugins/smart_group_qq /opt/hermes/qqbot-hk/plugins/smart_group_qq
COPY --chmod=0644 config/hermes-config.yaml /opt/hermes/qqbot-hk/hermes-config.yaml

RUN python /opt/hermes/verify-hermes-audio.py \
    && python /opt/hermes/verify-hermes-chat-reasoning.py \
    && python /opt/hermes/verify-hermes-compression-recovery.py \
    && python /opt/hermes/verify-hermes-qq-commands.py \
    && python /opt/hermes/verify-hermes-qq-output.py
