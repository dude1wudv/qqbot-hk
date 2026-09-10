FROM nousresearch/hermes-agent@sha256:76d5d17a201bb623268c02d43e397925e8f0127eb29b2e00fc48632d74945b05

LABEL org.opencontainers.image.title="qqbot-hk Hermes policy image" \
      io.qqbot-hk.hermes-base-digest="sha256:76d5d17a201bb623268c02d43e397925e8f0127eb29b2e00fc48632d74945b05" \
      io.qqbot-hk.audio-patch="v1" \
      io.qqbot-hk.plugin-discovery-patch="v1"

COPY --chmod=0755 scripts/patch-hermes-plugin-discovery.py /tmp/patch-hermes-plugin-discovery.py
RUN python /tmp/patch-hermes-plugin-discovery.py \
    && rm -f /tmp/patch-hermes-plugin-discovery.py

COPY --chmod=0755 scripts/patch-hermes-audio.py /tmp/patch-hermes-audio.py
RUN python /tmp/patch-hermes-audio.py \
    && rm -f /tmp/patch-hermes-audio.py

COPY --chmod=0755 scripts/verify-hermes-audio.py /opt/hermes/verify-hermes-audio.py
COPY --chmod=0644 config/hermes-config.yaml /opt/hermes/qqbot-hk/hermes-config.yaml

RUN python /opt/hermes/verify-hermes-audio.py
