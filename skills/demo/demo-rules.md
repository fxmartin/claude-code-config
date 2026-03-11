# Demo Execution Rules

## Step 1: Discover What to Demo

### Finding Completed Stories
1. Read `STORIES.md` for navigation overview
2. Scan `stories/epic-*.md` files for stories marked as completed/done
3. Prioritize the most recently completed epic or sprint
4. If no stories directory exists, ask the user what to demo

### Identifying Demo-able Features
From each completed story, extract:
- **User story**: The "As a... I want... So that..." statement
- **Acceptance criteria**: These become your demo steps
- **UI components**: Pages, forms, buttons, flows to navigate
- **API endpoints**: If relevant, verify via network requests

## Step 2: Detect the Application URL

Priority order:
1. URL provided via `url:` argument
2. Check for running dev servers on common ports (3000, 3001, 4000, 5000, 5173, 8000, 8080)
3. Check `package.json` for dev/start scripts and their configured ports
4. Check `.env` or `.env.local` for PORT or BASE_URL variables
5. Check `README.md` for documented URLs
6. Ask the user if nothing is detected

Verify the URL is reachable before starting the demo:
- Navigate to the URL with `browser_navigate`
- Take a snapshot to confirm the page loaded
- If it fails, suggest the user start the dev server first

## Step 3: Generate the Demo Script

Create a structured demo script with this format:

```
# Demo Script: [Epic/Story Name]
Date: [today]
App URL: [detected URL]

## Feature 1: [Story Title]
Story: [story ID from epic file]
Status: Completed

### Steps:
1. Navigate to [page] — verify [expected element]
2. Click [element] — verify [expected result]
3. Fill [form] with [test data] — verify [validation/response]
4. Assert [acceptance criterion met]

## Feature 2: [Story Title]
...
```

Present this script to the user and ask: **"Ready to run this demo? (yes/edit/skip)"**

## Step 4: Execute the Demo

### TTS Narration (Voice Output)

ElevenLabs provides high-quality AI voice narration during demos. Audio is generated via API, saved to a temp file, and played in the background with `afplay` so it never blocks browser actions.

**Requires:** `ELEVENLABS_API_KEY` environment variable.

**Step 0 — Load environment (once at demo start):**
```bash
source ~/.claude/.env 2>/dev/null
```
If `ELEVENLABS_API_KEY` is still unset after sourcing and `--tts:say` is not specified, warn the user and fall back to text-only narration.

**Flag parsing from `$ARGUMENTS`:**
- No flags → ElevenLabs TTS with default voice (`Rachel`)
- `--silent` → TTS disabled, text-only narration (original behavior)
- `--voice:<name>` → ElevenLabs TTS with specified voice (e.g., `--voice:Drew`)
- `--tts:say` → macOS `say` fallback (offline, no API key needed)
- `--tts:say:<voice>` → macOS `say` with specific voice (e.g., `--tts:say:Daniel`)

**Step 1 — Resolve the ElevenLabs voice ID (once at demo start):**
```bash
# List available voices and find the ID for the chosen voice name
curl -s -H "xi-api-key: $ELEVENLABS_API_KEY" \
  "https://api.elevenlabs.io/v1/voices" | \
  python3 -c "import sys,json; voices=json.load(sys.stdin)['voices']; print(next((v['voice_id'] for v in voices if v['name'].lower()=='rachel'), ''))"
```
Cache the resolved `voice_id` for the entire demo session.

**Step 2 — The `narrate()` pattern — run before each browser action:**
```bash
# Only if TTS is NOT --silent:
NARRATION_FILE=$(mktemp /tmp/demo-narration-XXXXX.mp3)
curl -s -X POST "https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}" \
  -H "xi-api-key: $ELEVENLABS_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"<narration text>\", \"model_id\": \"eleven_multilingual_v2\", \"voice_settings\": {\"stability\": 0.5, \"similarity_boost\": 0.75}}" \
  -o "$NARRATION_FILE" && afplay "$NARRATION_FILE" &
```

- Always run with `&` (background) so the browser action proceeds immediately
- Between features, add `sleep 1` for a natural pause before the next narration
- Temp files are cleaned up automatically on macOS reboot (`/tmp`)

**macOS `say` fallback (when using `--tts:say`):**
```bash
say -v Daniel -r 180 "<narration text>" &
```

**Recommended ElevenLabs voices:**
| Voice | Style | Best For |
|-------|-------|----------|
| `Rachel` | Calm, professional | Product demos (default) |
| `Drew` | Confident, warm | Executive presentations |
| `Clyde` | Deep, authoritative | Technical walkthroughs |
| `Domi` | Assertive, clear | Feature announcements |
| `Bella` | Friendly, engaging | User onboarding demos |

**Narration text guidelines for speech:**
- Keep sentences under 15 words — short and clear for listeners
- Avoid technical jargon — speak as if presenting to a stakeholder
- Use contractions naturally ("we'll see", "that's confirmed")
- Don't read out URLs, CSS selectors, or code — describe what's happening

**Example flow:**
```bash
# Resolve voice ID once at demo start
VOICE_ID="21m00Tcm4TlvDq8ikWAM"  # Rachel

# TTS narration (background, non-blocking)
NARRATION_FILE=$(mktemp /tmp/demo-narration-XXXXX.mp3)
curl -s -X POST "https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}" \
  -H "xi-api-key: $ELEVENLABS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Opening the dashboard. We can see the main metrics panel.", "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}' \
  -o "$NARRATION_FILE" && afplay "$NARRATION_FILE" &
# Then immediately proceed with the browser action:
# → browser_navigate to dashboard URL
# → browser_snapshot to verify

# Between features:
sleep 1
NARRATION_FILE=$(mktemp /tmp/demo-narration-XXXXX.mp3)
curl -s -X POST "https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}" \
  -H "xi-api-key: $ELEVENLABS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Now let us look at the user management feature.", "model_id": "eleven_multilingual_v2", "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}' \
  -o "$NARRATION_FILE" && afplay "$NARRATION_FILE" &
# → browser_navigate to users page
```

For each demo step:

### Navigation Steps
1. Narrate via TTS (background): short description of where we're going and what to expect
2. Use `browser_navigate` to go to the target URL
3. Use `browser_snapshot` to verify the page loaded correctly
4. Narrate (text): "**Navigating to [page name]** — We can see [key elements visible]"

### Interaction Steps
1. Narrate via TTS (background): describe the action and its purpose
2. Use appropriate MCP tools (`browser_click`, `browser_fill_form`, `browser_type`, etc.)
3. After each interaction, use `browser_snapshot` to verify the result
4. Narrate (text): "**[Action]** — [What happened and why it matters]"

### Verification Steps
1. Use `browser_snapshot` to check accessibility tree for expected elements
2. Use `browser_take_screenshot` to capture visual evidence
3. Use `browser_console_messages` to check for errors
4. Use `browser_network_requests` to verify API calls if relevant
5. Narrate via TTS (background): confirm what was verified
6. Narrate (text): "**Verified**: [acceptance criterion] — PASS/FAIL"

### Between Features
- `sleep 1` for a natural pause
- Narrate via TTS (background): brief transition ("Now let's look at...")
- Provide text transition: "Moving on to the next feature..."
- Reset state if needed (navigate to home, clear forms)

## Step 5: Generate Demo Report

After completing all demo steps, generate a report:

```markdown
# Demo Report
Date: [timestamp]
App URL: [url]
Stories Demonstrated: [count]

## Results Summary
| Story | Feature | Status | Notes |
|-------|---------|--------|-------|
| [id]  | [name]  | PASS/FAIL | [details] |

## Screenshots
- [step]: [screenshot description]

## Issues Found
- [any issues encountered during demo]

## Acceptance Criteria Verification
### [Story ID]: [Story Title]
- [x] Criterion 1 — verified via [method]
- [x] Criterion 2 — verified via [method]
- [ ] Criterion 3 — FAILED: [reason]
```

Save this report to `docs/demo-reports/demo-[date].md` (create directory if needed).

## Demo Best Practices

### Narration Style
- Be professional and concise — this is a stakeholder demo
- Explain the "what" and "why", not the technical "how"
- Use business language, not developer jargon
- Highlight user value, not implementation details

### Handling Failures
- If a page doesn't load: note it, try once more, then skip with a note
- If an element isn't found: take a screenshot, note the discrepancy, continue
- If a feature is partially working: demo what works, note what doesn't
- Never spend more than 2 attempts on a failing step

### Test Data
- Use realistic but obviously fake data (e.g., "Jane Demo", "demo@example.com")
- Never use real credentials or personal information
- If the app requires authentication, ask the user for test credentials
- Clean up any data created during the demo if possible

### Performance
- Don't rush — pause briefly between major steps for readability
- Group related actions together
- Skip repetitive variations (demo the pattern once, mention others exist)
