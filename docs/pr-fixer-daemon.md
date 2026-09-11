# PR fixer daemon

The daemon is designed to be a drop-in replacement for the existing `remotecc` PR-fixer
listener. It runs the Python listener under systemd and keeps Cloudflare Tunnel as a separate,
dependent service. Secrets and tunnel credentials stay outside this repository.

```bash
mkdir -p ~/.config/dev-agents ~/.config/systemd/user
cp config/projects.example.yaml ~/.config/dev-agents/projects.yaml
cp ops/systemd/dev-agents-pr-fixer.service.example ~/.config/systemd/user/dev-agents-pr-fixer.service
cp ops/systemd/dev-agents-pr-fixer-tunnel.service.example ~/.config/systemd/user/dev-agents-pr-fixer-tunnel.service
# Reuse the existing remotecc secrets and tunnel config:
#   ~/.config/codex-pr-review/webhook.env
#   ~/.cloudflared/codex-pr-review.yml
systemctl --user daemon-reload
systemctl --user disable --now codex-pr-review-webhook.service codex-pr-review-tunnel.service || true
systemctl --user enable --now dev-agents-pr-fixer.service dev-agents-pr-fixer-tunnel.service
```

The GitHub webhook remains `https://pr-webhook.codexcryptica.com/github`; configure its secret to
match `GITHUB_WEBHOOK_SECRET`. Check both services with `systemctl --user status`, inspect logs with
`journalctl --user -u dev-agents-pr-fixer.service -f`, and verify the local listener with
`curl http://127.0.0.1:8788/health`.

The new services reuse `~/.config/codex-pr-review/webhook.env` and
`~/.cloudflared/codex-pr-review.yml`; do not copy those files, their credential JSON, or tunnel
IDs into Git.

Completed run logs are retained for 30 days by default. Stale temporary worktree directories older
than two days are removed at daemon startup; active/recent worktrees are left alone. Adjust
`log_retention_days` and `worktree_retention_days` in the project configuration if needed.
