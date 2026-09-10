# ComfyUI cloud image (Rancher / K8s, PodSecurity=restricted friendly)
# Mirrors the ffformer deploy conventions: non-root UID 1000, /workspace/data as
# the persistent base dir (models, custom_nodes, input, output, user), state on PVC.
#
# Build:  docker build -t comfyui:0.1.0 -f Dockerfile .
# Run:    docker run --rm --gpus all -p 8188:8188 -v $PWD/data:/workspace/data comfyui:0.1.0

ARG BASE_IMAGE=pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime
FROM ${BASE_IMAGE}

# Pin these for reproducible rebuilds (git tag / commit); "master" = latest.
ARG COMFYUI_REPO=https://github.com/comfyanonymous/ComfyUI.git
ARG COMFYUI_REF=master
ARG MANAGER_REPO=https://github.com/Comfy-Org/ComfyUI-Manager.git
ARG MANAGER_REF=main

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    PIP_NO_CACHE_DIR=1

# ── System deps (git for Manager, ffmpeg/libgl for video & image nodes) ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates ffmpeg libgl1 libglib2.0-0 fonts-noto-cjk \
        build-essential aria2 \
    && rm -rf /var/lib/apt/lists/*

# ── ComfyUI core ────────────────────────────────────────────────────────
WORKDIR /opt
RUN git clone --depth 1 --branch "${COMFYUI_REF}" "${COMFYUI_REPO}" /opt/ComfyUI \
    && cd /opt/ComfyUI \
    && pip install -r requirements.txt \
    && echo "ComfyUI $(git rev-parse --short HEAD)" > /opt/COMFYUI_VERSION

# ── ComfyUI-Manager (seed copy; entrypoint copies it into the PVC once) ─
RUN git clone --depth 1 --branch "${MANAGER_REF}" "${MANAGER_REPO}" /opt/seed/ComfyUI-Manager \
    && pip install -r /opt/seed/ComfyUI-Manager/requirements.txt

# ── Non-root user (UID 1000 to match the K8s securityContext) ───────────
RUN useradd -m -u 1000 -s /bin/bash comfy \
    && mkdir -p /workspace/data \
    && chown -R comfy:comfy /opt/ComfyUI /opt/seed /workspace/data \
    # let custom nodes pip-install into the env when PIP_USER is not set
    && chmod -R a+rwX /opt/conda/lib/python3.*/site-packages /opt/conda/bin

# ── Bundled custom nodes (synced into the PVC at every start) ───────────
COPY --chown=comfy:comfy custom_nodes/ /opt/seed/custom_nodes/
RUN pip install paramiko requests openai-whisper   # whisper: subtitle forced alignment

COPY --chown=comfy:comfy entrypoint.sh /opt/entrypoint.sh
COPY --chown=comfy:comfy fetch-model.sh /usr/local/bin/fetch-model
COPY --chown=comfy:comfy fetch-llm.sh /usr/local/bin/fetch-llm
RUN chmod +x /opt/entrypoint.sh /usr/local/bin/fetch-model /usr/local/bin/fetch-llm

USER comfy
ENV HOME=/home/comfy \
    COMFYUI_BASE_DIR=/workspace/data \
    COMFYUI_PORT=8188 \
    COMFYUI_ARGS="" \
    COMFYUI_AUTO_UPDATE=0

EXPOSE 8188
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -fsS http://localhost:8188/system_stats >/dev/null || exit 1

CMD ["/opt/entrypoint.sh"]
