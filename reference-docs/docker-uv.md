# Container Tools Reference (Podman + uv)

## Podman (OCI-compliant, rootless)

- Use `podman` instead of `docker`
- Use `Containerfile` instead of `Dockerfile`
- `podman build -t <name> .` to build
- `podman run --rm -it <name>` to run
- `podman-compose` for multi-service setups

## Python + uv in Containers

### Multi-stage Containerfile pattern:

```dockerfile
# Stage 1: Install dependencies
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

# Stage 2: Runtime
FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "myapp"]
```

## Key Patterns

- Always use multi-stage builds to minimize image size
- Copy `uv` binary from official image
- Use `--frozen` flag in CI/containers
- Non-root user in production images
- `.dockerignore` / `.containerignore` for build context
