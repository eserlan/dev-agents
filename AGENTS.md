# Development guidance

- Think before coding. Surface meaningful assumptions in changes and documentation.
- Prefer the smallest clear design that meets the active workflow requirement.
- Make surgical changes; do not mix unrelated cleanup with feature work.
- Keep workflows goal-driven and their state explicit and inspectable.
- Keep orchestration here and repository-specific knowledge in the target checkout.
- Tests must construct their own temporary repositories and must not depend on a developer's
  local product checkout.
