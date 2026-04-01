# Interactive Question Flow for Project Init

Ask these questions **one at a time**. Wait for each answer before proceeding. Keep it fast — this is a bootstrap, not a deep dive.

## Questions

### 1. Project Objective
> In 1-2 sentences, what is this project about? What problem does it solve and for whom?

Use the answer to set the tone for CLAUDE.md and pick sensible defaults for later questions.

### 2. Tech Stack
> What's the primary tech stack?
> - **Language**: Python, TypeScript, Go, Rust, etc.
> - **Framework**: FastAPI, Express, Next.js, none, etc.
> - **Runtime**: Node, Bun, Deno, CPython, etc.
>
> (List all that apply, or describe your ideal stack and I'll suggest one.)

### 3. Architecture Style
> What kind of project is this?
> - **Web app** (frontend + backend)
> - **API service** (backend only)
> - **CLI tool**
> - **Library / SDK**
> - **Monorepo** (multiple packages)
> - **Data pipeline**
> - **Other**: describe it

### 4. Repo Visibility
> Should the GitHub repository be **public** or **private**?

### 5. Anything Else?
> Any additional context I should capture now? Constraints, team conventions, target platform, etc.
>
> (Skip if nothing comes to mind — `/dev:brainstorm` will dig deeper.)

## Summary & Confirmation

After gathering all answers, present a structured summary:

> Here's what I'll set up:
>
> | Setting | Value |
> |---------|-------|
> | **Project** | `<name>` |
> | **Objective** | `<1-line summary>` |
> | **Stack** | `<language + framework + runtime>` |
> | **Architecture** | `<style>` |
> | **Visibility** | `<public/private>` |
>
> I'll create:
> - Git repo + GitHub remote
> - `.gitignore` (tailored to stack)
> - Standard GitHub issue labels
> - `CLAUDE.md` (lightweight, to be enriched later)
> - `PROJECT-SEED.md` (handoff file for `/dev:brainstorm`)
> - Initial commit + push
>
> **Ready to proceed?** (Or adjust anything?)
