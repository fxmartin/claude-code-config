<!-- ABOUTME: Agent I/O JSON-schema contracts reference (Story 7.2-001). -->
<!-- ABOUTME: How agents emit structured results and how the controller validates them. -->

# Agent I/O Contracts

Every agent the SDLC orchestrator dispatches must end its response with a
machine-readable result block. The external controller (`sdlc`) parses that
block, validates it against a published JSON schema, and acts on the typed
result instead of scraping prose. A malformed or missing block is treated as a
failure and routed to the bugfix loop.

## The result-marker block

Agents emit the JSON object as the **final** content of their response, fenced
with these markers:

```
<<<RESULT_JSON>>>
{ ... }
<<<END_RESULT>>>
```

- Everything before `<<<RESULT_JSON>>>` and after `<<<END_RESULT>>>` is ignored,
  so agents keep emitting human-readable prose (and the legacy `KEY: value`
  status lines) around the block.
- The fenced payload must be a single JSON **object** (not an array or scalar).
- Extra fields are allowed — schemas are forward-compatible
  (`additionalProperties: true`), so agents may add fields without breaking
  older controllers.

## Schemas

Schemas live in
[`controller/src/sdlc/schemas/`](../controller/src/sdlc/schemas/) (bundled
inside the `sdlc` package so they ship in the installed wheel) in
[JSON Schema draft 2020-12](https://json-schema.org/draft/2020-12/) format.

| Agent type | Schema file | Required fields |
|------------|-------------|-----------------|
| `build`    | `build-agent-response.schema.json`    | `branch_name`, `build_status`, `commit_sha` (optional `pr_number`, `error_summary`) |
| `coverage` | `coverage-agent-response.schema.json` | `pr_number`, `pr_url`, `coverage_pct`, `tests_added`, `coverage_status`, `security_status` |
| `review`   | `review-agent-response.schema.json`   | `pr_number`, `approval_status`, `change_count`, `final_status` |
| `merge`    | `merge-agent-response.schema.json`    | `pr_number`, `merge_status`, `merge_sha`, `merged_at` |
| `bugfix`   | `bugfix-agent-response.schema.json`   | `failure_category`, `fix_status`, `tests_passing`, `bugs_fixed`, `tests_fixed` (optional `issue_number`) |

### Status enums

Several status fields are constrained to canonical enums. Agent prompts that
historically emit a richer vocabulary (e.g. coverage `SECURITY_BLOCK`, merge
`REBASE_CONFLICT`) map their value into the schema enum in the result block:

- `build_status`: `SUCCESS` | `FAILED`
- `coverage_status` / `security_status`: `PASS` | `WARN` | `FAIL`
- `approval_status`: `APPROVED` | `CHANGES_NEEDED`; `final_status`: `APPROVED` | `REJECTED`
- `merge_status`: `MERGED` | `FAILED` | `SKIPPED`
- `fix_status`: `FIXED` | `UNFIXED` | `N/A`
- `failure_category`: `CODE_BUG` | `TEST_BUG` | `ENV_ISSUE` | `BUILD_ERROR` | `TEST_FAILURE` | `SCHEMA_ERROR`

## Examples

Build agent (success):

```
<<<RESULT_JSON>>>
{"branch_name": "feature/7.2-001", "build_status": "SUCCESS", "commit_sha": "abc123"}
<<<END_RESULT>>>
```

Coverage agent:

```
<<<RESULT_JSON>>>
{"pr_number": 42, "pr_url": "https://github.com/fxmartin/repo/pull/42", "coverage_pct": 93.5, "tests_added": 7, "coverage_status": "PASS", "security_status": "PASS"}
<<<END_RESULT>>>
```

## Validating a response

The controller exposes the validation logic both as a library
(`sdlc.contracts`) and as a CLI command:

```bash
# Validate a captured agent response (file or stdin) against its schema.
sdlc validate build agent-response.txt
cat agent-response.txt | sdlc validate coverage
```

On success the validated JSON is printed and the command exits `0`. On failure
it exits non-zero with an **actionable** message that names the offending field,
for example:

```
error: build-agent response is missing required field 'branch_name': 'branch_name' is a required property
```

Programmatic use:

```python
from sdlc.contracts import parse_and_validate, ContractError

try:
    result = parse_and_validate("build", agent_response_text)
except ContractError as exc:
    # Treat as a build failure and route to the bugfix loop.
    ...
```

`parse_and_validate` raises `ResultBlockError` when the marker block is missing
or malformed, and `SchemaValidationError` when the JSON violates the schema —
both subclasses of `ContractError`.
