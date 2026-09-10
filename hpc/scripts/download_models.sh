#!/bin/bash
# Fetch everything the HPC side needs into $H3_ROOT (weights are NOT in git).
#   MiniMax-H3 FL2VA (t2va + fl2va)  ~139 GB   MiniMaxAI/MiniMax-H3
#   SeedVR2-7B sharp fp16 + VAE       ~17 GB   numz/SeedVR2_comfyUI
#   SeedVR2 CLI (third-party code)              github.com/numz/ComfyUI-SeedVR2_VideoUpscaler @ 4490bd1
# Optional: REF2VA=1 adds the Ref2VA partition (~144 GB); LLM=1 adds Qwen2.5-14B-Instruct for serve_llm.pbs.
set -euo pipefail
ROOT=${H3_ROOT:-/lustre1/work/c30636/test/minimax-h3}
mkdir -p "$ROOT"/{models,tools,logs,outputs}
source ~/miniconda3/etc/profile.d/conda.sh && conda activate minimax-h3
export PYTHONNOUSERSITE=1
hf download MiniMaxAI/MiniMax-H3 --local-dir "$ROOT/models/MiniMax-H3" \
   --include "FL2VA/*" "model_index.json" "LICENSE" "docs/*" "scripts/*"
[ "${REF2VA:-0}" = 1 ] && hf download MiniMaxAI/MiniMax-H3 --local-dir "$ROOT/models/MiniMax-H3" --include "Ref2VA/*"
hf download numz/SeedVR2_comfyUI seedvr2_ema_7b_sharp_fp16.safetensors ema_vae_fp16.safetensors \
   --local-dir "$ROOT/models/SeedVR2"
[ "${LLM:-0}" = 1 ] && hf download Qwen/Qwen2.5-14B-Instruct --local-dir "$ROOT/models/Qwen2.5-14B-Instruct" \
   --include "*.json" "*.safetensors" "merges.txt" "vocab.json"
T="$ROOT/tools/ComfyUI-SeedVR2_VideoUpscaler"
[ -d "$T/.git" ] || git clone -q https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git "$T"
git -C "$T" checkout -q 4490bd1
du -sh "$ROOT"/models/* "$T"
