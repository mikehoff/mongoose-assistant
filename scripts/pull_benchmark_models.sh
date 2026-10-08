#!/usr/bin/env bash
set -euo pipefail

# Pulls the model shortlist that `mongoose benchmark` compares. These are the
# 5 models that fit a 24 GB base M4 AND run in Ollama. Pulling ALL of them is a
# large download (~60 GB total) and they are NOT all needed to run Mongoose;
# they exist only so the benchmark can compare them on your machine.
#
# Skip any you don't want to test by setting it to 0, e.g.:
#   PULL_QWEN30=0 ./scripts/pull_benchmark_models.sh
#
# Tags must match DEFAULT_SHORTLIST in src/mongoose/benchmark.py.

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is not installed. Install Ollama first."
  exit 1
fi

# tag                              approx size   default on?
GEMMA26="gemma4:26b-a4b-it-q4_K_M"      # ~17 GB   (you likely already have this)
QWEN30="qwen3:30b-a3b"                  # ~19 GB
GEMMA12="gemma4:12b-it-q4_K_M"          # ~7 GB
GPTOSS="gpt-oss:20b"                    # ~13 GB
QWEN14="qwen3:14b"                      # ~9 GB

pull() {
  local tag="$1" flag="$2" size="$3"
  if [[ "$flag" == "0" ]]; then
    echo "skip   $tag"
    return
  fi
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$tag"; then
    echo "have   $tag (already pulled)"
    return
  fi
  echo "pull   $tag  (~$size)"
  ollama pull "$tag"
}

echo "Benchmark shortlist (~60 GB total if pulling all). Already-present models are skipped."
echo

pull "$GEMMA26" "${PULL_GEMMA26:-1}" "17 GB"
pull "$QWEN30"  "${PULL_QWEN30:-1}"  "19 GB"
pull "$GEMMA12" "${PULL_GEMMA12:-1}" "7 GB"
pull "$GPTOSS"  "${PULL_GPTOSS:-1}"  "13 GB"
pull "$QWEN14"  "${PULL_QWEN14:-1}"  "9 GB"

echo
echo "Done. Verify with: ollama list"
echo "Then run: mongoose benchmark        (or 'mongoose benchmark --no-cold' to skip eviction churn)"
