---
name: meltdown
description: Trimmed test copy (headings kept, one line per section).
---

# /meltdown

Something goes in. A 15–60 s film comes out. Then the ice cube melts.

## Pick the style

Use **explainer** only when he says "explainer", "calm", "walk me through", or the

## Team and effort

- **Lead:** Opus 5.5 at `xhigh` effort. Not `max`: it costs more and does not score

## 0. Where films go

- In a git repo: `<repo-root>/videos/<slug>/`. Add `videos/` to `.gitignore` if missing.

## 1. Material, brief, script → `script.md`

- If it is about a project, send an Explore agent first for **real** names, numbers,

## 2. Scaffold

Use the project's own fonts if it has them (copy woff2 into `assets/fonts/`, declare

## 3. Voice (ElevenLabs MCP)

1. `creative_generate_speech` with the cloned voice and the v4 model.

## 4. Timeline → `timeline.json`

For word-level hits, temporarily lower the silence threshold of the voice detector.

## 5. Plan → `build/plan.md`

One block per shot from `reference/plan.md`: regions in px, every element's states

## 6. Compose → `index.html` + `film.js`

- Build on `fx.js` (catalogue in `reference/reel.md`) and `Studio` (`reference/engine.md`).

## 7. Gate → review loop → render

Then the loop in `reference/review.md`: a fresh reviewer agent scores 8 criteria

## 8. Report and lessons

MP4 path, length, style, voice + credits, the reviewer's final scores, what is real vs.
