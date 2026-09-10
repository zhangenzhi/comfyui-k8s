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


def validate_plan(plan, min_clips=4):
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


_WHISPER = {}


def _asr_srt(video_path, plan_srt, mode="hybrid", model_name=None):
    """Re-time subtitles from the clip's actual speech with Whisper.
    hybrid: ASR timing, text = best-matching planned line when similar enough, else ASR text.
    asr:    ASR timing and text."""
    import difflib
    import whisper
    name = model_name or os.environ.get("H3_WHISPER_MODEL", "large-v3-turbo")
    if name not in _WHISPER:
        _WHISPER.clear()
        _WHISPER[name] = whisper.load_model(name, download_root="/workspace/data/models/whisper")
    model = _WHISPER[name]
    res = model.transcribe(video_path, language="zh", task="transcribe", fp16=True,
                           condition_on_previous_text=False, no_speech_threshold=0.5)
    segs = [x for x in res.get("segments", []) if x.get("text", "").strip()]
    if not segs:
        return ""            # nothing spoken -> no subtitles
    planned = []
    for block in plan_srt.strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) >= 3:
            planned.append("".join(lines[2:]).replace("\n", ""))
    out, used = [], set()
    for i, sg in enumerate(segs, 1):
        text = sg["text"].strip().replace(" ", "")
        if mode == "hybrid" and planned:
            best, score = None, 0.0
            for j, pl in enumerate(planned):
                r = difflib.SequenceMatcher(None, pl, text).ratio()
                if r > score:
                    best, score = j, r
            if best is not None and score >= 0.45:
                text = planned[best]
                used.add(best)
        out.append(f"{i}\n{_srt_ts(float(sg['start']))} --> {_srt_ts(float(sg['end']))}\n{_wrap_zh(text)}\n")
    return "\n".join(out)


def _shift_srt(srt, delta):
    out = []
    for line in srt.splitlines():
        m = re.match(r"(\d+):(\d+):(\d+),(\d+) --> (\d+):(\d+):(\d+),(\d+)", line)
        if m:
            v = [int(x) for x in m.groups()]
            a = max(0.0, v[0] * 3600 + v[1] * 60 + v[2] + v[3] / 1000 + delta)
            b = max(0.0, v[4] * 3600 + v[5] * 60 + v[6] + v[7] / 1000 + delta)
            line = f"{_srt_ts(a)} --> {_srt_ts(b)}"
        out.append(line)
    return "\n".join(out)


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
                "clips": ("INT", {"default": 6, "min": 3, "max": 16, "tooltip": "片段数（每段 13–15 s；90 秒一集 = 6 段）"}),
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
                        seed=seed, max_new_tokens=20000, ollama_url=ollama_url, openai_url=llm_url)
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
                             seed=seed + 7, max_new_tokens=20000, ollama_url=ollama_url, openai_url=llm_url)
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
                             seed=seed + 1, max_new_tokens=20000, ollama_url=ollama_url, openai_url=llm_url)
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
        for cid, c in bible.characters().items():
            chars.setdefault(cid, {}).update(c)
        plan.setdefault("style_en", bible.style())
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
                "task": (["auto", "t2va", "fl2va"], {"default": "auto",
                         "tooltip": "auto: 第 1 段 t2va，之后按 chain_mode 决定是否用上一段末帧做首帧(fl2va)"}),
                "chain_mode": (["auto", "always", "never"], {"default": "auto",
                               "tooltip": "auto = 按分镜 JSON 的 continue_from_previous（同一场景才延续）"}),
                "last_frame_given": ("BOOLEAN", {"default": False,
                                     "tooltip": "fl2va 且同时提供末帧图时勾选"}),
            },
        }

    RETURN_TYPES = ("STRING", "FLOAT", "STRING", "INT", "STRING", "BOOLEAN")
    RETURN_NAMES = ("h3_prompt", "duration_seconds", "subtitles_srt", "clip_count", "aspect_ratio", "chain")
    FUNCTION = "build"
    CATEGORY = CATEGORY

    def build(self, shot_plan_json, clip_index, task, chain_mode, last_frame_given):
        plan = json.loads(shot_plan_json)
        clips = plan["clips"]
        if not 1 <= clip_index <= len(clips):
            raise ValueError(f"clip_index {clip_index} out of range 1..{len(clips)}")
        clip = clips[clip_index - 1]
        # chain: use the previous clip's last frame as this clip's first frame?
        if chain_mode == "always":      # e.g. clip 1 seeded with an SDXL keyframe
            chain = True
        elif chain_mode == "never" or clip_index == 1:
            chain = False
        else:
            cfp = clip.get("continue_from_previous")
            if cfp is None:   # not specified: continue when the location text did not change
                prev = clips[clip_index - 2]
                cfp = (clip.get("location_en", "").strip().lower()[:40] ==
                       prev.get("location_en", "").strip().lower()[:40])
            chain = bool(cfp)
        if task == "auto":
            task = "fl2va" if chain else "t2va"
        chars = {**bible.characters(), **plan.get("characters", {})}
        # The live bible wins over whatever style the planner wrote into the JSON, unless the
        # plan explicitly sets style_override (lets the style be tuned without re-planning).
        style = plan.get("style_override") or bible.style()
        duration = float(clip.get("duration", 10))
        shots = clip.get("shots", [])
        if not shots:
            raise ValueError(f"clip {clip_index} has no shots")

        introduced = set()
        parts, srt, srt_i = [], [], 1
        prev_tail = ""
        if chain and clip_index > 1:
            pshots = clips[clip_index - 2].get("shots") or []
            if pshots:
                last = pshots[-1]
                prev_tail = (" This clip begins exactly where the previous one ended: "
                             + last.get("action_en", "").strip().rstrip(".")
                             + ". The action continues without a cut.")
        for n, sh in enumerate(shots, 1):
            start = float(sh.get("start", 0.0))
            nxt = float(shots[n]["start"]) if n < len(shots) else duration
            span = max(1.0, nxt - start)
            seg = []
            if n == 1:
                seg.append(f"[Shot 1] {style}, a {sh.get('shot_type', 'medium shot')} frames "
                           f"{clip.get('location_en', 'the scene')}.{prev_tail}")
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

        parts.append("No subtitles, captions, lettering, signage or logos appear anywhere in the frame; "
                     "any documents or screens are out of focus.")
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
        return (body, duration, "\n".join(srt), len(clips), plan.get("aspect_ratio", "9:16"), chain)


class H3VideoConcat:
    """Concatenate up to 12 clips (paths from 'MiniMax-H3 Generate'), optionally
    burn Chinese subtitles (SRT per clip, times are clip-relative) with ffmpeg."""

    @classmethod
    def INPUT_TYPES(cls):
        opt = {}
        for i in range(1, 13):
            opt[f"clip_{i}"] = ("STRING", {"default": "", "forceInput": True})
            opt[f"srt_{i}"] = ("STRING", {"default": "", "forceInput": True})
        opt["font_file"] = ("STRING", {"default": os.environ.get(
            "H3_SUBTITLE_FONT", "/workspace/data/fonts/NotoSansCJK-Regular.ttc")})
        return {"required": {
                    "filename_prefix": ("STRING", {"default": "h3/episode"}),
                    "burn_subtitles": ("BOOLEAN", {"default": True}),
                    "font_size": ("INT", {"default": 44, "min": 12, "max": 160,
                                  "tooltip": "字幕字高（视频像素）。768x1344 用 44，2K 用 80 左右"}),
                    "crossfade_s": ("FLOAT", {"default": 0.25, "min": 0.0, "max": 1.0, "step": 0.05,
                                    "tooltip": "crossfade seconds; 0 = hard cut"}),
                    "subtitle_source": (["hybrid", "asr", "plan"], {"default": "hybrid",
                                        "tooltip": "hybrid: Whisper 识别实际语音的时间，文本优先用分镜原句；asr: 全用识别结果；plan: 按分镜估算（旧行为）"}),
                    "trim_head_frames": ("INT", {"default": 3, "min": 0, "max": 24,
                                         "tooltip": "drop the first N frames of clips 2..n: a chained clip's first frame duplicates the previous last frame"}),
                },
                "optional": opt}

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "video_path")
    FUNCTION = "concat"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def concat(self, filename_prefix, burn_subtitles, font_size, crossfade_s, subtitle_source="hybrid",
               trim_head_frames=3, font_file="", **kw):
        clips = [(kw.get(f"clip_{i}", ""), kw.get(f"srt_{i}", "")) for i in range(1, 13)]
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
                head = trim_head_frames if i > 1 else 0
                if burn_subtitles and subtitle_source != "plan":
                    try:
                        srt = _asr_srt(p, srt, mode=subtitle_source)
                    except Exception as e:  # noqa: BLE001
                        print(f"[H3 concat] ASR subtitle alignment failed ({e}); falling back to plan timing")
                if burn_subtitles and srt.strip():
                    if head:
                        srt = _shift_srt(srt, -head / 24.0)
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
                cmd = ["ffmpeg", "-y", "-loglevel", "error"]
                if head:
                    cmd += ["-ss", f"{head / 24.0:.4f}"]
                cmd += ["-i", p, "-vf", vf,
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


class H3LastFrame:
    """Last (or first) frame of a clip as an IMAGE, for fl2va chaining."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video_path": ("STRING", {"default": "", "forceInput": True}),
                             "which": (["last", "first"], {"default": "last"})}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "grab"
    CATEGORY = CATEGORY

    def grab(self, video_path, which):
        import io
        import numpy as np
        import torch
        from PIL import Image
        if not os.path.exists(video_path):
            raise FileNotFoundError(video_path)
        if which == "last":
            cmd = ["ffmpeg", "-loglevel", "error", "-sseof", "-0.2", "-i", video_path,
                   "-update", "1", "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
        else:
            cmd = ["ffmpeg", "-loglevel", "error", "-i", video_path,
                   "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
        png = subprocess.run(cmd, check=True, capture_output=True).stdout
        img = Image.open(io.BytesIO(png)).convert("RGB")
        arr = np.asarray(img).astype(np.float32) / 255.0
        return (torch.from_numpy(arr)[None, ...],)


NODE_CLASS_MAPPINGS = {
    "MiniMaxH3_LastFrame": H3LastFrame,
    "MiniMaxH3_EpisodePlanner": H3EpisodePlanner,
    "MiniMaxH3_ClipPromptBuilder": H3ClipPromptBuilder,
    "MiniMaxH3_VideoConcat": H3VideoConcat,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3_LastFrame": "H3 Last Frame (for fl2va chaining)",
    "MiniMaxH3_EpisodePlanner": "H3 Episode Planner (LLM)",
    "MiniMaxH3_ClipPromptBuilder": "H3 Clip Prompt Builder",
    "MiniMaxH3_VideoConcat": "H3 Video Concat + Subtitles",
}
