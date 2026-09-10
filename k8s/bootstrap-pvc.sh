#!/bin/bash
# One-time bootstrap of a fresh comfyui-pvc (run from any machine with kubectl access, after
# `kubectl apply -f k8s/00-pvc.yaml -f k8s/10-deployment.yaml` and the pod is Running).
# Installs what the image does not carry: CJK subtitle font copy, the vLLM sidecar venv (~8 GB),
# and the story-LLM weights (Qwen2.5-72B-Instruct-AWQ, ~39 GB). Idempotent; re-run is safe.
set -euo pipefail
NS=${NS:-c30636-default}
POD=$(kubectl -n "$NS" get pod -l app=comfyui -o jsonpath='{.items[0].metadata.name}')
echo "bootstrapping PVC through pod $POD"
kubectl -n "$NS" exec -i "$POD" -- sh -s <<'EOS'
set -e
B=/workspace/data
mkdir -p $B/fonts $B/venvs $B/llm $B/models/whisper $B/input/keyframes $B/user/default/workflows
# 1) CJK font used by the subtitle burner (image has fonts-noto-cjk; copy a stable path onto the PVC)
if [ ! -f $B/fonts/NotoSansCJK-Regular.ttc ]; then
  F=$(fc-list :lang=zh file 2>/dev/null | grep -m1 -i 'NotoSansCJK-Regular' | cut -d: -f1 || true)
  [ -n "$F" ] && cp "$F" $B/fonts/NotoSansCJK-Regular.ttc && echo "font copied"
fi
# 2) Whisper for forced-aligned subtitles (baked into newer images; PVC fallback for older ones)
python -c "import whisper" 2>/dev/null || PYTHONUSERBASE=$B/.pyuser pip install --user -q openai-whisper
# 3) vLLM sidecar venv (own torch; kept off the ComfyUI env)
if [ ! -x $B/venvs/vllm/bin/vllm ]; then
  python -m venv $B/venvs/vllm
  env -u PYTHONUSERBASE -u PIP_USER $B/venvs/vllm/bin/pip install -q --upgrade pip
  env -u PYTHONUSERBASE -u PIP_USER $B/venvs/vllm/bin/pip install -q vllm
fi
# 4) story-LLM weights
if [ ! -f $B/llm/Qwen2.5-72B-Instruct-AWQ/config.json ]; then
python - <<'PY'
from huggingface_hub import snapshot_download
snapshot_download("Qwen/Qwen2.5-72B-Instruct-AWQ", local_dir="/workspace/data/llm/Qwen2.5-72B-Instruct-AWQ",
                  max_workers=8, allow_patterns=["*.json", "*.safetensors", "merges.txt", "vocab.json", "*.txt"])
PY
fi
echo "== bootstrap done"; du -sh $B/venvs/vllm $B/llm/* 2>/dev/null || true
EOS
echo "Restart ComfyUI so the entrypoint starts the vLLM sidecar (loads in ~4 min):"
echo "  kubectl -n $NS rollout restart deploy/comfyui"
