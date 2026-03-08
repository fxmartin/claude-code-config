Generate a complete project retrospective report analyzing this repository. Create a structured analysis covering code metrics, dependencies, testing, and GitHub activity. Output as a markdown report with executive summary and actionable insights.

## ANALYSIS REQUIREMENTS:

### 1. CODE METRICS
- Run `cloc .` or `find . -name "*.py" | xargs wc -l` for line counts by language
- Count total files: `find . -type f | grep -E '\.(py|js|ts|java|go|rb|php)$' | wc -l`
- Calculate code-to-test ratio using test directory analysis
- If Python: analyze `requirements.txt`, `pyproject.toml`, or `Pipfile` for dependency count

### 2. TESTING & QUALITY
- Run test coverage: `pytest --cov=. --cov-report=term-missing` (if pytest available)
- Count test files: `find . -name "*test*.py" -o -name "test_*.py" | wc -l`
- Check for CI/CD: look for `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`

### 3. GITHUB INTELLIGENCE (use gh CLI)
- Repository overview: `gh repo view --json name,description,createdAt,pushedAt,stargazerCount,forkCount,language,size`
- Commit analysis: `git log --oneline | wc -l` and `git shortlog -sn --all`
- PR metrics: `gh pr list --state all --json number,state,createdAt,mergedAt,author`
- Issue tracking: `gh issue list --state all --json number,state,createdAt,closedAt,author`
- Contributor stats: `git shortlog -sn | head -10`

### 4. DEPENDENCY HEALTH
- List all dependencies with versions
- Check for security vulnerabilities (if tools available)
- Identify outdated packages

## OUTPUT STRUCTURE:

### Executive Summary
- Project scope (languages, total LOC, duration)
- Key metrics snapshot
- Health score assessment

### Detailed Metrics
- **Codebase**: Lines by language, file counts, complexity
- **Testing**: Coverage %, test count, test-to-code ratio
- **Dependencies**: Total count, language breakdown, health status
- **Activity**: Commit frequency, contributor count, issue resolution rate
- **Collaboration**: PR metrics, review patterns, merge velocity

### Intelligence & Insights
- Code quality indicators
- Development velocity trends
- Team collaboration patterns
- Technical debt indicators
- Recommendations for future projects

## ERROR HANDLING:
- If gh CLI unavailable, use git commands as fallback
- If coverage tools missing, estimate from test file analysis
- Skip unavailable metrics but note limitations

Generate actionable insights, not just raw numbers. Focus on what the data reveals about project health, team dynamics, and development practices.

## FINAL OUTPUT:
After completing the analysis, create a condensed summary file called `PROJECT-STATS.md` containing:
- Essential metrics only (LOC, dependencies, test coverage, commit count, contributors)
- Key health indicators (pass/fail status)
- One-line project assessment
- Critical recommendations (max 3 bullet points)

This file should be scannable in under 30 seconds and suitable for README inclusion or stakeholder sharing.
