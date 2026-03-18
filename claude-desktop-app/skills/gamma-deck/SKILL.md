---
name: gamma-deck
description: >
  Create professional presentations using Gamma.app's AI generation platform.
  This skill handles the full pipeline: conducting an iterative interview to
  build a detailed brief, writing a structured synopsis optimized for slide
  generation, then calling the Gamma MCP tools (get_themes, get_folders,
  generate) to produce the deck.

  MANDATORY TRIGGERS: Any request involving "presentation", "deck", "slides",
  "pitch deck", "keynote", "gamma", "gamma.app", or any mention of creating
  a visual presentation from content. Also trigger when the user asks to
  "present" findings, results, a project, or a plan — even if they don't
  explicitly say "presentation". If the user says "make a deck about X" or
  "I need to present X to Y", this skill applies.

  REQUIRES: Gamma MCP connector must be connected. If the Gamma MCP tools
  (generate, get_themes, get_folders) are not available, inform the user
  they need to connect the Gamma connector first via the MCP registry.
---

# Gamma Deck Skill

Create polished presentations through a three-stage pipeline:

```
Stage 1: BRIEF        Stage 2: SYNOPSIS        Stage 3: GENERATION
Interview → deck-brief.md → synopsis.md → Gamma MCP → Live deck
```

The quality of a generated deck is almost entirely determined by what happens
*before* generation. Gamma is a rendering engine — it does exactly what you tell it.
The brief captures intent. The synopsis translates intent into slide-by-slide content.
The generation step is mechanical. Invest the time in Stages 1 and 2.

---

## Stage 1: The Brief (Interview)

Build a comprehensive brief through iterative, one-question-at-a-time conversation
with the user. This is the PM-style requirements gathering pattern — each question
builds on the previous answer, progressively narrowing from strategic intent to
tactical slide content.

### Before You Ask Anything

Gather available context silently first. Check:

- **Conversation history** — the user may have already described what they want
- **Uploaded files** — documents, reports, data that should feed the deck
- **Workspace/repo** — if they mention a project, explore it (README, docs/, data files,
  previous presentations) to extract facts, metrics, timelines
- **Existing decks** — check for previous presentations in the workspace for tone/style precedent

Extract as much as you can. The more you know before the first question, the sharper
your questions will be and the fewer you'll need.

### The Interview

Ask **one question at a time**. Each question should build on the previous answer.
Don't front-load a multi-choice menu — have a conversation.

The interview covers these dimensions, but the order and depth should adapt to the
user's context. Skip questions you can already answer from context. Go deeper on
areas where the user's answers reveal complexity.

**Strategic Intent** (always start here):
- What's the **one thing** the audience should walk away thinking/doing after this deck?
- Why does this deck need to exist *now*? What's the triggering event?

**Audience & Stakes**:
- Who's in the room? (title, seniority, technical depth, decision-making power)
- What do they already know? What's their starting assumption?
- What's the political context? (allies, skeptics, competing priorities)

**Content & Narrative**:
- What's the story arc? (problem→solution→proof→ask? update→results→next steps? demo→data→recommendation?)
- What data/metrics/evidence do you have to support the narrative?
- What's explicitly out of scope? (things the audience might expect but you don't want to address)

**Format & Constraints**:
- How many slides? (If unsure, suggest based on context: 8-10 for C-suite, 12-15 for working sessions, 18-25 for detailed reviews)
- Time slot? (Helps calibrate density — a 10-min slot needs punchy slides, a 45-min slot can go deeper)
- Tone? (boardroom formal, working session casual, client-facing polished, internal scrappy)
- Any brand template or visual style requirements?

**Practical Notes:**
- Aim for 5-8 questions total. Fewer for simple decks, more for complex strategic presentations.
- If the user is getting impatient ("just make the deck"), pivot — use what you have and move to Stage 2.
  You can always iterate.
- If the user provides a wall of text or a document, acknowledge it and ask targeted follow-ups
  rather than re-asking things they've already told you.
- Push back when answers are vague. "Everyone" is not an audience. "Make it look good" is not
  a visual style. Ask for specifics.

### Save the Brief

Once the interview is complete, compile answers into a structured brief document
and save it to the workspace. Read `references/brief-template.md` for the format.

Save as `presentations/deck-brief.md` (or a more descriptive name if the user has
multiple decks). This becomes the input contract for Stage 2 — if the synopsis
doesn't match the brief, something went wrong.

Tell the user: "Here's the brief I've captured. Take a look — once you're happy
with it, I'll write the synopsis and generate the deck."

Wait for confirmation before proceeding.

---

## Stage 2: The Synopsis

Transform the brief into a slide-by-slide synopsis — the architect's blueprint for Gamma.

### Read the Format Guide

Read `references/synopsis-format.md` for the detailed format specification, title
conventions, data presentation patterns, and anti-patterns.

### Synopsis Writing Principles

- **One idea per slide.** If a slide has two distinct points, split it.
- **Titles are complete thoughts, not labels.**
  Bad: "Results". Good: "Claude Opus 4.6 Leads on Every Metric That Matters".
- **Include actual data.** Tables, numbers, metrics — verbatim. Don't write
  "insert chart here" — write the data Gamma should render.
- **Use markdown tables** for structured data. Gamma handles them well.
- **Bold key phrases** that should pop visually.
- **Write speaker-ready text.** Short sentences. Active voice. Punch.
- **Match the brief.** Every slide should trace back to something in the brief.
  If it doesn't, either the brief is incomplete or the slide is filler — fix one.

### Synopsis Structure

Each slide is a `## Slide N — Title` section separated by `---` dividers.
This maps directly to Gamma's card splitting.

```markdown
# [Deck Title]

> **Audience**: [from brief]
> **Tone**: [from brief]
> **Slides**: [count]

---

## Slide 1 — [Title as Complete Thought]

[Content for this slide]

---

## Slide 2 — [Title]

[Content...]

---

[...repeat...]
```

### Save the Synopsis

Save to workspace as `presentations/gamma-synopsis-[topic].md` alongside the brief.
This is the second artifact in the audit trail.

---

## Stage 3: Gamma Generation

With the synopsis locked, orchestrate Gamma MCP to produce the deck.

### Step 1: Find the Right Theme

Call `get_themes` to list available themes. The response includes `id`, `name`,
`type` (standard/custom), `colorKeywords`, and `toneKeywords`.

**Theme selection logic:**
1. **Custom template mentioned** → look for `type: "custom"` themes (user-uploaded)
2. **Style described** → match against `toneKeywords` and `colorKeywords`
3. **No preference** → pick based on content:
   - Corporate/consulting → "consultant", "chimney-smoke", "slate", "coal"
   - Tech/modern → "founder", "blue-steel", "commons"
   - Creative/bold → "aurora", "electric", "gamma"
   - Warm/earthy → "creme", "terracotta", "cigar"
4. Present your choice. Let the user override.

### Step 2: Check Folders (Optional)

Call `get_folders` if the user wants the deck in a specific Gamma folder. Skip if not mentioned.

### Step 3: Call Generate

Call the `generate` MCP tool. Read `references/gamma-api-params.md` for the complete
parameter reference. Key decisions:

| Parameter | Decision Logic |
|-----------|---------------|
| `inputText` | Full synopsis content with `\n---\n` slide separators |
| `textMode` | **"preserve"** for detailed synopses. "generate" only for brief outlines. |
| `format` | "presentation" |
| `themeId` | From Step 1 |
| `numCards` | Match synopsis slide count |
| `exportAs` | "pptx" if user wants PowerPoint, "pdf" for PDF, omit otherwise |
| `imageOptions.source` | "noImages" for data decks, "aiGenerated" for visual decks |
| `textOptions.amount` | "medium" default. "brief" for pitches. "detailed" for reports. |
| `textOptions.tone` | From brief |
| `textOptions.audience` | From brief |
| `cardOptions.dimensions` | "16x9" for presentations |

**Header/footer** (when brand template exists):
```json
{
  "topRight": { "type": "image", "source": "themeLogo", "size": "sm" },
  "bottomRight": { "type": "cardNumber" },
  "hideFromFirstCard": true
}
```

### Step 4: Deliver

The generate tool returns `generationId`, `status`, and `gammaUrl`.

Share the `gammaUrl`. Remind the user:
- They can refine in Gamma's editor (layouts, images, text)
- The brief and synopsis are saved in their workspace for iteration
- To regenerate with changes, edit the synopsis and re-run Stage 3

---

## Common Patterns

### Data-Heavy Executive Deck
- `imageOptions.source`: "noImages"
- `cardOptions.dimensions`: "16x9"
- `textMode`: "preserve"
- Synopsis heavy on tables, metrics, stat callouts

### Visual Storytelling Deck
- `imageOptions.source`: "aiGenerated"
- `imageOptions.style`: match topic
- `textMode`: "generate" (let Gamma expand)
- Synopsis provides structure, Gamma fills visuals

### Brand Template Deck
- Custom theme from `get_themes`
- Header/footer with `themeLogo`
- `cardOptions.dimensions`: "16x9"

### Quick Pitch (< 10 slides)
- Shorten the interview (3-4 questions max)
- `numCards`: 8-10
- `textOptions.amount`: "brief"
- Bold numbers, one message per slide

---

## Troubleshooting

**Gamma MCP not available:** Search MCP registry for "gamma", suggest the connector.

**Custom theme not found:** User must upload their template to gamma.app first,
then re-run `get_themes`.

**Generation fails:** Check inputText < 100k tokens. For long synopses, split
into two decks or use `textMode: "condense"`.

**Content rewritten unexpectedly:** Switch to `textMode: "preserve"`. Even preserve
may restructure slightly — Gamma adds layout intelligence.
