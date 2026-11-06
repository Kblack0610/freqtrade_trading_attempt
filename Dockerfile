# The trader image: the pinned Freqtrade release plus this repo, so the bots, the jobs
# and the site all run one artifact. home-config pins it by digest.
# FREQTRADE_TAG=2026.9_freqai builds the ML image (trader-ml) for lab/families/ml; the bots never run on it.
ARG FREQTRADE_TAG=2026.9
FROM freqtradeorg/freqtrade:${FREQTRADE_TAG}

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
# The entrypoint stays freqtrade's: every workload in home-config sets its own command.
# Healthy when the bot API (lab/league.py, :8080) or the site (web/app.py, :8000) answers. Docker
# and compose read this; k8s ignores it and uses the probes in home-config. A one-shot job has
# neither port, so it shows unhealthy under docker until it exits.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["python", "/repo/lab/healthcheck.py"]
