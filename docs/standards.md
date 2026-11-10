# Contributor standards

This reference names the repo's checks and the policies they enforce.

1. Run the commands in [package.json](../package.json) before landing: `npm run typecheck`, `npm run lint` and `npm test`, because CI uses that command manifest for the same checks. Install `lab/requirements-dev.txt` for static checks; tests run in the image with its runtime dependencies.
2. Keep auth configuration, database connection and image health-check annotations passing mypy, because these are the initial typed boundaries in [pyproject.toml](../pyproject.toml). Imports are skipped and missing third-party stubs are ignored; other modules and unannotated function bodies are outside this initial type-check scope.
3. Keep `lab`, `web`, `ci` and shipped strategies passing Ruff's configured syntax and undefined-name checks, because an unloadable job or strategy cannot run. [pyproject.toml](../pyproject.toml) owns the rule selection; this gate does not enforce formatting.
4. Preserve the [dry-run and live-entry controls](../README.md#the-no-real-money-guard), because research code must retain its trading boundary. `lab/tests/test_league.py` and `lab/tests/test_live.py` check the entry points; [operating rules](operating-rules.md) owns operator policy.
5. Run database tests only against disposable Postgres, because `lab/tests/conftest.py` truncates the schema. [README](../README.md#run-locally) owns the commands and [CI](../.github/workflows/image.yml) owns the merge checks.
6. Add schema changes as numbered files in `lab/migrations/`, because `lab/migrate.py` tracks which files were applied. CI applies migrations twice to verify repeat execution adds none.
7. Keep runtime data and credentials outside tracked files, because image builds include this checkout. [.gitignore](../.gitignore), [.dockerignore](../.dockerignore) and CI's new-commit secret scan define the checked boundary.
