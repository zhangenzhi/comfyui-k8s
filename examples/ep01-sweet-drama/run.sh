#!/bin/bash
# Submit the test episode to a running ComfyUI (5 clips x ~12 s, all t2va, hard cuts,
# subtitles force-aligned to the H3 audio). Outputs land in ComfyUI output/h3/examples/ep01/.
#   COMFY_URL=https://<host>/test/comfyui  COMFY_AUTH=user:pass  ./run.sh
#   (in-cluster: COMFY_URL=http://comfyui-svc:8188 and no auth)
set -euo pipefail
cd "$(dirname "$0")"
URL="${COMFY_URL:?set COMFY_URL}"; AUTH=(); [ -n "${COMFY_AUTH:-}" ] && AUTH=(-u "$COMFY_AUTH")
pid=$(curl -sk "${AUTH[@]}" -X POST -H "Content-Type: application/json" -d @workflow.api.json "$URL/api/prompt" \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("prompt_id") or d)')
echo "prompt_id=$pid"
while :; do
  h=$(curl -sk "${AUTH[@]}" "$URL/api/history/$pid")
  [ "$h" != "{}" ] && break; sleep 30; echo "$(date +%T) rendering..."
done
echo "$h" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read(),strict=False); h=list(d.values())[0]; print(h["status"]["status_str"]); [print(o["images"]) for o in h["outputs"].values() if "images" in o]'
