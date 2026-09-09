"""Default character bible / style for the campus-research romance drama.
Fixed English sentences are re-inserted verbatim in every clip so that H3 keeps
faces and voices stable across independently generated clips."""

STYLE_EN = ("Live-action, cinematic, cool blue-tinted night laboratory interior, "
            "shallow depth of field, in a vertical 9:16 frame")

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
        "appearance_en": ("a tall Chinese man of about 30 with short neat black hair, sharp features, "
                          "in a dark grey shirt with the collar open and no jacket"),
        "voice_en": "a low, cold, unhurried voice",
    },
}

DIALOGUE_LANG = "Chinese"
CHARS_PER_SECOND = 4.0   # Mandarin speech rate used for subtitle timing / length checks
