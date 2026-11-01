# 0002 — Main-branch workflow instead of per-milestone PRs

- Status: Accepted by the project owner, 1 November 2026
- Supersedes: the branch and pull-request parts of SPEC.md §0 rule 13 and §17

## Context

SPEC.md v1.2 required one feature branch and one PR per milestone. This is
a solo project, so the owner considers that overhead and wants the effort
to go into the application itself.

## Decision

- Work directly on `main`.
- Complete one milestone at a time; run its tests and fix failures.
- Commit with meaningful messages; no artificial or empty commits.
- Push `main` after each completed milestone.
- Report what was built, tests run and results, problems, and the commit SHA.
- STOP until the owner replies `APPROVE MILESTONE N`.

## Consequences

- CI runs on every push to `main` (`.github/workflows/ci.yml` already triggers on it).
- M0 was delivered on `chore/m00-project-setup` (PR #1, opened by the
  owner), then fast-forwarded into `main`. The remote branch is kept.
- `.github/PULL_REQUEST_TEMPLATE.md` is no longer part of the workflow.
  It stays in the repo as optional and unused.
