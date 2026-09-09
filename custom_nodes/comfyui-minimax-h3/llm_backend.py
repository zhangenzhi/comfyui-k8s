"""LLM backends for the drama nodes.

  ollama : the in-cluster Ollama service (CPU-only here, ~1.5 tok/s for 7B -> slow)
  local  : transformers on the ComfyUI pod's own GPU (idle while H3 renders on the HPC).
           Weights live on the PVC: <H3_LLM_DIR>/<model>  (default /workspace/data/llm).
           Download once with:  fetch-llm Qwen/Qwen2.5-14B-Instruct
"""
import os
import threading

import requests

LLM_DIR = os.environ.get("H3_LLM_DIR", "/workspace/data/llm")
DEFAULT_LOCAL = os.environ.get("H3_LOCAL_LLM", "Qwen2.5-14B-Instruct")
DEFAULT_OLLAMA = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
BACKENDS = ["local", "ollama"]

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


def chat(system, user, backend="local", model="", temperature=0.6, seed=0,
         max_new_tokens=6000, ollama_url="http://ollama:11434"):
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
