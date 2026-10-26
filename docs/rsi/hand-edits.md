# Hand edits and the RSI loop

Vilmer edits instruction files by hand. The loop must build on his edits, never fight them, and never take credit
or blame for them.

| Situation | What happens | Where |
|---|---|---|
| You edit a file | The night reads the file's current text, so every new proposal builds on your version. | `rsi_night.py` hypothesizer |
| A proposal was staged before your edit | Apply is refused: the live file no longer matches the proposal's "before" (stale baseline). The night proposes again on your text. | `improve promote` |
| You edit a file the loop changed before | Auto-pin locks only the sections you changed, for 14 days (git files; a file outside git is locked whole, also 14 days). Once per change: an expired auto-pin does not come back. | `improve` `autopin_scan`, `pins.json` `sections` |
| A pinned section | improve refuses any change that alters it (apply, stage, promote); the night's writer does not rewrite that rule. Other sections stay open. | `improve` `pinned_sections_hit`, `edits_targets.pinned_sections` |
| You rewrote a rule | Only deviations from sessions that started after the rule's own text last changed count as evidence, so a rule you just fixed starts clean. | `rule_versions.since`, `deviations.candidates(rule_since=...)` |
| Measuring an applied change | If the rule's text changed again inside the before/after windows, the verdict is `mixed`: not credited, no Undo advice. | `rsi_outcome.other_changes` |

Pins by Vilmer (`improve pin <file|file#heading|rule>`) never expire. `improve unpin <file#heading>` frees a section.
A rule's "text" is its section as the rule catalog cuts it (`edits_sections.section_text`); improve's section pins
cut at every heading, so a pin on a deeper heading locks the rule that contains it.
