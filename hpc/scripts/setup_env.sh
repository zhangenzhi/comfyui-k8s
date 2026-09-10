#!/bin/bash
# Build the minimax-h3 conda env (python 3.12 + ffmpeg + uv) and install sglang[diffusion].
set -euo pipefail
source ~/miniconda3/etc/profile.d/conda.sh
ENV=minimax-h3
if ! conda env list | awk '{print $1}' | grep -qx "$ENV"; then
  conda create -y -n "$ENV" -c conda-forge --override-channels python=3.12 ffmpeg uv
fi
conda activate "$ENV"
export PYTHONNOUSERSITE=1
uv pip install --upgrade pip
uv pip install "sglang[diffusion]" --prerelease=allow
echo "=== versions"; python -c "import torch, sglang; print('torch', torch.__version__, 'cuda', torch.version.cuda); print('sglang', sglang.__version__)"
which ffmpeg ffprobe; sglang --help 2>&1 | head -5
echo "=== ENV SETUP DONE"
