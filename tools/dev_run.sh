#!/bin/sh
# Run the whole stack unprivileged for development:
#   swtpm (software TPM) + dev polkit stand-in + openhellod on the session bus
#   + the setup GUI. Real fingerprint reader / IR camera are used as-is.
# State lives in ${OPENHELLO_DEV_DIR:-$HOME/.cache/openhello/dev-run}.
# Ctrl+C (or closing the GUI) stops everything.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DIR=${OPENHELLO_DEV_DIR:-$HOME/.cache/openhello/dev-run}
PORT=${OPENHELLO_SWTPM_PORT:-2321}
mkdir -p "$DIR/tpm"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export OPENHELLO_STATE_DIR="$DIR/state" OPENHELLO_BUS=session
export OPENHELLO_MODEL_DIR="${OPENHELLO_MODEL_DIR:-$HOME/.cache/openhello/models}"

pids=""
# Stop in reverse start order (daemon before swtpm, so its TPM cleanup
# doesn't wait on a dead socket), and wait for each.
cleanup() {
  for p in $(echo "$pids" | tr ' ' '\n' | tac); do
    kill "$p" 2>/dev/null && wait "$p" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

swtpm socket --tpm2 --tpmstate dir="$DIR/tpm" \
  --server type=tcp,port="$PORT" --ctrl type=tcp,port=$((PORT + 1)) \
  --flags not-need-init,startup-clear & pids="$pids $!"
python3 "$ROOT/tools/dev_polkit_allow.py" & pids="$pids $!"
sleep 0.5
python3 -m openhello.daemon --bus session --tcti "swtpm:port=$PORT" -v \
  >"$DIR/daemon.log" 2>&1 & pids="$pids $!"
sleep 1
echo "daemon log: $DIR/daemon.log"
python3 -m openhello.gui
