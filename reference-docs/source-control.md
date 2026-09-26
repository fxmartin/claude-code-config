# Source Control Reference

Local GitLab — the local-ci-cd stack on `home-lab` — is the authoritative remote
for every repo under `~/Work`; GitHub is a one-way push mirror. Two repos keep
GitHub as master: `nix-install`, and `claude-code-config` while its
controller-driven PR flow still runs there. Everything below assumes the GitLab
case unless a section says otherwise.

## Reaching GitLab

- URL: `http://gitlab.test` — port 80 on the tailnet via `tailscale serve` (raw
  tcp) plus split DNS for `.test`. Every URL GitLab advertises (`web_url`, clone
  URLs, MR and issue links) opens from any tailnet device.
- `glab`: set `GITLAB_HOST=gitlab.test`; it authenticates as `root` from the
  keyring.
- Do not use `home-lab.tailac3c7a.ts.net:8080` or `home-lab:8080`: the `:8080`
  tailnet http mapping is unreliable. A global git rewrite
  (`url.http://gitlab.test/.insteadOf`) keeps stale `:8080` remotes working, but
  the raw `remote.origin.url` should still read `http://gitlab.test/root/<repo>.git`.
- `ssh m1pro` (alias `home-lab`): keys only, tailnet only, **one session at a
  time** — a second concurrent session is refused with `Permission denied ()`.

## Git Workflow

- Feature branches from `main`
- Conventional commit messages (see `CLAUDE.md` › Commit Format)
- Always open a merge request for review before merging
- Merge on GitLab once the appliance pipeline is green; never merge the mirror
  copy on GitHub

## Branch Naming

- `feature/<description>` for new features
- `fix/<description>` for bug fixes
- `refactor/<description>` for refactoring
- `docs/<description>` for documentation

## Commit Messages

Format: `<type>: <description>`

Types: feat, fix, refactor, docs, test, chore, ci

## MR Process (GitLab-master repos)

1. Create a feature branch from `main`
2. Make changes with tests
3. `git push -u origin <branch>`
4. Open the MR **from inside the repo**:
   `glab mr create --source-branch <branch> --target-branch main --title ... --description ...`
   `glab` takes the *source* project from the cwd and treats `--repo` as the
   target, so a cross-repo call fails with "not a fork". For another repo use
   `glab api -X POST projects/<id>/merge_requests -f source_branch=<branch> -f target_branch=main -f title=...`
5. Address review; merge on GitLab — `glab mr merge <iid>`, or
   `glab api -X PUT projects/<id>/merge_requests/<iid>/merge`

## PR Process (GitHub-master repos only)

`nix-install` and `claude-code-config`: feature branch → changes with tests →
`gh pr create` → review → squash merge (`gh pr merge --squash`).

## GitLab CLI (`glab`)

- `glab issue create --title ... --description ... --label ...` — issues live on GitLab
- `glab mr create ...`, `glab mr view <iid>`, `glab mr merge <iid>`
- `glab api projects/<id>/pipelines?ref=<branch>` — pipeline status
- `glab api ...` for anything else; `-X POST|PUT|DELETE` for writes

## GitHub CLI (`gh`)

For the two GitHub-master repos and for read-only mirror checks, e.g.
`gh api repos/fxmartin/<repo>/commits/main` and `gh pr checks`. Do not rely on
a GitHub MCP server.

## Mirrors (GitLab → GitHub)

Configured per project on the appliance with local-ci-cd:

    export LOCAL_CI_CD_MIRROR_GITHUB_TOKEN="$(cat "$HOME/Library/Application Support/local-ci-cd/secrets/mirror-github-token")"
    local-ci-cd mirror add root/<repo> \
      --target https://github.com/fxmartin/<repo>.git \
      --username x-access-token --token-env LOCAL_CI_CD_MIRROR_GITHUB_TOKEN \
      --keep-divergent-refs

- The GitHub PAT needs **Contents: Read and write** and **Workflows: Read and
  write** across every mirrored repo; without Workflows, any push that touches
  `.github/workflows/` is rejected.
- Credentials cannot be rotated in place (GitLab's PUT accepts no `url`):
  `DELETE projects/<id>/remote_mirrors/<mirror_id>`, then `mirror add` again.
- Force a sync (the UI's "Update now"):
  `POST projects/<id>/remote_mirrors/<mirror_id>/sync`. A newly configured
  mirror sits at `update_status: none` until a push or this call.
- `keep_divergent_refs` protects GitHub-only refs, which makes drift silent: the
  status stays `finished` while diverged refs are skipped. Keep merges on GitLab.
- The GitLab `release` job publishes the GitHub release only after the mirror has
  synced the tag; it defers otherwise.

## Appliance recovery (`home-lab`)

After a `darwin-rebuild` followed by a reboot the stack does not come back on its
own (local-ci-cd issues #1 and #2): the boot LaunchAgent's plist holds a
garbage-collected nix store path, and nothing starts the container apiserver.
Until those land, on `home-lab`:

    container system start
    local-ci-cd up

`up` rewrites the plist for the current build; GitLab readiness takes 4–6 minutes.
