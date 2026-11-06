# The trader image: the pinned Freqtrade release plus this repo, so the bots, the jobs
# and the site all run one artifact. home-config pins it by digest.
ARG FREQTRADE_TAG=2026.9
FROM freqtradeorg/freqtrade:${FREQTRADE_TAG}

COPY lab/requirements.txt /tmp/lab-requirements.txt
RUN pip install --user --no-cache-dir -r /tmp/lab-requirements.txt

COPY --chown=ftuser:ftuser . /repo
WORKDIR /repo
# Strategies can `import patterns` (lab/patterns.py). No lab module shares a name with an installed
# one; lab/tests/test_families.py checks that.
ENV PYTHONPATH=/repo/lab
# The userdir subfolders .dockerignore leaves out. freqtrade creates missing ones itself, but its
# check-then-create races when several freqtrade processes start at once (lab/backfill.py runs one
# per exchange) and the loser exits with "exists already and is not a directory".
RUN mkdir -p user_data/logs user_data/data user_data/backtest_results user_data/hyperopt_results user_data/plot
# The entrypoint stays freqtrade's: every workload in home-config sets its own command.
