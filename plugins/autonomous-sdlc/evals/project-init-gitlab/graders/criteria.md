<!-- ABOUTME: GREEN rubric for the project-init-gitlab scenario (issue #697). -->
<!-- ABOUTME: LLM-judge criteria — GitLab is master, GitHub is only the mirror target. -->

# GREEN grader — project-init on a GitLab-master repo

Score **PASS** only if every criterion holds; otherwise **FAIL**.

1. Pre-flight runs `glab auth status` against `gitlab.test` and `curl -sI http://gitlab.test/` before creating anything.
2. `origin` is `http://gitlab.test/root/agentic-monitor.git`, created via `glab api -X POST projects`; `home-lab:8080` never appears.
3. `gh repo create` is added as remote `github`; nothing is pushed to it.
4. Labels are applied with `glab label create` (one call per label), not `gh label`.
5. `.sdlc-forge.yaml` contains `forge: gitlab` and `gitlab_url: http://gitlab.test` and is committed with the seed files.
6. `git config --local credential.http://gitlab.test.helper '!glab auth git-credential'` is set before the push.
7. `.gitlab-ci.yml` is installed and `.github/workflows/ci.yml` is kept.
8. Generated `CLAUDE.md` documents the change flow, "never commit `vendor/`" / `GOMODCACHE`, and the offline warm-up.
9. Because the `local-ci-cd` CLI is absent, the summary prints the exact `local-ci-cd mirror add root/agentic-monitor --username x-access-token --token-env LOCAL_CI_CD_MIRROR_GITHUB_TOKEN` command, and warns that a GitHub-side merge strands GitLab (`keep_divergent_refs`).
