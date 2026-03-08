# Claude Code CLI Prompt: Approve Requirements (External Hash Validation)

## Objective
You are a requirements management assistant. Your task is to update the docs/REQUIREMENTS.md file with a formal stakeholder approval section and create external hash validation for tamper-evident integrity protection.

## Context
This command formalizes the requirements approval process by adding a clear audit trail to the REQUIREMENTS.md document and storing cryptographic validation data externally to avoid circular dependency issues.

## Instructions

### Step 1: Validate Current State and Detect Re-Approval
1. **Read the existing REQUIREMENTS.md file**
2. **Check if an approval section already exists**
3. **If approval section exists:**
   - This is a **CHANGE CONTROL RE-APPROVAL** scenario
   - Extract current document version from existing approval section
   - Analyze document for changes since last approval
   - Validate change control process compliance
4. **If no approval section exists:**
   - This is **INITIAL APPROVAL** scenario
   - Proceed with standard approval process

### Step 1a: Change Control Detection Logic
Execute this analysis when approval section already exists:

```bash
# Check if approval section exists
if grep -q "^# STAKEHOLDER APPROVAL" REQUIREMENTS.md; then
    echo "🔄 CHANGE CONTROL RE-APPROVAL DETECTED"

    # Extract current version
    CURRENT_VERSION=$(grep "Document Version" REQUIREMENTS.md | head -1 | grep -o 'v[0-9]\+\.[0-9]\+')
    echo "Current Document Version: $CURRENT_VERSION"

    # Check for change log entries
    echo "Analyzing change log..."
    CHANGE_COUNT=$(grep -A 20 "Post-Approval Change Log" REQUIREMENTS.md | grep -v "No changes since baseline" | grep -c "|")

    if [ $CHANGE_COUNT -gt 1 ]; then
        echo "✅ Changes documented in change log"
        grep -A 20 "Post-Approval Change Log" REQUIREMENTS.md | grep "|" | tail -n +2
    else
        echo "❌ NO CHANGES DOCUMENTED - Manual validation required"
        echo "PROMPT USER: What changes are being approved in this re-approval?"
    fi

    # Calculate new version number
    MAJOR=$(echo $CURRENT_VERSION | cut -d'.' -f1 | sed 's/v//')
    MINOR=$(echo $CURRENT_VERSION | cut -d'.' -f2)
    NEW_MINOR=$((MINOR + 1))
    NEW_VERSION="v${MAJOR}.${NEW_MINOR}"
    echo "New Document Version: $NEW_VERSION"

else
    echo "📝 INITIAL APPROVAL DETECTED"
    NEW_VERSION="v1.0"
fi
```

### Step 1b: Change Analysis Protocol
When re-approval is detected, perform this analysis:

```markdown
## Change Control Analysis Required

### Current State Assessment
- **Document Version**: [Current version from existing approval]
- **Last Approval Date**: [Extract from existing approval section]
- **Last Approved By**: [Extract from existing approval section]

### Change Detection
Analyze the document for modifications since last approval:

1. **Check Change Log Entries**:
   - Look for entries in "Post-Approval Change Log" table
   - Verify each change has proper documentation
   - Confirm change approval authority

2. **Content Analysis**:
   - Compare current content against baseline if available
   - Identify new sections, modified requirements, deleted content
   - Assess scope and impact of changes

3. **Change Validation Questions** (If changes not clearly documented):
   ```
   REQUIRED USER INPUT:
   - What specific changes are being approved in this re-approval?
   - What is the business justification for these changes?
   - What is the impact assessment (timeline, budget, scope)?
   - Who authorized these changes?
   - Are these minor, major, or critical changes?
   ```

### Change Control Compliance Check
- [ ] Changes documented in change log table
- [ ] Business justification provided
- [ ] Impact assessment completed
- [ ] Proper approval authority confirmed
- [ ] Version increment appropriate for change scope
```

### Step 2: Generate Approval Section

#### For Initial Approval (No existing approval section):
Add the following approval section to the end of the REQUIREMENTS.md file:

```markdown
---

# STAKEHOLDER APPROVAL

## Approval Status
**Status**: ✅ APPROVED
**Approval Date**: [Current UTC Timestamp]
**Approved By**: [Stakeholder Name/Role]
**Document Version**: v1.0
**Approval Type**: INITIAL APPROVAL

## Approval Criteria Met
- [ ] Business objectives clearly defined
- [ ] Functional requirements complete and testable
- [ ] Non-functional requirements specified
- [ ] User personas and journeys documented
- [ ] Technical constraints identified
- [ ] Success criteria measurable
- [ ] Dependencies and assumptions documented
- [ ] Risk assessment completed

## Change Control
**Baseline Established**: [Current UTC Timestamp]
**Change Control Process**: Any modifications to these requirements after approval must follow the change control process defined in CLAUDE.md

### Post-Approval Change Log
| Date | Change Description | Impact Assessment | Approved By | Version |
|------|-------------------|-------------------|-------------|---------|
| - | No changes since baseline | - | - | v1.0 |

## Development Authorization
**Authorization to Proceed**: ✅ GRANTED
**Story Development**: Authorized to proceed with STORIES.md generation
**Sprint Planning**: Authorized to begin sprint planning activities
**Development Start**: Authorized to begin development work

## Approval Signatures
**Stakeholder Approval**:
- Name: [Stakeholder Name]
- Role: [Stakeholder Role]
- Date: [Current UTC Timestamp]
- Digital Signature: [Generated Hash]

**Technical Review**:
- Name: [Technical Lead Name]
- Role: Technical Lead
- Date: [Current UTC Timestamp]
- Digital Signature: [Generated Hash]

## Cryptographic Integrity
**Baseline Hash**: [SHA-256 hash of requirements content only]
**Approval Timestamp**: [Current UTC Timestamp]
**Integrity Validation**: External hash validation stored in `requirements-integrity.json`

### Hash Generation Commands
```bash
# Generate baseline hash (requirements content only)
sed '/^# STAKEHOLDER APPROVAL/,$d' REQUIREMENTS.md | sha256sum | cut -d' ' -f1

# Generate current timestamp
date -u +"%Y-%m-%dT%H:%M:%SZ"

# Verify document integrity
./verify-requirements-integrity.sh
```

### Cryptographic Protection
- **Tamper Detection**: External hash validation detects any modifications
- **Audit Verification**: Separate validation file prevents circular dependencies
- **Baseline Protection**: Original requirements content cryptographically sealed
- **Change Control**: Post-approval modifications invalidate external hash

## Next Steps
1. ✅ Requirements approved and baselined
2. ✅ External integrity validation established
3. 🔄 Generate STORIES.md using modular stories prompt
4. 🔄 Begin sprint planning with epic prioritization
5. 🔄 Set up development environment and repository
6. 🔄 Create initial project structure

## Compliance Notes
- This approval establishes the requirements baseline for change control
- All subsequent changes require formal change requests
- Development activities may proceed based on this approval
- Regular stakeholder reviews scheduled for milestone checkpoints
- External hash validation provides tamper-evident protection
```

#### For Change Control Re-Approval (Existing approval section found):
Replace the existing approval section with updated version:

```markdown
---

# STAKEHOLDER APPROVAL

## Approval Status
**Status**: ✅ APPROVED
**Approval Date**: [Current UTC Timestamp]
**Approved By**: [Stakeholder Name/Role]
**Document Version**: [Incremented Version - e.g., v1.1, v1.2, v2.0]
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
**Change Control Process**: Any modifications to these requirements after approval must follow the change control process defined in CLAUDE.md

### Post-Approval Change Log
| Date | Change Description | Impact Assessment | Approved By | Version |
|------|-------------------|-------------------|-------------|---------|
| [Previous entries preserved] | [Previous change history] | [Previous entries] | [Previous entries] | [Previous versions] |
| [Current UTC Timestamp] | [Current change description] | [Current impact assessment] | [Current stakeholder] | [New version] |

## Development Authorization
**Authorization to Proceed**: ✅ GRANTED
**Story Development**: [✅ AUTHORIZED / 🔄 UPDATE REQUIRED]
**Sprint Planning**: [✅ CONTINUE / 🔄 RE-PLAN REQUIRED]
**Development Work**: [✅ CONTINUE / ⚠️ IMPACT ASSESSMENT REQUIRED]

## Approval Signatures
**Stakeholder Re-Approval**:
- Name: [Stakeholder Name]
- Role: [Stakeholder Role]
- Date: [Current UTC Timestamp]
- Digital Signature: [Generated Hash]
- Change Authority: [Minor/Major/Critical approval level]

**Technical Review**:
- Name: [Technical Lead Name]
- Role: Technical Lead
- Date: [Current UTC Timestamp]
- Digital Signature: [Generated Hash]
- Technical Impact: [Assessment of technical changes]

## Cryptographic Integrity
**Previous Baseline Hash**: [Hash from previous approval]
**New Baseline Hash**: [SHA-256 hash of updated requirements content]
**Approval Timestamp**: [Current UTC Timestamp]
**Integrity Validation**: External hash validation updated in `requirements-integrity.json`

### Hash Generation Commands
```bash
# Generate new baseline hash (requirements content only)
sed '/^# STAKEHOLDER APPROVAL/,$d' REQUIREMENTS.md | sha256sum | cut -d' ' -f1

# Generate current timestamp
date -u +"%Y-%m-%dT%H:%M:%SZ"

# Update integrity validation
./update-requirements-integrity.sh

# Verify document integrity
./verify-requirements-integrity.sh
```

### Change Control Protection
- **Change Tracking**: All modifications tracked in change log
- **Version Control**: Document version incremented appropriately
- **Integrity Update**: New external hash validation generated
- **Audit Trail**: Complete history of changes and approvals maintained

## Next Steps
1. ✅ Change control re-approval completed
2. ✅ Updated external integrity validation established
3. 🔄 Update STORIES.md if requirements changes affect stories
4. 🔄 Assess impact on current sprint planning
5. 🔄 Communicate changes to development team
6. 🔄 Update project timeline if needed

## Compliance Notes
- This re-approval establishes new requirements baseline for change control
- Previous baseline superseded by this approval
- Development activities may continue based on impact assessment
- Additional stakeholder reviews may be required for major changes
- Updated external hash validation provides tamper-evident protection
```

```markdown
---

# STAKEHOLDER APPROVAL

## Approval Status
**Status**: ✅ APPROVED
**Approval Date**: [Current UTC Timestamp]
**Approved By**: [Stakeholder Name/Role]
**Document Version**: v1.0

## Approval Criteria Met
- [ ] Business objectives clearly defined
- [ ] Functional requirements complete and testable
- [ ] Non-functional requirements specified
- [ ] User personas and journeys documented
- [ ] Technical constraints identified
- [ ] Success criteria measurable
- [ ] Dependencies and assumptions documented
- [ ] Risk assessment completed

## Change Control
**Baseline Established**: [Current UTC Timestamp]
**Change Control Process**: Any modifications to these requirements after approval must follow the change control process defined in CLAUDE.md

### Post-Approval Change Log
| Date | Change Description | Impact Assessment | Approved By | Version |
|------|-------------------|-------------------|-------------|---------|
| - | No changes since baseline | - | - | v1.0 |

## Development Authorization
**Authorization to Proceed**: ✅ GRANTED
**Story Development**: Authorized to proceed with STORIES.md generation
**Sprint Planning**: Authorized to begin sprint planning activities
**Development Start**: Authorized to begin development work

## Approval Signatures
**Stakeholder Approval**:
- Name: [Stakeholder Name]
- Role: [Stakeholder Role]
- Date: [Current UTC Timestamp]
- Digital Signature: [Generated Hash]

**Technical Review**:
- Name: [Technical Lead Name]
- Role: Technical Lead
- Date: [Current UTC Timestamp]
- Digital Signature: [Generated Hash]

## Cryptographic Integrity
**Baseline Hash**: [SHA-256 hash of requirements content only]
**Approval Timestamp**: [Current UTC Timestamp]
**Integrity Validation**: External hash validation stored in `requirements-integrity.json`

### Hash Generation Commands
```bash
# Generate baseline hash (requirements content only)
sed '/^# STAKEHOLDER APPROVAL/,$d' REQUIREMENTS.md | sha256sum | cut -d' ' -f1

# Generate current timestamp
date -u +"%Y-%m-%dT%H:%M:%SZ"

# Verify document integrity
./verify-requirements-integrity.sh
```

### Cryptographic Protection
- **Tamper Detection**: External hash validation detects any modifications
- **Audit Verification**: Separate validation file prevents circular dependencies
- **Baseline Protection**: Original requirements content cryptographically sealed
- **Change Control**: Post-approval modifications invalidate external hash

## Next Steps
1. ✅ Requirements approved and baselined
2. ✅ External integrity validation established
3. 🔄 Generate STORIES.md using modular stories prompt
4. 🔄 Begin sprint planning with epic prioritization
5. 🔄 Set up development environment and repository
6. 🔄 Create initial project structure

## Compliance Notes
- This approval establishes the requirements baseline for change control
- All subsequent changes require formal change requests
- Development activities may proceed based on this approval
- Regular stakeholder reviews scheduled for milestone checkpoints
- External hash validation provides tamper-evident protection
```

### Step 3: Update Document Metadata
Update or add document metadata at the top of REQUIREMENTS.md:

```markdown
---
title: "[Project Name] Requirements Document"
version: "v1.0"
status: "APPROVED"
approval_date: "[Current UTC Timestamp]"
approved_by: "[Stakeholder Name]"
integrity_file: "requirements-integrity.json"
change_control: "true"
---
```

### Step 4: Version Management and Change Control Logic

#### Version Increment Rules:
```bash
# Determine version increment based on change scope
increment_version() {
    CURRENT_VERSION=$1
    CHANGE_SCOPE=$2

    MAJOR=$(echo $CURRENT_VERSION | cut -d'.' -f1 | sed 's/v//')
    MINOR=$(echo $CURRENT_VERSION | cut -d'.' -f2)

    case $CHANGE_SCOPE in
        "Minor")
            # Minor changes: increment patch version (v1.0 -> v1.1)
            NEW_MINOR=$((MINOR + 1))
            NEW_VERSION="v${MAJOR}.${NEW_MINOR}"
            ;;
        "Major")
            # Major changes: increment minor version significantly (v1.0 -> v1.5 or v2.0)
            if [ $MINOR -lt 5 ]; then
                NEW_MINOR=$((MINOR + 5))
                NEW_VERSION="v${MAJOR}.${NEW_MINOR}"
            else
                NEW_MAJOR=$((MAJOR + 1))
                NEW_VERSION="v${NEW_MAJOR}.0"
            fi
            ;;
        "Critical")
            # Critical changes: increment major version (v1.x -> v2.0)
            NEW_MAJOR=$((MAJOR + 1))
            NEW_VERSION="v${NEW_MAJOR}.0"
            ;;
        *)
            # Default: minor increment
            NEW_MINOR=$((MINOR + 1))
            NEW_VERSION="v${MAJOR}.${NEW_MINOR}"
            ;;
    esac

    echo $NEW_VERSION
}
```

#### User Prompt for Undocumented Changes:
When re-approval is detected but changes are not in the change log, prompt:

```markdown
## 🔄 CHANGE CONTROL RE-APPROVAL DETECTED

An existing approval section was found, indicating this is a change control re-approval.
However, changes are not clearly documented in the change log.

**REQUIRED**: Please provide the following information:

### 1. Change Description
What specific changes are being approved in this re-approval?
- [ ] New requirements added
- [ ] Existing requirements modified
- [ ] Requirements removed
- [ ] Technical constraints updated
- [ ] Business objectives changed
- [ ] Other: _______________

**Detail the changes**: _________________________________

### 2. Business Justification
Why are these changes necessary?
- [ ] Market requirements changed
- [ ] Technical discoveries
- [ ] Stakeholder feedback
- [ ] Regulatory compliance
- [ ] Budget/timeline constraints
- [ ] Other: _______________

**Explanation**: _________________________________

### 3. Impact Assessment
**Timeline Impact**: [ ] No impact [ ] +___ days/weeks [ ] -___ days/weeks
**Budget Impact**: [ ] No impact [ ] +$____ [ ] -$____
**Scope Impact**: [ ] No change [ ] Scope increase [ ] Scope reduction

### 4. Authorization
**Change Requestor**: _______________
**Business Approval**: _______________
**Technical Approval**: _______________

### 5. Change Scope Classification
- [ ] **Minor**: Clarifications, small additions, no timeline/budget impact
- [ ] **Major**: Significant changes affecting timeline, budget, or scope
- [ ] **Critical**: Fundamental changes requiring full stakeholder approval

**Selected Scope**: _______________

This information will be added to the change log and used for version increment.
```
### Step 5: Generate External Integrity File

```bash
# Generate baseline hash (content before approval)
BASELINE_HASH=$(sed '/^# STAKEHOLDER APPROVAL/,$d' REQUIREMENTS.md | sha256sum | cut -d' ' -f1)

# Generate final document hash (complete approved document)
DOCUMENT_HASH=$(sha256sum REQUIREMENTS.md | cut -d' ' -f1)

# Generate timestamp
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Create integrity validation file
cat > requirements-integrity.json << EOF
{
  "document": "REQUIREMENTS.md",
  "approval_timestamp": "$TIMESTAMP",
  "document_version": "v1.0",
  "approved_by": "[Stakeholder Name]",
  "hashes": {
    "baseline_content": "$BASELINE_HASH",
    "final_document": "$DOCUMENT_HASH",
    "approval_section": "$(sed -n '/^# STAKEHOLDER APPROVAL/,\$p' REQUIREMENTS.md | sha256sum | cut -d' ' -f1)"
  },
  "validation": {
    "method": "SHA-256",
    "creation_date": "$TIMESTAMP",
    "status": "APPROVED"
  },
  "change_control": {
    "baseline_locked": true,
    "change_process_required": true,
    "next_version": "v1.1"
  }
}
EOF

echo "✅ External integrity validation created: requirements-integrity.json"
```

### Step 6: Create Verification Script
Generate `verify-requirements-integrity.sh`:

```bash
#!/bin/bash
# verify-requirements-integrity.sh

echo "Requirements Document Integrity Verification"
echo "==========================================="

# Check if integrity file exists
if [ ! -f "requirements-integrity.json" ]; then
    echo "❌ INTEGRITY FILE MISSING: requirements-integrity.json not found"
    exit 1
fi

# Check if requirements file exists
if [ ! -f "REQUIREMENTS.md" ]; then
    echo "❌ REQUIREMENTS FILE MISSING: REQUIREMENTS.md not found"
    exit 1
fi

# Extract stored hash from integrity file
STORED_HASH=$(grep '"final_document"' requirements-integrity.json | cut -d'"' -f4)
APPROVAL_DATE=$(grep '"approval_timestamp"' requirements-integrity.json | cut -d'"' -f4)
APPROVED_BY=$(grep '"approved_by"' requirements-integrity.json | cut -d'"' -f4)

# Calculate current document hash
CURRENT_HASH=$(sha256sum REQUIREMENTS.md | cut -d' ' -f1)

echo "Document: REQUIREMENTS.md"
echo "Approved By: $APPROVED_BY"
echo "Approval Date: $APPROVAL_DATE"
echo "Stored Hash:  $STORED_HASH"
echo "Current Hash: $CURRENT_HASH"
echo ""

# Verify integrity
if [ "$STORED_HASH" = "$CURRENT_HASH" ]; then
    echo "Status: ✅ INTEGRITY VERIFIED"
    echo "Document unchanged since stakeholder approval"
    echo "Development authorized to proceed"
    exit 0
else
    echo "Status: ❌ INTEGRITY COMPROMISED"
    echo "Document has been modified since approval"
    echo "Change control process required"
    echo "Development authorization REVOKED"
    exit 1
fi
```

**Make script executable:**
```bash
chmod +x verify-requirements-integrity.sh
```

### Step 7: Generate Current UTC Timestamp
**Use the current UTC timestamp in ISO 8601 format**: `YYYY-MM-DDTHH:MM:SSZ`

Example: `2024-11-08T15:30:00Z`

### Step 8: Complete Execution Logic
Execute the full approval process:

```bash
#!/bin/bash
# complete-approval-process.sh

echo "Requirements Approval Process Starting..."

# Step 1: Detect approval type
if grep -q "^# STAKEHOLDER APPROVAL" REQUIREMENTS.md; then
    echo "🔄 CHANGE CONTROL RE-APPROVAL DETECTED"
    APPROVAL_TYPE="CHANGE_CONTROL"

    # Extract current version
    CURRENT_VERSION=$(grep "Document Version" REQUIREMENTS.md | head -1 | grep -o 'v[0-9]\+\.[0-9]\+')
    echo "Current Version: $CURRENT_VERSION"

    # Check for documented changes
    CHANGE_COUNT=$(grep -A 20 "Post-Approval Change Log" REQUIREMENTS.md | grep -v "No changes since baseline" | grep -c "|")

    if [ $CHANGE_COUNT -le 1 ]; then
        echo "❌ Changes not documented - User input required"
        echo "Please provide change details as specified in the prompt above"
        echo "Waiting for user input..."
        exit 1
    fi

else
    echo "📝 INITIAL APPROVAL DETECTED"
    APPROVAL_TYPE="INITIAL"
    CURRENT_VERSION="v0.0"
fi

# Step 2: Generate new version
case $APPROVAL_TYPE in
    "INITIAL")
        NEW_VERSION="v1.0"
        ;;
    "CHANGE_CONTROL")
        # This would be set based on user input for change scope
        # For now, default to minor increment
        MAJOR=$(echo $CURRENT_VERSION | cut -d'.' -f1 | sed 's/v//')
        MINOR=$(echo $CURRENT_VERSION | cut -d'.' -f2)
        NEW_MINOR=$((MINOR + 1))
        NEW_VERSION="v${MAJOR}.${NEW_MINOR}"
        ;;
esac

echo "New Version: $NEW_VERSION"

# Step 3: Generate hashes and create files
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
BASELINE_HASH=$(sed '/^# STAKEHOLDER APPROVAL/,$d' REQUIREMENTS.md | sha256sum | cut -d' ' -f1)

# Step 4: Update/create approval section with appropriate template
# Step 5: Generate final document hash and create integrity file
# Step 6: Create verification script

echo "✅ Approval process completed with version $NEW_VERSION"
```

### Step 9: Hash Generation Process
Execute these commands for tamper-evident protection:

```bash
# Step 1: Generate baseline hash before approval section
echo "Generating baseline hash..."
BASELINE_HASH=$(sed '/^# STAKEHOLDER APPROVAL/,$d' REQUIREMENTS.md | sha256sum | cut -d' ' -f1)
echo "Baseline Hash: $BASELINE_HASH"

# Step 2: Add approval section to REQUIREMENTS.md
# [Approval section added here]

# Step 3: Generate final document hash
echo "Generating final document hash..."
FINAL_HASH=$(sha256sum REQUIREMENTS.md | cut -d' ' -f1)
echo "Final Document Hash: $FINAL_HASH"

# Step 4: Create external integrity file
echo "Creating external integrity validation..."
./create-integrity-file.sh

# Step 5: Verify setup
echo "Verifying integrity setup..."
./verify-requirements-integrity.sh

echo "✅ Cryptographic integrity protection established"
```

### Step 10: Validation Checklist
Before marking as approved, verify:

```markdown
## Pre-Approval Validation
- [ ] All sections in REQUIREMENTS.md are complete
- [ ] Business objectives are measurable and specific
- [ ] Functional requirements have clear acceptance criteria
- [ ] Non-functional requirements include performance targets
- [ ] User personas are detailed and realistic
- [ ] Technical architecture is feasible
- [ ] Dependencies are identified and manageable
- [ ] Risks are assessed with mitigation strategies
- [ ] Success metrics are defined and measurable
- [ ] Timeline and milestones are realistic
```

### Step 11: Change Control Integration
Update CLAUDE.md to include this change control process:

```markdown
## Requirements Change Control

### Baseline Protection
Once requirements are approved via `approve-requirements.md` command:
- Requirements document is considered baselined
- External integrity file provides tamper-evident validation
- All changes require formal change control process
- Document hash validation prevents unauthorized modifications

### External Integrity Validation
- **Integrity File**: `requirements-integrity.json` contains validation hashes
- **Verification Script**: `verify-requirements-integrity.sh` validates document integrity
- **Separation of Concerns**: Validation data stored externally to prevent circular dependencies
- **Tamper Detection**: Any modification to requirements invalidates stored hash

### Change Request Process
1. **Identify Change Need**: Document business justification
2. **Impact Assessment**: Analyze effect on timeline, budget, scope
3. **Stakeholder Review**: Present change to approval stakeholders
4. **Approval Decision**: Formal approval/rejection with rationale
5. **Document Update**: Update REQUIREMENTS.md with change log entry
6. **Integrity Update**: Regenerate external integrity validation
7. **Communication**: Notify all stakeholders of approved changes

### Change Control Authority
- **Minor Changes** (clarifications, typos): Technical Lead approval
- **Major Changes** (scope, timeline, budget): Stakeholder approval required
- **Critical Changes** (fundamental approach): Full stakeholder committee approval

### Change Tracking
All changes tracked in the Post-Approval Change Log with:
- Change description and rationale
- Impact assessment results
- Approval authority and date
- Updated document version
- New integrity validation hash
```

## Execution Guidelines
1. **Read existing REQUIREMENTS.md completely**
2. **Validate document completeness against checklist**
3. **Generate baseline hash of requirements content**
4. **Generate current UTC timestamp**
5. **Add comprehensive approval section to REQUIREMENTS.md**
6. **Generate final document hash**
7. **Create external integrity validation file**
8. **Generate verification script**
9. **Update document metadata**
10. **Update CLAUDE.md with change control process**
11. **Test integrity verification system**
12. **Confirm all validation criteria are met**

## File Structure After Approval
```
project/
├── REQUIREMENTS.md (approved document)
├── requirements-integrity.json (external validation)
├── verify-requirements-integrity.sh (verification script)
└── CLAUDE.md (updated with change control)
```

## Quality Standards
- **External Validation**: Hash stored separately to prevent circular dependencies
- **Timestamp Accuracy**: Use precise UTC timestamp for audit trail
- **Hash Integrity**: Generate verifiable document hashes externally
- **Approval Authority**: Clear identification of approval stakeholders
- **Change Control**: Robust process for post-approval modifications
- **Audit Trail**: Complete documentation of approval process
- **Compliance**: Meet enterprise change management standards

## Output Confirmation
After execution, confirm:
- ✅ REQUIREMENTS.md updated with approval section
- ✅ Document metadata includes approval information
- ✅ External integrity file created (requirements-integrity.json)
- ✅ Verification script created and executable
- ✅ Hash verification commands tested and working
- ✅ Change control process documented in CLAUDE.md
- ✅ Approval audit trail complete
- ✅ External cryptographic integrity protection active
- ✅ Authorization granted for next development phases

## Integrity Verification Commands
```bash
# Quick integrity check
./verify-requirements-integrity.sh

# Manual verification
STORED=$(grep '"final_document"' requirements-integrity.json | cut -d'"' -f4)
CURRENT=$(sha256sum REQUIREMENTS.md | cut -d' ' -f1)
echo "Stored: $STORED"
echo "Current: $CURRENT"
[ "$STORED" = "$CURRENT" ] && echo "✅ VERIFIED" || echo "❌ COMPROMISED"

# Detailed integrity report
echo "=== REQUIREMENTS INTEGRITY REPORT ==="
echo "Document: REQUIREMENTS.md"
echo "Integrity File: requirements-integrity.json"
echo "Verification Status:"
./verify-requirements-integrity.sh
echo "====================================="
```

## Security Benefits
- **No Circular Dependencies**: External hash storage prevents modification paradox
- **Tamper Evidence**: Any change to requirements immediately detectable
- **Audit Compliance**: Separate validation file provides clear audit trail
- **Version Control**: External file tracks integrity across document versions
- **Enterprise Grade**: Meets banking/financial industry security standards

**Result**: REQUIREMENTS.md is now cryptographically protected with external tamper-evident validation, providing enterprise-grade integrity verification without circular dependency issues.
