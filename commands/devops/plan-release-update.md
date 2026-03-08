# Plan Release Update

Plan the implementation of a release monitor GitHub issue.

## Usage

```
/plan-release-update <issue-number>
```

Example: `/plan-release-update 64`

## Instructions

1. **Read the GitHub issue** using: `gh issue view <issue-number> --repo fxmartin/nix-install`

2. **Identify the issue type** from labels and title:
   - `security` label → Security Update
   - `breaking-change` label → Breaking Change
   - `enhancement` + `profile/power` → Ollama Model
   - `enhancement` + title contains "New feature" → New Feature
   - `enhancement` + title contains version arrow (→ or ->) → Notable Update

3. **Propose an implementation plan** based on issue type:

### For Security Updates (`security` label)
- **Priority**: HIGH - Should be applied ASAP
- **Plan**:
  1. Check current version: `brew info <package>` or `nix eval`
  2. Review security advisory/changelog
  3. Test update in isolation if possible
  4. Apply via `rebuild` or `update` command
  5. Verify fix applied: check version, run tests
- **Files likely affected**: `flake.lock`, possibly `darwin/homebrew.nix`

### For Breaking Changes (`breaking-change` label)
- **Priority**: HIGH - Review before next rebuild
- **Plan**:
  1. Read the full changelog for breaking changes
  2. Identify impact on current configuration
  3. Search codebase for affected usage: `rg "<tool-name>"`
  4. Prepare migration steps if needed
  5. Test in VM before applying to hardware
  6. Update documentation if behavior changes
- **Files likely affected**: Depends on tool (check usage in `darwin/`, `home-manager/`, `scripts/`)

### For New Features (`enhancement` + "New feature")
- **Priority**: MEDIUM - Evaluate usefulness
- **Plan**:
  1. Read feature documentation/release notes
  2. Assess relevance to current workflow
  3. If adopting:
     - Identify configuration changes needed
     - Add to appropriate Nix module
     - Test functionality
     - Document usage
  4. If deferring: Close issue with rationale
- **Files likely affected**: Feature-specific module in `home-manager/modules/` or `darwin/`

### For Ollama Models (`enhancement` + `profile/power`)
- **Priority**: LOW - Try when convenient
- **Plan**:
  1. Check model requirements: `ollama show <model>`
  2. Verify disk space available
  3. Pull model: `ollama pull <model>`
  4. Test with sample prompts
  5. If keeping permanently:
     - Add to `flake.nix` power profile Ollama models list
     - Update documentation
  6. If not keeping: `ollama rm <model>` and close issue
- **Files likely affected**: `flake.nix` (power profile activation script)

### For Notable Updates (`enhancement` + version arrow)
- **Priority**: LOW - Apply during regular maintenance
- **Plan**:
  1. Check if update is pulled by `nix flake update` or `brew upgrade`
  2. Review changelog for notable changes
  3. Apply via normal `update` command
  4. Verify functionality after update
  5. Close issue
- **Files likely affected**: `flake.lock` (for Nix), none for Homebrew

## Output Format

Provide:
1. **Issue Summary**: One-line description
2. **Issue Type**: Security/Breaking/Feature/Model/Update
3. **Priority**: HIGH/MEDIUM/LOW
4. **Impact Analysis**: What parts of the system are affected
5. **Implementation Steps**: Numbered action items
6. **Files to Modify**: List of files that may need changes
7. **Testing Strategy**: How to verify the change works
8. **Rollback Plan**: How to revert if something goes wrong

## Context Files

Reference these for understanding the system:
- `flake.nix` - System definition, Ollama models
- `darwin/homebrew.nix` - Homebrew packages
- `darwin/configuration.nix` - System packages
- `home-manager/modules/` - User configuration modules
- `scripts/` - Maintenance and utility scripts
