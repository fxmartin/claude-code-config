#!/usr/bin/env bash
#
# sdlc-state.sh — durable ledger CLI for `build-stories` runs (Story 4.1-001).
#
# Today the truth source for a `build-stories` run is `docs/stories/.build-progress.md`.
# Markdown cannot reliably carry typed state across a crash. This script is the
# foundation of Epic-04: a tiny wrapper over `sqlite3` that owns the database
# lifecycle (init/migrate/show/prune/backup) so that the orchestrator and every
# dispatched agent can write structured state without learning SQL.
#
# This script is intentionally minimal. Stories 4.2-001 / 4.2-002 / 4.3-001
# will add write helpers, a markdown renderer, and a resume subcommand — they
# will reuse the migration runner and `--db` flag established here.
#
# Usage:
#   sdlc-state.sh [--db <path>] init
#   sdlc-state.sh [--db <path>] migrate
#   sdlc-state.sh [--db <path>] show <run-id>
#   sdlc-state.sh [--db <path>] prune --older-than <duration>
#   sdlc-state.sh [--db <path>] backup <dest>
#   sdlc-state.sh [--db <path>] render [--out <path>]
#   sdlc-state.sh [--db <path>] render-all [--out <path>]
#
# Write path (Story 4.2-001 — orchestrator + agents call these via
# ~/.claude/hooks/sdlc-state-emit.sh, never raw SQL):
#   sdlc-state.sh [--db <path>] run-create <scope> <mode>                          # prints new run_id
#   sdlc-state.sh [--db <path>] run-update-status <run_id> <status>
#   sdlc-state.sh [--db <path>] story-upsert <run_id> <story_id> <epic_id> <title> <priority> <points> <agent_type> <branch> <pr_number> <status>
#   sdlc-state.sh [--db <path>] stage-start <run_id> <story_id> <stage_name> [attempt]
#   sdlc-state.sh [--db <path>] stage-finish <run_id> <story_id> <stage_name> <attempt> <status> <failure_category> <output_path>
#   sdlc-state.sh [--db <path>] event-log <run_id> <story_id> <level> <source> <message>
#
# Default DB path: `.sdlc-state.db` in the current directory. Tests override
# with `--db <tmpfile>` so the real ledger is never touched.
#
# Duration format for `prune --older-than`:
#   <N>d  N days   (e.g. 7d, 30d)
#   <N>h  N hours
#   <N>m  N minutes
#
# Exit status:
#   0  success.
#   1  usage error or runtime failure (missing sqlite3, unknown run-id, etc.).
#   2  malformed argument (e.g. unparseable duration).

set -euo pipefail

# --- Resolve script paths -------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MIGRATIONS_DIR="${REPO_ROOT}/state/migrations"

# --- Usage helper ---------------------------------------------------------

usage() {
    cat >&2 <<'EOF'
Usage: sdlc-state.sh [--db <path>] <subcommand> [args...]

Lifecycle subcommands:
  init                          Create the DB and apply all migrations.
  migrate                       Apply any pending migrations (idempotent).
  show <run-id>                 Print a run's summary and its stories.
  prune --older-than <duration> Delete DONE/FAILED runs finished before the cutoff.
  backup <dest>                 Write a consistent SQLite-level copy to <dest>.
  render [--out <path>]         Render the .build-progress.md view from SQLite
                                (read-only). Without --out, writes to stdout.
  render-all [--out <path>]     Alias for `render` — provided so callers that
                                bootstrap a missing .build-progress.md can ask
                                for a full regeneration without ambiguity.

Write-path subcommands (Story 4.2-001):
  run-create <scope> <mode>                                            Print a fresh run_id and INSERT a runs row.
  run-update-status <run_id> <status>                                  Transition runs.status (stamps finished_at on terminal states).
  story-upsert <run_id> <story_id> <epic_id> <title> <priority>
               <points> <agent_type> <branch> <pr_number> <status>     INSERT OR REPLACE a stories row.
  stage-start <run_id> <story_id> <stage_name> [attempt]               Append an IN_PROGRESS stages row (default attempt=1).
  stage-finish <run_id> <story_id> <stage_name> <attempt> <status>
               <failure_category> <output_path>                        UPDATE the stages row to a terminal status.
  event-log <run_id> <story_id> <level> <source> <message>             Append an events row.

Default --db path: .sdlc-state.db in the current directory.

Examples:
  sdlc-state.sh init
  sdlc-state.sh --db /tmp/test.db migrate
  sdlc-state.sh show 7a3f-...
  sdlc-state.sh prune --older-than 14d
  sdlc-state.sh backup .sdlc-state.db.bak
  RUN_ID=$(sdlc-state.sh run-create epic-04 parallel)
  sdlc-state.sh stage-start "$RUN_ID" 4.2-001 build 1
EOF
}

err() {
    echo "error: $*" >&2
}

require_sqlite3() {
    if ! command -v sqlite3 >/dev/null 2>&1; then
        err "sqlite3 CLI is required but not on PATH"
        exit 1
    fi
}

# --- Argument parsing -----------------------------------------------------

DB_PATH=".sdlc-state.db"

# Strip leading --db <path> if present.
while [ $# -gt 0 ]; do
    case "$1" in
        --db)
            if [ $# -lt 2 ]; then
                err "--db requires a path argument"
                usage
                exit 1
            fi
            DB_PATH="$2"
            shift 2
            ;;
        --db=*)
            DB_PATH="${1#--db=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            break
            ;;
    esac
done

if [ $# -lt 1 ]; then
    usage
    exit 1
fi

SUBCOMMAND="$1"
shift

require_sqlite3

# --- Migration runner -----------------------------------------------------
#
# Reads `state/migrations/NNN-<name>.sql` files in numeric order. Each file's
# numeric prefix is its version. The `_migrations` table records applied
# versions; migrations with a version <= the max recorded version are skipped.
# Each migration is wrapped in BEGIN/COMMIT so a syntax error rolls back
# cleanly without leaving the schema half-built.

ensure_bookkeeping_table() {
    sqlite3 "${DB_PATH}" <<'SQL'
CREATE TABLE IF NOT EXISTS _migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
SQL
}

enable_wal() {
    # WAL persists in the DB header so this is a one-time setup. Re-running is harmless.
    sqlite3 "${DB_PATH}" "PRAGMA journal_mode=WAL;" >/dev/null
}

current_version() {
    sqlite3 "${DB_PATH}" "SELECT COALESCE(MAX(version), 0) FROM _migrations;"
}

# Print "<version>\t<name>\t<path>" for every migration file, sorted by version.
list_migrations() {
    local f base version name
    if [ ! -d "${MIGRATIONS_DIR}" ]; then
        return 0
    fi
    # The glob is stable: NNN-<name>.sql with N in [0-9]. `LC_ALL=C` makes the
    # sort behavior reproducible across locales.
    LC_ALL=C find "${MIGRATIONS_DIR}" -maxdepth 1 -type f -name '[0-9][0-9][0-9]-*.sql' -print0 \
        | LC_ALL=C sort -z \
        | while IFS= read -r -d '' f; do
            base="$(basename "${f}")"
            version="${base%%-*}"
            # Strip leading zeros so 001 becomes 1 (integer compare in SQLite).
            version=$((10#${version}))
            name="${base#*-}"
            name="${name%.sql}"
            printf '%s\t%s\t%s\n' "${version}" "${name}" "${f}"
        done
}

apply_pending_migrations() {
    ensure_bookkeeping_table
    enable_wal

    local current applied=0
    current="$(current_version)"

    local version name path
    while IFS=$'\t' read -r version name path; do
        if [ "${version}" -le "${current}" ]; then
            continue
        fi
        # Wrap each migration in a transaction so a failure rolls back cleanly.
        if ! sqlite3 "${DB_PATH}" <<SQL
BEGIN;
$(cat "${path}")
INSERT INTO _migrations(version, name) VALUES (${version}, '${name//\'/\'\'}');
COMMIT;
SQL
        then
            err "migration ${version}-${name} failed; database rolled back"
            exit 1
        fi
        echo "applied: ${version}-${name}"
        applied=$((applied + 1))
    done < <(list_migrations)

    if [ "${applied}" -eq 0 ]; then
        echo "schema is up to date (0 applied)"
    else
        echo "${applied} applied"
    fi
}

# --- Duration parser ------------------------------------------------------
#
# Convert "<N><unit>" into a SQLite datetime modifier like "-7 days".
parse_duration() {
    local raw="$1"
    local n unit
    if [[ ! "${raw}" =~ ^([0-9]+)([dhm])$ ]]; then
        err "invalid duration '${raw}'; expected <N>d|<N>h|<N>m"
        exit 2
    fi
    n="${BASH_REMATCH[1]}"
    unit="${BASH_REMATCH[2]}"
    case "${unit}" in
        d) echo "-${n} days" ;;
        h) echo "-${n} hours" ;;
        m) echo "-${n} minutes" ;;
    esac
}

# --- Write-path helpers (Story 4.2-001) -----------------------------------
#
# Safety: all interpolated TEXT values are escaped via `sql_quote` (single-quote
# doubling, then wrapped in single quotes). Integer columns (points, pr_number,
# attempt) are coerced through arithmetic expansion so any non-numeric input
# becomes 0, never SQL. NULL is the literal token, distinct from 'NULL' the
# string. This keeps every write parameterized without requiring a sqlite3
# `.param set` round-trip per call (the `--cmd` flow does not preserve
# binding across statements in older sqlite3 builds shipped with macOS).

# Quote a TEXT value for safe single-quoted SQL interpolation.
# An empty string is preserved (returned as ''), not turned into NULL —
# callers that want NULL pass it explicitly.
sql_quote() {
    local v="${1-}"
    # Replace every ' with '' (SQL standard escape for embedded apostrophes).
    printf "'%s'" "${v//\'/\'\'}"
}

# Return the literal SQL token 'NULL' if empty, else a quoted string.
# Used for nullable TEXT columns where empty input should round-trip as NULL.
sql_quote_or_null() {
    local v="${1-}"
    if [ -z "${v}" ]; then
        printf 'NULL'
    else
        sql_quote "${v}"
    fi
}

# Return an integer literal or NULL for nullable INTEGER columns.
# Anything non-numeric collapses to NULL — we never let user input reach
# the SQL layer as a bare token.
sql_int_or_null() {
    local v="${1-}"
    if [ -z "${v}" ]; then
        printf 'NULL'
        return
    fi
    if [[ "${v}" =~ ^[0-9]+$ ]]; then
        printf '%d' "${v}"
    else
        printf 'NULL'
    fi
}

# Generate a UUIDv4-ish run identifier. `uuidgen` is part of macOS BSD base
# (and most Linux distros via util-linux); fall back to /dev/urandom so the
# script never depends on an external package.
new_run_id() {
    if command -v uuidgen >/dev/null 2>&1; then
        uuidgen | tr '[:upper:]' '[:lower:]'
    else
        local hex
        hex=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
        printf '%s-%s-%s-%s-%s\n' \
            "${hex:0:8}" "${hex:8:4}" "${hex:12:4}" "${hex:16:4}" "${hex:20:12}"
    fi
}

# Run a SQL statement and propagate failure. Foreign keys are explicitly
# enabled per-connection — sqlite3 does NOT inherit FK enforcement from the
# DB header, so every write helper must set it.
db_exec() {
    local sql="$1"
    sqlite3 "${DB_PATH}" <<SQL
PRAGMA foreign_keys = ON;
${sql}
SQL
}

# Return the SELECT result as a single token (or empty string on no rows).
db_scalar() {
    local sql="$1"
    sqlite3 "${DB_PATH}" "PRAGMA foreign_keys = ON; ${sql}"
}

# --- Markdown renderer (Story 4.2-002) ------------------------------------
#
# The renderer is intentionally implemented in pure bash + sqlite3. Keeping
# it inside `sdlc-state.sh` (rather than spawning a separate Python/awk
# tool) means the hook layer can reuse the existing `--db` path discovery
# and graceful-degradation contract without learning a new binary.
#
# Output shape (matches the legacy hand-maintained `.build-progress.md`):
#
#   # Build Progress
#   <!-- AUTOGENERATED ... -->
#   ## Historical (pre-ledger)
#   <verbatim snapshot of pre-ledger history>
#   ## Run <id>           (one block per run, newest first or as recorded)
#   - **Scope**: ...
#   - **Mode**: ...
#   - **Started**: ...
#   - **Total stories**: N
#   ### Epic-NN Build Log
#   | Story ID | Title | Status | Branch | PR | Started | Completed |
#   ...
#   ## E2E Gate
#   | Epic | Result | Notes |
#
# The renderer never WRITES to the DB. PRAGMA query_only=1 is asserted on
# every query connection so a buggy migration cannot accidentally land via
# the read path.

# Locate the historical pre-ledger snapshot. The renderer is robust to it
# being absent (tests using temp DBs do not require the snapshot file).
HISTORICAL_SNAPSHOT="${REPO_ROOT}/docs/stories/.build-progress.historical.md"

# Markdown-escape a cell value:
#   * `|` becomes `\|` (GFM escape) so a title with a pipe does not break
#     the table layout.
#   * Newlines collapse to a single space — multi-line cells confuse most
#     markdown renderers.
# This is applied AFTER values come out of SQLite and are safe to print.
md_escape_cell() {
    local v="${1-}"
    # Replace every '|' with '\|', then strip newlines.
    v="${v//|/\\|}"
    v="${v//$'\n'/ }"
    v="${v//$'\r'/ }"
    printf '%s' "${v}"
}

# Emit "—" if a value is empty, else the value itself, after cell-escaping.
# Used for nullable columns so an empty cell renders consistently across
# story tables.
md_cell_or_dash() {
    local v="${1-}"
    if [ -z "${v}" ]; then
        printf '—'
    else
        md_escape_cell "${v}"
    fi
}

# Read-only sqlite3 helper. Asserting `query_only=1` is belt-and-braces:
# even if a future SELECT inadvertently embeds DML, the connection refuses
# to mutate. We use ASCII Unit Separator (US, 0x1F) as the column separator
# because user-supplied titles / branches / notes legitimately contain `|`
# and `\t`, and we still want bash `IFS=$'\x1f' read -r ...` to split
# rows safely.
SDLC_FIELD_SEP=$'\x1f'

db_select() {
    local sql="$1"
    sqlite3 -separator "${SDLC_FIELD_SEP}" "${DB_PATH}" "PRAGMA query_only=1; ${sql}"
}

# Emit the header block. The AUTOGENERATED banner is the contract that
# discourages hand edits — `sdlc-state show <run-id>` is the recommended
# direct-query path for humans.
render_header() {
    cat <<'MD'
# Build Progress

<!-- AUTOGENERATED FROM SQLite. EDITS WILL BE OVERWRITTEN. Run `sdlc-state show <run-id>` to query directly. -->

MD
}

# Stream the historical snapshot under its own H2 heading so the pre-ledger
# timeline survives the cut-over. Missing snapshot is a no-op — fresh repos
# without history just skip the section.
render_historical() {
    if [ -f "${HISTORICAL_SNAPSHOT}" ]; then
        echo "## Historical (pre-ledger)"
        echo
        cat "${HISTORICAL_SNAPSHOT}"
        echo
    fi
}

# Render the metadata block + per-epic story tables for a single run.
# Arguments:
#   $1  run_id
#   $2  scope
#   $3  mode
#   $4  started_at (string from SQLite, may be empty)
#   $5  total_stories (integer)
render_run_block() {
    local rid="$1" scope="$2" mode="$3" started="$4" total="$5"

    # Metadata block, one bullet per dimension. Matches the legacy shape.
    echo "## Run ${rid}"
    echo
    echo "- **Scope**: $(md_escape_cell "${scope:-—}")"
    echo "- **Mode**: $(md_escape_cell "${mode:-—}")"
    echo "- **Started**: $(md_escape_cell "${started:-—}")"
    echo "- **Total stories**: ${total:-0}"
    echo

    # Discover the epics that have stories under this run, sorted ascending.
    # NULL epic_id is filtered out — a story without an epic does not get
    # an epic-section heading.
    local epics
    epics=$(db_select "
        SELECT DISTINCT epic_id
          FROM stories
         WHERE run_id = $(sql_quote "${rid}")
           AND epic_id IS NOT NULL
           AND epic_id != ''
         ORDER BY epic_id;
    ")

    if [ -z "${epics}" ]; then
        # The run has no stories yet. Still render an empty Build Log so
        # the section is detectable by tooling.
        echo "### Build Log"
        echo
        echo "| Story ID | Title | Status | Branch | PR | Started | Completed |"
        echo "|----------|-------|--------|--------|----|---------|-----------|"
        echo "_(no stories recorded)_"
        echo
        return 0
    fi

    local epic
    while IFS= read -r epic; do
        [ -z "${epic}" ] && continue
        # Pad single-digit epic IDs to two digits in the heading for
        # consistent sort order in tooling that reads the markdown.
        local epic_label="${epic}"
        if [[ "${epic_label}" =~ ^[0-9]$ ]]; then
            epic_label="0${epic_label}"
        fi
        echo "## Epic-${epic_label} Build Log"
        echo
        echo "| Story ID | Title | Status | Branch | PR | Started | Completed |"
        echo "|----------|-------|--------|--------|----|---------|-----------|"

        # Pull the stories for this (run, epic) pair, ordered deterministically.
        # `started_at` and `finished_at` for the STORY are derived from the
        # MIN/MAX timestamps on its stages — the schema does not store them
        # on the stories table directly.
        local rows
        rows=$(db_select "
            SELECT
                REPLACE(REPLACE(s.story_id, char(10), ' '), char(13), ''),
                REPLACE(REPLACE(COALESCE(s.title, ''), char(10), ' '), char(13), ''),
                COALESCE(s.status, ''),
                REPLACE(REPLACE(COALESCE(s.branch, ''), char(10), ' '), char(13), ''),
                COALESCE(CAST(s.pr_number AS TEXT), ''),
                COALESCE((SELECT MIN(started_at)
                            FROM stages
                           WHERE run_id = s.run_id
                             AND story_id = s.story_id), ''),
                COALESCE((SELECT MAX(finished_at)
                            FROM stages
                           WHERE run_id = s.run_id
                             AND story_id = s.story_id
                             AND status IN ('DONE','FAILED','ABORTED')), '')
              FROM stories s
             WHERE s.run_id = $(sql_quote "${rid}")
               AND s.epic_id = $(sql_quote "${epic}")
             ORDER BY s.story_id;
        ")
        if [ -z "${rows}" ]; then
            echo "_(no stories recorded)_"
        else
            local story_id title status branch pr started_at finished_at
            # IFS is the ASCII Unit Separator so titles / branches that
            # contain `|` round-trip into the markdown unchanged.
            while IFS=$'\x1f' read -r story_id title status branch pr started_at finished_at; do
                [ -z "${story_id}" ] && continue
                # PR is rendered as "#NNN" if numeric, "—" otherwise.
                local pr_cell="—"
                if [[ "${pr}" =~ ^[0-9]+$ ]]; then
                    pr_cell="#${pr}"
                fi
                printf '| %s | %s | %s | %s | %s | %s | %s |\n' \
                    "$(md_escape_cell "${story_id}")" \
                    "$(md_cell_or_dash "${title}")" \
                    "$(md_cell_or_dash "${status}")" \
                    "$(md_cell_or_dash "${branch}")" \
                    "${pr_cell}" \
                    "$(md_cell_or_dash "${started_at}")" \
                    "$(md_cell_or_dash "${finished_at}")"
            done <<< "${rows}"
        fi
        echo
    done <<< "${epics}"
}

# Render the global E2E Gate section. Convention (matches story 4.2-001):
# e2e-gate results are stored as event rows with source='e2e-gate' and
# message formatted as "<epic>|<result>|<notes>". The renderer pulls all
# such events across all runs, sorts them by epic ascending then by event
# id ascending (deterministic order), and emits one row per event.
render_e2e_gate() {
    echo "## E2E Gate"
    echo
    echo "| Epic | Result | Notes |"
    echo "|------|--------|-------|"
    # The message itself embeds `|` separators (epic|result|notes), so we
    # cannot use `|` again as the row separator out of sqlite3. Pull only
    # the message column and split it in bash; sort deterministically by
    # the full message string (ties broken by event id via a stable
    # subquery — but in practice message uniqueness is enough).
    local rows
    rows=$(sqlite3 "${DB_PATH}" "PRAGMA query_only=1;
        SELECT message FROM events
         WHERE source = 'e2e-gate'
         ORDER BY message, id;
    ")
    if [ -z "${rows}" ]; then
        echo "_(no E2E gate runs recorded in the ledger yet)_"
        echo
        return 0
    fi
    local msg epic result notes rest
    while IFS= read -r msg; do
        [ -z "${msg}" ] && continue
        # epic|result|notes — split on the first two `|`, treat the rest
        # as notes so a `|` inside notes does not corrupt the row.
        epic="${msg%%|*}"
        rest="${msg#*|}"
        result="${rest%%|*}"
        notes="${rest#*|}"
        # If there was no third `|`, notes equals rest equals result —
        # render an empty notes cell.
        if [ "${notes}" = "${rest}" ] && [ "${result}" = "${rest}" ]; then
            notes=""
        fi
        printf '| %s | %s | %s |\n' \
            "$(md_escape_cell "${epic}")" \
            "$(md_escape_cell "${result}")" \
            "$(md_cell_or_dash "${notes}")"
    done <<< "${rows}"
    echo
}

# Top-level renderer. Iterates all runs (deterministic order), emits the
# header, the historical snapshot, every run's block, and finally the E2E
# Gate section. The trailing newline is intentional — POSIX text files end
# in a newline.
render_document() {
    render_header
    render_historical
    # Iterate runs in a stable order: started_at ascending, then id ascending
    # as a tie-breaker (UUIDs sort lexicographically — fine for determinism).
    local runs
    runs=$(db_select "
        SELECT id,
               COALESCE(scope, ''),
               COALESCE(mode, ''),
               COALESCE(started_at, ''),
               COALESCE(total_stories, 0)
          FROM runs
         ORDER BY started_at, id;
    ")
    if [ -n "${runs}" ]; then
        local rid rscope rmode rstarted rtotal
        while IFS=$'\x1f' read -r rid rscope rmode rstarted rtotal; do
            [ -z "${rid}" ] && continue
            render_run_block "${rid}" "${rscope}" "${rmode}" "${rstarted}" "${rtotal}"
        done <<< "${runs}"
    fi
    render_e2e_gate
}

# Subcommand entry point. Parses `--out <path>` and orchestrates the
# atomic write. Without `--out`, output goes to stdout.
render_subcommand() {
    local out_path=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --out)
                if [ $# -lt 2 ]; then
                    err "render --out requires a path argument"
                    exit 1
                fi
                out_path="$2"
                shift 2
                ;;
            --out=*)
                out_path="${1#--out=}"
                shift
                ;;
            *)
                err "render: unknown argument: $1"
                exit 1
                ;;
        esac
    done

    # `render` is read-only — but the DB must exist for SELECTs to succeed.
    if [ ! -f "${DB_PATH}" ]; then
        err "render: database does not exist: ${DB_PATH}"
        exit 1
    fi

    if [ -z "${out_path}" ]; then
        render_document
        return 0
    fi

    # Atomic write: render into <out>.tmp, then rename. The rename is atomic
    # on POSIX filesystems so concurrent readers either see the old file or
    # the new one — never a partial render.
    local tmp_path="${out_path}.tmp"
    # Ensure the destination directory exists. We do NOT create deep paths
    # because the caller should know where they want the file — but the
    # parent dir is almost always docs/stories/, which already exists.
    local dest_dir
    dest_dir="$(dirname "${out_path}")"
    if [ ! -d "${dest_dir}" ]; then
        err "render --out: destination directory does not exist: ${dest_dir}"
        exit 1
    fi
    render_document >"${tmp_path}"
    mv "${tmp_path}" "${out_path}"
}

# --- Subcommand dispatch --------------------------------------------------

case "${SUBCOMMAND}" in
    init)
        # init is intentionally equivalent to migrate on a fresh DB so that
        # `init` and `migrate` converge on the same `_migrations` state.
        apply_pending_migrations
        ;;

    migrate)
        apply_pending_migrations
        ;;

    show)
        if [ $# -lt 1 ]; then
            err "show requires a <run-id> argument"
            exit 1
        fi
        run_id="$1"
        # Confirm the run exists before printing anything.
        exists=$(sqlite3 "${DB_PATH}" "SELECT COUNT(*) FROM runs WHERE id = '${run_id//\'/\'\'}';" 2>/dev/null || echo 0)
        if [ "${exists}" = "0" ]; then
            err "run not found: ${run_id}"
            exit 1
        fi
        echo "Run ${run_id}"
        echo "----"
        sqlite3 -header -column "${DB_PATH}" \
            "SELECT id, scope, mode, status, total_stories, completed, failed, started_at, finished_at
               FROM runs WHERE id = '${run_id//\'/\'\'}';"
        echo
        echo "Stories"
        echo "-------"
        sqlite3 -header -column "${DB_PATH}" \
            "SELECT story_id, epic_id, title, status, current_stage, branch, pr_number
               FROM stories WHERE run_id = '${run_id//\'/\'\'}'
               ORDER BY story_id;"
        ;;

    prune)
        if [ $# -lt 2 ] || [ "$1" != "--older-than" ]; then
            err "prune requires --older-than <duration>"
            usage
            exit 1
        fi
        cutoff_mod="$(parse_duration "$2")"
        # IN_PROGRESS runs are never pruned regardless of age — a long-running
        # build that has not yet emitted a finished_at must survive a prune.
        deleted=$(sqlite3 "${DB_PATH}" <<SQL
DELETE FROM runs
 WHERE status != 'IN_PROGRESS'
   AND finished_at IS NOT NULL
   AND finished_at < datetime('now', '${cutoff_mod}');
SELECT changes();
SQL
)
        echo "pruned ${deleted} runs older than ${2}"
        ;;

    backup)
        if [ $# -lt 1 ]; then
            err "backup requires a destination path"
            exit 1
        fi
        dest="$1"
        if [ ! -f "${DB_PATH}" ]; then
            err "source DB does not exist: ${DB_PATH}"
            exit 1
        fi
        # Use the SQLite-level `.backup` so it is consistent across active WAL
        # writers — this is the same primitive sqlite3 uses for online backups.
        sqlite3 "${DB_PATH}" ".backup '${dest}'"
        echo "backup written: ${dest}"
        ;;

    # --- Write path (Story 4.2-001) --------------------------------------
    #
    # Every write helper:
    #   * enables FOREIGN_KEYS so cross-table integrity is enforced
    #     (sqlite3 does NOT inherit this from the DB header);
    #   * escapes every TEXT value with sql_quote / sql_quote_or_null;
    #   * coerces every INTEGER value with sql_int_or_null;
    #   * fails fast (exit 1) on unknown IDs or missing rows so the
    #     orchestrator can route to the bugfix path instead of silently
    #     losing state.

    run-create)
        # Usage: run-create <scope> <mode>
        # Prints the new run_id on stdout (the orchestrator captures this and
        # exports it as SDLC_RUN_ID so sub-agents inherit it).
        if [ $# -lt 2 ]; then
            err "run-create requires <scope> <mode>"
            exit 1
        fi
        scope="$1"
        mode="$2"
        run_id="$(new_run_id)"
        db_exec "INSERT INTO runs(id, scope, mode, status, started_at)
                 VALUES ($(sql_quote "${run_id}"),
                         $(sql_quote "${scope}"),
                         $(sql_quote "${mode}"),
                         'IN_PROGRESS',
                         CURRENT_TIMESTAMP);"
        echo "${run_id}"
        ;;

    run-update-status)
        # Usage: run-update-status <run_id> <status>
        # Terminal statuses (DONE/FAILED/ABORTED) also stamp finished_at.
        if [ $# -lt 2 ]; then
            err "run-update-status requires <run_id> <status>"
            exit 1
        fi
        run_id="$1"
        new_status="$2"
        exists=$(db_scalar "SELECT COUNT(*) FROM runs WHERE id = $(sql_quote "${run_id}");")
        if [ "${exists}" = "0" ]; then
            err "run-update-status: unknown run_id: ${run_id}"
            exit 1
        fi
        case "${new_status}" in
            DONE|FAILED|ABORTED)
                db_exec "UPDATE runs
                            SET status = $(sql_quote "${new_status}"),
                                finished_at = CURRENT_TIMESTAMP
                          WHERE id = $(sql_quote "${run_id}");"
                ;;
            *)
                # IN_PROGRESS or any other non-terminal label: leave finished_at as-is
                # (which is NULL for the freshly-created row, or whatever it was).
                db_exec "UPDATE runs
                            SET status = $(sql_quote "${new_status}")
                          WHERE id = $(sql_quote "${run_id}");"
                ;;
        esac
        ;;

    story-upsert)
        # Usage: story-upsert <run_id> <story_id> <epic_id> <title> <priority>
        #                     <points> <agent_type> <branch> <pr_number> <status>
        # Uses ON CONFLICT DO UPDATE rather than INSERT OR REPLACE: the
        # latter is implemented as DELETE+INSERT in SQLite, which fires the
        # FK cascade on stages and wipes the per-attempt history. ON
        # CONFLICT keeps the existing row alive and only patches the
        # changed columns — exactly the semantics the orchestrator needs
        # when transitioning a story from IN_PROGRESS to DONE while its
        # stage rows must survive.
        if [ $# -lt 10 ]; then
            err "story-upsert requires <run_id> <story_id> <epic_id> <title> <priority> <points> <agent_type> <branch> <pr_number> <status>"
            exit 1
        fi
        run_id="$1"; story_id="$2"; epic_id="$3"; title="$4"; priority="$5"
        points="$6"; agent_type="$7"; branch="$8"; pr_number="$9"; new_status="${10}"
        db_exec "INSERT INTO stories
                   (run_id, story_id, epic_id, title, priority, points,
                    agent_type, branch, pr_number, status)
                 VALUES ($(sql_quote "${run_id}"),
                         $(sql_quote "${story_id}"),
                         $(sql_quote_or_null "${epic_id}"),
                         $(sql_quote_or_null "${title}"),
                         $(sql_quote_or_null "${priority}"),
                         $(sql_int_or_null "${points}"),
                         $(sql_quote_or_null "${agent_type}"),
                         $(sql_quote_or_null "${branch}"),
                         $(sql_int_or_null "${pr_number}"),
                         $(sql_quote "${new_status}"))
                 ON CONFLICT(run_id, story_id) DO UPDATE SET
                     epic_id    = excluded.epic_id,
                     title      = excluded.title,
                     priority   = excluded.priority,
                     points     = excluded.points,
                     agent_type = excluded.agent_type,
                     branch     = excluded.branch,
                     pr_number  = excluded.pr_number,
                     status     = excluded.status;"
        ;;

    stage-start)
        # Usage: stage-start <run_id> <story_id> <stage_name> [attempt]
        # Inserts an IN_PROGRESS row. Attempt defaults to 1; story 4.3-001
        # (resume) will increment it when retrying a failed stage.
        if [ $# -lt 3 ]; then
            err "stage-start requires <run_id> <story_id> <stage_name> [attempt]"
            exit 1
        fi
        run_id="$1"; story_id="$2"; stage_name="$3"
        attempt="${4:-1}"
        attempt_sql="$(sql_int_or_null "${attempt}")"
        if [ "${attempt_sql}" = "NULL" ]; then
            attempt_sql=1
        fi
        db_exec "INSERT INTO stages
                   (run_id, story_id, stage_name, attempt, status, started_at)
                 VALUES ($(sql_quote "${run_id}"),
                         $(sql_quote "${story_id}"),
                         $(sql_quote "${stage_name}"),
                         ${attempt_sql},
                         'IN_PROGRESS',
                         CURRENT_TIMESTAMP);"
        ;;

    stage-finish)
        # Usage: stage-finish <run_id> <story_id> <stage_name> <attempt> <status>
        #                     <failure_category> <output_path>
        # Updates the IN_PROGRESS row. Empty failure_category and output_path
        # round-trip as NULL so successful stages do not pollute the
        # failure-category column.
        if [ $# -lt 7 ]; then
            err "stage-finish requires <run_id> <story_id> <stage_name> <attempt> <status> <failure_category> <output_path>"
            exit 1
        fi
        run_id="$1"; story_id="$2"; stage_name="$3"; attempt="$4"
        new_status="$5"; failure_category="$6"; output_path="$7"

        attempt_sql="$(sql_int_or_null "${attempt}")"
        if [ "${attempt_sql}" = "NULL" ]; then
            attempt_sql=1
        fi

        # Verify the row exists before updating — a silent zero-row UPDATE
        # would mask orchestrator bugs (wrong attempt number, missing
        # stage-start, etc.) and lose state.
        exists=$(db_scalar "SELECT COUNT(*) FROM stages
                              WHERE run_id     = $(sql_quote "${run_id}")
                                AND story_id   = $(sql_quote "${story_id}")
                                AND stage_name = $(sql_quote "${stage_name}")
                                AND attempt    = ${attempt_sql};")
        if [ "${exists}" = "0" ]; then
            err "stage-finish: no matching stage row for ${run_id}/${story_id}/${stage_name}/${attempt} — call stage-start first"
            exit 1
        fi

        db_exec "UPDATE stages
                    SET status           = $(sql_quote "${new_status}"),
                        finished_at      = CURRENT_TIMESTAMP,
                        failure_category = $(sql_quote_or_null "${failure_category}"),
                        output_path      = $(sql_quote_or_null "${output_path}")
                  WHERE run_id     = $(sql_quote "${run_id}")
                    AND story_id   = $(sql_quote "${story_id}")
                    AND stage_name = $(sql_quote "${stage_name}")
                    AND attempt    = ${attempt_sql};"
        ;;

    event-log)
        # Usage: event-log <run_id> <story_id> <level> <source> <message>
        # An empty story_id is allowed for run-level events (preflight, etc.).
        if [ $# -lt 5 ]; then
            err "event-log requires <run_id> <story_id> <level> <source> <message>"
            exit 1
        fi
        run_id="$1"; story_id="$2"; level="$3"; source="$4"; message="$5"
        db_exec "INSERT INTO events(run_id, story_id, level, source, message)
                 VALUES ($(sql_quote_or_null "${run_id}"),
                         $(sql_quote_or_null "${story_id}"),
                         $(sql_quote "${level}"),
                         $(sql_quote_or_null "${source}"),
                         $(sql_quote "${message}"));"
        ;;

    # --- Read-model rendering (Story 4.2-002) ----------------------------
    #
    # `render` regenerates the markdown view (`docs/stories/.build-progress.md`)
    # from SELECT-only queries against the ledger. The ledger is the source of
    # truth; the markdown is a deterministic read-model.
    #
    # Determinism: every SELECT used by the renderer carries an explicit
    # ORDER BY so the same DB state always produces byte-identical output.
    # We sort:
    #   * epics by epic_id ascending (NULLs last)
    #   * stories within an epic by story_id ascending
    #   * runs by started_at ascending, then id ascending as a tie-breaker
    #
    # Atomic write: `--out <path>` writes to `<path>.tmp` then renames in
    # place so a partial render is never observed by a reader.
    #
    # `render-all` is currently a synonym for `render`; the name exists so
    # that bootstrap callers (e.g. when `.build-progress.md` is missing
    # entirely) can be explicit about asking for a full regeneration.

    render|render-all)
        render_subcommand "$@"
        ;;

    *)
        err "unknown subcommand: ${SUBCOMMAND}"
        usage
        exit 1
        ;;
esac
