#!/usr/bin/env bash
# Weekly strategy experiments on the data lab/backfill.py keeps: backtest every strategy on each exchange,
# check every strategy for lookahead bias, hyperopt the best few, then summarize.
# One strategy per freqtrade call: one broken strategy must not sink the whole run.
set -uo pipefail

REPO=${REPO:-/repo}
USER_DATA="$REPO/user_data"
RESULTS=${LAB_RESULTS:-/lab}
EXCHANGES=${EXPERIMENT_EXCHANGES:-binanceus coinbase}   # kraken has no historic candles
BACKTEST_DAYS=${BACKTEST_DAYS:-90}
LOOKAHEAD_DAYS=${LOOKAHEAD_DAYS:-30}
HYPEROPT_TOP=${HYPEROPT_TOP:-3}
HYPEROPT_EPOCHS=${HYPEROPT_EPOCHS:-100}
HYPEROPT_JOBS=${HYPEROPT_JOBS:-4}

run_dir="$RESULTS/runs/$(date -u +%Y-%m-%d)"
mkdir -p "$run_dir"
log() { printf '%s experiments: %s\n' "$(date -u +%H:%M:%S)" "$*"; }
since() { date -u -d "-$1 days" +%Y%m%d; }
base_args() { echo "--userdir $USER_DATA -c $USER_DATA/config.base.json -c $USER_DATA/exchanges/$1.json --datadir $RESULTS/data/$1"; }

if [ -n "${EXPERIMENT_STRATEGIES:-}" ]; then   # space-separated subset, for a quick manual run
  read -r -a STRATEGIES <<< "$EXPERIMENT_STRATEGIES"
else
  mapfile -t STRATEGIES < <(freqtrade list-strategies -1 --recursive-strategy-search --userdir "$USER_DATA" \
    -c "$USER_DATA/config.base.json" -c "$USER_DATA/exchanges/binanceus.json" 2>/dev/null)
fi
log "${#STRATEGIES[@]} strategies, exchanges: $EXCHANGES, run dir $run_dir"
[ "${#STRATEGIES[@]}" -gt 0 ] || { log "no strategies loaded"; exit 1; }

for ex in $EXCHANGES; do
  for s in "${STRATEGIES[@]}"; do
    out="$run_dir/backtest/$ex/$s"; mkdir -p "$out"
    # shellcheck disable=SC2046
    freqtrade backtesting $(base_args "$ex") --recursive-strategy-search --strategy "$s" \
      --timerange "$(since "$BACKTEST_DAYS")-" --cache none --backtest-directory "$out" > "$out/run.log" 2>&1
    # freqtrade exits 0 on some config errors, so the result file is the only proof of a run
    if ! compgen -G "$out/backtest-result-*.zip" >/dev/null; then
      grep -E ' - (ERROR|CRITICAL) - |Error' "$out/run.log" | tail -n 3 > "$out/error.txt"
      [ -s "$out/error.txt" ] || tail -n 3 "$out/run.log" > "$out/error.txt"
      log "backtest $ex/$s failed"
    fi
  done
done

first_ex=${EXCHANGES%% *}
mkdir -p "$run_dir/lookahead"
for s in "${STRATEGIES[@]}"; do
  # shellcheck disable=SC2046
  freqtrade lookahead-analysis $(base_args "$first_ex") --recursive-strategy-search --strategy "$s" \
    --timerange "$(since "$LOOKAHEAD_DAYS")-" --backtest-directory "$run_dir/lookahead/bt" \
    --lookahead-analysis-exportfilename "$run_dir/lookahead/$s.csv" > "$run_dir/lookahead/$s.log" 2>&1 \
    || log "lookahead $s failed"
done

python3 "$REPO/lab/summarize.py" "$run_dir" || { log "summarize failed"; exit 1; }

mkdir -p "$run_dir/hyperopt"
# Candidates come ranked; a strategy with no hyperoptable parameters fails fast, so walk the list
# until HYPEROPT_TOP have run.
mapfile -t CANDIDATES < <(python3 -c 'import json,sys; [print(s) for s in json.load(open(sys.argv[1]))["hyperopt_candidates"]]' \
  "$run_dir/summary.json")
done_count=0
for s in "${CANDIDATES[@]}"; do
  [ "$done_count" -ge "$HYPEROPT_TOP" ] && break
  log "hyperopt $s on $first_ex"
  # shellcheck disable=SC2046
  if freqtrade hyperopt $(base_args "$first_ex") --recursive-strategy-search --strategy "$s" \
      --timerange "$(since "$BACKTEST_DAYS")-" --epochs "$HYPEROPT_EPOCHS" --spaces buy sell \
      --hyperopt-loss SharpeHyperOptLossDaily -j "$HYPEROPT_JOBS" --disable-param-export \
      > "$run_dir/hyperopt/$s.log" 2>&1; then
    # shellcheck disable=SC2046
    freqtrade hyperopt-show $(base_args "$first_ex") --best --print-json --no-header \
      > "$run_dir/hyperopt/$s.json" 2>>"$run_dir/hyperopt/$s.log" || true
    done_count=$((done_count + 1))
  else
    log "hyperopt $s failed (see hyperopt/$s.log)"
  fi
done

python3 "$REPO/lab/summarize.py" "$run_dir"
ln -sfn "$(basename "$run_dir")" "$RESULTS/runs/latest"
log "done: $run_dir/summary.md"
