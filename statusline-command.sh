#!/usr/bin/env bash
# ABOUTME: Claude Code status line command - mirrors Starship default prompt style

input=$(cat)

cwd=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // ""')

# Shorten home directory to ~
home="$HOME"
cwd_display="${cwd/#$home/\~}"

model=$(echo "$input" | jq -r '.model.display_name // ""')
branch=$(git -C "$cwd" rev-parse --abbrev-ref HEAD 2>/dev/null || true)
remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')

# Build context segment
if [ -n "$remaining" ]; then
  used=$(( 100 - remaining ))
  filled=$(( (used * 20 + 50) / 100 ))
  empty=$(( 20 - filled ))
  bar=$(printf '%0.s█' $(seq 1 "$filled" 2>/dev/null))
  bar+=$(printf '%0.s░' $(seq 1 "$empty" 2>/dev/null))

  if [ "$remaining" -le 20 ] 2>/dev/null; then
    ctx_seg=" | ⚠️  ${bar} ${used}%"
  else
    ctx_seg=" | ${bar} ${used}%"
  fi
else
  ctx_seg=""
fi

# Build branch segment
if [ -n "$branch" ]; then
  branch_seg=" |  ${branch}"
else
  branch_seg=""
fi

# Build rate-limit segment (Claude.ai subscription only — absent for API users)
five_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
week_pct=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')

if [ -n "$five_pct" ] || [ -n "$week_pct" ]; then
  rl_label=""
  if [ -n "$five_pct" ]; then
    five_int=$(printf '%.0f' "$five_pct")
    if [ "$five_int" -ge 80 ] 2>/dev/null; then
      rl_label="⚠️  rl: ${five_int}%"
    else
      rl_label="rl: ${five_int}%"
    fi
  fi
  if [ -n "$week_pct" ]; then
    week_int=$(printf '%.0f' "$week_pct")
    if [ -n "$rl_label" ]; then
      rl_label="${rl_label}/7d:${week_int}%"
    else
      rl_label="rl: 7d:${week_int}%"
    fi
  fi
  rl_seg=" | ${rl_label}"
else
  rl_seg=""
fi

printf '%s%s | %s%s%s' "$cwd_display" "$branch_seg" "$model" "$ctx_seg" "$rl_seg"
