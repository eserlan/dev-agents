# dev-agents

Reusable, repository-aware LangGraph workflows for software-engineering automation.

This project owns workflow orchestration and reusable context loading. Target repositories own
their architecture knowledge, coding rules, and product terminology; dev-agents reads that
information from their current local checkout.

## Setup

Install [uv](https://docs.astral.sh/uv/) and create a local project configuration:

```bash
cp config/projects.example.yaml config/projects.yaml
# Edit config/projects.yaml for your machine.
uv sync --group dev
```

Inspect a configured repository without modifying it:

```bash
uv run dev-agents inspect codex-cryptica
uv run dev-agents inspect codex-cryptica --base main --head HEAD
```

The command writes a JSON inspection summary to stdout. Its Git commands are read-only.

## Run the PR fixer as a daemon

See [docs/pr-fixer-daemon.md](docs/pr-fixer-daemon.md) for the systemd and Cloudflare Tunnel
setup. Secrets and tunnel credentials remain outside the repository.

## Visualize workflow runs

Generate a local HTML view of the LangGraph flows and recent job timelines:

```bash
uv run dev-agents visualize codex-cryptica --config config/projects.yaml \
  --output /tmp/dev-agents-flow.html
```

See [docs/observability.md](docs/observability.md) for details.

## Development

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```
