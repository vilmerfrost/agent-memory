# Hand edits and the RSI loop

Vilmer edits instruction files by hand. The loop must build on his edits, never fight them, and never take credit
or blame for them.

| Situation | What happens | Where |
|---|---|---|
| You edit a file | The night reads the file's current text, so every new proposal builds on your version. | `rsi_night.py` hypothesizer |
| A proposal was staged before your edit | Apply is refused: the live file no longer matches the proposal's "before" (stale baseline). The night proposes again on your text. | `improve promote` |
| You edit a file the loop changed before | Auto-pin locks only the sections you changed, for 14 days (git files; a file outside git is locked whole, also 14 days). Once per change: an expired auto-pin does not come back. | `improve` `autopin_scan`, `pins.json` `sections` |
| A pinned section | improve refuses any change that alters it (apply, stage, promote); the night's writer does not rewrite that rule. Other sections stay open. | `improve` `pinned_sections_hit`, `edits_targets.pinned_sections` |
| A pinned file, or a file outside improve's allowed targets | improve never changes it. The night still tests ideas on it, ranked like any other idea; a winning change is a suggestion you apply by hand ("Suggested edits for locked files" in the brief, no Apply command), never a proposal. The pin stays. | `edits_targets.lock_action`, `rsi_night` `SuggestStager` / `suggestion_record`, `rsi_brief` |
| A rule of the delegation policy in CLAUDE.md or AGENTS.md | Both files carry a generated copy of `~/agent-config/parent/DELEGATION.md`. The night targets the source, and only suggests: edit DELEGATION.md by hand and regenerate the copies. | `edits_targets.delegation_rule_id`, `rsi_night._route` |
| You rewrote a rule | Only deviations from sessions that started after the rule's own text last changed count as evidence, so a rule you just fixed starts clean. | `rule_versions.since`, `deviations.candidates(rule_since=...)` |
| Measuring an applied change | If the rule's text changed again inside the before/after windows, the verdict is `mixed`: not credited, no Undo advice. | `rsi_outcome.other_changes` |

Pins by Vilmer (`improve pin <file|file#heading|rule>`) never expire. `improve unpin <file#heading>` frees a section.
A rule's "text" is its section as the rule catalog cuts it (`edits_sections.section_text`); improve's section pins
cut at every heading, so a pin on a deeper heading locks the rule that contains it.
