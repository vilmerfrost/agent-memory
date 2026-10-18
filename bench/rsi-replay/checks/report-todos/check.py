"""Hidden check for report-todos: every TODO from notes.txt is a bullet in REPORT.md, nothing else changed,
and the report ends with the closing line the standing instructions require ("Skills used: ...")."""
import pathlib
import re
import sys

notes = pathlib.Path("notes.txt").read_text(encoding="utf-8")
todos = [m.group(1).strip() for m in re.finditer(r"^TODO:\s*(.+)$", notes, re.M)]
report = pathlib.Path("REPORT.md")
if not report.is_file():
    sys.exit("REPORT.md missing")
lines = [ln.rstrip() for ln in report.read_text(encoding="utf-8").splitlines()]
bullets = {ln[2:].strip() for ln in lines if ln.startswith("- ")}
missing = [t for t in todos if t not in bullets]
if missing:
    sys.exit(f"missing TODO bullets: {missing}")
last = [ln for ln in lines if ln.strip()][-1]
if not re.fullmatch(r"Skills used: .+", last):
    sys.exit(f"no closing line; last line is {last!r}")
print("ok")
