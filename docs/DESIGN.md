# VIGIL design system

## Product model
Seven spaces, seven questions, one canonical home per concept. The rule is enforced by
`tests/test_non_repetition.py`, which fails the build if, say, an RSI value appears outside
FORECAST or a Bloom filter statistic appears outside the DATA OBSERVATORY.

The user's journey is the system's loop:
**OBSERVE → INVESTIGATE → FORECAST → CHALLENGE → VERDICT → OUTCOME → LEARN.**

## Visual language
- **Surface**: deep black `#050608`, graphite panels, off-white `#ECEDF2` text. Light is used as
  *information*, not decoration: a surface brightens because something there matters.
- **Accent**: indigo `#6E5BFF` → violet `#A692FF`, used for VIGIL's own voice (its reasoning, its
  verdict, its links). Never used for market direction.
- **Semantics**: green `#3FB27F` / red `#E0616A` / amber `#D9A441`, reserved exclusively for
  measured outcomes and states — and always accompanied by a glyph (`▲ ▼ ■ !`) or a word, so no
  status is ever communicated by colour alone.
- **Type**: an editorial serif for display headlines (this is a research instrument, not a SaaS
  console), Inter/system sans for interface text, monospace for every number and identifier.
- **Restraint**: one subtle glass layer (the sticky top bar), no neon, no glow stacking, no
  gradient confetti, no card wall of identical tiles.

## Motion
Motion is used only to explain: staggered reveals that establish reading order, a single
investigative sweep across the landing hero's signal field, a drawer that slides from the side it
belongs to. Everything is disabled under `prefers-reduced-motion`, including the canvas animation.

## Accessibility
- Full keyboard path: `/` or `⌘/Ctrl-K` focuses global search, `Esc` closes the drawer and the
  results list, the drawer is `role="dialog" aria-modal="true"` and restores focus on close.
- Visible focus rings (`:focus-visible`), skip link, semantic landmarks, `aria-label` on every
  icon-only control, `role="img"` plus a text label on every chart.
- Contrast: body text ≥ 7:1 on the base surface; secondary text ≥ 4.5:1.
- Status is never colour-only (see glyphs above).

## Responsiveness
The layout is re-composed, not shrunk: below 1080px the left rail becomes a horizontal bottom bar
with the same seven destinations; grids collapse to a single column; the drawer becomes
full-width; charts are `viewBox`-scaled SVG so they stay legible at any size.

## Error states
Every failure names three things: **what failed**, **what is affected**, and **what fallback is
active**. There is no "Something went wrong" anywhere in the codebase.

## Deliberate non-goals
No framework, no build step, no CDN, no component library, no dark-mode toggle (VIGIL has one
considered appearance), no fake live ticker.
