#!/bin/bash
# Local gateway. Keys come from $TCADCAL_SECRETS_DIR; values are never printed.
set -euo pipefail
NF=$(cd "$(dirname "$0")/.." && pwd)
TCADCAL_KEYS=${TCADCAL_SECRETS_DIR:-$HOME/.config/tcadcal}
PORT=${1:-4000}
LOG=${2:-$NF/tmp/gateway_$PORT.log}
mkdir -p "$NF/tmp" "$(dirname "$LOG")"
export NVIDIA_API_KEY="$(tr -d '[:space:]' < "$TCADCAL_KEYS/nvidia_api_key")"
export LITELLM_MASTER_KEY="$(tr -d '[:space:]' < "$TCADCAL_KEYS/litellm_master_key")"
test -n "$NVIDIA_API_KEY" && test -n "$LITELLM_MASTER_KEY"
export LITELLM_TELEMETRY=False LITELLM_LOG=INFO TMPDIR="$NF/tmp"
exec "$NF/env/bin/litellm" --config "$NF/fabric/litellm.yaml" --host 127.0.0.1 --port "$PORT" >> "$LOG" 2>&1
