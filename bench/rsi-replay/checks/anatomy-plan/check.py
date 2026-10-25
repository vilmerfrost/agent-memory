"""Hidden check for anatomy-plan: the planning the anatomy skill requires before any geometry.

Graded from ~/.agents/skills/anatomy/SKILL.md ("Workflow" steps 1-4 and "The one idea that matters"): a quick,
one-mechanism request is a Figure of about 30-90 minutes; the object is a real machine, not boxes and arrows; the
parts list has 40-80 parts grouped as base, structure, mechanism, controls and subject; the subject is the one lit
part; the readout uses the request's true numbers; and no geometry is written before the plan. Stdlib only.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

GROUPS = {"base", "structure", "mechanism", "controls", "subject"}
NUMBERS = ("82", "1.2", "94")
CODE = {".svg", ".html", ".htm", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css"}
ABSTRACT = re.compile(r"\b(diagram|flow ?chart|arrows?|boxes|slabs?|labell?ed box)\b", re.I)


def fail(msg: str) -> None:
    sys.exit(f"anatomy-plan: {msg}")


def num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


path = pathlib.Path("plan.json")
if not path.is_file():
    fail("plan.json missing")
try:
    plan = json.loads(path.read_text(encoding="utf-8"))
except ValueError as exc:
    fail(f"plan.json is not JSON ({exc})")
if not isinstance(plan, dict):
    fail("plan.json is not an object")
if plan.get("size") != "Figure":
    fail(f"size {plan.get('size')!r}: one quick mechanism is a Figure")
est = plan.get("estimate_minutes")
if not (isinstance(est, list) and len(est) == 2 and all(num(x) for x in est) and 30 <= est[0] <= est[1] <= 90):
    fail(f"estimate_minutes {est!r}: a Figure takes about 30-90 minutes")
obj = plan.get("object")
if not isinstance(obj, str) or len(obj.split()) < 2:
    fail("object must name a real physical object")
if ABSTRACT.search(obj):
    fail(f"object {obj!r} is a diagram, not a machine")
motion = plan.get("motion")
if not isinstance(motion, str) or len(motion.split()) < 5:
    fail("motion must say in one sentence what moves and what that means")
parts = plan.get("parts")
if not isinstance(parts, list) or not all(isinstance(p, dict) and isinstance(p.get("name"), str)
                                           and isinstance(p.get("group"), str) for p in parts):
    fail("parts must be a list of {name, group}")
if not 40 <= len(parts) <= 80:
    fail(f"a Figure has 40-80 parts; the list has {len(parts)}")
names = [p["name"].strip().lower() for p in parts]
if len(set(names)) != len(names):
    fail("part names repeat")
groups = {p["group"].strip().lower() for p in parts}
if groups != GROUPS:
    fail(f"part groups {sorted(groups)}: want exactly {sorted(GROUPS)}")
lit = plan.get("lit_part")
match = [p for p in parts if isinstance(lit, str) and p["name"].strip().lower() == lit.strip().lower()]
if not match or match[0]["group"].strip().lower() != "subject":
    fail(f"lit_part {lit!r} must be a listed part of the subject group")
nums = plan.get("numbers")
if not isinstance(nums, list) or not all(isinstance(x, str) for x in nums):
    fail("numbers must be a list of strings")
missing = [n for n in NUMBERS if not any(n in x for x in nums)]
if missing:
    fail(f"the readout must use the request's true numbers; missing {missing}")
code = sorted(p.as_posix() for p in pathlib.Path(".").rglob("*") if p.is_file() and p.suffix.lower() in CODE)
if code:
    fail(f"no geometry before the plan; found {code}")
print("ok")
