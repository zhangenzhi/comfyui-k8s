#!/bin/bash
# Download a model into the persistent models dir.
#   fetch-model <subdir> <url> [filename]
#   e.g. fetch-model checkpoints https://huggingface.co/.../sd_xl_base_1.0.safetensors
# Set HF_TOKEN for gated Hugging Face repos.
set -euo pipefail
sub="${1:?subdir (checkpoints|vae|loras|diffusion_models|...)}"
url="${2:?url}"
name="${3:-$(basename "${url%%\?*}")}"
dest="${COMFYUI_BASE_DIR:-/workspace/data}/models/${sub}"
mkdir -p "${dest}"
if [ -s "${dest}/${name}" ]; then
    echo "[fetch-model] exists: ${dest}/${name}"; exit 0
fi
hdr=()
[ -n "${HF_TOKEN:-}" ] && hdr=(--header "Authorization: Bearer ${HF_TOKEN}")
echo "[fetch-model] ${url} -> ${dest}/${name}"
if command -v aria2c >/dev/null; then
    aria2c -x 8 -s 8 --continue=true --dir "${dest}" --out "${name}" "${hdr[@]}" "${url}"
else
    curl -L --fail --retry 5 -C - "${hdr[@]}" -o "${dest}/${name}" "${url}"
fi
echo "[fetch-model] done: $(du -h "${dest}/${name}" | cut -f1)"
