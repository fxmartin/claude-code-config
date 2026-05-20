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

Subcommands:
  init                          Create the DB and apply all migrations.
  migrate                       Apply any pending migrations (idempotent).
  show <run-id>                 Print a run's summary and its stories.
  prune --older-than <duration> Delete DONE/FAILED runs finished before the cutoff.
  backup <dest>                 Write a consistent SQLite-level copy to <dest>.

Default --db path: .sdlc-state.db in the current directory.

Examples:
  sdlc-state.sh init
  sdlc-state.sh --db /tmp/test.db migrate
  sdlc-state.sh show 7a3f-...
  sdlc-state.sh prune --older-than 14d
  sdlc-state.sh backup .sdlc-state.db.bak
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

    *)
        err "unknown subcommand: ${SUBCOMMAND}"
        usage
        exit 1
        ;;
esac
