You are tasked with converting a detailed BUILD-PLAN.md section into atomic, executable tasks for immediate coding implementation.

**Instructions:**

1. **Read BUILD-PLAN.md**: Parse the complete BUILD-PLAN.md file to understand context and dependencies.

2. **Extract Target Section**: Focus on the section specified by parameter: {SECTION_NAME}

3. **Generate Atomic TODO.md**: Create granular, code-ready tasks that can be executed immediately.

## TODO.md Output Structure:

### Section Overview
- **Section**: {SECTION_NAME}
- **Total Estimated Hours**: [Calculate from atomic tasks]
- **Prerequisites**: [List dependencies from other sections]
- **Key Deliverables**: [Primary outputs from this section]

### Atomic Task List

For each task, provide:
Task [ID]: [Specific Action]

Type: [Setup/Code/Test/Deploy/Documentation]
Estimated Time: [30min/1hr/2hrs/etc.]
Prerequisites: [Task IDs that must complete first]
Files to Create/Modify: [Specific file paths]
Acceptance Criteria:

 [Specific, testable outcome 1]
 [Specific, testable outcome 2]


Implementation Notes: [Technical details, code snippets, specific requirements]


### Task Categories to Include:

**Setup Tasks** (S001, S002, etc.):
- Environment configuration
- Dependency installation
- Database setup
- Configuration file creation

**Development Tasks** (D001, D002, etc.):
- Function/method implementation
- Class creation
- API endpoint development
- Database schema changes
- UI component building

**Integration Tasks** (I001, I002, etc.):
- API connections
- Database integration
- Third-party service integration
- Module interconnection

**Testing Tasks** (T001, T002, etc.):
- Unit test creation
- Integration test development
- End-to-end test scenarios
- Performance testing

**Documentation Tasks** (DOC001, DOC002, etc.):
- Code commenting
- API documentation
- User guide sections
- Technical specifications

**Deployment Tasks** (DEP001, DEP002, etc.):
- Build script creation
- Configuration management
- Deployment preparation
- Environment validation

### Task Sequencing Requirements:

1. **Logical Order**: Tasks must be sequenced in executable order
2. **Dependency Mapping**: Clear prerequisite relationships
3. **Parallel Execution**: Identify tasks that can run simultaneously
4. **Critical Path**: Highlight tasks that block other work

### Atomic Task Criteria:

Each task must be:
- **Specific**: Clear, unambiguous action
- **Measurable**: Defined completion criteria
- **Achievable**: Completable in one coding session (max 4 hours)
- **Relevant**: Directly contributes to section objectives
- **Time-bound**: Realistic time estimate

### Output Format:
- Use markdown with clear task separation
- Include checkboxes [ ] for progress tracking
- Number tasks for easy reference
- Group related tasks under logical headers
- Include code snippets or pseudo-code where helpful

**Parameter**: {SECTION_NAME} - Replace with specific section from BUILD-PLAN.md

**Key Requirement**: Each task should be atomic enough that a developer can pick it up, understand exactly what to do, and complete it within the estimated timeframe without needing additional clarification.

Generate a TODO.md that transforms the strategic plan into executable development work.
