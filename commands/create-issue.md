# Record Issue - Claude Code Prompt

Investigate the reported defect: "$ARGUMENTS" and create a comprehensive GitHub issue.

## DEFECT INVESTIGATION PROCESS

### PHASE 1: Project Context Analysis
1. **Detect project structure**:
   - Analyze package.json, pyproject.toml, Cargo.toml to identify tech stack
   - Identify frameworks: React, FastAPI, Express, Django, etc.
   - Determine test frameworks: Jest, pytest, Vitest, etc.
   - Note current git branch and recent commits (last 5)

2. **Parse defect description**:
   - Extract key terms and technical references
   - Identify mentioned file names, components, or error messages
   - Detect urgency indicators and impact scope

### PHASE 2: Defect Classification
3. **Assess severity** based on description keywords:
   - **CRITICAL**: crash, data loss, security vulnerability, cannot login, fatal error, broken core functionality
   - **HIGH**: error, failure, not working, broken feature, significant bug
   - **MEDIUM**: slow performance, minor functionality issue, usability problem
   - **LOW**: typo, cosmetic issue, enhancement request, polish

4. **Categorize defect type**:
   - **bug**: error, crash, failure, broken functionality
   - **performance**: slow, lag, timeout, memory issues
   - **ui/ux**: layout, design, responsive, mobile, accessibility
   - **security**: auth, permissions, vulnerability, data exposure
   - **api**: endpoint, request/response, HTTP errors
   - **database**: query, data integrity, migration issues
   - **frontend**: React components, UI rendering, client-side
   - **backend**: server, service, worker, API logic
   - **documentation**: missing docs, unclear instructions

5. **Identify affected components**:
   - **authentication**: login, signup, JWT, tokens, user management
   - **dashboard**: main view, overview, home page
   - **portfolio**: holdings, investments, trading, crypto data
   - **api**: REST endpoints, GraphQL, external integrations
   - **database**: models, queries, migrations, data processing
   - **mobile**: responsive design, PWA, mobile-specific features
   - **docker**: containerization, deployment, environment issues

### PHASE 3: Codebase Investigation
6. **Search for relevant files**:
   - Look for explicit file mentions in defect description
   - Search codebase for keywords using `grep -r -l --include="*.py" --include="*.js" --include="*.ts" --include="*.jsx" --include="*.tsx" [keyword] .`
   - Focus on files that likely contain related functionality
   - Limit to top 10 most relevant files to avoid noise

7. **Analyze recent changes**:
   - Check `git log --oneline -10` for recent commits that might be related
   - Look for commits in the same functional area
   - Identify if this might be a regression from recent changes

### PHASE 4: Reproduction Strategy
8. **Generate reproduction steps** based on defect category:
   - **Login/Auth issues**:
     ```
     1. Navigate to login page
     2. Enter valid credentials
     3. Click login button
     4. Observe [specific issue]
     ```

   - **API/Backend issues**:
     ```
     1. Start the application/API server
     2. Make request to [specific endpoint]
     3. Check response status and content
     4. Observe [specific error]
     ```

   - **UI/Mobile issues**:
     ```
     1. Open application on [device/browser]
     2. Navigate to [specific page/component]
     3. [Perform specific interaction]
     4. Observe layout/behavior issue
     ```

   - **Performance issues**:
     ```
     1. Open developer tools/profiler
     2. Navigate to [specific feature]
     3. Monitor performance metrics
     4. Observe slow response/high resource usage
     ```

### PHASE 5: Similar Issue Detection
9. **Check for existing issues**:
   - Use `gh issue list --search "[key terms]" --state all --limit 5`
   - Extract 2-3 most important keywords from description
   - Report any similar issues found to avoid duplicates

### PHASE 6: Issue Creation
10. **Generate issue title**:
    - Format: `[CATEGORY] [SEVERITY] Brief description`
    - Example: `[API] [HIGH] Portfolio endpoint returns 500 error for crypto data`
    - Keep under 80 characters, be specific and actionable
    - Available labels are: api, authentication, backend-py, backend-ts, blocked, breacking-change, bug, code-review, critical, crypto, dashboard, database, docker, documentation, duplicate, enhancement, feature-request, frontend, good first issue, good-first-issue, help wanted, help-wanted, high, hotfix, in-progress, invalid, low, medium, mobile, multi-agent

11. **Create comprehensive issue body**:
    ```markdown
    ## Description
    [Original defect description]

    ## Steps to Reproduce
    [Generated reproduction steps]

    ## Expected Result
    [What should happen instead]

    ## Actual Result
    [Current incorrect behavior]

    ## Technical Context
    - **Project Type**: [detected project type]
    - **Languages**: [detected languages]
    - **Frameworks**: [detected frameworks]
    - **Current Branch**: [git branch name]
    - **Test Framework**: [detected test framework]

    ## Potentially Relevant Files
    [List of files that might be related]

    ## Recent Changes
    [Recent commits that might be related]

    ## Similar Issues
    [Any existing issues found]

    ## Issue Metadata
    - **Severity**: [assessed severity]
    - **Category**: [defect category]
    - **Components**: [affected components]
    - **Created**: [timestamp]
    - **Auto-generated**: ✅ via claude-code
    ```

12. **Determine appropriate labels**:
    - Add category label: `bug`, `performance`, `ui/ux`, `security`, `api`, etc.
    - Add severity label if high/critical: `high`, `critical`
    - Add component labels: `authentication`, `dashboard`, `mobile`, etc.
    - Add technology labels: `typescript`, `python`, `react`, `fastapi`
    - Limit to 5 most relevant labels

### PHASE 7: Issue Creation & Validation
13. **Create the GitHub issue**:
    ```bash
    gh issue create \
      --title "[Generated title]" \
      --body "[Generated body]" \
      --label "[comma-separated labels]"
    ```

14. **Validate creation**:
    - Confirm issue was created successfully
    - Provide the issue URL
    - Suggest next steps (assignment, milestone, etc.)

## ERROR HANDLING & EDGE CASES

**If GitHub CLI not available**:
- Check `gh auth status` first
- Guide user to install/authenticate GitHub CLI
- Provide manual issue creation template as fallback

**If not in Git repository**:
- Detect .git directory presence
- Prompt to initialize git repository first
- Explain importance of version control for issue tracking

**If no similar keywords found in codebase**:
- Still create the issue but note limited technical context
- Focus on clear reproduction steps and user impact
- Suggest manual investigation by developer

**If description is too vague**:
- Ask clarifying questions about:
  - When does this happen?
  - What were you trying to do?
  - What browser/device are you using?
  - Can you provide error messages?

## OUTPUT REQUIREMENTS

Always provide:
- **Investigation summary**: severity, category, components affected
- **Similar issues found**: any duplicates or related issues
- **Issue preview**: title and key details before creation
- **Creation confirmation**: issue URL and next steps
- **Relevant files identified**: for developer reference
- **Suggested immediate actions**: if critical/high severity

Remember: This creates **actionable, professional GitHub issues** with proper investigation, context, and reproduction steps. The goal is to make developer triage and fixing as efficient as possible.
