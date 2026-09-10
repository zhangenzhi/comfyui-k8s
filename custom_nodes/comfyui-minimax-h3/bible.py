"""Default character bible / style for the campus-research romance drama.
Fixed English sentences are re-inserted verbatim in every clip so that H3 keeps
faces and voices stable across independently generated clips."""

STYLE_EN = ("Live-action, documentary-style realism: handheld camera, natural colours with no colour grading, slight film grain; the room is lit only by practical light sources (a desk lamp, the white glow of a computer monitor, a microscope's stage light) with the overhead fluorescent tubes switched off; a real, cluttered university laboratory with labelled bottles, pipette racks and paper on the benches; in a vertical 9:16 frame")

CHARACTERS = {
    "shen": {
        "name_zh": "沈听澜", "speaker": "S2",
        "appearance_en": ("a young Chinese woman of about 24 with thin black-rimmed glasses, pale skin, "
                          "shoulder-length straight black hair tied low, and a white lab coat over a "
                          "light grey sweater"),
        "voice_en": "a clear, slightly trembling but steady voice",
    },
    "gu": {
        "name_zh": "顾辞渊", "speaker": "S1",
        "appearance_en": ("a tall Chinese man of about 31 with a mature, composed, commanding presence, sharp clean jawline, clear cool-toned skin with no shine and no stubble, neatly groomed short black hair, cold steady eyes, in a well-fitted dark grey shirt with the collar open and sleeves rolled once, no jacket, "
                          "smooth even complexion with fine skin texture, soft diffused key light on his face"),
        "voice_en": "a low, cold, unhurried voice with quiet authority",
    },
}

DIALOGUE_LANG = "Chinese"
CHARS_PER_SECOND = 4.0   # Mandarin speech rate used for subtitle timing / length checks


# ── Runtime override: bible.json next to this file (edit without restarting ComfyUI) ──
import json as _json
import os as _os
_JSON = _os.path.join(_os.path.dirname(__file__), "bible.json")


def characters():
    """CHARACTERS merged with bible.json (if present). Called per run, not at import."""
    data = {k: dict(v) for k, v in CHARACTERS.items()}
    try:
        with open(_JSON, encoding="utf-8") as f:
            for cid, c in _json.load(f).get("characters", {}).items():
                data.setdefault(cid, {}).update(c)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[MiniMax-H3] bible.json ignored: {e}")
    return data


def style():
    try:
        with open(_JSON, encoding="utf-8") as f:
            return _json.load(f).get("style_en") or STYLE_EN
    except Exception:
        return STYLE_EN
