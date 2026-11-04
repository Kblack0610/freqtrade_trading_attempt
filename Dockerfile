# The trader image: the pinned Freqtrade release plus this repo, so the bots, the jobs
# and the site all run one artifact. home-config pins it by digest.
ARG FREQTRADE_TAG=2026.9
FROM freqtradeorg/freqtrade:${FREQTRADE_TAG}

COPY lab/requirements.txt /tmp/lab-requirements.txt
RUN pip install --user --no-cache-dir -r /tmp/lab-requirements.txt

COPY --chown=ftuser:ftuser . /repo
WORKDIR /repo
# The entrypoint stays freqtrade's: every workload in home-config sets its own command.
