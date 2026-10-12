# Reviewer replay results, 12 October 2026

Model: `gpt-6.1-sol`, low reasoning. These were read-only, isolated calls using the final rubric.
The model could inspect only the supplied immutable source and original/correction briefs, so
later GitHub critiques could not supply expected findings. Each case was sampled once with the
final rubric; this is regression evidence, not a guarantee of deterministic future judgments.

| Case | Editorial result | Replay assertion |
| --- | --- | --- |
| CC PR #4008, merge `3f29081970494091098a4982c3e377a1fbe419dc` | Material repetition and misleading comparison; needs human review | Passed: both checks blocking, actionable grouped findings |
| CC #4011 / PR #4014, merged correction | Clean | Passed: repetition, examples and flow pass |
| CC PR #3915, corrected dice Answer | Clean | Passed: prior correction remains usable; no invented blocker |
| Concise notation reference | Clean; worked example not applicable | Passed |
| Unrelated ferry-town worldbuilding fixture | Clean | Passed |

The initial weaker rubric incorrectly accepted #4008. Requiring each repeated passage to justify
its added reader use, and requiring comparisons to isolate the actual causal variable, fixed
that negative replay while preserving the positive cases. No keyword ban or article-specific
phrase rule was added to the reviewer.

Replay artifacts were saved under the configured user's dev-agents state directory in
`codex-cryptica/evals/issue-21-comparisons` and `issue-21-clean-final`. Each includes frozen inputs,
logs, assessments and a summary. Use the main manifest to repeat all five cases or `--only` to
repeat an individual benchmark. The unit/integration suites for copy review, issue fixing and
PR fixing passed 99 tests; lint and reviewer type checking passed as well.
