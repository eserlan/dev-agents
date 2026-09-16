# Workflow run explorer

Render the current LangGraph flows and recent persisted jobs for a configured project:

```bash
uv run dev-agents visualize codex-cryptica \
  --config config/projects.yaml \
  --output /tmp/dev-agents-flow.html
```

Open the generated HTML file in a browser. It contains an interactive canvas explorer:

- a dynamic daemon-run index across all configured SQLite state, including webhook deliveries,
  reconciliation passes, feedback runs, internal PR reviews, and auto-merge requests;
- search and workflow/status filters, with run duration and source database shown;
- clickable index rows that open graph-backed runs in the canvas or show details for daemon-only
  runs;
- choose a workflow and one of its recent runs;
- see the actual LangGraph topology vertically, with execution moving top-to-bottom and branches
  arranged side-by-side;
- inspect the `pr-review` flow, including the initial general/Codex-Cryptica passes and any bounded
  targeted post-fix verification used when a green PR has no submitted Copilot review;
- see release-comms fan out from `publish_destinations` into platform branches; these branches are
  colored from persisted publication receipts and show their public destination when clicked;
- click a node to inspect every event recorded for that node, including timestamps and metadata;
- click an event in the timeline to focus its node;
- drag to pan, scroll to zoom, and reset the view when needed;
- inspect failure details without embedding large agent output.

PR review run metadata includes the bounded structured review result when Luna emits a valid
`REPORT_JSON`: verdict, concrete findings, categories checked, validation, and fixes. Raw agent
output remains in the configured log files.

The graph and run data are embedded in the HTML, so the report is usable as a local file and does
not depend on a Mermaid CDN or a running application server. The report is a snapshot: rerun the
command to include newer SQLite events. Completed workflow jobs automatically regenerate the
configured report asynchronously; reconciliation dispatch bookkeeping intentionally does not
trigger a refresh. For this project the report is `/tmp/dev-agents-flow-canvas.html`.

Each reconciliation pass is now persisted as a `pr-reconcile` run with dispatch counts and its
own event timeline, so the index exposes actual daemon work rather than only a `reconcile-start`
log line.

Use `--workflow release-comms` to focus on one graph and `--limit 20` to reduce the graph run
section. The daemon index always reads the complete persisted history. The command only reads
existing SQLite databases. Missing state databases produce an empty run section; running a
workflow creates its normal configured state database.

Set `visualization_path` on a project to choose another automatic destination. The refresh is
best-effort and isolated from workflow success; the state transaction completes first, then a
detached report worker reads the latest SQLite state and atomically replaces the HTML file.

To upload each generated report to Vercel, opt a project in with `report_vercel_project` (and
optionally `report_vercel_scope` and `report_vercel_alias`). Install the Vercel CLI and either
log in as the daemon user or provide a token under `report_vercel_token_env` (default:
`VERCEL_TOKEN`). A token is preferred for unattended services, while the local CLI login works
for a daemon running as the same user. Unchanged report content reuses its previous deployment;
changed reports are throttled to once every hour by default and capped at 24 deployments per
rolling 24 hours. The local HTML snapshot still refreshes immediately; only public Vercel uploads
are batched. Configure `report_vercel_min_interval_seconds` and
`report_vercel_max_deployments_24h` if needed. For example:

```yaml
report_vercel_project: codex-cryptica-flow
report_vercel_scope: your-team-slug
report_vercel_alias: flow.example.com
```

The worker deploys a directory containing only the report as `index.html` after the local file
is written. The upload runs in the detached report worker and is best-effort; a missing CLI,
login, token, or failed upload never changes the workflow result. If using a token, put it in
the service's private environment file rather than in YAML.

PR review lifecycle comments link directly to the report with URL parameters such as
`?workflow=pr-review&run=<run-id>`. The report applies those parameters on load, filtering the
run index and selecting the matching workflow run when it is present in the snapshot.
