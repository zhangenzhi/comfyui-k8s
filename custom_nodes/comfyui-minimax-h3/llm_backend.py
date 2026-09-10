"""LLM backends for the drama nodes.

  ollama : the in-cluster Ollama service (CPU-only here, ~1.5 tok/s for 7B -> slow)
  openai : any OpenAI-compatible chat endpoint (H3_LLM_URL / H3_LLM_MODEL / H3_LLM_KEY),
           e.g. an LLM served on the HPC by minimax-h3/scripts/serve_llm.pbs, or a cloud API.
  local  : transformers on the ComfyUI pod's own H100 (the research-cloud GPU), kept
           RESIDENT between calls (decision 2026-09-09). Video inference stays on the HPC.
           ComfyUI runs with --reserve-vram 30 so its own model manager leaves room for it.
           Weights: <H3_LLM_DIR>/<model> (fetch-llm Qwen/Qwen2.5-14B-Instruct).
"""
import os
import threading

import requests

LLM_DIR = os.environ.get("H3_LLM_DIR", "/workspace/data/llm")
DEFAULT_LOCAL = os.environ.get("H3_LOCAL_LLM", "Qwen2.5-14B-Instruct")
DEFAULT_OLLAMA = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OPENAI_URL = os.environ.get("H3_LLM_URL", "")          # e.g. http://172.31.17.244:30011/v1
OPENAI_MODEL = os.environ.get("H3_LLM_MODEL", "Qwen2.5-14B-Instruct")
OPENAI_KEY = os.environ.get("H3_LLM_KEY", "EMPTY")
DEFAULT_BACKEND = os.environ.get("H3_LLM_BACKEND", "local")
BACKENDS = ["local", "openai", "ollama"]

_lock = threading.Lock()
_cache = {}  # model name -> (tokenizer, model)


def local_models():
    try:
        return sorted(d for d in os.listdir(LLM_DIR)
                      if os.path.exists(os.path.join(LLM_DIR, d, "config.json")))
    except FileNotFoundError:
        return []


def _load_local(name):
    with _lock:
        if name in _cache:
            return _cache[name]
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        path = os.path.join(LLM_DIR, name)
        if not os.path.exists(os.path.join(path, "config.json")):
            raise FileNotFoundError(
                f"{path} not found. Run in the pod: fetch-llm Qwen/{name}  (or pick another model)")
        tok = AutoTokenizer.from_pretrained(path)
        model = AutoModelForCausalLM.from_pretrained(
            path, torch_dtype=torch.bfloat16, low_cpu_mem_usage=True).to("cuda").eval()
        _cache.clear()          # keep at most one LLM resident next to ComfyUI's own models
        _cache[name] = (tok, model)
        return tok, model


def unload_local():
    with _lock:
        _cache.clear()
    try:
        import torch
        torch.cuda.empty_cache()
    except Exception:
        pass


def chat(system, user, backend="openai", model="", temperature=0.6, seed=0,
         max_new_tokens=6000, ollama_url="http://ollama:11434", openai_url=""):
    if backend == "openai":
        base = (openai_url or OPENAI_URL).rstrip("/")
        if not base:
            raise RuntimeError("openai backend: set H3_LLM_URL (OpenAI-compatible /v1 endpoint, "
                               "e.g. the HPC LLM server from minimax-h3/scripts/serve_llm.pbs) "
                               "or fill the node's llm_url field")
        r = requests.post(f"{base}/chat/completions", timeout=3600,
                          headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                          json={"model": model or OPENAI_MODEL, "temperature": float(temperature),
                                "max_tokens": int(max_new_tokens), "seed": int(seed),
                                "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": user}]})
        if r.status_code >= 400:
            raise RuntimeError(f"llm endpoint {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]
    if backend == "ollama":
        r = requests.post(f"{ollama_url.rstrip('/')}/api/chat", timeout=3600, json={
            "model": model or DEFAULT_OLLAMA, "stream": False,
            "options": {"temperature": float(temperature), "num_ctx": 16384,
                        "num_predict": int(max_new_tokens), "seed": int(seed)},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        if r.status_code >= 400:
            raise RuntimeError(f"ollama {r.status_code}: {r.text[:300]}")
        return r.json().get("message", {}).get("content", "")

    import torch
    tok, mdl = _load_local(model or DEFAULT_LOCAL)
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    enc = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt",
                                  return_dict=True)
    enc = {k: v.to("cuda") for k, v in enc.items()}
    gen = dict(max_new_tokens=int(max_new_tokens), do_sample=temperature > 0,
               pad_token_id=tok.eos_token_id)
    if temperature > 0:
        gen.update(temperature=float(temperature), top_p=0.9)
        torch.manual_seed(int(seed))
    with torch.inference_mode():
        out = mdl.generate(**enc, **gen)
    return tok.decode(out[0, enc["input_ids"].shape[1]:], skip_special_tokens=True)


# ── vLLM sidecar launcher (also invoked from the image entrypoint; this copy lives on the
#    PVC so it works without an image rebuild). Idempotent: skips if the port already answers.
def ensure_sidecar():
    import socket
    import subprocess
    if os.environ.get("H3_LLM_SIDECAR", "0") != "1":
        return
    base = "/workspace/data"
    exe = os.path.join(base, "venvs", "vllm", "bin", "vllm")
    port = int(os.environ.get("H3_LLM_PORT", "8001"))
    model = os.environ.get("H3_LLM_MODEL", "Qwen2.5-72B-Instruct-AWQ")
    mdir = os.path.join(os.environ.get("H3_LLM_DIR", os.path.join(base, "llm")), model)
    if not (os.path.exists(exe) and os.path.exists(os.path.join(mdir, "config.json"))):
        print(f"[MiniMax-H3] LLM sidecar not started: missing {exe} or {mdir}")
        return
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            print(f"[MiniMax-H3] LLM sidecar already listening on {port}")
            return
    except OSError:
        pass
    log = open(os.path.join(base, "venvs", "vllm_serve.log"), "wb")   # fresh log per launch
    # Verified 2026-09-10 on the H100 pod: plain "awq" (the awq_marlin repack OOMs on 72B),
    # expandable segments, and no FlashInfer sampler (its JIT needs nvcc, absent in the image).
    cmd = [exe, "serve", mdir, "--served-model-name", model, "--host", "127.0.0.1", "--port", str(port),
           "--gpu-memory-utilization", os.environ.get("H3_LLM_GPU_FRAC", "0.85"),
           "--max-model-len", os.environ.get("H3_LLM_CTX", "24576"), "--max-num-seqs", "4",
           "--quantization", os.environ.get("H3_LLM_QUANT", "awq"), "--dtype", "float16"]
    env = dict(os.environ)
    env.pop("PYTHONUSERBASE", None); env.pop("PIP_USER", None); env.pop("PYTHONPATH", None)
    # ComfyUI sets PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync in os.environ; the sidecar must
    # NOT inherit it (no expandable segments -> ~18 GB fragmentation -> OOM on the 72B).
    env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
    env["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    env.pop("CUDA_MODULE_LOADING", None)
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    print(f"[MiniMax-H3] LLM sidecar starting: {model} on 127.0.0.1:{port} (log: venvs/vllm_serve.log)")


try:
    ensure_sidecar()
except Exception as _e:  # noqa: BLE001
    print(f"[MiniMax-H3] LLM sidecar launch failed: {_e}")
