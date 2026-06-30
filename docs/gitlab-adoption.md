# Adopt a GitLab project for the autonomous pipeline

> Story 23.6-002 (Epic-23 — *Pipeline on GitLab*). A guided check, and a worked
> example, that takes a company GitLab repo from zero to its first green
> `sdlc build` Merge Request — without a run failing halfway on a missing
> prerequisite.

The framework itself stays on GitHub (see the Epic-23 non-goals). This guide is
for pointing the autonomous build loop at a **company GitLab project** so a story
goes branch → MR → GitLab CI green → merge → issue auto-closed, with zero GitHub
calls.

## The preflight

Before the first build, run the adoption preflight from the target repo:

```bash
cd /path/to/company-repo
sdlc doctor --gitlab            # preflight against the current repo (cwd)
sdlc doctor --gitlab --target /path/to/company-repo
sdlc doctor --gitlab --exit-code   # non-zero exit for CI/automation gating
```

It extends the Epic-15 `sdlc doctor` health-check (install / ledger / runs /
config / dependencies) with four GitLab-target checks, each reporting
`CLEAN`/`WARN`/`FAIL` plus the command that fixes it:

| Check                | Verifies                                                          | Remedy when it FAILs                                            |
| -------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------- |
| **glab installed**   | the GitLab CLI is on `PATH`                                       | `brew install glab` — https://gitlab.com/gitlab-org/cli        |
| **glab authenticated** | the CLI is logged in (the build loop acts as *you*, no shared token) | `glab auth login`                                        |
| **GitLab project**   | the repo's remote resolves to a project **with a default branch** | check the `origin` remote; push an initial commit              |
| **GitLab CI enabled** | CI/CD is on, so a pipeline can gate the MR                        | enable CI/CD: Settings → General → Visibility → CI/CD          |
| **Gate template**    | `.gitlab-ci.yml` is present at the repo root                      | `cp templates/gitlab-ci.yml <repo>/.gitlab-ci.yml` (Story 23.3-001) |

The preflight is **read-only**: it never mutates the repo or the GitLab project.
Combine it with the plain `sdlc doctor` checks in one pass — the overall status
is the worst of every finding, and `--json` emits the full report (each GitLab
finding carries `"check": "gitlab"`).

## Worked example — zero to a first green MR

Starting from a fresh company GitLab repo:

1. **Authenticate the CLI** (the identity provider is the host — no shared
   service token; see [issue-host-adapters.md](issue-host-adapters.md)):

   ```bash
   glab auth login            # pick your company GitLab instance, paste a PAT
   ```

   The PAT needs the least-privilege scopes documented in
   [issue-host-adapters.md](issue-host-adapters.md#auth--ci-tokens-story-236-001):
   `api` and `write_repository`.

2. **Install the gate template** so a Merge Request is held to the same standard
   a Pull Request is (the gate the merge polls — Story 23.2-002):

   ```bash
   cp templates/gitlab-ci.yml /path/to/company-repo/.gitlab-ci.yml
   cd /path/to/company-repo
   git add .gitlab-ci.yml && git commit -m "ci: add autonomous-SDLC quality gates"
   git push
   ```

   Make sure the gate's prerequisite files exist too — `.gitleaks.toml`,
   `.commitlintrc.json`, and a `uv`-managed Python project — as listed in
   [gitlab-ci-template.md](gitlab-ci-template.md#prerequisites-in-the-target-repo).

3. **Provision the board** (issues + the taxonomy labels the pipeline aligns to —
   Epic-22):

   ```bash
   sdlc issues init           # auto-detects the GitLab host from the remote
   ```

4. **Run the preflight and clear every gap** until it is all green:

   ```bash
   sdlc doctor --gitlab
   # [CLEAN] glab installed — glab is on PATH
   # [CLEAN] glab authenticated — authenticated as <you>
   # [CLEAN] GitLab project — project acme/widgets (default branch: main)
   # [CLEAN] GitLab CI enabled — CI/CD is enabled
   # [CLEAN] Gate template — .gitlab-ci.yml present
   # doctor: all N checks passed (CLEAN).
   ```

5. **Build the first story.** The loop opens a *Merge Request* (not a PR),
   GitLab CI runs the gate pipeline, and the merge is blocked until it is green:

   ```bash
   sdlc build <story-id>
   ```

   On a green pipeline the MR merges and `Closes #<iid>` auto-closes the story
   issue — the full loop, with zero GitHub calls.

## See also

- [gitlab-ci-template.md](gitlab-ci-template.md) — the installable gate template
  and its GitHub-Actions ↔ GitLab-CI parity table.
- [issue-host-adapters.md](issue-host-adapters.md) — the host adapter, identity,
  and CI-token scopes the GitLab path shares with GitHub.
