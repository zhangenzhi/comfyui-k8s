"""Thin HTTP client for the MiniMax-H3 SGLang server (OpenAI-style /v1/videos).

Request shape mirrors minimax-h3/scripts/t2va_request.sh, which is verified
against the running server. Endpoint discovery order:
  1. explicit `endpoint` argument
  2. env H3_ENDPOINT (set in the K8s Deployment)
  3. optional SSH to the HPC login node to read logs/server_endpoint.txt
"""
import base64
import os
import time

import requests

DEFAULT_TIMEOUT = int(os.environ.get("H3_TIMEOUT", "1800"))
POLL_INTERVAL = float(os.environ.get("H3_POLL_INTERVAL", "3"))
MODEL_NAME = os.environ.get("H3_MODEL", "MiniMaxAI/MiniMax-H3")

TERMINAL_OK = {"completed", "succeeded", "success", "done"}
TERMINAL_FAIL = {"failed", "error", "cancelled", "canceled"}


class H3Error(RuntimeError):
    pass


def _norm(endpoint):
    endpoint = (endpoint or "").strip()
    if not endpoint:
        raise H3Error("No H3 endpoint. Set H3_ENDPOINT on the pod or fill the node's "
                      "`endpoint` field (host:port of the SGLang server).")
    if not endpoint.startswith("http"):
        endpoint = "http://" + endpoint
    return endpoint.rstrip("/")


_cache = {"ep": None, "t": 0.0}


def _alive(base, timeout=3):
    try:
        r = requests.get(f"{base}/health", timeout=timeout)
        return r.ok
    except Exception:
        return False


def discover_endpoint_via_ssh():
    """Find the running H3 server on the HPC over SSH (needs Secret comfyui-hpc-ssh):
    1. a running PBS job named h3_serve -> its exec_host -> IP:PORT
    2. else <H3_ROOT>/logs/server_endpoint.txt (may be stale after a walltime kill)
    Returns 'ip:port' or None."""
    key = os.environ.get("HPC_KEY_PATH", "")
    if not os.path.exists(key):
        key = "/workspace/data/.ssh/key"
    host = os.environ.get("HPC_HOST", "")
    user = os.environ.get("HPC_USER", "")
    root = os.environ.get("H3_ROOT", "")
    port = os.environ.get("H3_PORT", "30010")
    if not (os.path.exists(key) and host and user and root):
        return None
    try:
        import paramiko
    except ImportError:
        return None
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, key_filename=key, timeout=15,
                   allow_agent=False, look_for_keys=False)
    try:
        cmd = (
            "j=$(/opt/pbs/bin/qstat -u $USER 2>/dev/null | awk '/h3_serve/ && $10==\"R\" {print $1; exit}'); "
            "if [ -n \"$j\" ]; then h=$(/opt/pbs/bin/qstat -f $j | tr -d '\\n\\t ' | grep -o 'exec_host=[^/]*' | cut -d= -f2); "
            "  ip=$(getent hosts $h | awk '{print $1}'); [ -n \"$ip\" ] && printf '%s:%s' \"$ip\" \"" + port + "\"; "
            f"else ep=$(cat {root}/logs/server_endpoint.txt 2>/dev/null); "
            "  [ -n \"$ep\" ] && printf '%s:%s' \"$(getent hosts ${ep%%:*} | awk '{print $1}')\" \"${ep##*:}\"; fi")
        _, out, _ = client.exec_command(cmd, timeout=30)
        ep = out.read().decode().strip()
        return ep or None
    finally:
        client.close()


def resolve_endpoint(endpoint=None):
    """Explicit endpoint > env H3_ENDPOINT (if alive) > cached discovery > SSH discovery."""
    ep = (endpoint or "").strip()
    if ep:
        return _norm(ep)
    env_ep = os.environ.get("H3_ENDPOINT", "").strip()
    if env_ep and _alive(_norm(env_ep)):
        return _norm(env_ep)
    if _cache["ep"] and time.time() - _cache["t"] < 120 and _alive(_norm(_cache["ep"])):
        return _norm(_cache["ep"])
    found = discover_endpoint_via_ssh()
    if found:
        _cache.update(ep=found, t=time.time())
        print(f"[MiniMax-H3] endpoint discovered via HPC: {found}")
        return _norm(found)
    if env_ep:
        raise H3Error(f"H3 server at {env_ep} is not responding and no running h3_serve job was found on the HPC. "
                      "Start it with: qsub scripts/serve_h3.pbs (in minimax-h3)")
    return _norm("")


def health(endpoint=None, timeout=10):
    base = resolve_endpoint(endpoint)
    r = requests.get(f"{base}/health", timeout=timeout)
    r.raise_for_status()
    return base, r.json()


def _image_condition(role, png_bytes):
    return {"type": "image", "role": role,
            "data": "data:image/png;base64," + base64.b64encode(png_bytes).decode()}


def build_request(prompt, task="t2va", seconds=5, aspect_ratio="16:9", seed=1101,
                  steps=50, flow_shift=12.0, audio_flow_shift=3.0, short_edge=768,
                  conditions=None, num_outputs=1):
    return {
        "model": MODEL_NAME,
        "prompt": prompt,
        "seconds": int(seconds),
        "task": task,
        "conditions": conditions or [],
        "target": {"short_edge": int(short_edge), "aspect_ratio": aspect_ratio,
                   "duration_seconds": int(seconds)},
        "num_outputs_per_prompt": int(num_outputs),
        "num_inference_steps": int(steps),
        "flow_shift": float(flow_shift),
        "audio_flow_shift": float(audio_flow_shift),
        "seed": int(seed),
    }


def submit(body, endpoint=None):
    base = resolve_endpoint(endpoint)
    r = requests.post(f"{base}/v1/videos", json=body, timeout=60)
    if r.status_code >= 400:
        raise H3Error(f"submit failed HTTP {r.status_code}: {r.text[:500]}")
    vid = r.json().get("id")
    if not vid:
        raise H3Error(f"submit returned no id: {r.text[:300]}")
    return base, vid


def status(base, vid):
    r = requests.get(f"{base}/v1/videos/{vid}", timeout=30)
    r.raise_for_status()
    return r.json()


def cancel(base, vid):
    try:
        requests.delete(f"{base}/v1/videos/{vid}", timeout=15)
    except Exception:
        pass


def wait(base, vid, timeout=DEFAULT_TIMEOUT, on_progress=None, should_abort=None):
    """Poll until terminal. on_progress(percent:int, status:str); should_abort() -> bool."""
    t0 = time.time()
    last = None
    while True:
        if should_abort and should_abort():
            cancel(base, vid)
            raise H3Error("cancelled")
        s = status(base, vid)
        st = str(s.get("status", "")).lower()
        prog = s.get("progress")
        if on_progress and (st, prog) != last:
            try:
                on_progress(int(prog or 0), st)
            except Exception:
                pass
            last = (st, prog)
        if st in TERMINAL_OK:
            return s
        if st in TERMINAL_FAIL:
            raise H3Error(f"generation failed: {s.get('error') or s}")
        if time.time() - t0 > timeout:
            cancel(base, vid)
            raise H3Error(f"timed out after {timeout}s (status={st})")
        time.sleep(POLL_INTERVAL)


def download(base, vid, dest_path):
    with requests.get(f"{base}/v1/videos/{vid}/content", stream=True, timeout=300) as r:
        r.raise_for_status()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest_path
