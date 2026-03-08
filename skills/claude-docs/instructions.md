# Claude Code Documentation Index

## Base URL

All doc pages are available at: `https://code.claude.com/docs/en/<slug>.md`

For HTML rendering: `https://code.claude.com/docs/en/<slug>`

## Complete Page Index

### Getting Started
| Slug | Title |
|------|-------|
| overview | Claude Code overview |
| quickstart | Quickstart |
| setup | Advanced setup |
| how-claude-code-works | How Claude Code works |
| authentication | Authentication |

### Environments
| Slug | Title |
|------|-------|
| vs-code | Use Claude Code in VS Code |
| jetbrains | JetBrains IDEs |
| desktop | Use Claude Code Desktop |
| desktop-quickstart | Get started with the desktop app |
| claude-code-on-the-web | Claude Code on the web |
| chrome | Use Claude Code with Chrome (beta) |
| slack | Claude Code in Slack |
| terminal-config | Optimize your terminal setup |
| interactive-mode | Interactive mode (keyboard shortcuts, input modes) |

### Configuration & Customization
| Slug | Title |
|------|-------|
| settings | Claude Code settings |
| memory | How Claude remembers your project (CLAUDE.md, auto memory) |
| skills | Extend Claude with skills (custom commands) |
| hooks | Hooks reference (events, schema, JSON) |
| hooks-guide | Automate workflows with hooks |
| permissions | Configure permissions |
| keybindings | Customize keyboard shortcuts |
| model-config | Model configuration (aliases like opusplan) |
| output-styles | Output styles |
| statusline | Customize your status line |
| fast-mode | Speed up responses with fast mode |
| features-overview | Extend Claude Code (when to use what) |

### Tools & Integrations
| Slug | Title |
|------|-------|
| mcp | Connect Claude Code to tools via MCP |
| sub-agents | Create custom subagents |
| agent-teams | Orchestrate teams of Claude Code sessions |
| headless | Run Claude Code programmatically (Agent SDK) |
| plugins | Create plugins |
| plugins-reference | Plugins reference (schemas, CLI, specs) |
| plugin-marketplaces | Create and distribute plugin marketplaces |
| discover-plugins | Discover and install prebuilt plugins |

### CI/CD & Automation
| Slug | Title |
|------|-------|
| github-actions | Claude Code GitHub Actions |
| gitlab-ci-cd | Claude Code GitLab CI/CD |
| scheduled-tasks | Run prompts on a schedule (/loop, cron) |
| cli-reference | CLI reference (commands and flags) |

### Enterprise & Security
| Slug | Title |
|------|-------|
| security | Security safeguards and best practices |
| sandboxing | Sandboxed bash tool (filesystem/network isolation) |
| data-usage | Data usage policies |
| zero-data-retention | Zero Data Retention (ZDR) |
| legal-and-compliance | Legal and compliance |
| network-config | Enterprise network configuration (proxy, CA, mTLS) |
| llm-gateway | LLM gateway configuration |
| server-managed-settings | Server-managed settings |
| devcontainer | Development containers |
| third-party-integrations | Enterprise deployment overview |
| monitoring-usage | Monitoring (OpenTelemetry) |
| analytics | Track team usage with analytics |

### Cloud Providers
| Slug | Title |
|------|-------|
| amazon-bedrock | Claude Code on Amazon Bedrock |
| google-vertex-ai | Claude Code on Google Vertex AI |
| microsoft-foundry | Claude Code on Microsoft Foundry |

### Other
| Slug | Title |
|------|-------|
| best-practices | Best practices |
| common-workflows | Common workflows |
| costs | Manage costs effectively |
| checkpointing | Checkpointing (track/rewind edits) |
| remote-control | Remote Control (continue from any device) |
| troubleshooting | Troubleshooting |
| changelog | Changelog |

## Search Strategy

1. **Keyword matching**: Match the user's query to page titles and slugs above
2. **Category matching**: If the query is about a broad topic, identify the right category
3. **Common query mappings**:
   - "hooks" → `hooks.md` (reference) + `hooks-guide.md` (guide)
   - "MCP" / "model context protocol" → `mcp.md`
   - "settings" / "config" → `settings.md`
   - "CLAUDE.md" / "memory" / "instructions" → `memory.md`
   - "commands" / "skills" / "slash commands" → `skills.md`
   - "permissions" / "allow" / "deny" → `permissions.md`
   - "subagents" / "sub-agents" / "parallel agents" → `sub-agents.md`
   - "agent teams" / "multi-agent" → `agent-teams.md`
   - "CLI" / "flags" / "options" → `cli-reference.md`
   - "keyboard" / "shortcuts" / "keybindings" → `keybindings.md` + `interactive-mode.md`
   - "VS Code" / "vscode" → `vs-code.md`
   - "JetBrains" / "IntelliJ" / "PyCharm" → `jetbrains.md`
   - "desktop app" → `desktop.md` + `desktop-quickstart.md`
   - "web" / "browser" / "cloud" → `claude-code-on-the-web.md`
   - "GitHub Actions" / "CI" → `github-actions.md`
   - "GitLab" → `gitlab-ci-cd.md`
   - "Bedrock" / "AWS" → `amazon-bedrock.md`
   - "Vertex" / "GCP" → `google-vertex-ai.md`
   - "security" → `security.md`
   - "cost" / "tokens" / "usage" → `costs.md`
   - "plugins" → `plugins.md` + `plugins-reference.md`
   - "scheduled" / "cron" / "loop" → `scheduled-tasks.md`
   - "sandbox" → `sandboxing.md`
   - "model" / "opus" / "sonnet" → `model-config.md`
   - "status line" / "statusline" → `statusline.md`
   - "fast mode" → `fast-mode.md`
   - "troubleshoot" / "error" / "issue" → `troubleshooting.md`
   - "install" / "setup" → `setup.md`
4. **Fallback**: Use `WebSearch` with `site:code.claude.com <query>`
