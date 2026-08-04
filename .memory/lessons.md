# Lessons Learned

## Gotchas

- Fixed ports and shared run directories make simulator tests race.
- Python `socket.timeout` aliases `TimeoutError`; scenario deadline exceptions
  must not derive from it.
- Two concurrent BlueZ discoveries on one adapter conflict.
- Long soaks must checkpoint before the final battery read.
- Simulator authentication is injected policy state, not SMP.

## Patterns

- Use `uv run` and keep hardware dependencies in the optional hardware group.
- Allocate one simulator per scenario and terminate its process group.
- Write result JSON through a temporary sibling and `os.replace`.
- Upload artifacts under `if: always()` in CI.
- Record exact SHAs before running cross-repository tests.

## Decisions

| Decision | Rationale | Date |
|---|---|---|
| Eight headless suites are the automated integration gate | Cover policy and protocol without false RF claims | 2026-08-04 |
| Physical failures return nonzero and retain evidence | Prevent false acceptance and data loss | 2026-08-04 |

## Checkpoint Log

| Date | Tasks Since Last Checkpoint | Notes |
|---|---:|---|
| 2026-08-04 | 10 | Lab, scenarios, CI, hardware tooling, docs, and commit complete |
