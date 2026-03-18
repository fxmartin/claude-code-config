# Gamma Generate API Parameters Reference

Complete parameter reference for the Gamma MCP `generate` tool.

## Required Parameters

### inputText (string, required)
The content to generate from. Can range from a one-line prompt to a full structured
synopsis with slide separators.

- Token limit: 100,000 (~400,000 characters)
- Supports markdown formatting (headers, tables, bold, lists)
- Supports image URLs inline — insert URLs where images should appear
- Use `\n---\n` to mark card/slide boundaries (works with cardSplit: "inputTextBreaks")
- May need JSON escaping for special characters

### textMode (string, required)
How Gamma processes your inputText:

| Mode | Use When | Behavior |
|------|----------|----------|
| `generate` | Short prompt or outline | Gamma rewrites and expands content |
| `condense` | Long document or article | Gamma summarizes to fit slide count |
| `preserve` | Detailed synopsis with exact content | Gamma uses text as-is, adds structure |

**Rule of thumb:** If you wrote a detailed synopsis with specific data/tables/metrics,
always use "preserve". If you wrote a brief outline, use "generate".

## Optional Parameters

### format (string, default: "presentation")
- `presentation` — slide deck
- `document` — long-form document
- `social` — social media post
- `webpage` — single-page website

### themeId (string)
Theme ID from `get_themes`. Determines colors, fonts, visual style.
- Standard themes: Gamma built-in (e.g., "consultant", "slate", "aurora")
- Custom themes: User-uploaded templates (type: "custom" in get_themes response)
- If omitted, uses workspace default theme

### numCards (integer, default: 10)
Number of slides/cards to generate.
- Pro users: 1-60
- Ultra users: 1-75
- Ignored if cardSplit is "inputTextBreaks"

### additionalInstructions (string, max 2000 chars)
Extra guidance for Gamma's AI. Use for:
- Layout preferences ("use two-column layouts for comparison slides")
- Content emphasis ("highlight the cost metrics prominently")
- Style notes ("keep titles under 8 words")

### exportAs (string, optional)
- `pptx` — export as PowerPoint file
- `pdf` — export as PDF
- If omitted, deck is only available via gammaUrl (can export later from Gamma app)

### folderIds (array of strings)
Gamma folder IDs from `get_folders`. Organizes the generated deck into specific folders.

## textOptions (object)

### textOptions.amount (string, default: "medium")
Text density per slide: `brief`, `medium`, `detailed`, `extensive`

### textOptions.tone (string, max 500 chars)
Writing voice. Examples: "professional, strategic", "casual, friendly", "technical, precise"

### textOptions.audience (string, max 500 chars)
Target readers. Examples: "C-level executives", "engineering team", "potential investors"

### textOptions.language (string, default: "en")
Output language code. Supports 60+ languages. Common: en, fr, de, es, zh-cn, ja, ko

## imageOptions (object)

### imageOptions.source (string, default: "aiGenerated")

| Source | Notes |
|--------|-------|
| `aiGenerated` | AI-generated images (can set model + style) |
| `webAllImages` | Web images (licensing unknown) |
| `webFreeToUse` | Free-to-use licensed images |
| `webFreeToUseCommercially` | Commercially licensed images |
| `pictographic` | Illustration-style drawings |
| `pexels` | Pexels stock photos |
| `giphy` | GIFs |
| `placeholder` | Empty placeholders for manual insertion |
| `noImages` | No images at all — text/data only |

**For data-heavy executive decks:** Use `noImages` to keep focus on content.
**For storytelling decks:** Use `aiGenerated` with a style directive.

### imageOptions.model (string, optional)
AI image model when source is "aiGenerated". Options include:
flux-1-quick, flux-1-pro, flux-1-ultra, flux-kontext-fast, flux-kontext-pro,
imagen-3-flash, imagen-3-pro, imagen-4-fast, imagen-4-pro,
ideogram-v3-turbo, ideogram-v3, ideogram-v3-quality,
leonardo-phoenix, recraft-v3, recraft-v3-svg

### imageOptions.style (string, max 500 chars)
Artistic style for AI images. Examples: "photorealistic", "minimal line art",
"watercolor illustration", "corporate photography"

## cardOptions (object)

### cardOptions.dimensions (string)

| Format | Options |
|--------|---------|
| presentation | `fluid` (default), `16x9`, `4x3` |
| document | `fluid` (default), `pageless`, `letter`, `a4` |
| social | `1x1`, `4x5` (default), `9x16` |

### cardOptions.headerFooter (object)
Configure headers and footers on slides. Positions available:
`topLeft`, `topCenter`, `topRight`, `bottomLeft`, `bottomCenter`, `bottomRight`

Each position takes:
- `type`: "text", "image", or "cardNumber"
- For text: `value` (string)
- For image: `source` ("themeLogo" or "custom"), `size` ("sm"/"md"/"lg"/"xl")
  - If custom: `src` (image URL)
- `hideFromFirstCard`: boolean (default false)
- `hideFromLastCard`: boolean (default false)

Example — logo top-right, page numbers bottom-right, hidden on title slide:
```json
{
  "topRight": { "type": "image", "source": "themeLogo", "size": "sm" },
  "bottomRight": { "type": "cardNumber" },
  "hideFromFirstCard": true
}
```

## sharingOptions (object)

### workspaceAccess (string)
Access for workspace members: `noAccess`, `view`, `comment`, `edit`, `fullAccess`

### externalAccess (string)
Access for external users: `noAccess`, `view`, `comment`, `edit`

### emailOptions (object)
- `recipients`: array of email addresses
- `access`: "view", "comment", "edit", "fullAccess"

## Response Format

### POST generate returns:
```json
{
  "generationId": "abc123",
  "status": "pending",
  "gammaUrl": "https://gamma.app/generations/abc123"
}
```

The gammaUrl will redirect to the final deck once generation completes (typically 1-2 minutes).
If exportAs was set, the export download link will be available in the completed response.
