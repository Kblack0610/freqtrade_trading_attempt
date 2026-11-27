# trader: traps for a session

Repo rules live in [docs/standards.md](docs/standards.md); the layout and commands in [README.md](README.md). This file holds only the traps sessions keep hitting. Each line has its why.

## Landing and deploy

- Land with `agent-land <N>`, never `gh pr checks --watch` plus `gh pr merge`: the watch exits 0 with "no checks reported" before CI registers (#17, #35 merged unchecked).
- A master merge goes live on its own: the `bump-home-config` CI job opens the home-config digest PR after the image build and merges it once that PR's checks pass, about 2 min later. Do not bump by hand, queue a bump item, or write "not live until the bump" as Next. Check the live digest first: `kubectl --context home-k3s get deploy trader-web -n freqtrade`. No bump PR after a green build means `FORGEJO_DEPLOY_TOKEN` is unset or master moved on.
- There is no hold on the bump (`ci/bump_home_config.py` has no skip switch). Order merges so a cache-key change lands before the 01:00 UTC walk-forward.
- Deploy is home-config `apps/freqtrade/` on Forgejo; never push or open PRs for it on GitHub.

## Tests

- `test_kit_files_match_the_lock` fails in a fresh worktree or any `-v "$PWD:/repo"` run: `web/static/kit/` is gitignored and only the image build fetches it. Run `python ~/.dotfiles/.local/src/web-kit/fetch-kit.py web/kit.lock web/static/kit` once or deselect that test; do not edit `kit.lock` or the test. At least 9 sessions re-diagnosed it.
- DB tests run only against a disposable Postgres: `lab/tests/conftest.py` truncates the schema.

## Cluster

- Never run Python or an analysis query inside a pod with `kubectl exec`: it shares the container memory limit (trader-web 512Mi OOM-killed by tr-213; league-52 by ar-4293). Read pod memory from Prometheus `container_memory_rss`; read data by port-forwarding `databases/svc/postgres` with the `web-database-url` key of Secret `freqtrade/trader-db` (read-only role), started with run_in_background and stopped by its PID, never `pkill -f`.
- `OOMKilled` is not always the pod's own limit: check `kubectl get events -A | grep SystemOOM` first (the lab PVC pins analysis and walk-forward to one node).
- Pod N runs roster row N counting only non-comment, non-blank lines of `apps/freqtrade/roster.tsv`: use `grep -vc '^#\|^$'` before deleting a PVC (tr-144 wiped 5 unrelated pods by file line).
- Delete a Job by `job/<name>`, never by selector (a selector delete removed every completed Job in the namespace).

## Reading runs and dispatching

- The walk-forward stops starting folds after `WF_BUDGET_S` and records `folds_left`; a backlog drains over several nights. Any change to a fold_key input reruns every stored fold once. Read a verdict only from a run with `folds_left` 0, after a night with no key change.
- A phase that reads a run or another phase carries `after` at queue time; an `at:` time starts it early.
- A "check after the next nightly" line gets done only if a queue item owns it: queue it before wind-down with `--tags at:<time>`.
- A dispatched item may already be landed or not yet runnable: check `git log origin/master`, the live digest and the latest job log first, then close it cancelled or move it to waiting.
- Do not post an ask for work that can run paper-only; release the hold and keep going.

## Money

- No real money, ever; `lab/crypto/league.py` refuses a non-dry-run or keyed config. A hub or routing line names, per leg, API or person-only (Fidelity has no money-moving API).
