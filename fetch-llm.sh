#!/bin/bash
# Download a Hugging Face LLM repo into the PVC for the local drama-LLM backend.
#   fetch-llm Qwen/Qwen2.5-14B-Instruct       -> /workspace/data/llm/Qwen2.5-14B-Instruct
set -euo pipefail
repo="${1:?HF repo id, e.g. Qwen/Qwen2.5-14B-Instruct}"
name="${2:-$(basename "$repo")}"
dest="${H3_LLM_DIR:-/workspace/data/llm}/${name}"
mkdir -p "$dest"
python - "$repo" "$dest" <<'PY'
import sys, os
from huggingface_hub import snapshot_download
repo, dest = sys.argv[1:3]
snapshot_download(repo, local_dir=dest, token=os.environ.get("HF_TOKEN") or None,
                  allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.py", "*.tiktoken", "merges.txt", "vocab*"])
print("done:", dest)
PY
du -sh "$dest"
