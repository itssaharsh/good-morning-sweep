#!/usr/bin/env bash
# Start Gemma 4 E2B on this machine's CPU with llama.cpp's server (OpenAI-style API on 127.0.0.1:8089).
# Get the two files from https://huggingface.co/ggml-org (gemma-4-E2B-it GGUF + its mmproj) and point the vars at them.
set -euo pipefail
LLAMA_DIR="${LLAMA_DIR:-$HOME/.local/opt/llama/llama-b11851}"
MODEL="${MODEL:-$HOME/.local/opt/models/gemma-4-E2B-it-Q4_0.gguf}"
MMPROJ="${MMPROJ:-$HOME/.local/opt/models/mmproj-gemma-4-E2B-it-Q8_0.gguf}"
THREADS="${THREADS:-12}"
# --image-max-tokens 70: Gemma 4's smallest picture budget; enough to read a greeting card, ~3x faster than default.
# --reasoning-budget 0: answer directly, no hidden thinking.
LD_LIBRARY_PATH="$LLAMA_DIR" exec "$LLAMA_DIR/llama-server" -m "$MODEL" --mmproj "$MMPROJ" --jinja \
  --reasoning-budget 0 --image-min-tokens 70 --image-max-tokens 70 -t "$THREADS" -c 4096 \
  --host 127.0.0.1 --port "${PORT:-8089}" -np 1
