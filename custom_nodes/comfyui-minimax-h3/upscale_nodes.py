"""SeedVR2 2K upscale of H3 clips on the HPC `sg` queue (scripts/upscale_seedvr2.pbs)."""
import os
import posixpath
import time

import folder_paths
import comfy.utils
import comfy.model_management as mm

from . import hpc_jobs as hpc

CATEGORY = "MiniMax-H3 (HPC)/post"
N = 12


class H3SeedVR2Upscale:
    """Submit up to 12 clips to the HPC in parallel (one PBS job each on queue `sg`),
    wait for all, and download the 2K results into ComfyUI's output folder.
    Outputs keep slot order: up_1 corresponds to clip_1, etc. (empty if unused)."""

    @classmethod
    def INPUT_TYPES(cls):
        opt = {f"clip_{i}": ("STRING", {"default": "", "forceInput": True}) for i in range(1, N + 1)}
        return {"required": {
                    "resolution": ("INT", {"default": 1440, "min": 720, "max": 2160, "step": 16,
                                   "tooltip": "short edge in px (1440 = 2K for 9:16)"}),
                    "batch_size": ("INT", {"default": 33, "min": 5, "max": 121, "step": 4,
                                   "tooltip": "frames per batch, 4n+1"}),
                    "timeout_s": ("INT", {"default": 5400, "min": 300, "max": 43200, "step": 60}),
                    "filename_prefix": ("STRING", {"default": "h3/upscaled/clip"}),
                },
                "optional": opt}

    RETURN_TYPES = tuple(["STRING"] * N + ["STRING"])
    RETURN_NAMES = tuple([f"up_{i}" for i in range(1, N + 1)] + ["report"])
    FUNCTION = "upscale"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def upscale(self, resolution, batch_size, timeout_s, filename_prefix, **kw):
        slots = [(i, kw.get(f"clip_{i}", "")) for i in range(1, N + 1) if kw.get(f"clip_{i}", "")]
        if not slots:
            raise ValueError("connect at least one clip path (from 'MiniMax-H3 Generate')")
        for _, p in slots:
            if not os.path.exists(p):
                raise FileNotFoundError(p)
        if batch_size % 4 != 1:
            batch_size = (batch_size // 4) * 4 + 1

        client = hpc.connect()
        sftp = client.open_sftp()
        jobs, remote_out = {}, {}
        try:
            pbar = comfy.utils.ProgressBar(100)
            for i, p in slots:
                remote_in = hpc.stage_file(sftp, p, tag=f"c{i}_{int(time.time())}")
                jid = hpc.qsub(client, "scripts/upscale_seedvr2.pbs",
                               {"IN": remote_in, "RES": resolution, "BATCH": batch_size})
                jobs[jid] = i
                base = posixpath.basename(remote_in)[:-4]
                remote_out[jid] = posixpath.join(hpc.H3_ROOT, "outputs", "upscaled",
                                                 f"{base}_seedvr2_{resolution}p.mp4")
                print(f"[SeedVR2/HPC] clip_{i} -> {jid}")
            pbar.update_absolute(5)

            def on_update(states, elapsed):
                fin = sum(1 for v in states.values() if isinstance(v, int))
                pbar.update_absolute(5 + int(85 * fin / len(states)))
                print(f"[SeedVR2/HPC] {int(elapsed)}s {states}")

            exits = hpc.wait_jobs(client, jobs, timeout=timeout_s, on_update=on_update,
                                  should_abort=mm.processing_interrupted)

            outs = [""] * N
            full_dir, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                filename_prefix, folder_paths.get_output_directory())
            report = []
            for jid, i in jobs.items():
                if exits.get(jid) not in (0, None):
                    raise RuntimeError(f"clip_{i} job {jid} failed with exit {exits[jid]} "
                                       f"(see {hpc.H3_ROOT}/logs/{jid.split('.')[0]}.sjms.OU)")
                local = os.path.join(full_dir, f"{filename}{i}_{counter:05}_seedvr2_{resolution}p.mp4")
                hpc.sftp_get(sftp, remote_out[jid], local)
                outs[i - 1] = local
                report.append(f"clip_{i}: {jid} -> {os.path.basename(local)}")
            pbar.update_absolute(100)
            ui = [{"filename": os.path.basename(o), "subfolder": subfolder, "type": "output"}
                  for o in outs if o]
            return {"ui": {"images": ui, "animated": (True,)},
                    "result": tuple(outs + ["\n".join(report)])}
        finally:
            sftp.close()
            client.close()


NODE_CLASS_MAPPINGS = {"MiniMaxH3_SeedVR2Upscale": H3SeedVR2Upscale}
NODE_DISPLAY_NAME_MAPPINGS = {"MiniMaxH3_SeedVR2Upscale": "H3 SeedVR2 Upscale 2K (HPC sg)"}
