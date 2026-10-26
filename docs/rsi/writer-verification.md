# Independent verification of the pushed section-writer fix

## Existing fix

Pushed commit `560ad15366396ee12e991bc4e3c83edf3f32894a` (2026-10-26) is already present in this task's starting
tree. It added `section_for_hypothesis` when a candidate's rule key does not name a section, plus structured
`implementation.section_from`, a writer/fallback tally, and loud all-fallback reporting. This work does not
recreate that matcher fix.

The historical three selected candidates were skill-score shapes: `skill:pepe`, `skill:meltdown`, and
`skill:anatomy`. A skill name is not a section rule ID. The new matcher resolves their first hypotheses to
`return-mode`, `gate-review-loop-render` and `workflow`, respectively. Pins, section size limits, the untrusted
evidence envelope, one-section edit validation, content guard and artifact checks remain active.

## Deterministic demonstration

`tests/test_rsi_night_skill_writer.py` uses recorded structured candidate/hypothesis shapes and trimmed copies
of the actual skill heading structures. A controlled writer response substitutes for inference. Tests require:

1. The intended matcher makes a writer call rather than taking the fallback.
2. A known hypothesis-specific instruction reaches the expected section: Pepe's five ranked return items,
   Meltdown's sub-eight review-stop disclosure, and Anatomy's named shipped-example choice before geometry.
3. Only that section changes. Frontmatter, heading order and all other sections remain unchanged.
4. The original source/baseline remains byte-identical to A. The artifact supplies distinct B bytes.
5. Manifest path, before/after SHA256, artifact digest, experiment ID and baseline ID bind those exact bytes.
6. `implementation.section_id` names the matched source section. The manifest's `rsi-night-*` rule ID is the
   established synthetic staging identity; it is not misrepresented as the source section ID.
7. The result differs from `stub_change` and reports `implementer=writer`, no fallback reason and
   `intended_intervention=true`.

These tests prove the deterministic selection/edit/artifact path with representative input shapes and known
valid output. They do not prove that tonight's model will produce that output or that the proposed instruction
will improve behavior. No full experimental batch, model call or production staging/promotion was run.

## Remaining fallback behavior and additional correction

Fallback remains possible for a pinned file/rule/section, unmatched or oversized section, invalid writer output,
writer error, budget stop or content-guard refusal. Its exact reason and `implementer=stub-fallback` are retained.

Previously an improving replay of fallback bytes could reach the intended candidate's proposal path. Now a
fallback replay always has effective `INCONCLUSIVE` for the intended intervention. Its raw `replay_verdict` is
retained separately and `intended_intervention=false` is recorded in experiment metadata and the report. Tests
cover raw IMPROVES, WORSE and NO_EFFECT and require zero staged proposals in every fallback case.

The raw artifact/evaluation is still useful diagnostic evidence about the bytes it actually tested. Consumers
must retain its fallback scope rather than promote it to evidence for the hypothesized writer change. Human
approval and the existing production freeze remain unchanged.
