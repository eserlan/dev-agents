# Answer writer replay

This replays frozen article briefs through the actual writer (the `answer`
issue queue's provider, model and reasoning effort) and, with `--score`,
grades each draft with the existing copy reviewer. It is the writer-side
counterpart to the reviewer replay in `../answer-quality/`: until now only
the reviewer had a benchmark, so a model or effort change for the writer
could not be justified or regression-tested.

```bash
.venv/bin/python evals/replay_answer_writer.py \
  --config ~/.config/dev-agents/projects.yaml --project codex-cryptica \
  --output ~/.local/state/dev-agents/codex-cryptica/evals/writer-high --score
```

Compare settings by repeating the run with an override, for example
`--reasoning-effort medium --output .../writer-medium`, and reading the two
`summary.json` files side by side: page created, reviewer verdict and score
per case. Model output is nondeterministic; treat a single run as a sample,
not a ranking.

Safety: drafts are written in a detached temporary worktree of the target
repository. The replay never pushes, never opens a PR, and instructs the
writer to skip the image/R2 workflow and use a placeholder `seo.image`.
Drafts, logs and assessments are kept in the output directory; the worktree
is removed afterwards.

Use `--dry-run` first to confirm the resolved provider, model and effort.
The settings come from the project's `answer` queue unless overridden, so a
dry run proves the pinned effort (for example `muse-wonder-1.0` at `high`)
before a live replay spends it.
