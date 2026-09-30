#!/usr/bin/env bash
# Launcher for the local llama.cpp model server used by agent narrative drafting.
#
# Environment:
#   AGENT_MODEL_PATH   required: local .gguf model file (no default is assumed).
#   AGENT_LLAMA_SERVER default ~/llama.cpp/build/bin/llama-server.
#   AGENT_PORT         default 8080.
#   AGENT_MODEL_ALIAS  default gemma-4-12b-qat.
#   AGENT_CTX          default 8192.
#   AGENT_LOG          default ~/llama_server.log (never under this repo).
#   AGENT_CUDA_HOME    optional: when set, its lib64 is prepended to LD_LIBRARY_PATH.
#
# The server binds only 127.0.0.1 with full GPU offload, one parallel slot,
# q8_0 K/V cache and reasoning disabled.
set -eu

AGENT_LLAMA_SERVER="${AGENT_LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"
AGENT_PORT="${AGENT_PORT:-8080}"
AGENT_MODEL_ALIAS="${AGENT_MODEL_ALIAS:-gemma-4-12b-qat}"
AGENT_CTX="${AGENT_CTX:-8192}"
AGENT_LOG="${AGENT_LOG:-$HOME/llama_server.log}"

if [ -z "${AGENT_MODEL_PATH:-}" ]; then
  echo "agent_server: AGENT_MODEL_PATH is required (path to a local .gguf model file)" >&2
  exit 1
fi
if [ ! -f "$AGENT_MODEL_PATH" ]; then
  echo "agent_server: model file not found: $AGENT_MODEL_PATH" >&2
  exit 1
fi
if [ ! -x "$AGENT_LLAMA_SERVER" ]; then
  echo "agent_server: llama-server binary not found or not executable: $AGENT_LLAMA_SERVER" >&2
  exit 1
fi
if command -v curl >/dev/null 2>&1 && curl -sf "http://127.0.0.1:${AGENT_PORT}/health" >/dev/null 2>&1; then
  echo "agent_server: port ${AGENT_PORT} already serves /health; refusing to start a second server" >&2
  exit 1
fi

if [ -n "${AGENT_CUDA_HOME:-}" ]; then
  export LD_LIBRARY_PATH="${AGENT_CUDA_HOME}/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

exec "$AGENT_LLAMA_SERVER" \
  -m "$AGENT_MODEL_PATH" \
  --alias "$AGENT_MODEL_ALIAS" \
  --host 127.0.0.1 \
  --port "$AGENT_PORT" \
  -c "$AGENT_CTX" \
  -ngl 999 \
  --parallel 1 \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --reasoning off \
  >>"$AGENT_LOG" 2>&1
