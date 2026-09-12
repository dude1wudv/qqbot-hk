FROM nousresearch/hermes-agent@sha256:9469b3e78b9545b6d576eb8887a95352e9a0ea83730eaf31431cf862ca1010e1

LABEL org.opencontainers.image.title="qqbot-hk Hermes policy image" \
      io.qqbot-hk.hermes-base-digest="sha256:9469b3e78b9545b6d576eb8887a95352e9a0ea83730eaf31431cf862ca1010e1" \
      io.qqbot-hk.audio-patch="v1"


COPY --chmod=0755 scripts/patch-hermes-audio.py /tmp/patch-hermes-audio.py
RUN python /tmp/patch-hermes-audio.py \
    && rm -f /tmp/patch-hermes-audio.py

COPY --chmod=0755 scripts/verify-hermes-audio.py /opt/hermes/verify-hermes-audio.py
COPY --chmod=0644 config/hermes-config.yaml /opt/hermes/qqbot-hk/hermes-config.yaml

RUN python /opt/hermes/verify-hermes-audio.py
