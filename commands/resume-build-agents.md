# Resume Development Work - Multi-Agent Claude Code Prompt

Please analyze and resume development work for $ARGUMENTS using specialized agents for optimal results.

## AGENT SELECTION & COORDINATION
Available specialized agents:
- **backend-typescript-architect**: API design, TypeScript backend, architecture decisions
- **python-backend-engineer**: Python services, data processing, backend logic
- **senior-code-reviewer**: Code quality, security, performance, best practices
- **ui-engineer**: Frontend components, user experience, responsive design
- **bash-zsh-macos-engineer**: Shell scripting, automation, system administration, DevOps workflows
- **qa-engineer**: Quality assurance, test strategy, manual/automated testing, defect management
- **podman-container-architect**: Containerization, orchestration, deployment, microservices architecture

## CONTEXT & CONFIGURATION
- Working directory: $PWD
- Project type: Detect from package.json/pyproject.toml/scripts/etc.
- Test framework: Auto-detect (pytest/jest/vitest/bats/etc.)
- Linting tools: Auto-detect (ruff/eslint/shellcheck/etc.)
- Type checking: Auto-detect (mypy/typescript/etc.)
- **Agent selection**: Auto-detect based on story type and affected components

## SETUP & VALIDATION
- Verify working directory contains STORIES.md
- Check git status - ensure clean working directory or stash changes
- Parse $ARGUMENTS to identify target story/epic:
  - Story ID format: "US-001", "EPIC-02", "OPS-001", etc.
  - Epic name: "authentication", "dashboard", "deployment", "automation", etc.
  - "next" = auto-select next logical story from STORIES.md
- If story not found, list available options and exit with suggestions
- Verify GitHub CLI authentication: `gh auth status`

## ARGUMENT VALIDATION & STORY SELECTION
- If $ARGUMENTS="next":
  - Parse /docs/STORIES.md to find highest priority unblocked story
  - Consider stories marked as TODO with satisfied dependencies
  - Prefer continuing existing epics over starting new ones
- If $ARGUMENTS is story-id: validate it exists and is actionable
- If $ARGUMENTS is epic-name: find next story within that epic
- STOP if target story is: DONE, BLOCKED, or missing acceptance criteria

## BRANCH MANAGEMENT
- Extract clean story ID from selected story (e.g., US-001, EPIC-02-AUTH, OPS-001)
- Check if feature branch exists: `git branch --list "feature/$STORY_ID"`
- If branch exists:
  - `git checkout feature/$STORY_ID`
  - `git rebase origin/main` (handle conflicts if any)
  - Review existing commits to understand current progress
- If new branch:
  - `git checkout -b feature/$STORY_ID`
  - Ensure branched from latest main/develop

## AGENT SELECTION LOGIC
Based on story analysis, select primary and supporting agents:

**Story Type Detection:**
- **API/Backend Logic** → `backend-typescript-architect` or `python-backend-engineer`
- **Frontend/UI** → `ui-engineer`
- **Database/Data Processing** → `python-backend-engineer`
- **Architecture/System Design** → `backend-typescript-architect`
- **Code Quality/Refactoring** → `senior-code-reviewer`
- **Automation/DevOps/Scripting** → `bash-zsh-macos-engineer`
- **Testing/QA/Quality Assurance** → `qa-engineer`
- **Containerization/Deployment/Orchestration** → `podman-container-architect`

**Technology Stack Detection:**
- TypeScript/Node.js files → `backend-typescript-architect`
- Python/FastAPI files → `python-backend-engineer`
- React/Frontend files → `ui-engineer`
- Shell scripts (.sh/.zsh/.bash) → `bash-zsh-macos-engineer`
- Test files (.test.js/.spec.ts/.test.py) → `qa-engineer`
- E2E test files (cypress, playwright, selenium) → `qa-engineer`
- Container files (Dockerfile, Containerfile, docker-compose.yml, .dockerignore) → `podman-container-architect`
- Kubernetes files (*.yaml, *.yml in k8s/, manifests/) → `podman-container-architect`
- CI/CD files (.github/workflows, scripts/) → `bash-zsh-macos-engineer`
- DevOps configuration → `bash-zsh-macos-engineer`
- Complex refactoring → `senior-code-reviewer` + primary agent

## DISCOVERY PHASE
1. **Read project context**:
   - STORIES.md for overall epic structure and relationships
   - Locate and read relevant epic file: `docs/stories/epic-XX-*.md`
   - Extract: story details, acceptance criteria, dependencies, technical notes
   - **Agent Assignment**: Analyze story requirements to determine primary agent

2. **Assess current progress**:
   - Check if work already started on this branch
   - Review existing commits: `git log --oneline origin/main..HEAD`
   - Identify what's already implemented vs. what's remaining
   - **Multi-agent context**: Share progress with selected agents

3. **Codebase investigation** (agent-specific):
   - **backend-typescript-architect**: Focus on API routes, services, middleware, types
   - **python-backend-engineer**: Focus on models, business logic, data processing, workers
   - **ui-engineer**: Focus on components, pages, hooks, styles, mobile responsiveness
   - **bash-zsh-macos-engineer**: Focus on scripts, automation, CI/CD, deployment, system configuration
   - **qa-engineer**: Focus on test coverage, test frameworks, quality metrics, defect patterns
   - **podman-container-architect**: Focus on containerization, orchestration, deployment configs, service mesh
   - **senior-code-reviewer**: Focus on patterns, security, performance, technical debt

## AGENT COORDINATION STRATEGY
4. **Primary agent selection**:
   ```
   IF story involves API design OR TypeScript backend:
     PRIMARY = backend-typescript-architect
   ELSE IF story involves Python services OR data processing:
     PRIMARY = python-backend-engineer
   ELSE IF story involves UI/UX OR frontend components:
     PRIMARY = ui-engineer
   ELSE IF story involves automation OR DevOps OR shell scripting:
     PRIMARY = bash-zsh-macos-engineer
   ELSE IF story involves testing OR QA OR quality assurance:
     PRIMARY = qa-engineer
   ELSE IF story involves containerization OR deployment OR orchestration:
     PRIMARY = podman-container-architect
   ELSE IF story is refactoring OR code quality:
     PRIMARY = senior-code-reviewer
   ```

5. **Supporting agent involvement**:
   - **Always include senior-code-reviewer** for final review phase
   - **Always include qa-engineer** for quality validation and testing strategy
   - **Cross-stack stories** require multiple agents
   - **Full-stack features** need coordination between backend + frontend agents
   - **DevOps integration** stories need `bash-zsh-macos-engineer` + application agents
   - **Quality-critical features** need `qa-engineer` + primary development agent
   - **Deployment/containerization** stories need `podman-container-architect` + application agents

## DEVELOPMENT PHASE (AGENT-DRIVEN TDD)

### PHASE 1: Architecture & Planning (if needed)
**Agent**: `backend-typescript-architect` (for system design), `bash-zsh-macos-engineer` (for automation/DevOps), `podman-container-architect` (for containerization/orchestration), `qa-engineer` (for quality strategy), or `senior-code-reviewer` (for refactoring)
- Design API contracts and data flow
- Plan component architecture and interfaces
- Design automation workflows and deployment pipelines
- Plan containerization strategy and orchestration
- Plan system integration and configuration management
- Define quality gates and testing strategy
- Plan test coverage and quality metrics
- Identify integration points and dependencies
- Create technical design document in epic file
- **Handoff**: Provide detailed implementation plan to primary agent

### PHASE 2: Test-First Development
**Primary Agent**: Based on story type
- **backend-typescript-architect**:
  - Write API endpoint tests (request/response validation)
  - Integration tests for service layer
  - TypeScript type safety tests

- **python-backend-engineer**:
  - Unit tests for business logic
  - Database integration tests
  - Data processing pipeline tests

- **ui-engineer**:
  - Component rendering tests
  - User interaction tests
  - Responsive design tests
  - Accessibility tests

- **bash-zsh-macos-engineer**:
  - Script unit tests using bats or similar framework
  - Integration tests for automation workflows
  - System validation tests
  - Error handling and edge case tests
  - Performance tests for scripts

- **qa-engineer**:
  - Design comprehensive test strategy
  - Write unit, integration, and E2E test suites
  - Create manual test cases and scenarios
  - Implement API testing and contract validation
  - Design performance and security test plans
  - Set up test data management
  - Create quality metrics and reporting

- **podman-container-architect**:
  - Design containerization test strategy
  - Write container integration and orchestration tests
  - Create deployment validation tests
  - Implement service mesh and networking tests
  - Design scalability and performance tests
  - Set up multi-environment testing
  - Create container security and compliance tests

**Test Strategy**:
- Analyze existing test coverage for the feature area
- Write failing tests that define expected behavior from acceptance criteria
- Run tests to confirm they fail with expected messages
- Focus on: happy path, edge cases, error conditions

### PHASE 3: Implementation Cycle
**Primary Agent**: Implement core functionality
- Follow TDD cycle: Red → Green → Refactor
- Implement minimal code to make tests pass
- Focus on single responsibility and clean interfaces
- Add proper error handling and logging

**Cross-Agent Collaboration**:
- **Full-stack features**:
  - Backend agent implements API
  - Frontend agent implements UI
  - Both ensure contract compatibility
  - qa-engineer validates integration points
  - podman-container-architect containerizes components

- **Data flow features**:
  - Python agent handles data processing
  - TypeScript agent handles API layer
  - UI agent handles presentation
  - qa-engineer tests data flow end-to-end
  - podman-container-architect orchestrates service interactions

- **DevOps integration features**:
  - bash-zsh-macos-engineer implements automation
  - Backend agents provide integration points
  - All agents ensure deployment compatibility
  - qa-engineer validates deployment quality
  - podman-container-architect handles containerization and orchestration

- **Quality-critical features**:
  - Primary agent implements functionality
  - qa-engineer designs comprehensive test coverage
  - senior-code-reviewer ensures code quality
  - podman-container-architect ensures deployment reliability
  - All agents collaborate on quality metrics

- **Containerization/deployment features**:
  - podman-container-architect designs container architecture
  - Application agents ensure containerization compatibility
  - bash-zsh-macos-engineer handles deployment automation
  - qa-engineer validates containerized environments
  - senior-code-reviewer ensures security and best practices

### PHASE 4: Code Review & Quality (MANDATORY)
**Agent**: `senior-code-reviewer`
- Review all code changes for:
  - Security vulnerabilities
  - Performance implications
  - Code maintainability
  - Adherence to project patterns
  - Error handling completeness
- **GATE**: Must approve before proceeding to integration

### PHASE 5: Integration & Testing
**Multi-Agent Validation**:
- Run agent-specific test suites
- Cross-integration testing between layers
- End-to-end testing for full-stack features
- Performance testing for critical paths
- Automation workflow testing

### PHASE 6: Agent-Specific Quality Gates
**backend-typescript-architect**:
- TypeScript compilation: `tsc --noEmit`
- API documentation updated
- OpenAPI/Swagger specs current
- Performance benchmarks if applicable

**python-backend-engineer**:
- Type checking: `mypy .`
- Linting: `ruff check .`
- Security scan: `bandit` or similar
- Database migration validity

**ui-engineer**:
- Bundle size analysis
- Accessibility audit (a11y)
- Cross-browser compatibility
- Mobile responsiveness validation

**bash-zsh-macos-engineer**:
- Shell script linting: `shellcheck`
- Script execution permissions and security
- Cross-platform compatibility validation
- Performance profiling for automation scripts
- Error handling and logging validation
- macOS-specific feature testing

**qa-engineer**:
- Test coverage analysis: minimum 90% coverage
- Test suite execution: all tests passing
- Quality metrics validation: defect density, test effectiveness
- Risk assessment: security, performance, usability testing
- Test automation: regression suite automated
- Documentation: test plans and quality reports updated

**podman-container-architect**:
- Container security scanning: vulnerability assessment completed
- Image optimization: multi-stage builds, minimal base images
- Orchestration validation: Kubernetes/Podman Compose testing
- Service mesh configuration: networking and communication validation
- Resource optimization: CPU/memory limits and requests configured
- Health checks: container and service health monitoring implemented

**senior-code-reviewer** (Final Gate):
- Architecture consistency
- Security review completion
- Performance impact assessment
- Technical debt evaluation

## DELIVERY PHASE (MULTI-AGENT COORDINATION)

### Commit Strategy
**Agent-specific commit patterns**:
```
# Backend changes
feat(api): implement user authentication endpoint (#US-001)

# Frontend changes
feat(ui): add responsive dashboard layout (#US-002)

# Containerization changes
feat(containers): add microservices containerization (#CONTAINER-001)

# Orchestration changes
feat(k8s): implement Kubernetes deployment manifests (#CONTAINER-002)

# Testing changes
test: add comprehensive E2E test suite (#QA-001)

# Quality assurance changes
test(qa): implement API testing framework (#QA-002)

# DevOps/Automation changes
feat(ops): add automated deployment pipeline (#OPS-001)

# Shell scripting changes
feat(scripts): implement environment setup automation (#DEV-001)

# Full-stack feature
feat: implement crypto portfolio tracking (#EPIC-01)

Co-authored-by: backend-typescript-architect
Co-authored-by: ui-engineer
Co-authored-by: bash-zsh-macos-engineer
Co-authored-by: qa-engineer
Co-authored-by: podman-container-architect
Co-authored-by: senior-code-reviewer
```

### PR Creation with Agent Context
```bash
gh pr create \
  --title "feat: [Story Title] (#STORY-ID)" \
  --body "## Summary
Implements [story description]

## Agent Contributions
- **Primary**: [primary-agent] - [main implementation]
- **Architecture**: [architect] - [design decisions]
- **Automation**: bash-zsh-macos-engineer - [DevOps/scripting contributions]
- **Quality Assurance**: qa-engineer - [testing strategy and validation]
- **Containerization**: podman-container-architect - [containerization and orchestration]
- **Review**: senior-code-reviewer - [quality gates passed]

## Technical Details
- **Backend Changes**: [API endpoints, services, models]
- **Frontend Changes**: [components, pages, styling]
- **Database Changes**: [migrations, schema updates]
- **Automation Changes**: [scripts, CI/CD, deployment]
- **Testing Changes**: [test suites, quality metrics, coverage]
- **Container Changes**: [Containerfiles, orchestration, deployment configs]

## Testing Strategy
- **Unit Tests**: [coverage stats]
- **Integration Tests**: [cross-layer validation]
- **E2E Tests**: [user journey validation]
- **Automation Tests**: [script validation, system tests]
- **Quality Metrics**: [test coverage, defect density, performance benchmarks]
- **Container Tests**: [containerization validation, orchestration testing]

## Performance Impact
- **Bundle Size**: [before/after if applicable]
- **API Response Time**: [benchmarks if applicable]
- **Database Query Performance**: [analysis if applicable]
- **Script Performance**: [execution time, resource usage]

Closes #STORY-ID"
```

## INTEGRATION & DOCUMENTATION
7. **Code integration**:
   - Ensure feature integrates properly with existing systems
   - Update configuration files if needed
   - Add/update API documentation if endpoints changed
   - Update automation documentation if scripts changed
   - Update README or developer docs if setup process changed
   - Document new scripts in project documentation

8. **Progress tracking**:
   - Update STORIES.md: mark story as DONE or IN_PROGRESS with % completion
   - Update relevant `docs/stories/epic-XX-*.md` with:
     - Completion status
     - Implementation notes
     - Any discovered dependencies or blockers
     - Performance or technical debt notes
     - Automation improvements or script optimizations

## DELIVERY PHASE
9. **Commit preparation**:
   - Review all changes: `git diff --staged`
   - Ensure no debug code, console.logs, temporary files, or test scripts
   - Stage changes thoughtfully: group related changes
   - Create descriptive commit message following conventional commits:
   ```
   feat(epic-name): implement [story description] (#STORY-ID)

   - Add [specific functionality 1]
   - Implement [specific functionality 2]
   - Update [documentation/tests/etc.]
   - Add [automation/scripts/deployment changes]

   Acceptance criteria:
   - [x] Criteria 1 completed
   - [x] Criteria 2 completed
   - [ ] Criteria 3 (if any remaining)

   Refs: #STORY-ID
   ```

10. **Push and PR creation**:
    - Push feature branch: `git push origin feature/$STORY_ID`
    - Create PR with comprehensive description:
    ```bash
    gh pr create \
      --title "feat: [Story Title] (#STORY-ID)" \
      --body "## Summary
    Implements [story description]

    ## Changes
    - [List key changes including automation/scripting]

    ## Testing
    - [How to test the feature]
    - [How to test automation/scripts]

    ## Acceptance Criteria
    - [x] All criteria met

    Closes #STORY-ID"
    ```
    - Add relevant labels: enhancement/bug/documentation/automation
    - Assign reviewers if team project

## AGENT WORKFLOW EXAMPLES

### Example 1: Full-Stack Authentication Feature
```
Story: "Implement JWT-based user authentication"
Primary Agent: backend-typescript-architect
Supporting: ui-engineer, senior-code-reviewer

Workflow:
1. backend-typescript-architect: Design auth API endpoints
2. backend-typescript-architect: Implement JWT middleware
3. ui-engineer: Create login/register components
4. ui-engineer: Implement auth state management
5. senior-code-reviewer: Security review of auth flow
6. ALL: Integration testing
```

### Example 2: Python Data Processing Feature
```
Story: "Add crypto price aggregation worker"
Primary Agent: python-backend-engineer
Supporting: backend-typescript-architect, bash-zsh-macos-engineer, senior-code-reviewer

Workflow:
1. python-backend-engineer: Implement price fetching logic
2. python-backend-engineer: Add data validation and storage
3. backend-typescript-architect: Create API endpoints for data access
4. bash-zsh-macos-engineer: Add worker deployment automation
5. senior-code-reviewer: Performance and error handling review
6. ALL: End-to-end testing
```

### Example 3: UI Enhancement
```
Story: "Responsive dashboard for mobile devices"
Primary Agent: ui-engineer
Supporting: senior-code-reviewer

Workflow:
1. ui-engineer: Analyze current layout constraints
2. ui-engineer: Implement responsive grid system
3. ui-engineer: Add mobile-optimized components
4. senior-code-reviewer: Accessibility and performance review
5. ui-engineer: Cross-device testing
```

### Example 4: DevOps Automation Feature
```
Story: "Automated deployment pipeline with rollback capability"
Primary Agent: bash-zsh-macos-engineer
Supporting: backend-typescript-architect, python-backend-engineer, senior-code-reviewer

Workflow:
1. bash-zsh-macos-engineer: Design deployment workflow
2. bash-zsh-macos-engineer: Implement CI/CD scripts
3. bash-zsh-macos-engineer: Add rollback automation
4. backend-typescript-architect: Ensure API deployment compatibility
5. python-backend-engineer: Ensure service deployment compatibility
6. senior-code-reviewer: Security and reliability review
7. ALL: End-to-end deployment testing
```

### Example 5: Environment Setup Automation
```
Story: "Developer environment setup script for new team members"
Primary Agent: bash-zsh-macos-engineer
Supporting: senior-code-reviewer

Workflow:
1. bash-zsh-macos-engineer: Analyze current setup requirements
2. bash-zsh-macos-engineer: Implement automated installation scripts
3. bash-zsh-macos-engineer: Add environment validation
4. bash-zsh-macos-engineer: Create documentation and troubleshooting
5. senior-code-reviewer: Security and maintainability review
6. bash-zsh-macos-engineer: Cross-platform testing
```

### Example 7: Quality Assurance Implementation
```
Story: "Implement comprehensive test automation framework"
Primary Agent: qa-engineer
Supporting: backend-typescript-architect, ui-engineer, bash-zsh-macos-engineer, senior-code-reviewer

Workflow:
1. qa-engineer: Design test strategy and framework architecture
2. qa-engineer: Implement unit and integration test suites
3. qa-engineer: Create E2E test scenarios
4. backend-typescript-architect: Ensure API testability
5. ui-engineer: Add component test utilities
6. bash-zsh-macos-engineer: Integrate tests into CI/CD pipeline
7. senior-code-reviewer: Quality and maintainability review
8. ALL: Comprehensive testing validation
```

### Example 9: Microservices Containerization
```
Story: "Containerize monolithic application into microservices"
Primary Agent: docker-expert
Supporting: backend-typescript-architect, python-backend-engineer, bash-zsh-macos-engineer, qa-engineer, senior-code-reviewer

Workflow:
1. docker-expert: Design microservices container architecture
2. docker-expert: Create Dockerfiles and multi-stage builds
3. backend-typescript-architect: Refactor API for service separation
4. python-backend-engineer: Optimize data services for containers
5. docker-expert: Implement service discovery and networking
6. bash-zsh-macos-engineer: Automate container deployment pipeline
7. qa-engineer: Design container testing strategy
8. senior-code-reviewer: Security and performance review
9. ALL: End-to-end containerized system testing
```

### Example 10: Kubernetes Orchestration Implementation
```
Story: "Deploy application to Kubernetes with auto-scaling"
Primary Agent: docker-expert
Supporting: bash-zsh-macos-engineer, qa-engineer, senior-code-reviewer

Workflow:
1. docker-expert: Design Kubernetes deployment architecture
2. docker-expert: Create manifests for deployments, services, ingress
3. docker-expert: Implement auto-scaling and resource management
4. bash-zsh-macos-engineer: Integrate K8s deployment into CI/CD
5. qa-engineer: Design containerized environment testing
6. docker-expert: Configure monitoring and logging
7. senior-code-reviewer: Security and reliability review
8. ALL: Production readiness validation
```

### Example 11: Multi-Environment Container Strategy
```
Story: "Implement consistent dev/staging/prod container environments"
Primary Agent: docker-expert
Supporting: bash-zsh-macos-engineer, backend-typescript-architect, qa-engineer

Workflow:
1. docker-expert: Design multi-environment container strategy
2. docker-expert: Create environment-specific configurations
3. bash-zsh-macos-engineer: Automate environment provisioning
4. backend-typescript-architect: Ensure application environment compatibility
5. qa-engineer: Validate environment consistency testing
6. docker-expert: Implement secrets and configuration management
7. ALL: Multi-environment deployment validation
```
```
Story: "Comprehensive quality validation for mobile and web"
Primary Agent: qa-engineer
Supporting: ui-engineer, senior-code-reviewer

Workflow:
1. qa-engineer: Design cross-platform test strategy
2. qa-engineer: Implement mobile and web compatibility tests
3. qa-engineer: Create performance and accessibility test suites
4. ui-engineer: Ensure responsive design testability
5. qa-engineer: Execute comprehensive test validation
6. senior-code-reviewer: Review quality standards compliance
7. ALL: Cross-platform testing and validation
```
```
Story: "Implement monitoring and alerting system"
Primary Agent: bash-zsh-macos-engineer
Supporting: python-backend-engineer, backend-typescript-architect, senior-code-reviewer

Workflow:
1. bash-zsh-macos-engineer: Design monitoring infrastructure
2. python-backend-engineer: Implement metrics collection
3. backend-typescript-architect: Add API health endpoints
4. bash-zsh-macos-engineer: Configure alerting automation
5. senior-code-reviewer: Reliability and security review
6. ALL: Monitoring validation and testing
```

## ERROR HANDLING & AGENT-SPECIFIC SAFEGUARDS
- **If agent expertise mismatch**:
  - Auto-reassign to appropriate agent
  - Document why reassignment occurred
  - Ensure knowledge transfer between agents

- **If cross-agent conflicts**:
  - senior-code-reviewer mediates technical decisions
  - Document resolution approach in epic file
  - Ensure all agents agree on final implementation

- **If agent unavailable/errors**:
  - Fallback to general implementation with detailed comments
  - Flag for specialized agent review when available
  - Don't block progress but mark areas for optimization

- **Agent-specific failure modes**:
  - **backend-typescript-architect**: Type errors → document and fix incrementally
  - **python-backend-engineer**: Import/dependency issues → virtual env validation
  - **ui-engineer**: Build failures → component isolation and testing
  - **bash-zsh-macos-engineer**: Permission/environment issues → validate system requirements
  - **qa-engineer**: Test framework issues → validate test environment and dependencies
  - **podman-container-architect**: Container/orchestration issues → validate Podman/K8s environment
  - **senior-code-reviewer**: Standards conflicts → document exceptions with reasoning

## SMART BOUNDARIES & TIME MANAGEMENT
- **Complexity assessment**:
  - If story estimated >8 hours, suggest breaking down
  - If investigation takes >30min without progress, document findings and pause

- **Scope discipline**:
  - Implement only what's in acceptance criteria
  - Note "nice to have" improvements as separate stories
  - Don't fix unrelated issues (create separate issues instead)

- **Progress checkpoints**:
  - Every hour: commit WIP if significant progress
  - If interrupted: detailed commit message about current state
  - End of session: update epic file with current status

## OUTPUT REQUIREMENTS (MULTI-AGENT SUMMARY)
Always provide comprehensive summary with agent attribution:
- **Story worked on**: ID, title, epic context
- **Agent coordination**: Primary agent, supporting agents, handoffs completed
- **Branch status**: new/existing, conflicts resolved, ready for review
- **Files modified by agent**:
  - Backend (TypeScript): [list files and changes]
  - Backend (Python): [list files and changes]
  - Frontend: [list files and changes]
  - Scripts/Automation: [list files and changes]
  - Tests/QA: [list files and changes]
  - Containers/Orchestration: [list files and changes]
  - Tests: [coverage by layer/agent]
- **Quality gates passed**:
  - Agent-specific linting/type checking
  - Security review completion
  - Performance validation
  - Cross-integration testing
  - Automation validation
  - Quality assurance validation
  - Containerization validation
- **Next steps with agent assignment**:
  - If story complete: PR ready for review
  - If story partial: which agent continues, what's remaining
  - Suggested next story and recommended primary agent
- **Agent expertise gained**: Document new patterns or solutions for future stories
- **Cross-agent collaboration notes**: What worked well, what to improve

## WORKFLOW INTEGRATION & AGENT LEARNING
- **Agent specialization tracking**: Update agent capabilities based on completed work
- **Pattern library building**: Each agent contributes reusable patterns to project
- **Knowledge sharing**: Document agent-specific solutions for future reference
- **Epic continuity with agents**: Prefer same agent for related stories within epic

## AGENT SELECTION COMMAND EXAMPLES
```bash
# Auto-select agent based on story
claude-code resume-development next

# Force specific agent (override auto-selection)
claude-code resume-development US-001 --agent backend-typescript-architect

# DevOps/automation story
claude-code resume-development OPS-001 --agent bash-zsh-macos-engineer

# Containerization/deployment story
claude-code resume-development CONTAINER-001 --agent podman-container-architect

# QA/testing story
claude-code resume-development QA-001 --agent qa-engineer

# Multi-agent story (full-stack feature)
claude-code resume-development EPIC-02-DASHBOARD --agents ui-engineer,backend-typescript-architect

# Multi-agent with automation (deployment feature)
claude-code resume-development EPIC-03-DEPLOY --agents bash-zsh-macos-engineer,python-backend-engineer

# Multi-agent with containerization (microservices deployment)
claude-code resume-development EPIC-05-MICROSERVICES --agents podman-container-architect,backend-typescript-architect,python-backend-engineer

# Multi-agent with testing (quality-critical feature)
claude-code resume-development EPIC-04-SECURITY --agents qa-engineer,backend-typescript-architect

# Code review mode (senior-code-reviewer leads)
claude-code resume-development US-005 --mode review
```

## BASH-ZSH-MACOS-ENGINEER SPECIFIC CAPABILITIES
- **Script Development**: Environment setup, build automation, deployment scripts
- **CI/CD Integration**: GitHub Actions, GitLab CI, custom pipeline automation
- **System Administration**: User management, software installation, configuration
- **Development Workflow**: Git hooks, testing automation, code generation scripts
- **macOS Integration**: Homebrew automation, system preferences, keychain management
- **Performance Optimization**: Script profiling, resource usage optimization
- **Security Implementation**: Permission management, secret handling, audit scripts
- **Cross-Platform Support**: Linux/macOS compatibility, Docker integration

## PODMAN-CONTAINER-ARCHITECT SPECIFIC CAPABILITIES
- **Containerization Strategy**: Multi-stage builds, image optimization, security scanning
- **Orchestration Design**: Kubernetes manifests, service mesh, auto-scaling configuration
- **Deployment Architecture**: Multi-environment strategy, secrets management, configuration
- **Service Discovery**: Container networking, load balancing, service communication
- **Monitoring & Logging**: Container observability, health checks, performance metrics
- **Security Compliance**: Container vulnerability scanning, runtime security, policy enforcement
- **Performance Optimization**: Resource limits, scaling strategies, efficiency optimization
- **CI/CD Integration**: Container build pipelines, registry management, deployment automation
- **Podman Expertise**: Rootless containers, daemonless architecture, OCI compliance

## QA-ENGINEER SPECIFIC CAPABILITIES
- **Test Strategy Design**: Comprehensive test planning, risk assessment, coverage analysis
- **Test Automation**: Framework development, CI/CD integration, regression suites
- **Manual Testing**: Exploratory testing, usability testing, accessibility validation
- **API Testing**: Contract testing, integration validation, performance testing
- **Mobile Testing**: Cross-device compatibility, responsive design validation
- **Performance Testing**: Load testing, stress testing, bottleneck identification
- **Security Testing**: Vulnerability assessment, authentication testing, data validation
- **Quality Metrics**: Test coverage analysis, defect tracking, quality reporting
- **Test Environment Management**: Data management, environment setup, configuration control

Remember: This is **agent-orchestrated, TDD-driven, Git-disciplined development**. Each agent brings specialized expertise while maintaining project consistency through the senior-code-reviewer gate. Every story benefits from the right specialist while ensuring quality through cross-agent collaboration. The podman-container-architect adds critical containerization and orchestration capabilities with emphasis on rootless, secure, and enterprise-ready container solutions.
