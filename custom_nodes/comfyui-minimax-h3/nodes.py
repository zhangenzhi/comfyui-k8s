"""ComfyUI nodes for MiniMax-H3 (video + stereo audio) served by SGLang on the HPC."""
import io
import os
import json

import numpy as np
import requests
import torch
from PIL import Image

import folder_paths
import comfy.utils
import comfy.model_management as mm

from . import h3_client as h3
from . import llm_backend as llm

CATEGORY = "MiniMax-H3 (HPC)"
ASPECTS = ["16:9", "9:16", "1:1", "4:3", "3:4", "auto"]


# ── helpers ────────────────────────────────────────────────────────────
def _png_bytes(image):
    """IMAGE tensor [B,H,W,C] in 0..1 -> PNG bytes of the first frame."""
    arr = (image[0].detach().cpu().numpy().clip(0, 1) * 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _keyframe(image, frame_index):
    return {"type": "image", "role": "keyframe", "frame_index": frame_index,
            "uri": h3._image_condition("keyframe", _png_bytes(image))["data"]}


def _reference_image(image):
    return {"type": "image", "role": "reference",
            "uri": h3._image_condition("reference", _png_bytes(image))["data"]}


def _output_path(prefix):
    full_dir, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
        prefix, folder_paths.get_output_directory())
    name = f"{filename}_{counter:05}_.mp4"
    return os.path.join(full_dir, name), name, subfolder


def _as_video(path):
    try:
        from comfy_api.latest import InputImpl
        return InputImpl.VideoFromFile(path)
    except Exception:
        try:
            from comfy_api.input_impl import VideoFromFile
            return VideoFromFile(path)
        except Exception:
            return None


# ── nodes ──────────────────────────────────────────────────────────────
class H3Generate:
    """Submit one MiniMax-H3 job (t2va / fl2va / ref2va) to the HPC SGLang server,
    wait for it, and save the MP4 into ComfyUI's output folder."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "",
                           "tooltip": "H3 prompt (integrated_multimodal_description / "
                                      "overall_soundscape / non_diegetic_music). "
                                      "Use the 'H3 Prompt Rewrite' node to produce one."}),
                "task": (["t2va", "fl2va", "ref2va"], {"default": "t2va"}),
                "duration_seconds": ("FLOAT", {"default": 5.0, "min": 4.0, "max": 15.0, "step": 0.5}),
                "aspect_ratio": (ASPECTS, {"default": "16:9"}),
                "seed": ("INT", {"default": 1101, "min": 0, "max": 2**31 - 1,
                         "control_after_generate": True}),
                "steps": ("INT", {"default": 50, "min": 1, "max": 200}),
                "short_edge": ("INT", {"default": 768, "min": 256, "max": 1080, "step": 16}),
                "flow_shift": ("FLOAT", {"default": 12.0, "min": 0.0, "max": 30.0, "step": 0.5}),
                "audio_flow_shift": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 30.0, "step": 0.5}),
                "filename_prefix": ("STRING", {"default": "h3/h3"}),
                "timeout_s": ("INT", {"default": 1800, "min": 60, "max": 21600, "step": 60,
                              "tooltip": "Give up (and cancel on the server) after this long."}),
            },
            "optional": {
                "first_frame": ("IMAGE", {"tooltip": "fl2va: clip starts on this image"}),
                "last_frame": ("IMAGE", {"tooltip": "fl2va: clip ends on this image"}),
                "reference_image": ("IMAGE", {"tooltip": "ref2va: identity/style reference"}),
                "endpoint": ("STRING", {"default": "",
                             "tooltip": "host:port of the SGLang server. Empty = env H3_ENDPOINT "
                                        "(or SSH discovery if the HPC key is mounted)."}),
            },
        }

    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "video_path", "video_id")
    FUNCTION = "generate"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def generate(self, prompt, task, duration_seconds, aspect_ratio, seed, steps, short_edge,
                 flow_shift, audio_flow_shift, filename_prefix, timeout_s,
                 first_frame=None, last_frame=None, reference_image=None, endpoint=""):
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is empty")

        conditions = []
        if first_frame is not None:
            conditions.append(_keyframe(first_frame, 0))
        if last_frame is not None:
            conditions.append(_keyframe(last_frame, -1))
        if reference_image is not None:
            conditions.append(_reference_image(reference_image))
        if task == "t2va" and conditions:
            raise ValueError("t2va takes no image inputs; disconnect them or pick fl2va/ref2va")
        if task == "fl2va" and not (first_frame is not None or last_frame is not None):
            raise ValueError("fl2va needs first_frame and/or last_frame")
        if task == "ref2va" and not conditions:
            raise ValueError("ref2va needs reference_image")

        body = h3.build_request(
            prompt, task=task, seconds=duration_seconds, aspect_ratio=aspect_ratio,
            seed=seed, steps=steps, flow_shift=flow_shift, audio_flow_shift=audio_flow_shift,
            short_edge=short_edge, conditions=conditions)
        body["target"]["duration_seconds"] = float(duration_seconds)

        base, vid = h3.submit(body, endpoint)
        print(f"[MiniMax-H3] submitted {task} id={vid} on {base}")

        pbar = comfy.utils.ProgressBar(100)

        def on_progress(pct, st):
            pbar.update_absolute(max(0, min(100, pct)))
            print(f"[MiniMax-H3] {vid} {st} {pct}%")

        h3.wait(base, vid, timeout=timeout_s, on_progress=on_progress,
                should_abort=mm.processing_interrupted)
        pbar.update_absolute(100)

        path, name, subfolder = _output_path(filename_prefix)
        h3.download(base, vid, path)
        print(f"[MiniMax-H3] saved {path}")

        return {
            "ui": {"images": [{"filename": name, "subfolder": subfolder, "type": "output"}],
                   "animated": (True,)},
            "result": (_as_video(path), path, vid),
        }


class H3ServerStatus:
    """Health of the HPC SGLang server + how many jobs it currently holds."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"refresh": ("INT", {"default": 0, "min": 0, "max": 1_000_000,
                                                 "tooltip": "bump to re-run"})},
                "optional": {"endpoint": ("STRING", {"default": ""})}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "endpoint")
    FUNCTION = "check"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def check(self, refresh, endpoint=""):
        try:
            base, hj = h3.health(endpoint)
        except Exception as e:  # noqa: BLE001
            msg = f"H3 server unreachable: {e}"
            return {"ui": {"text": [msg]}, "result": (msg, "")}
        jobs = ""
        try:
            r = requests.get(f"{base}/v1/videos", timeout=15)
            if r.ok:
                data = r.json().get("data", [])
                active = [d for d in data if str(d.get("status", "")).lower()
                          not in h3.TERMINAL_OK | h3.TERMINAL_FAIL]
                jobs = f", jobs total={len(data)} active={len(active)}"
        except Exception:
            pass
        msg = f"{base} health={hj}{jobs}"
        return {"ui": {"text": [msg]}, "result": (msg, base)}


REWRITE_SYSTEM = """You rewrite a user's short video idea into a MiniMax-H3 generation prompt. Output ONLY the final prompt text, nothing else, no markdown, no explanations.

FORMAT (exactly these three fields, each on its own paragraph separated by one blank line):
integrated_multimodal_description: [Shot 1] ... [Shot 2] At 00:0S.SSS, the camera cuts to ... 
overall_soundscape: ...
non_diegetic_music: ...

RULES for integrated_multimodal_description:
- Written in English. Begin [Shot 1] with the overall style (e.g. Live-action, cinematic / 2D-animated / 3D CG / claymation / watercolor / vintage film) and the initial composition and frame orientation.
- Describe only what is visible or audible, along the timeline: subject appearance and position, scene and props, actions and reactions, cuts, speech, and diegetic sounds.
- Do not put a timestamp on the first shot. Each later shot starts with "[Shot N] At MM:SS.mmm, the camera cuts to ..." with strictly increasing times inside the total duration. Keep the number of shots reasonable for the duration (roughly one shot per 2.5 to 5 seconds).
- Camera motion is written as a natural action using this vocabulary: zoom in/out, push in/pull out, pan left/right, truck left/right, tilt up/down, pedestal up/down, arc shot, tracking shot, static shot, shake slightly/strongly, POV, roll clockwise/counterclockwise; optionally add "with small/large amplitude" and "at slow/fast speed". Example: "The camera pushes in with small amplitude at slow speed toward her hands."
- Speakers get stable IDs (S1), (S2)... introduced with voice traits, e.g. "A man with a low, unhurried voice (S1) says: <d>[LANG] line</d>". Dialogue text goes inside <d>[Language] ...</d>, in the requested dialogue language. For voiceover use exactly "says in an off-screen voiceover" and state that the visible character's lips remain completely closed.
- Visible on-screen text is quoted verbatim in double quotes.
RULES for overall_soundscape: 1 to 4 English sentences in one paragraph: ambient sound, physical action sounds, non-verbal human sounds. No dialogue, no music.
RULES for non_diegetic_music: 1 to 3 English sentences: instrumentation, tempo, rhythm, dynamics. No mood adjectives, no emotional explanation.
If the task is fl2va, the very first line must be:
"How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot N) aligns with the D.DD-second mark of the target video."
where N is the last shot index and D.DD is the total duration with two decimals, followed by one blank line, then the three fields.
"""


class H3PromptRewrite:
    """Turn a plain idea (any language) into an H3-formatted prompt using the
    in-cluster Ollama LLM (stand-in for MiniMax's closed H3-Context-IR)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "idea": ("STRING", {"multiline": True, "default": ""}),
                "task": (["t2va", "fl2va"], {"default": "t2va"}),
                "duration_seconds": ("FLOAT", {"default": 5.0, "min": 4.0, "max": 15.0, "step": 0.5}),
                "aspect_ratio": (ASPECTS, {"default": "16:9"}),
                "dialogue_language": (["Chinese", "English", "Japanese", "none"], {"default": "Chinese"}),
                "style": ("STRING", {"default": "Live-action, cinematic"}),
                "backend": (llm.BACKENDS, {"default": llm.DEFAULT_BACKEND}),
                "model": ("STRING", {"default": llm.OPENAI_MODEL if llm.DEFAULT_BACKEND == "openai" else llm.DEFAULT_OLLAMA}),
                "temperature": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 2.0, "step": 0.05}),
            },
            "optional": {
                "ollama_url": ("STRING", {"default": os.environ.get("OLLAMA_URL", "http://ollama:11434")}),
                "llm_url": ("STRING", {"default": llm.OPENAI_URL}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("prompt",)
    FUNCTION = "rewrite"
    CATEGORY = CATEGORY

    def rewrite(self, idea, task, duration_seconds, aspect_ratio, dialogue_language, style,
                backend, model, temperature, ollama_url="http://ollama:11434", llm_url=""):
        idea = (idea or "").strip()
        if not idea:
            raise ValueError("idea is empty")
        user = (f"Task: {task}\nTotal duration: {duration_seconds:.2f} seconds\n"
                f"Frame: {aspect_ratio}\nStyle: {style}\n"
                f"Dialogue language: {dialogue_language}\n\nUser idea:\n{idea}")
        text = llm.chat(REWRITE_SYSTEM, user, backend=backend, model=model, temperature=temperature,
                        seed=0, max_new_tokens=1500, ollama_url=ollama_url, openai_url=llm_url).strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if "integrated_multimodal_description" not in text:
            raise RuntimeError(f"LLM did not return an H3 prompt:\n{text[:500]}")
        return (text,)


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3_Generate": H3Generate,
    "MiniMaxH3_PromptRewrite": H3PromptRewrite,
    "MiniMaxH3_ServerStatus": H3ServerStatus,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3_Generate": "MiniMax-H3 Generate (HPC)",
    "MiniMaxH3_PromptRewrite": "H3 Prompt Rewrite (Ollama)",
    "MiniMaxH3_ServerStatus": "H3 Server Status",
}

from .episode_nodes import (NODE_CLASS_MAPPINGS as _EP, NODE_DISPLAY_NAME_MAPPINGS as _EPN)  # noqa: E402
NODE_CLASS_MAPPINGS.update(_EP)
NODE_DISPLAY_NAME_MAPPINGS.update(_EPN)
