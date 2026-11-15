# CodeSight GitHub MCP Plugin (`codesight-github`)

Workspace-scoped Antigravity plugin providing the official **GitHub MCP Server** (`ghcr.io/github/github-mcp-server:latest`) for **CodeSight-AI**.

## Features & Security
- **Zero Plaintext Secrets:** Reads the active GitHub token dynamically at process startup via `gh auth token` (OS keyring) and passes it in-memory to the ephemeral container.
- **Scoped Toolsets:** Enables `repos`, `pull_requests`, `issues`, `actions`, and `code_security`.
- **Prerequisites:** `gh auth status` logged in + Docker Desktop running.
