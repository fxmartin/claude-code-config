# Check Releases

Analyze the latest release notes for Homebrew, Nix, nix-darwin, and Ollama.

## What This Does

1. **Fetches release notes** from:
   - Homebrew (outdated packages)
   - NixOS/nixpkgs (GitHub releases)
   - LnL7/nix-darwin (GitHub releases)
   - Ollama (installed models + GitHub releases)

2. **Analyzes with Claude** to identify:
   - Security updates (critical)
   - Breaking changes
   - New features relevant to Python, Podman, AI tools
   - Interesting Ollama models
   - Notable dependency updates

3. **Reports findings** with actionable recommendations

## Instructions

Run the release monitor workflow:

```bash
# Full workflow (fetch + analyze + create issues + email)
~/Documents/nix-install/scripts/release-monitor.sh

# Or step by step:

# Step 1: Fetch release notes
~/Documents/nix-install/scripts/fetch-release-notes.sh /tmp/release-notes.json

# Step 2: Analyze with Claude
~/Documents/nix-install/scripts/analyze-releases.sh /tmp/release-notes.json /tmp/analysis.json

# Step 3: View results
cat /tmp/analysis.json | jq .
```

## Automated Schedule

When configured, this runs automatically every Monday at 7 AM via launchd.

## Files

- **Log**: `~/.local/log/release-monitor.log`
- **Release notes**: `/tmp/release-notes-*.json`
- **Analysis**: `/tmp/analysis-results-*.json`
- **Created issues**: `/tmp/created-issues-*.json`
