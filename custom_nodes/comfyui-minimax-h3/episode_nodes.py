"""Episode-level nodes: LLM shot plan -> per-clip H3 prompts -> concat + subtitles."""
import json
import os
import re
import shutil
import subprocess
import tempfile

import requests

import folder_paths

from . import bible
from . import llm_backend as llm

CATEGORY = "MiniMax-H3 (HPC)/drama"
PLANNER_SYSTEM_PATH = os.path.join(os.path.dirname(__file__), "drama_system_prompt.md")


def _load_system_prompt(path=""):
    p = path or os.environ.get("H3_DRAMA_SYSTEM_PROMPT", "") or PLANNER_SYSTEM_PATH
    with open(p, encoding="utf-8") as f:
        s = f.read()
    # Only the "## 系统提示词" section is the actual system prompt.
    m = re.search(r"## 系统提示词.*?(?=\n## 与 v1|\Z)", s, re.S)
    return (m.group(0) if m else s).strip()


EXAMPLE_PLAN_PATH = os.path.join(os.path.dirname(__file__), "example_shot_plan.json")


def _example_plan_text(max_clips=2):
    try:
        with open(EXAMPLE_PLAN_PATH, encoding="utf-8") as f:
            ex = json.load(f)
        ex["clips"] = ex["clips"][:max_clips]
        return json.dumps(ex, ensure_ascii=False, indent=1)
    except Exception:
        return ""


def validate_plan(plan, min_clips=3):
    """Return a list of human-readable problems (empty = OK)."""
    probs = []
    clips = plan.get("clips") or []
    if len(clips) < min_clips:
        probs.append(f"只有 {len(clips)} 个片段，至少要 {min_clips} 个")
    for c in clips:
        i = c.get("index", "?")
        d = float(c.get("duration", 0) or 0)
        if not 4 <= d <= 15:
            probs.append(f"片段 {i} 时长 {d}s 不在 4–15 s 内")
        shots = c.get("shots") or []
        if len(shots) < 2:
            probs.append(f"片段 {i} 只有 {len(shots)} 个镜头，需要 2–4 个")
        last = -1.0
        for n, sh in enumerate(shots, 1):
            st = float(sh.get("start", 0) or 0)
            if n == 1 and st != 0:
                probs.append(f"片段 {i} 镜头 1 的 start 必须是 0")
            if st <= last and n > 1:
                probs.append(f"片段 {i} 镜头 {n} 的 start 没有递增")
            last = st
            if not sh.get("action_en"):
                probs.append(f"片段 {i} 镜头 {n} 缺 action_en")
            for dlg in sh.get("dialogue") or []:
                t = dlg.get("text_zh", "")
                nxt = float(shots[n]["start"]) if n < len(shots) else d
                if len(t) > max(6, (nxt - st) * bible.CHARS_PER_SECOND * 1.3 + 2):   # ~25% tolerance
                    probs.append(f"片段 {i} 镜头 {n} 台词太长（{len(t)} 字，镜头只有 {nxt - st:.1f} s）")
        total = sum(len(dl.get("text_zh", "")) for sh in shots for dl in (sh.get("dialogue") or []))
        if total > 45:
            probs.append(f"片段 {i} 台词总字数 {total} > 45")
    return probs


def _extract_json(text):
    tag = text.find("【分镜计划JSON】")
    body = text[tag + len("【分镜计划JSON】"):] if tag >= 0 else text
    body = body.strip().strip("`")
    if body.lower().startswith("json"):
        body = body[4:]
    a, b = body.find("{"), body.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("no JSON object found in LLM output")
    return json.loads(body[a:b + 1])


def _fmt_ts(sec):
    m = int(sec // 60)
    s = sec - m * 60
    return f"{m:02d}:{s:06.3f}"


def _cap(s):
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


def _wrap_zh(text, width=14):
    """libass in ffmpeg 4.4 does not line-break CJK text without spaces: pre-wrap it.
    Breaks preferably right after Chinese punctuation, never right before it."""
    text = text.strip()
    if len(text) <= width:
        return text
    punct = "，。？！；：、》」”）"   # closing marks must not start a line
    out, cur = [], ""
    for i, ch in enumerate(text):
        cur += ch
        nxt = text[i + 1] if i + 1 < len(text) else ""
        inside_word = ch.isascii() and ch.isalnum() and nxt.isascii() and nxt.isalnum()
        if (ch in punct and len(cur) >= width - 5) or (len(cur) >= width and nxt not in punct and not inside_word):
            out.append(cur); cur = ""
    if cur:
        out.append(cur)
    return "\n".join(out[:3])   # at most 3 lines on screen


def _srt_ts(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}"


# ── nodes ──────────────────────────────────────────────────────────────
class H3EpisodePlanner:
    """Write one episode with the drama system prompt (v2) on Ollama and return
    the human script + the machine shot-plan JSON."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "request": ("STRING", {"multiline": True,
                            "default": "第一集《深夜实验室，魔鬼导师撕了我的论文》：反派师姐白天抢走超分辨仪机时，女主深夜偷用男主权限被抓。"}),
                "episode": ("INT", {"default": 1, "min": 1, "max": 999}),
                "clips": ("INT", {"default": 6, "min": 3, "max": 8, "tooltip": "片段数（每段 8–15 s）"}),
                "backend": (llm.BACKENDS, {"default": llm.DEFAULT_BACKEND,
                            "tooltip": "local = 常驻在 pod 的 H100 上（默认）；openai = HPC/云端 OpenAI 兼容接口(H3_LLM_URL)；"
                                       "ollama = 集群 CPU 服务（慢）"}),
                "model": ("STRING", {"default": llm.DEFAULT_LOCAL if llm.DEFAULT_BACKEND == "local" else (llm.OPENAI_MODEL if llm.DEFAULT_BACKEND == "openai" else llm.DEFAULT_OLLAMA),
                          "tooltip": "openai/ollama: 服务端模型名；local: /workspace/data/llm/<name>"}),
                "temperature": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 2.0, "step": 0.05}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 2**31 - 1, "control_after_generate": True}),
            },
            "optional": {
                "system_prompt_path": ("STRING", {"default": "", "tooltip": "留空用内置 v2；可指向 PVC 上自定义的 md"}),
                "ollama_url": ("STRING", {"default": os.environ.get("OLLAMA_URL", "http://ollama:11434")}),
                "llm_url": ("STRING", {"default": llm.OPENAI_URL,
                            "tooltip": "openai 后端的 /v1 地址，留空用 H3_LLM_URL"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("script_zh", "shot_plan_json", "title")
    FUNCTION = "plan"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def plan(self, request, episode, clips, backend, model, temperature, seed,
             system_prompt_path="", ollama_url="http://ollama:11434", llm_url=""):
        system = _load_system_prompt(system_prompt_path)
        ex = _example_plan_text()
        if ex:
            system += ("\n\n### 7. 完整示例（第一集前两段的分镜计划 JSON，严格照此粒度：每段 2–4 个镜头、"
                       "每镜头有 start / shot_type / characters / action_en / camera_en / dialogue / sfx_en）\n" + ex)
        user = (f"请写第 {episode} 集，切成 {clips} 个片段。需求：{request.strip()}\n"
                "先输出【剧本】，再输出【分镜计划JSON】和 JSON 本体。characters 里必须包含 shen 和 gu，"
                "外形与声音描述照抄人物圣经。action_en 里用 she/he 或 the woman/the man 指代，不要写名字。"
                "每个片段必须有 2–4 个镜头，镜头 1 的 start 为 0，后续镜头 start 递增；台词按语速 4 字/秒控制长度。")
        text = llm.chat(system, user, backend=backend, model=model, temperature=temperature,
                        seed=seed, max_new_tokens=12000, ollama_url=ollama_url, openai_url=llm_url)
        try:
            plan = _extract_json(text)
        except Exception as e:  # noqa: BLE001
            # One repair round for syntax errors: hand the broken JSON + parser message back.
            print(f"[H3 planner] JSON parse failed ({e}); asking the model to fix the syntax")
            tag = text.find("【分镜计划JSON】")
            broken = text[tag + len("【分镜计划JSON】"):] if tag >= 0 else text
            fix_user = (f"下面这段 JSON 无法解析，解析器报错：{e}\n"
                        "请只输出修正后的完整 JSON（不要剧本、不要解释、不要代码围栏），内容保持不变，只修语法"
                        "（未闭合的引号/括号、多余逗号、字符串里的英文双引号要转义）：\n" + broken.strip())
            text2 = llm.chat(system, fix_user, backend=backend, model=model, temperature=0.1,
                             seed=seed + 7, max_new_tokens=12000, ollama_url=ollama_url, openai_url=llm_url)
            try:
                plan = _extract_json(text2)
            except Exception as e2:  # noqa: BLE001
                raise RuntimeError(f"shot plan JSON invalid after repair ({e2}). Raw output tail:\n{text2[-1500:]}")
        probs = validate_plan(plan)
        if probs:
            print(f"[H3 planner] {len(probs)} problems, asking the model to repair: {probs[:6]}")
            fix_user = ("下面是你刚才输出的分镜计划 JSON，它违反了这些生成规则：\n- " + "\n- ".join(probs[:12]) +
                        "\n请只输出修正后的完整 JSON（不要剧本、不要解释、不要代码围栏），保持同样的结构：\n" +
                        json.dumps(plan, ensure_ascii=False))
            text2 = llm.chat(system, fix_user, backend=backend, model=model, temperature=max(0.2, temperature - 0.2),
                             seed=seed + 1, max_new_tokens=12000, ollama_url=ollama_url, openai_url=llm_url)
            try:
                plan2 = _extract_json(text2)
                if len(validate_plan(plan2)) < len(probs):
                    plan = plan2
                    probs = validate_plan(plan)
            except Exception as e:  # noqa: BLE001
                print(f"[H3 planner] repair round failed to parse: {e}")
        if probs:
            print(f"[H3 planner] remaining problems: {probs}")
        plan["_problems"] = probs
        plan.setdefault("episode", episode)
        # Force the bible in, whatever the model wrote.
        chars = plan.setdefault("characters", {})
        for cid, c in bible.CHARACTERS.items():
            chars.setdefault(cid, {}).update(c)
        plan.setdefault("style_en", bible.STYLE_EN)
        plan.setdefault("aspect_ratio", "9:16")
        script = text[:text.find("【分镜计划JSON】")].strip() if "【分镜计划JSON】" in text else text
        pj = json.dumps(plan, ensure_ascii=False, indent=1)
        return {"ui": {"text": [script[:4000]]}, "result": (script, pj, plan.get("title", ""))}


class H3ClipPromptBuilder:
    """Deterministically turn clip N of a shot plan into an H3 prompt.
    Also emits the clip duration, an SRT of the Chinese dialogue, and the count."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shot_plan_json": ("STRING", {"multiline": True, "default": ""}),
                "clip_index": ("INT", {"default": 1, "min": 1, "max": 64}),
                "task": (["t2va", "fl2va"], {"default": "t2va",
                         "tooltip": "fl2va 时自动加首行对齐说明（Picture 1 = 首帧）"}),
                "last_frame_given": ("BOOLEAN", {"default": False,
                                     "tooltip": "fl2va 且同时提供末帧图时勾选"}),
            },
        }

    RETURN_TYPES = ("STRING", "FLOAT", "STRING", "INT", "STRING")
    RETURN_NAMES = ("h3_prompt", "duration_seconds", "subtitles_srt", "clip_count", "aspect_ratio")
    FUNCTION = "build"
    CATEGORY = CATEGORY

    def build(self, shot_plan_json, clip_index, task, last_frame_given):
        plan = json.loads(shot_plan_json)
        clips = plan["clips"]
        if not 1 <= clip_index <= len(clips):
            raise ValueError(f"clip_index {clip_index} out of range 1..{len(clips)}")
        clip = clips[clip_index - 1]
        chars = {**bible.CHARACTERS, **plan.get("characters", {})}
        style = plan.get("style_en", bible.STYLE_EN)
        duration = float(clip.get("duration", 10))
        shots = clip.get("shots", [])
        if not shots:
            raise ValueError(f"clip {clip_index} has no shots")

        introduced = set()
        parts, srt, srt_i = [], [], 1
        for n, sh in enumerate(shots, 1):
            start = float(sh.get("start", 0.0))
            nxt = float(shots[n]["start"]) if n < len(shots) else duration
            span = max(1.0, nxt - start)
            seg = []
            if n == 1:
                seg.append(f"[Shot 1] {style}, a {sh.get('shot_type', 'medium shot')} frames "
                           f"{clip.get('location_en', 'the scene')}.")
            else:
                seg.append(f"[Shot {n}] At {_fmt_ts(start)}, the camera cuts to a "
                           f"{sh.get('shot_type', 'medium shot')}.")
            # character introductions (fixed sentences, once per clip)
            for cid in sh.get("characters", []):
                c = chars.get(cid)
                if c and cid not in introduced:
                    seg.append(_cap(f"{c.get('appearance_en', cid)} is in frame."))
                    introduced.add(cid)
            if sh.get("action_en"):
                seg.append(_cap(sh["action_en"].strip().rstrip(".") + "."))
            for d in sh.get("dialogue", []) or []:
                c = chars.get(d.get("speaker", ""), {})
                sid = c.get("speaker", "S9")
                who = f"The {'woman' if d.get('speaker') == 'shen' else 'man'} with {c.get('voice_en', 'a steady voice')} ({sid})"
                text = d.get("text_zh", "").strip()
                if not text:
                    continue
                lang = d.get("lang", bible.DIALOGUE_LANG)
                if d.get("mode") == "voiceover":
                    seg.append(f"{who} says in an off-screen voiceover: <d>[{lang}] {text}</d> "
                               f"while the visible character's lips remain completely closed.")
                else:
                    seg.append(f"{who} says: <d>[{lang}] {text}</d>")
                dur = min(span, max(1.0, len(text) / bible.CHARS_PER_SECOND))
                srt.append(f"{srt_i}\n{_srt_ts(start)} --> {_srt_ts(start + dur)}\n{_wrap_zh(text)}\n")
                srt_i += 1
            if sh.get("sfx_en"):
                seg.append(_cap(sh["sfx_en"].strip().rstrip(".") + " is audible."))
            if sh.get("camera_en"):
                cam = sh["camera_en"].strip().rstrip(".")
                low = cam.lower()
                if low.startswith("the camera"):
                    seg.append(cam + ".")
                elif low in ("static", "static shot", "holds", "hold"):
                    seg.append("The camera holds a static shot.")
                elif low.startswith(("static shot", "holds a static")):
                    seg.append("The camera holds a static shot.")
                else:
                    seg.append(f"The camera {cam}.")
            parts.append(" ".join(seg))

        body = ("integrated_multimodal_description: " + " ".join(parts) + "\n\n"
                f"overall_soundscape: {clip.get('soundscape_en', 'Low room tone.')}\n\n"
                f"non_diegetic_music: {clip.get('music_en', 'Sparse piano notes at a slow tempo.')}")
        if task == "fl2va":
            head = ("How the reference pictures align with the target video — Picture 1 (from Shot 1) "
                    "aligns with the 0.00-second mark of the target video")
            if last_frame_given:
                head += (f"; Picture 2 (from Shot {len(shots)}) aligns with the "
                         f"{duration:.2f}-second mark of the target video")
            body = head + ".\n\n" + body
        return (body, duration, "\n".join(srt), len(clips), plan.get("aspect_ratio", "9:16"))


class H3VideoConcat:
    """Concatenate up to 8 clips (paths from 'MiniMax-H3 Generate'), optionally
    burn Chinese subtitles (SRT per clip, times are clip-relative) with ffmpeg."""

    @classmethod
    def INPUT_TYPES(cls):
        opt = {}
        for i in range(1, 9):
            opt[f"clip_{i}"] = ("STRING", {"default": "", "forceInput": True})
            opt[f"srt_{i}"] = ("STRING", {"default": "", "forceInput": True})
        opt["font_file"] = ("STRING", {"default": os.environ.get(
            "H3_SUBTITLE_FONT", "/workspace/data/fonts/NotoSansCJK-Regular.ttc")})
        return {"required": {
                    "filename_prefix": ("STRING", {"default": "h3/episode"}),
                    "burn_subtitles": ("BOOLEAN", {"default": True}),
                    "font_size": ("INT", {"default": 44, "min": 12, "max": 160,
                                  "tooltip": "字幕字高（视频像素）。768x1344 用 44，2K 用 80 左右"}),
                    "crossfade_s": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.1,
                                    "tooltip": "0 = hard cut (recommended for drama pacing)"}),
                },
                "optional": opt}

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "video_path")
    FUNCTION = "concat"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def concat(self, filename_prefix, burn_subtitles, font_size, crossfade_s, font_file="", **kw):
        clips = [(kw.get(f"clip_{i}", ""), kw.get(f"srt_{i}", "")) for i in range(1, 9)]
        clips = [(p, s) for p, s in clips if p]
        if not clips:
            raise ValueError("connect at least one clip path")
        for p, _ in clips:
            if not os.path.exists(p):
                raise FileNotFoundError(p)
        work = tempfile.mkdtemp(prefix="h3concat_")
        try:
            normed = []
            for i, (p, srt) in enumerate(clips, 1):
                out = os.path.join(work, f"c{i}.mp4")
                vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=24"
                if burn_subtitles and srt.strip():
                    # ffmpeg converts SRT to ASS with PlayResY=288 and scales by video height,
                    # so an ASS font size of S renders at S*H/288 px. Convert the requested px size.
                    try:
                        h_px = int(subprocess.check_output(
                            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=height", "-of", "csv=p=0", p]).decode().strip().split(",")[0])
                    except Exception:
                        h_px = 1344
                    ass_size = max(6, round(font_size * 288 / h_px))
                    sp = os.path.join(work, f"c{i}.srt")
                    with open(sp, "w", encoding="utf-8") as f:
                        f.write(srt)
                    fontsdir = os.path.dirname(font_file) if font_file and os.path.exists(font_file) else ""
                    style = f"FontSize={ass_size},Outline=1,Shadow=0,MarginV=24,MarginL=8,MarginR=8"
                    if fontsdir:
                        style = "FontName=Noto Sans CJK SC," + style
                        vf += f",subtitles={sp}:fontsdir={fontsdir}:force_style='{style}'"
                    else:
                        vf += f",subtitles={sp}:force_style='{style}'"
                cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", p, "-vf", vf,
                       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", out]
                subprocess.run(cmd, check=True)
                normed.append(out)

            full_dir, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
                filename_prefix, folder_paths.get_output_directory())
            name = f"{filename}_{counter:05}_.mp4"
            final = os.path.join(full_dir, name)
            if len(normed) == 1:
                shutil.copy(normed[0], final)
            elif crossfade_s > 0 and len(normed) > 1:
                # xfade/acrossfade chain
                inputs = sum((["-i", n] for n in normed), [])
                durs = [float(subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", n]))
                        for n in normed]
                fc, off, v, a = [], 0.0, "[0:v]", "[0:a]"
                for i in range(1, len(normed)):
                    off += durs[i - 1] - crossfade_s
                    fc.append(f"{v}[{i}:v]xfade=transition=fade:duration={crossfade_s}:offset={off:.3f}[v{i}]")
                    fc.append(f"{a}[{i}:a]acrossfade=d={crossfade_s}[a{i}]")
                    v, a = f"[v{i}]", f"[a{i}]"
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex",
                                ";".join(fc), "-map", v, "-map", a, "-c:v", "libx264", "-crf", "18",
                                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", final], check=True)
            else:
                lst = os.path.join(work, "list.txt")
                with open(lst, "w") as f:
                    f.writelines(f"file '{n}'\n" for n in normed)
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                                "-i", lst, "-c", "copy", final], check=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)

        video = None
        try:
            from comfy_api.latest import InputImpl
            video = InputImpl.VideoFromFile(final)
        except Exception:
            pass
        return {"ui": {"images": [{"filename": name, "subfolder": subfolder, "type": "output"}],
                       "animated": (True,)},
                "result": (video, final)}


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3_EpisodePlanner": H3EpisodePlanner,
    "MiniMaxH3_ClipPromptBuilder": H3ClipPromptBuilder,
    "MiniMaxH3_VideoConcat": H3VideoConcat,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3_EpisodePlanner": "H3 Episode Planner (Ollama)",
    "MiniMaxH3_ClipPromptBuilder": "H3 Clip Prompt Builder",
    "MiniMaxH3_VideoConcat": "H3 Video Concat + Subtitles",
}
