# Senior Code Reviewer - Project Analysis

## Mission
Use the senior-code-reviewer agent to conduct a comprehensive technical and business analysis of this codebase. Focus on enterprise readiness, maintainability, and risk assessment. No fluff—actionable insights only.  Your job is to create a REVIEW.md file that executives can scan in 30 seconds and engineers can use to assess the project's readiness and risks.

## Analysis Framework

### 1. Architecture Assessment
- **Design Patterns**: Evaluate adherence to SOLID principles, identify anti-patterns
- **Scalability**: Assess horizontal/vertical scaling limitations
- **Modularity**: Review component coupling and cohesion
- **Technical Debt**: Quantify and prioritize debt items with business impact

### 2. Code Quality Audit
- **Standards Compliance**: Check naming conventions, formatting, documentation
- **Security Vulnerabilities**: Scan for OWASP Top 10, dependency vulnerabilities
- **Performance Bottlenecks**: Identify inefficient queries, memory leaks, blocking operations
- **Testing Coverage**: Analyze unit/integration/e2e test gaps

### 3. Business Risk Analysis
- **Maintenance Burden**: Estimate ongoing development overhead
- **Deployment Readiness**: Evaluate CI/CD pipeline, environment parity
- **Compliance Gaps**: Check regulatory requirements (PCI, SOX, GDPR where applicable)
- **Knowledge Dependencies**: Identify bus factor risks

## Deliverables Required

### Executive Summary (2-3 sentences)
Overall project health, go/no-go recommendation, primary risk factors

### Critical Issues (High Priority)
- Security vulnerabilities requiring immediate attention
- Performance issues impacting user experience
- Architecture decisions blocking scalability

### Technical Debt Backlog
Prioritized list with effort estimates and business impact rationale

### Recommendations Matrix
| Issue | Impact | Effort | Priority | Owner |
|-------|---------|---------|----------|--------|

## Analysis Constraints
- Focus on production readiness, not perfection
- Consider business context and timeline pressures
- Provide specific, actionable recommendations
- Include effort estimates for remediation

Execute this analysis methodically. Start with the codebase overview, then dive into each framework area. No hand-waving—I need concrete findings I can act on.
