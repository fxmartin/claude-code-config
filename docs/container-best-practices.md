# Container Best Practices

Both **Podman** and **Docker** are valid — use whichever is installed. Prefer `Containerfile` (works with both); fall back to `Dockerfile` if tooling requires it.

## Mandatory Tools

| Tool | Purpose | Detect |
|------|---------|--------|
| `podman` or `docker` | Container runtime | `command -v podman \|\| command -v docker` |
| `podman-compose` or `docker compose` | Multi-service orchestration | `command -v podman-compose \|\| docker compose version` |
| `hadolint` | Containerfile linter | `brew install hadolint` / `uv tool install hadolint` |

## Quality Gates

| Gate | Command | Blocks on |
|------|---------|-----------|
| Lint | `hadolint Containerfile` | Any warning |
| Build | `<runtime> build --no-cache -t <name> .` | Build failure |
| Non-root | Verify `USER` directive in final stage | Missing or root |

## Canonical Multi-Stage Containerfile

```dockerfile
# --- Builder ---
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-cache --no-dev

# --- Runtime ---
FROM debian:bookworm-slim
RUN useradd --create-home --shell /bin/bash app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --chown=app:app src/ ./src/
USER app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "myapp"]
```

## Multi-Stage Rules

| Rule | Why |
|------|-----|
| `--frozen` | Exact versions from lockfile — reproducible builds |
| `--no-cache` | Prevent uv cache from bloating the image |
| `--no-dev` | Exclude dev dependencies in production |
| Copy lockfile before src | Better Docker layer caching |
| Specific image tags | Never use `:latest` — pin to exact version or digest |

## Security Hardening

- **Non-root user**: `useradd` + `USER` directive in final stage
- **`--chown`**: Set ownership on `COPY` — no extra `RUN chown`
- **No secrets in ENV/build-args**: Use runtime secrets or mounted files
- **Pin digests for production**: `FROM debian:bookworm-slim@sha256:...`
- **Minimal base images**: `bookworm-slim` or `alpine` — no full distros

## .containerignore

Create `.containerignore` (symlink as `.dockerignore` if using Docker):

```
.git
.venv
__pycache__
*.pyc
.env
.mypy_cache
.ruff_cache
.pytest_cache
node_modules
dist
build
```

## Compose Patterns

```yaml
services:
  api:
    build: .
    ports: ["8000:8000"]
    depends_on:
      db: { condition: service_healthy }
    networks: [backend]
    deploy:
      resources:
        limits: { cpus: "1.0", memory: 512M }

  db:
    image: postgres:16-alpine
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 3
    networks: [backend]

networks:
  backend:

volumes:
  pgdata:
```

- Custom networks for service isolation
- Named volumes for persistence
- Healthchecks on all stateful services
- Resource limits on all services
- Commit `.env.example` with safe defaults — never commit `.env`

## Podman-Specific Notes

Only relevant when using Podman:

- **Rootless by default** — ports < 1024 need `sysctl` or root
- **No daemon** — each command is self-contained
- **Pod support** — group tightly coupled containers: `podman pod create`
- **Socket path** — `/run/user/$UID/podman/podman.sock` (not `/var/run/docker.sock`)
- **UID mapping** — volume permissions may differ from Docker; use `--userns=keep-id` if needed
