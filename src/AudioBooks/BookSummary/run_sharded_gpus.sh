#!/bin/bash
# Launch one summarizer replica per GPU so a multi-GPU box (e.g. 2x L4 on
# g2-standard-24) runs as independent BOOK-LEVEL shards.
#
# Each replica is pinned to a single GPU via CUDA_VISIBLE_DEVICES, so:
#   - device_map="auto" never pipeline-splits the model across GPUs, and
#   - every book is summarized end-to-end on one GPU, keeping the per-book
#     rolling story_so_far fully intact (no book is ever split across GPUs).
#
# Throughput scales ~linearly with GPU count by running different whole books
# in parallel — not by parallelizing a single book.
#
# Usage (extra args are passed through to every replica):
#   ./src/AudioBooks/BookSummary/run_sharded_gpus.sh --validate --max-books 100
#
# Run from the repo root so the package import path resolves.
set -euo pipefail

NUM_GPUS=$(nvidia-smi -L | wc -l)
if [ "$NUM_GPUS" -lt 1 ]; then
  echo "No GPUs detected (nvidia-smi -L returned nothing)." >&2
  exit 1
fi
echo "Detected $NUM_GPUS GPU(s); launching $NUM_GPUS shard(s)."

pids=()
for ((i = 0; i < NUM_GPUS; i++)); do
  echo "  shard $i -> GPU $i"
  CUDA_VISIBLE_DEVICES="$i" python -m AudioBooks.BookSummary.spot_vm_summarize \
    --mode local --load-in-4bit \
    --num-shards "$NUM_GPUS" --shard-index "$i" \
    "$@" &
  pids+=("$!")
done

# Wait for every replica; exit non-zero if any shard failed.
fail=0
for pid in "${pids[@]}"; do
  wait "$pid" || fail=1
done

if [ "$fail" -ne 0 ]; then
  echo "One or more shards failed." >&2
fi
exit "$fail"