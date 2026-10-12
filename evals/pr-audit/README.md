# Independent PR auditor replay

```bash
.venv/bin/python evals/replay_pr_auditor.py \
  --config ~/.config/dev-agents/projects.yaml --project codex-cryptica \
  --output ~/.local/state/dev-agents/codex-cryptica/evals/issue-22
```

The runner selects the exact commit used by the first Copilot review of each benchmark PR,
not its subsequently repaired head. It creates disposable Git clones, resolves the historical
merge base and bundles the snapshot's skill files. The auditor receives the source and original
PR description but no reference comments or later source. It uses the configured PR model and
reasoning level with enforced read-only shell permissions and no user-configured MCP tools.
It never invokes a fixer, pushes changes or publishes comments.

The known-defect cases exercise sparse graph extents (#3685), per-frame full-canvas hull copies
(#3687), themed control contrast (#3690), and untrusted Markdown image requests (#3691). Expected
locations and impact patterns are only host-side assertions, not auditor instructions. Inspect
the finding evidence after a replay; matching these patterns alone is not semantic proof.

Artifacts include each diff, bundled prompt, schema, audit, comparison and summary. Subsequent
Copilot data is read only after auditing, filtered to the audited commit and compared by location.
Overlap is an explicitly limited positional proxy, not a confirmed precision/recall measurement.
The model can legitimately return incomplete when it cannot verify an applicable boundary. Such
a replay cannot pass as clean, and an execution/parse failure is retained separately in the summary.
Sampling is nondeterministic; investigate failed cases rather than weakening the expected defect.

Ownership: dev-agents #22. The application continues to use the existing bounded review/fix chain.

See [recorded results and limitations](RESULTS.md) for the implementation replay.
