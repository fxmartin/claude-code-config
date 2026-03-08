# Interactive Question Flow for Command Creation

When no arguments are provided, guide the user through these questions **one at a time**. Wait for each answer before asking the next.

## Questions

### 1. Purpose
> What should this command do? Describe its purpose in a sentence or two.

### 2. Category
Based on the purpose, suggest a category from the existing ones and ask for confirmation:
> Based on your description, this fits best in `<suggested-category>`. The existing categories are:
> - `dev` — Core development lifecycle
> - `quality` — Testing, reviews, code quality
> - `issues` — Issue creation and management
> - `project` — Progress tracking, stats, documentation
> - `devops` — Release and deployment management
> - `research` — Domain-specific analysis
>
> Use `<suggested>` or choose a different one? (You can also create a new category.)

### 3. Interaction Style
> How should this command work?
> - **Direct**: takes input, produces output (most common)
> - **Interactive**: asks questions one at a time to build context
> - **Hybrid**: starts with questions, then produces a deliverable

### 4. Persona
> What persona should the command adopt? For example:
> - "Senior DevOps engineer with 10+ years of CI/CD experience"
> - "Meticulous technical writer who values clarity"
> - "No specific persona — just task-focused"

### 5. Output
> What should the command produce?
> - A file (specify format/name)
> - Terminal output (report, analysis, suggestions)
> - Code modifications
> - Something else?

### 6. Confirmation
Summarize the gathered requirements and ask:
> Here's what I'll generate:
> - **Name**: `<derived-name>.md`
> - **Category**: `<category>`
> - **Style**: `<style>`
> - **Persona**: `<persona>`
> - **Output**: `<output-description>`
>
> Ready to generate? (Or adjust anything?)

## Complexity Check

After question 1, if the described command seems to need:
- Multiple supporting files
- Tool restrictions
- Auto-invocation by the model
- Complex conditional flows

Then suggest:
> This sounds like it might benefit from being a **skill** instead of a command. Skills support supporting files, tool restrictions, and richer capabilities. Want to use `/create-skill` instead, or continue with a command?
