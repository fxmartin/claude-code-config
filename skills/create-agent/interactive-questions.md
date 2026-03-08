# Interactive Question Flow for Agent Creation

When no arguments are provided, guide the user through these questions **one at a time**. Wait for each answer before asking the next.

## Questions

### 1. Purpose
> What should this agent specialize in? Describe its domain and primary responsibilities.

### 2. Domain Expertise
> What level of expertise should it embody? For example:
> - "Senior backend engineer with 10+ years in distributed systems"
> - "Security researcher specializing in web application vulnerabilities"
> - "DevOps architect experienced in Kubernetes and CI/CD"

### 3. Key Tasks
> What are the 3-5 core tasks this agent should perform when invoked?

### 4. Tools Needed
Based on the described tasks, suggest a tool set and ask for confirmation:
> Based on your description, this agent needs: `<suggested-tools>`
>
> Common tool sets:
> - **Read-only**: `Read, Grep, Glob` (analysis, review)
> - **Code modification**: `Read, Edit, Write, Grep, Glob, Bash`
> - **Research**: `WebFetch, WebSearch, Read, Write`
> - **Full access**: `All tools`
>
> Use the suggested set or adjust?

### 5. Auto-Delegation Trigger
> When should Claude automatically delegate to this agent? Describe the user scenarios that should trigger it. For example:
> - "When the user asks to optimize database queries"
> - "When reviewing pull requests for security issues"

### 6. Color
> Choose a terminal color for this agent: Red, Blue, Green, Yellow, Purple, Orange, Pink, Cyan
>
> Already in use by existing agents: [list colors from existing agents]

### 7. Confirmation
Summarize the gathered requirements and ask:
> Here's what I'll generate:
> - **Name**: `<derived-kebab-case-name>`
> - **Role**: `<expertise-description>`
> - **Tools**: `<tool-list>`
> - **Color**: `<color>`
> - **Delegation trigger**: `<trigger-description>`
>
> Ready to generate? (Or adjust anything?)
