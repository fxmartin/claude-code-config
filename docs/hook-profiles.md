# Hook profiles & context controls

> Epic-15 · Story 15.2-001 — tune hook strictness and SessionStart context size
> from the environment, without editing any hook script.

Hooks ship with safe defaults. When you need to run lean on a low-context setup,
or lock everything down on a hardened one, three environment variables let you
adjust behavior without touching the scripts. **Leaving all of them unset
preserves today's behavior exactly.**

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `SDLC_HOOK_PROFILE` | `minimal` \| `standard` \| `strict` | `standard` | Overall strictness. |
| `SDLC_DISABLED_HOOKS` | space/comma list of hook names | _(empty)_ | Named hooks are skipped; the rest still run. |
| `SDLC_SESSION_CONTEXT_MAX` | integer (characters) | `0` (unlimited) | Caps the size of SessionStart-injected context. |

## Profiles

- **`minimal`** — runs only essential and guardrail work. Non-essential
  *notification* and *sidebar* hooks (e.g. Telegram pings) are skipped. Use this
  on a constrained box where you want the framework to stay out of the way.
- **`standard`** — the default. Every hook runs unless you explicitly disable it.
- **`strict`** — runs everything, and **guardrail-class hooks cannot be
  disabled**: a `SDLC_DISABLED_HOOKS` entry naming a guardrail is ignored, so the
  protection stays on. Non-guardrail hooks can still be disabled.

A typo in `SDLC_HOOK_PROFILE` falls back to `standard` rather than failing.

## Disable list

`SDLC_DISABLED_HOOKS` names hooks to skip. Whitespace and commas are equivalent,
so both of these skip the Telegram notifier while leaving everything else alone:

```sh
export SDLC_DISABLED_HOOKS="notify-telegram"
export SDLC_DISABLED_HOOKS="notify-telegram,worktree-gc"
```

Under the `strict` profile, an entry that names a guardrail-class hook has no
effect — guardrails are not user-disablable in strict mode.

## SessionStart context cap

`SDLC_SESSION_CONTEXT_MAX` caps, in characters, the context a SessionStart hook
injects. Unset, `0`, or a non-numeric value means *unlimited* (today's
behavior); a positive integer truncates longer context to that many characters.

```sh
export SDLC_SESSION_CONTEXT_MAX=4000   # cap injected context at 4000 chars
```

The registered SessionStart hook `hooks/session-context.sh` is the reference
consumer: it injects a concise, secret-free project/branch banner and pipes it
through the cap. As *sidebar*-class work it is also skipped under the `minimal`
profile or when `SDLC_DISABLED_HOOKS` names `session-context`, and it is a silent
no-op outside a git repository.

## How it works

The controls live in a single sourceable helper, `hooks/hook-profile.sh`. Any
hook opts in by sourcing it and asking whether it should run (or by emitting its
context through the cap):

```sh
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=hooks/hook-profile.sh
. "$HOOK_DIR/hook-profile.sh"

# Skip this hook when the profile/disable-list says so. The second argument is
# the hook's class: essential | notification | sidebar | guardrail.
hook_should_run notify-telegram notification || exit 0

# Emit SessionStart context truncated to SDLC_SESSION_CONTEXT_MAX (if set).
printf '%s' "$context" | hook_emit_context
```

The helper degrades gracefully: a hook that cannot find or source it simply
behaves as it always has. `hooks/notify-telegram.sh` is the reference consumer —
it classifies itself as a `notification`, so `minimal` and an explicit disable
entry both turn it into a silent no-op.

See `tests/hook-profile.bats` for the behavioral contract.
