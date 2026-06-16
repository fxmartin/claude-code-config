# Security Gates — Secrets Scanning

The secrets gate (Epic-09, Story 9.2-001) stops a credential, API key, or token
from ever reaching the default branch. The autonomous build loop commits code on
its own; this gate is the guarantee that it can never autonomously commit a
secret. It runs in two places, both backed by the same [`.gitleaks.toml`](../.gitleaks.toml):

- **CI (mandatory).** The `secrets-scan` job in `.github/workflows/ci.yml` runs
  `gitleaks detect --no-banner --redact` on every pull request and push to
  `main`. It is the **first** job in the pipeline — the build and test jobs
  (`behavior-tests`, `controller-smoke`, `smoke-test`) declare `needs:
  secrets-scan`, so a leaked credential fails the build before any later job
  runs and risks echoing the secret into its logs. Findings are redacted
  (`--redact`): the gate reports *where* a secret is, never the value.
- **Pre-commit (opt-in, recommended).** [`.pre-commit-config.yaml`](../.pre-commit-config.yaml)
  runs the same scan locally before each commit so a leak is caught before it is
  ever pushed. See the install steps in [`onboarding.md`](onboarding.md).

## Configuration

[`.gitleaks.toml`](../.gitleaks.toml) extends the gitleaks default ruleset
(`extend.useDefault = true`) — AWS, GitHub, Slack, Stripe, generic high-entropy,
and more — and adds an allowlist for known-safe patterns:

- **Paths**: `.env.example` / `.env.sample` files and the deliberate test
  fixture `tests/fixtures/leaked-key.txt`. The fixture is allowlisted so its
  planted token never blocks a commit or the CI gate; the bats test
  (`tests/gitleaks-secrets.bats`) scans it with the *default* config instead, so
  detection is still proven.
- **Regexes**: obvious placeholder tokens (`example-key`, `your-token-here`,
  long `xxxx…` runs) so documentation examples that show the *shape* of a
  credential do not trip the gate.

The same config governs CI, the opt-in pre-commit hook, and (on FX's machines)
the Home Manager-managed gitleaks hook — there is one allowlist to maintain, not
three.

## Opt-in: enable the local pre-commit hook

The pre-commit hook is the same scan, run before the secret ever leaves your
machine. It is opt-in because not every contributor uses the
[pre-commit framework](https://pre-commit.com):

```bash
# one-time, per clone
pipx install pre-commit   # or: brew install pre-commit / uv tool install pre-commit
pre-commit install        # registers the git hook from .pre-commit-config.yaml
```

After that, every `git commit` runs `gitleaks protect --staged --redact` against
your staged changes and blocks the commit if it finds a secret.

> FX's machines already run a Home Manager-managed gitleaks pre-commit hook, so
> this step is for colleagues without that setup. Running both is harmless — they
> share `.gitleaks.toml`.

## What to do when the gate finds a real secret

A finding is not a false alarm to be silenced. A committed secret must be treated
as **compromised the moment it lands**, because the history is public the instant
it is pushed. The framework **does not auto-rotate** — that is a deliberate human
decision. Do all of the following, in order:

1. **Rotate the secret first.** Revoke the leaked credential at its source (AWS
   IAM, GitHub token settings, the provider's console) and issue a replacement.
   Removing it from git does **not** un-leak it — assume it was scraped.
2. **Scrub it from history.** Removing the secret in a new commit is not enough;
   it still lives in the earlier commit. Either:
   - rewrite history to drop the secret (`git filter-repo` or BFG) and
     **force-push** the cleaned branch, or
   - if the branch is short-lived, delete and recreate it without the secret.
3. **Re-run the gate.** Push again; `secrets-scan` must come back green before
   the PR can merge.
4. **Add a real false positive to the allowlist — sparingly.** If (and only if)
   the finding is genuinely not a secret (a placeholder, a sample, a hash that
   looks like a key), add a narrow path or regex entry to `.gitleaks.toml` with a
   comment explaining why it is safe. Never allowlist a live value.

## Verifying the gate locally

```bash
# Scan the working tree exactly as CI does:
gitleaks detect --no-banner --redact --config .gitleaks.toml

# Run the bats coverage for this gate (requires gitleaks + bats):
bats tests/gitleaks-secrets.bats
```

The bats test plants the fixture token in a temp dir, confirms gitleaks flags it
with the default rules, confirms the value is redacted out of the output, and
confirms `.gitleaks.toml` allowlists the fixture at its real path.
