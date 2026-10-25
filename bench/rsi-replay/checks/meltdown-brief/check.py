"""Hidden check for meltdown-brief: the film decisions follow the meltdown skill's hard rules and style table.

Graded from ~/.agents/skills/meltdown/SKILL.md (intro "Hard rules" and "Pick the style"): the request names no
style and no length, so the default is a 15 s reel; the voice is ElevenLabs v4 with Vilmer's voice id; a separate
reviewer scores the passes; reel lines are 2-4 words and 15 s is about 25 words. Stdlib only, no network.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

VOICE_ID = "choA5fzIKsVFxO1Mg9ZL"


def fail(msg: str) -> None:
    sys.exit(f"meltdown-brief: {msg}")


def words(line: str) -> list[str]:
    """Spoken words only: delivery tags like [pause] and bare punctuation are not words."""
    text = re.sub(r"\[[^\]]*\]", " ", line)
    return [w for w in text.split() if re.search(r"[A-Za-z0-9]", w)]


path = pathlib.Path("film.json")
if not path.is_file():
    fail("film.json missing")
try:
    film = json.loads(path.read_text(encoding="utf-8"))
except ValueError as exc:
    fail(f"film.json is not JSON ({exc})")
if not isinstance(film, dict):
    fail("film.json is not an object")
if film.get("style") != "reel":
    fail(f"style {film.get('style')!r}: the request asks for no explainer, so the default is a reel")
length = film.get("length_s")
if isinstance(length, bool) or not isinstance(length, (int, float)) or length != 15:
    fail(f"length_s {length!r}: no length was named, a reel defaults to 15 s")
voice = film.get("voice")
if not isinstance(voice, dict):
    fail("voice must be an object")
if voice.get("model_id") != "eleven_v4":
    fail(f"voice model {voice.get('model_id')!r}: only eleven_v4 is allowed")
if voice.get("voice_id") != VOICE_ID:
    fail(f"voice id {voice.get('voice_id')!r}: the default voice is Vilmer Frost {VOICE_ID}")
if film.get("reviewer") != "separate-agent":
    fail(f"reviewer {film.get('reviewer')!r}: the maker never grades its own work")
lines = film.get("lines")
if not isinstance(lines, list) or not lines or not all(isinstance(x, str) and x.strip() for x in lines):
    fail("lines must be a non-empty list of strings")
for line in lines:
    n = len(words(line))
    if not 2 <= n <= 4:
        fail(f"reel lines have 2-4 words; {line!r} has {n}")
total = sum(len(words(x)) for x in lines)
if not 18 <= total <= 32:
    fail(f"a 15 s reel is about 25 words; the script has {total}")
script = pathlib.Path("script.md")
if not script.is_file():
    fail("script.md missing")
text = script.read_text(encoding="utf-8")
missing = [x for x in lines if x.strip() not in text]
if missing:
    fail(f"script.md lacks lines {missing}")
print("ok")
