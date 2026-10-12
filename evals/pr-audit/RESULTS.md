# Issue #22 historical replay results

Run on 12 October 2026 with the configured `gpt-6-luna` model and `high` reasoning.
All snapshots were audited read-only, with no fixer and no reference findings in the prompt.
Linked guidance, including the patterns catalogue and constitution, was bundled from each snapshot.

| PR | Original audited revision | Observed relevant finding |
| --- | --- | --- |
| #3685 | `7dfa6f23c4ff7765037c0e3c5790b30069d2fdb1` | Node-only extents can clip parallel curved edges. |
| #3687 | `cb65c399de32c738c1d0cd1a390b310f00610bf9` | Up to 24 full-surface clears and copies per frame; missing saved Groups preference assertions. |
| #3690 | `4eb866137badde03724b7556b0b2f01a3f6d8704` | Strong-state toolbar icon contrast below 3:1 in a supplied theme. |
| #3691 | `dce9f27b0a9b177beddfce5f13000fe5124523c8` | Untrusted assistant Markdown can render a remote image and trigger a passive tracking request. |

The initial #3691 replay missed the remote-image defect. The general security/privacy lens was
then strengthened to enumerate renderer output elements and trace passive resource requests
separately from script sanitisation and click-time link validation. A second isolated #3691 replay
identified that defect. This is an observed improvement on a known case, not a held-out quality
estimate. The other three observations precede that additional prompt instruction.

The initial automated #3690 assertion also returned false because it searched only the impact
sentence for “contrast”; the actual finding contained the contrast calculation in its evidence.
The assertion now checks evidence, impact and remediation together. The expected location and
defect pattern remain unchanged. Findings were read to confirm the reported mechanisms.

Local raw artifacts are under
`~/.local/state/dev-agents/codex-cryptica/evals/issue-22/` and
`~/.local/state/dev-agents/codex-cryptica/evals/issue-22-privacy/`.
They retain prompts, original diffs, JSON findings, logs and reference comparisons.

These four defect detections do not mean all subsequent Copilot findings were reproduced.
Location overlap can miss the same defect reported at another line and can match different defects
at the same line. Treat its rate as a diagnostic proxy requiring human adjudication. Replays are
nondeterministic and contain only four known regression cases.

Implementation validation: 81 focused tests passed; lint and type checking of the changed modules
passed. The full suite had 305 passes and the existing unrelated
`test_release_comms.py::test_progress_events_trace_phases` failure: its expected drafted payload
does not include the already-present Instagram and Threads counts.
