# Dependency Resolver

## DAG Construction

Build a directed acyclic graph (DAG) from parsed stories:

1. Each story is a node, keyed by its story ID
2. Each dependency creates a directed edge: dependency → dependent story
3. Only include **incomplete** stories in the graph
4. Dependencies on **complete** stories are considered satisfied (remove the edge)

## Topological Sort (Kahn's Algorithm)

```
1. Compute in-degree for each node
2. Initialize queue with all nodes having in-degree == 0
3. While queue is not empty:
   a. Pick node with highest priority_weight from queue (tiebreak below)
   b. Add to sorted output
   c. For each neighbor of this node:
      - Decrease neighbor's in-degree by 1
      - If in-degree becomes 0, add neighbor to queue
4. If sorted output length != total nodes → CYCLE DETECTED
```

## Priority Tiebreaking

When multiple stories have in-degree == 0 simultaneously, pick by:

1. **Priority weight** (descending): Must Have (3) > Should Have (2) > Could Have (1)
2. **Epic order** (ascending): Epic 01 before Epic 02
3. **Story points** (ascending): Smaller stories first (faster throughput)
4. **Story ID** (ascending): Lexicographic order as final tiebreaker

## Cycle Detection

If topological sort doesn't include all nodes:

1. Identify remaining nodes (those still with in-degree > 0)
2. Report cycle: list the story IDs involved
3. **Action**: STOP and report to user — do not attempt to break cycles automatically
4. Suggest: "Review dependencies for stories: [IDs] — circular dependency detected"

## Filtering by Scope

Apply filters BEFORE building the DAG:

- `all` — include all incomplete stories from all epics
- `epic-NN` — include only stories from epic number NN
- `epic-name` — match epic by name (case-insensitive partial match on epic file name)
- `resume` — load stories from `.build-progress.md`, include only PENDING and FAILED

When filtering by epic, still resolve cross-epic dependencies:
- If story A (in scope) depends on story B (out of scope) and B is incomplete → BLOCK story A
- Report blocked stories separately: "Story A blocked by out-of-scope dependency B"

## Output

Produce an ordered list of stories ready to build:

```markdown
## Build Queue

| Order | Story ID | Title | Priority | Points | Dependencies | Agent |
|-------|----------|-------|----------|--------|--------------|-------|
| 1 | 01.1-001 | Setup project | Must Have | 2 | None | bash-zsh-macos-engineer |
| 2 | 01.1-002 | Database schema | Must Have | 3 | 01.1-001 | python-backend-engineer |
| 3 | 01.2-001 | Auth API | Must Have | 5 | 01.1-002 | python-backend-engineer |

### Blocked Stories
| Story ID | Blocked By | Reason |
|----------|-----------|--------|
| 02.1-001 | 01.3-002 | Out-of-scope dependency incomplete |

### Completed (skipped)
- 01.1-001: Project Setup (all DoD checked)
```
