# Synopsis Format Specification

A Gamma-optimized synopsis is a markdown document where each slide maps to a section.
The `\n---\n` separators between sections tell Gamma where to split cards when using
`cardSplit: "inputTextBreaks"`.

## Structure

```markdown
# Deck Title

> **Audience**: [who]
> **Tone**: [descriptive words]
> **Deck length**: [N slides]

---

## Slide 1 — [Compelling Title as Complete Thought]

[Content for this slide. Can include:]
- Prose paragraphs
- Bullet points (use sparingly)
- **Bold callouts** for key stats
- Markdown tables for structured data

---

## Slide 2 — [Next Slide Title]

[Content...]

---

[...repeat for each slide...]
```

## Title Conventions

Slide titles should be **complete thoughts**, not labels.

| Bad (Label) | Good (Complete Thought) |
|-------------|------------------------|
| Results | Claude Opus 4.6 Leads on Every Metric That Matters |
| Timeline | From Idea to Production in 11 Days |
| Problem | Legacy Code Is Where Transformation Projects Die |
| Next Steps | Three Moves, In Order of Priority |
| Architecture | Two Secured API Pathways, Zero Data Leakage |

## Data Presentation

### Tables
Use markdown tables for any structured data. Gamma renders them natively.

```markdown
| Model | Score | Cost |
|-------|-------|------|
| Claude Opus 4.6 | 8.27 | $1.53 |
| GPT-5.4 | 8.71 | $0.84 |
```

### Stat Callouts
For impact numbers, use bold + context:

```markdown
**40–60%** of engagement time burned on code archaeology
**$8.43** total benchmark cost across all models
**153** stories delivered in 7 days
```

### Comparison Patterns
For before/after or option comparison:

```markdown
**Primary: Claude Opus 4.6** — Best overall with ZDR compliance
- Score: 8.27, Cost: $1.53, ZDR: Yes

**Value pick: Qwen3.5 397B** — 95% quality at 11% cost
- Score: 7.88, Cost: $0.17, ZDR: Yes
```

## Content Density Guide

| Deck Type | Words per Slide | Tables | Stats |
|-----------|----------------|--------|-------|
| Boardroom pitch (8-10 slides) | 40-80 | 0-1 | 2-3 large callouts |
| Executive summary (12-15 slides) | 60-120 | 1-2 | 1-2 per slide |
| Detailed presentation (18-25 slides) | 80-150 | 2-3 | mixed |
| Technical deep-dive (25+ slides) | 100-200 | frequent | inline |

## Anti-Patterns

- **Don't write "Visual: [description]"** — Gamma can't use layout instructions.
  Instead, structure the content so Gamma infers the right layout.
- **Don't use code blocks for non-code content** — Gamma renders them literally.
  Use tables or bold text instead.
- **Don't nest headers** — Each slide should have exactly one `##` header.
  Use **bold text** for sub-sections within a slide.
- **Don't write meta-instructions** — "This slide should show..." is wasted tokens.
  Just write the content the slide should contain.
- **Don't duplicate the title in the body** — The `## Slide N — Title` IS the slide
  title. Don't repeat it as the first line of body text.
