#!/bin/bash
# One-shot: H3 t2va at 768p on the running server, then queue a SeedVR2-7B sharp upscale to 1440p on the sg queue.
# usage: scripts/h3_2k.sh "prompt" [seconds] [aspect_ratio] [seed]      or   PROMPT_FILE=... TAG=... scripts/h3_2k.sh "" 15 9:16 7
set -euo pipefail
ROOT=${H3_ROOT:-/lustre1/work/c30636/test/minimax-h3}   # work dir: models/, outputs/, logs/, tools/
out=$($ROOT/scripts/t2va_request.sh "$@" | tee /dev/stderr | sed -n 's/^done in .* -> //p')
[ -s "$out" ] || { echo "no H3 output"; exit 1; }
jid=$(qsub -v IN=$out $ROOT/scripts/upscale_seedvr2.pbs)
echo "upscale job $jid -> $ROOT/outputs/upscaled/$(basename ${out%.mp4})_seedvr2_1440p.mp4 (log $ROOT/logs/${jid%%.*}.sjms.OU)"
