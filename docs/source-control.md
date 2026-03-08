# Source Control Reference

## Git Workflow

- Feature branches from `main`
- Conventional commit messages
- Always create PR for review before merging
- Squash merge to keep history clean

## Branch Naming

- `feature/<description>` for new features
- `fix/<description>` for bug fixes
- `refactor/<description>` for refactoring
- `docs/<description>` for documentation

## Commit Messages

Format: `<type>: <description>`

Types: feat, fix, refactor, docs, test, chore, ci

## PR Process

1. Create feature branch
2. Make changes with tests
3. Push and create PR via `gh pr create`
4. Address review feedback
5. Squash merge when approved

## GitHub CLI (`gh`)

- `gh issue create` - create issues
- `gh pr create` - create pull requests
- `gh pr checks` - verify CI status
- `gh pr merge --squash` - merge with squash
