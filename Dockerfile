# The trader image: the pinned Freqtrade release plus this repo, so the bots, the jobs
# and the site all run one artifact. home-config pins it by digest.
# The base is pinned by digest, so a re-pushed tag cannot change what a cached backtest ran on (lab/reproduce.py).
# FREQTRADE_IMAGE=freqtradeorg/freqtrade:2026.9_freqai@sha256:... builds the ML image (trader-ml) for lab/families/ml;
# the bots never run on it. CI passes that ref (.github/workflows/image.yml, ML_BASE).
ARG FREQTRADE_IMAGE=freqtradeorg/freqtrade:2026.9@sha256:989aff5636d8f0213ed44fe334c3314a194a51c0c755d16866960247aafd2eae
FROM ${FREQTRADE_IMAGE}

COPY lab/requirements.txt /tmp/lab-requirements.txt
RUN pip install --user --no-cache-dir -r /tmp/lab-requirements.txt

COPY --chown=ftuser:ftuser . /repo
WORKDIR /repo
# lab and web are packages imported from the repo root (`from lab import patterns`, `uvicorn web.app:app`).
# A job run as `python /repo/lab/<job>.py` also has lab/ first on sys.path, so no lab module shares a name
# with an installed one; lab/tests/test_families.py checks that.
ENV PYTHONPATH=/repo
# The userdir subfolders .dockerignore leaves out. freqtrade creates missing ones itself, but its
# check-then-create races when several freqtrade processes start at once (lab/backfill.py runs one
# per exchange) and the loser exits with "exists already and is not a directory".
RUN mkdir -p user_data/logs user_data/data user_data/backtest_results user_data/hyperopt_results user_data/plot
# The site's shell (tokens, fonts, <kb-shell>) is the pinned web-kit: fetch_kit.py downloads it and checks every
# file against web/kit.lock, so a bad checksum fails the build. Needs no pip package.
RUN python web/fetch_kit.py
# The entrypoint stays freqtrade's: every workload in home-config sets its own command.
# Healthy when the bot API (lab/league.py, :8080) or the site (web/app.py, :8000) answers. Docker
# and compose read this; k8s ignores it and uses the probes in home-config. A one-shot job has
# neither port, so it shows unhealthy under docker until it exits.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["python", "/repo/lab/healthcheck.py"]
# The commit this image was built from, stored on every backtest chunk and walk-forward fold as their revision
# (CI passes it; GHCR tags the image sha-<first 7>). Last, so a new commit does not bust the layers above.
ARG REVISION=dev
ENV TRADER_REVISION=${REVISION}
