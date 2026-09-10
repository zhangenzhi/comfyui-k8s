#!/bin/bash
# Submit one text-to-video+audio request to the running SGLang server and download the MP4.
# usage: scripts/t2va_request.sh [prompt] [seconds] [aspect_ratio] [seed]
#        PROMPT_FILE=prompts/ep01_hook_t2va.txt scripts/t2va_request.sh "" 15 9:16 7   # read prompt from file
set -euo pipefail
ROOT=${H3_ROOT:-/lustre1/work/c30636/test/minimax-h3}   # work dir: models/, outputs/, logs/, tools/
EP=${EP:-$(cat $ROOT/logs/server_endpoint.txt)}
PROMPT=${1:-"At night, while their owner sleeps in a bedroom, three cats march in loudly playing tiny brass instruments, then abruptly file out."}
SECS=${2:-5}
AR=${3:-16:9}
SEED=${4:-1101}
if [ -n "${PROMPT_FILE:-}" ]; then PROMPT=$(cat "$PROMPT_FILE"); fi
TAG=${TAG:-t2va}
STEPS=${STEPS:-50}
QUALITY=${QUALITY:-lossless}   # lossless | extra-high | high (Cache-DiT, approximate)
OUT=$ROOT/outputs/${TAG}_$(date +%Y%m%d_%H%M%S)_seed${SEED}.mp4

curl -sS --fail "http://$EP/health" >/dev/null && echo "server $EP healthy"
body=$(jq -n --arg p "$PROMPT" --argjson s "$SECS" --arg ar "$AR" --argjson seed "$SEED" --argjson steps "$STEPS" --arg q "$QUALITY" --argjson se "${SHORT_EDGE:-768}" '{
  model:"MiniMaxAI/MiniMax-H3", prompt:$p, seconds:$s, task:"t2va", conditions:[],
  target:{short_edge:$se, aspect_ratio:$ar, duration_seconds:$s},
  num_outputs_per_prompt:1, num_inference_steps:$steps, quality:$q, flow_shift:12.0, audio_flow_shift:3.0, seed:$seed}')
video_id=$(curl -sS --fail-with-body -X POST "http://$EP/v1/videos" -H "Content-Type: application/json" -d "$body" | jq -r '.id')
echo "video_id=$video_id"; t0=$(date +%s)
while true; do
  status=$(curl -sS "http://$EP/v1/videos/$video_id" | jq -r '.status')
  [ "$status" = "completed" ] && break
  if [ "$status" = "failed" ]; then curl -sS "http://$EP/v1/videos/$video_id" | jq .; exit 1; fi
  sleep 3
done
curl -sS -L "http://$EP/v1/videos/$video_id/content" -o "$OUT"
echo "done in $(( $(date +%s) - t0 )) s -> $OUT"
ffprobe -v error -show_entries stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels -of compact "$OUT" 2>/dev/null || true
