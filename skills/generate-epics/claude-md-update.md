# CLAUDE.md Story Management Protocol

Add this section to the project's CLAUDE.md file after story generation:

```markdown
## Story Management Protocol

### Single Source of Truth
The `stories/` directory and its epic files are the **single source of truth** for all story definitions, progress tracking, and acceptance criteria.

### Story File Hierarchy
```
STORIES.md (overview and navigation)
└── docs/stories/
    ├── epic-01-[name].md
    ├── epic-02-[name].md
    └── non-functional-requirements.md
```

### Progress Update Protocol
1. Update story completion checkboxes in epic files
2. Update sprint breakdown tables in each epic
3. Mark completed acceptance criteria
4. Update dependency tracking
5. Track completed story points in epic progress sections

### Development Workflow
- **Sprint Planning**: Use epic files for story selection
- **Code Reviews**: Link PRs to story IDs (e.g., "Implements Story 01.2-001")
- **Deployment**: Update story status in epic files post-deployment
- **Updates**: Maintain within 24 hours of story completion
```
