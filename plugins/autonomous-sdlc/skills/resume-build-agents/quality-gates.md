# Agent-Specific Quality Gates

## Backend TypeScript Architect
- TypeScript compilation: `tsc --noEmit`
- API documentation updated
- OpenAPI/Swagger specs current
- Performance benchmarks if applicable

## Python Backend Engineer
- Type checking: `mypy .`
- Linting: `ruff check .`
- Security scan: `bandit` or similar
- Database migration validity

## UI Engineer
- Bundle size analysis
- Accessibility audit (a11y)
- Cross-browser compatibility
- Mobile responsiveness validation

## Bash/Zsh/macOS Engineer
- Shell script linting: `shellcheck`
- Script execution permissions and security
- Cross-platform compatibility validation
- Error handling and logging validation

## QA Engineer
- Test coverage analysis: minimum 90%
- All tests passing
- Quality metrics: defect density, test effectiveness
- Test automation: regression suite automated

## Podman Container Architect
- Container security scanning
- Image optimization: multi-stage builds, minimal base images
- Orchestration validation
- Resource optimization: CPU/memory limits configured
- Health checks implemented

## Senior Code Reviewer (Final Gate)
- Architecture consistency
- Security review completion
- Performance impact assessment
- Technical debt evaluation
- **GATE**: Must approve before merge

## Agent Capabilities Reference

### bash-zsh-macos-engineer
- Script development, CI/CD integration
- System administration, workflow automation
- macOS integration (Homebrew, keychain)
- Performance optimization, security

### podman-container-architect
- Multi-stage builds, image optimization
- Kubernetes manifests, service mesh
- Multi-environment strategy, secrets management
- Rootless containers, OCI compliance

### qa-engineer
- Test strategy design, framework development
- Manual and automated testing
- API testing, performance testing
- Quality metrics, defect tracking
