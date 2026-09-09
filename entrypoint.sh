#!/bin/bash
# ComfyUI entrypoint: prepare the persistent base dir on the PVC, then start.
set -euo pipefail

BASE="${COMFYUI_BASE_DIR:-/workspace/data}"
PORT="${COMFYUI_PORT:-8188}"
export HOME="${HOME:-/home/comfy}"

echo "[entrypoint] $(cat /opt/COMFYUI_VERSION 2>/dev/null) base_dir=${BASE}"

# ── Persistent layout (ComfyUI --base-directory expects these) ──────────
mkdir -p "${BASE}"/{custom_nodes,input,output,temp,user} \
         "${BASE}"/models/{checkpoints,diffusion_models,unet,vae,loras,clip,text_encoders,clip_vision,controlnet,upscale_models,embeddings,style_models,photomaker,gligen,hypernetworks,vae_approx,configs}

# ── Seed ComfyUI-Manager into the PVC once (later updates via Manager UI) ─
if [ ! -d "${BASE}/custom_nodes/ComfyUI-Manager" ]; then
    echo "[entrypoint] Seeding ComfyUI-Manager into ${BASE}/custom_nodes"
    cp -r /opt/seed/ComfyUI-Manager "${BASE}/custom_nodes/"
fi

# ── Bundled custom nodes from the image: always overwrite (versioned in git)
for d in /opt/seed/custom_nodes/*/; do
    [ -d "$d" ] || continue
    n=$(basename "$d")
    if [ -e "${BASE}/custom_nodes/${n}/.keep-local" ]; then
        echo "[entrypoint] keeping local (hot-patched) copy of ${n}"; continue
    fi
    rm -rf "${BASE}/custom_nodes/${n}"
    cp -r "$d" "${BASE}/custom_nodes/${n}"
    echo "[entrypoint] synced bundled custom node: ${n}"
done

# ── Persistent pip user site: packages installed by Manager / custom nodes
#    survive pod restarts because they live on the PVC, not in the image.
export PYTHONUSERBASE="${BASE}/.pyuser"
export PIP_USER=1
mkdir -p "${PYTHONUSERBASE}"
export PATH="${PYTHONUSERBASE}/bin:${PATH}"

# ── Optional: pull latest ComfyUI at start (code-only update, like ffformer)
if [ "${COMFYUI_AUTO_UPDATE:-0}" = "1" ]; then
    echo "[entrypoint] COMFYUI_AUTO_UPDATE=1 -> git pull"
    cd /opt/ComfyUI
    git config --global --add safe.directory /opt/ComfyUI || true
    if git pull --ff-only 2>&1; then
        pip install -r requirements.txt 2>&1 | tail -n 2 || true
        echo "[entrypoint] ComfyUI updated to $(git rev-parse --short HEAD)"
    else
        echo "[entrypoint] git pull failed, using image code"
    fi
fi

cd /opt/ComfyUI
echo "[entrypoint] Starting ComfyUI on 0.0.0.0:${PORT} ${COMFYUI_ARGS:-}"
# shellcheck disable=SC2086
exec python main.py \
    --listen 0.0.0.0 --port "${PORT}" \
    --base-directory "${BASE}" \
    ${COMFYUI_ARGS:-}
