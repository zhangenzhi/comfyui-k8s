#!/bin/bash
# Install SeedVR2 CLI deps into conda env `seedvr2` (created with python3.12 + ffmpeg + uv).
set -euo pipefail
source ~/miniconda3/etc/profile.d/conda.sh
conda activate seedvr2
export PYTHONNOUSERSITE=1
uv pip install torch==2.13.0 torchvision torchaudio
uv pip install -r ${H3_ROOT:-/lustre1/work/c30636/test/minimax-h3}/tools/ComfyUI-SeedVR2_VideoUpscaler/requirements.txt
uv pip install triton
python -c "import torch;print('torch',torch.__version__,torch.version.cuda)"
cd ${H3_ROOT:-/lustre1/work/c30636/test/minimax-h3}/tools/ComfyUI-SeedVR2_VideoUpscaler && python inference_cli.py --help | head -5
echo "=== SEEDVR2 ENV DONE"
