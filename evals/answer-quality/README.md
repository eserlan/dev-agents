# Answer editorial replay

This runs the existing reviewer with publication disabled. It freezes each input and saves the
agent log, structured assessment and summary in the selected output directory. It creates no
issues and changes no article source. Model output is nondeterministic; failures need inspection,
not a blanket assumption that the article or reviewer is wrong.

```bash
.venv/bin/python evals/replay_answer_quality.py \
  --config ~/.config/dev-agents/projects.yaml --project codex-cryptica \
  --output ~/.local/state/dev-agents/codex-cryptica/evals/issue-21
```

The negative benchmark is merged CC PR #4008. The reviewer must identify both grouped repetition
and the contrived example; the example must be material and prevent a clean verdict. Repetition
may be a suggestion when the reviewer finds no material reader harm. Only the original #4007
correction brief is supplied, not #4011's critique or this case's expected result. Replays prohibit
external lookups so later critiques cannot leak into the benchmark. Detection comes from the
article and reviewer rubric, not forbidden-word scans.

PR #4014 (the correction for #4011) is the positive benchmark and is skipped until merged.
PR #3915 replays the corrected dice explanation. Two small standalone fixtures check that a
concise reference and unrelated causal worldbuilding technique can pass without manufactured
findings or mandatory worked scenes. All cases require explicit repetition, examples and flow
evidence. Synthetic fixtures declare their scope and lack of product/presentation requirements.

The expectations in cases.json are evaluation assertions, never included in the agent prompt.
Use --cases to supply another manifest. The run exits unsuccessfully for failed assertions or
agent failures, and distinguishes skipped unmerged benchmarks. Re-run the positive benchmark
after #4014 merges; a skipped case is not evidence that the corrected article passes.

Ownership: dev-agents #21 is a reviewer subtask of #19. CC #3918 owns first-draft writer guidance.
