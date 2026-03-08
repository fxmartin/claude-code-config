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

printf '%s%s | %s%s' "$cwd_display" "$branch_seg" "$model" "$ctx_seg"
