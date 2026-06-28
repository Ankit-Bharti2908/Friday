#!/usr/bin/env bash
# Launch a local llama.cpp server (OpenAI-compatible) for Friday's local tier.
#
# Friday's `config/models.json` points the local-first entry at
# http://localhost:8080/v1 (model name `gemma-3n-e4b`). Run this, and the
# fast/standard/deep tiers are served locally with zero cloud keys.
#
# Requires llama.cpp's `llama-server` on PATH:
#   macOS:  brew install llama.cpp
#   else:   build from https://github.com/ggml-org/llama.cpp
#
# Usage:
#   scripts/llamacpp.sh                # Gemma 3n E4B on :8080  (Friday's default)
#   scripts/llamacpp.sh e2b            # Gemma 3n E2B — smaller/faster variant
#   scripts/llamacpp.sh e4b 8081       # E4B on a custom port
#   scripts/llamacpp.sh ggml-org/gemma-3-12b-it-GGUF   # any HF GGUF repo or local path
#   MODEL_HF=org/repo-GGUF scripts/llamacpp.sh         # override via env
#
# The model NAME in models.json is cosmetic for llama.cpp (the server answers
# with whatever weights it loaded) — switching variant here needs no JSON edit
# unless you change the port.
set -euo pipefail

variant="${1:-e4b}"
port="${2:-8080}"
ctx="${LLAMACPP_CTX:-8192}"
ngl="${LLAMACPP_NGL:-99}"   # GPU layers to offload (99 = all; Metal on Apple Silicon)

case "$variant" in
  e4b) repo="${MODEL_HF:-ggml-org/gemma-3n-E4B-it-GGUF}" ;;
  e2b) repo="${MODEL_HF:-ggml-org/gemma-3n-E2B-it-GGUF}" ;;
  *)   repo="${MODEL_HF:-$variant}" ;;   # treat the arg as an explicit HF repo / path
esac

if ! command -v llama-server >/dev/null 2>&1; then
  echo "error: llama-server not found on PATH. Install it (macOS: brew install llama.cpp)." >&2
  exit 1
fi

echo "Starting llama-server: $repo  →  http://localhost:${port}/v1  (ctx=${ctx}, ngl=${ngl})"
exec llama-server \
  -hf "$repo" \
  --host 127.0.0.1 \
  --port "$port" \
  --jinja \
  -c "$ctx" \
  -ngl "$ngl"
