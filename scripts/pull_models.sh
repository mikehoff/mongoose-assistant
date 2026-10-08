#!/usr/bin/env bash
set -euo pipefail

# Role-based model pulls. All tags pinned to Q4_K_M for a 24 GB base M4.
# On this machine the models are mutually exclusive in memory: pull all three
# so they are on disk, but only one runs at a time (see OLLAMA_KEEP_ALIVE).
CHAT_MODEL="${OLLAMA_CHAT_MODEL:-gemma4:26b-a4b-it-q4_K_M}"
CODE_MODEL="${OLLAMA_CODE_MODEL:-qwen2.5-coder:14b-instruct-q4_K_M}"

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is not installed. Install Ollama first."
  exit 1
fi

echo "Pulling chat/persona model: $CHAT_MODEL"
ollama pull "$CHAT_MODEL"

# The tool/reasoning role defaults to the resident CHAT model (no swap thrash on
# 24GB), so we do NOT pull a separate tool model unless OLLAMA_TOOL_MODEL is set
# to something different (e.g. gpt-oss:20b as an explicit override).
if [[ -n "${OLLAMA_TOOL_MODEL:-}" && "${OLLAMA_TOOL_MODEL}" != "$CHAT_MODEL" ]]; then
  echo "Pulling tool/reasoning model (override): $OLLAMA_TOOL_MODEL"
  ollama pull "$OLLAMA_TOOL_MODEL"
fi

# Code model is optional: only pulled when explicitly requested, since it is
# only used for coding turns and competes for the same memory budget.
if [[ "${MONGOOSE_PULL_CODE_MODEL:-0}" == "1" ]]; then
  echo "Pulling code model: $CODE_MODEL"
  ollama pull "$CODE_MODEL"
fi

echo "Done. Verify tags resolved with: ollama list"
