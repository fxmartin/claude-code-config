# Change Control Re-Approval Process

## Detection Logic

When an existing `# STAKEHOLDER APPROVAL` section is found:

1. Extract current document version from existing approval section
2. Analyze document for changes since last approval
3. Check the Post-Approval Change Log for documented changes

## Version Increment Rules

- **Minor changes** (clarifications, small additions): v1.0 → v1.1
- **Major changes** (significant scope/timeline/budget impact): v1.0 → v1.5 or v2.0
- **Critical changes** (fundamental approach change): v1.x → v2.0

## If Changes Are Not Documented

Prompt the user for:

1. **Change Description**: What specific changes are being approved?
2. **Business Justification**: Why are these changes necessary?
3. **Impact Assessment**: Effect on timeline, budget, scope
4. **Authorization**: Who authorized the changes?
5. **Change Scope Classification**: Minor / Major / Critical

## Change Control Compliance Check

- [ ] Changes documented in change log table
- [ ] Business justification provided
- [ ] Impact assessment completed
- [ ] Proper approval authority confirmed
- [ ] Version increment appropriate for change scope

## Re-Approval Section Template

Replace the existing approval section with:

```markdown
---

# STAKEHOLDER APPROVAL

## Approval Status
**Status**: ✅ APPROVED
**Approval Date**: [Current UTC Timestamp]
**Approved By**: [Stakeholder Name/Role]
**Document Version**: [Incremented Version]
**Approval Type**: CHANGE CONTROL RE-APPROVAL
**Previous Version**: [Previous version number]

## Change Control Re-Approval
**Change Request Date**: [Date changes were identified]
**Change Description**: [Summary of changes being approved]
**Business Justification**: [Why changes are necessary]
**Impact Assessment**: [Effect on timeline, budget, scope]
**Change Authority**: [Who authorized the changes]
**Change Scope**: [Minor/Major/Critical]

## Change Approval Criteria Met
- [ ] Changes documented and justified
- [ ] Impact assessment completed
- [ ] Proper change authority approval obtained
- [ ] Stakeholder review conducted
- [ ] Technical feasibility confirmed
- [ ] Updated requirements complete and testable
- [ ] Dependencies updated as needed
- [ ] Risk assessment updated

## Change Control
**Previous Baseline**: [Previous approval timestamp]
**New Baseline Established**: [Current UTC Timestamp]

### Post-Approval Change Log
| Date | Change Description | Impact Assessment | Approved By | Version |
|------|-------------------|-------------------|-------------|---------|
| [Previous entries preserved] | ... | ... | ... | ... |
| [Current date] | [Current change] | [Impact] | [Stakeholder] | [New version] |

## Development Authorization
**Authorization to Proceed**: ✅ GRANTED
**Story Development**: [✅ AUTHORIZED / 🔄 UPDATE REQUIRED]
**Sprint Planning**: [✅ CONTINUE / 🔄 RE-PLAN REQUIRED]

## Approval Signatures
**Stakeholder Re-Approval**:
- Name: [Stakeholder Name]
- Date: [Current UTC Timestamp]
- Change Authority: [Minor/Major/Critical approval level]

## Cryptographic Integrity
**Previous Baseline Hash**: [Hash from previous approval]
**New Baseline Hash**: [SHA-256 hash of updated requirements content]
**Approval Timestamp**: [Current UTC Timestamp]

## Next Steps
1. ✅ Change control re-approval completed
2. 🔄 Update STORIES.md if requirements changes affect stories
3. 🔄 Assess impact on current sprint planning
4. 🔄 Communicate changes to development team
```

## Post Re-Approval

- Update `requirements-integrity.json` with new hashes
- Run `verify-requirements-integrity.sh` to confirm new baseline
- Update CLAUDE.md change control section if needed
