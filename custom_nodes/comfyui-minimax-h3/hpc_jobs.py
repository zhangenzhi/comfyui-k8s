"""Push-mode HPC jobs from the ComfyUI pod (same pattern as ffformer/deploy/hpc_backend.py):
SFTP the clip to lustre, qsub a PBS script, poll qstat, SFTP the result back.

Env (set in the Deployment): HPC_HOST, HPC_USER, HPC_KEY_PATH (Secret comfyui-hpc-ssh),
H3_ROOT (minimax-h3 checkout on lustre). Fallback key: /workspace/data/.ssh/key.
"""
import os
import posixpath
import re
import time
import uuid

HPC_HOST = os.environ.get("HPC_HOST", "172.31.20.1")
HPC_USER = os.environ.get("HPC_USER", "c30746")
H3_ROOT = os.environ.get("H3_ROOT", "/lustre1/work/c30636/test/minimax-h3")
QSUB = os.environ.get("HPC_QSUB", "/opt/pbs/bin/qsub")
QSTAT = os.environ.get("HPC_QSTAT", "/opt/pbs/bin/qstat")
QDEL = os.environ.get("HPC_QDEL", "/opt/pbs/bin/qdel")
STAGE = posixpath.join(H3_ROOT, "deploy_jobs", "comfyui")


def key_path():
    for p in (os.environ.get("HPC_KEY_PATH", ""), "/workspace/data/.ssh/key"):
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError("no HPC SSH key: mount Secret comfyui-hpc-ssh at HPC_KEY_PATH "
                            "or put the key at /workspace/data/.ssh/key")


def connect(retries=3):
    import paramiko
    last = None
    for _ in range(retries):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(HPC_HOST, username=HPC_USER, key_filename=key_path(), timeout=20,
                      allow_agent=False, look_for_keys=False, banner_timeout=30)
            return c
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"SSH to {HPC_USER}@{HPC_HOST} failed: {last}")


def run(client, cmd, timeout=120):
    _, out, err = client.exec_command(cmd, timeout=timeout)
    o, e = out.read().decode(errors="replace"), err.read().decode(errors="replace")
    return out.channel.recv_exit_status(), o, e


def stage_file(sftp, local_path, tag=""):
    base = os.path.basename(local_path)
    remote = posixpath.join(STAGE, f"{tag or uuid.uuid4().hex[:8]}_{base}")
    try:
        sftp.stat(STAGE)
    except FileNotFoundError:
        _mkdirs(sftp, STAGE)
    sftp.put(local_path, remote)
    return remote


def _mkdirs(sftp, path):
    parts = path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def qsub(client, script_rel, env_vars, workdir=H3_ROOT):
    kv = ",".join(f"{k}={v}" for k, v in env_vars.items())
    rc, out, err = run(client, f"cd {workdir} && {QSUB} -v '{kv}' {script_rel}")
    if rc != 0 or not out.strip():
        raise RuntimeError(f"qsub failed rc={rc}: {(err or out).strip()[:400]}")
    return out.strip().splitlines()[-1]        # e.g. 637171.sjms


def job_state(client, job_id):
    """Return (state, exit_status): state in Q/R/E/F/H/..., exit_status int or None."""
    rc, out, _ = run(client, f"{QSTAT} -x -f {job_id} 2>/dev/null")
    m = re.search(r"job_state = (\w)", out)
    x = re.search(r"Exit_status = (-?\d+)", out)
    return (m.group(1) if m else "?"), (int(x.group(1)) if x else None)


def qdel(client, job_id):
    try:
        run(client, f"{QDEL} {job_id}")
    except Exception:
        pass


def wait_jobs(client, jobs, timeout=3600, poll=20, on_update=None, should_abort=None):
    """jobs: dict job_id -> label. Blocks until all finished. Returns {job_id: exit_status}."""
    t0 = time.time()
    done = {}
    while len(done) < len(jobs):
        if should_abort and should_abort():
            for j in jobs:
                if j not in done:
                    qdel(client, j)
            raise RuntimeError("cancelled")
        states = {}
        for j in jobs:
            if j in done:
                continue
            st, ex = job_state(client, j)
            states[j] = st
            if st == "F":
                done[j] = ex
        if on_update:
            on_update({jobs[j]: (done.get(j, states.get(j))) for j in jobs}, time.time() - t0)
        if time.time() - t0 > timeout:
            for j in jobs:
                if j not in done:
                    qdel(client, j)
            raise RuntimeError(f"HPC jobs timed out after {timeout}s: {states}")
        if len(done) < len(jobs):
            time.sleep(poll)
    return done


def sftp_get(sftp, remote, local):
    os.makedirs(os.path.dirname(local), exist_ok=True)
    sftp.get(remote, local)
    return local
