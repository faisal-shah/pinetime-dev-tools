# Progress

> **RULE: After each completed task or gate, update this file before moving
> on. Durable state lives here, not in chat history.**

## Resume Here

- Next task: P4-T1
- Next action: copy `hardware-plan.example.json`, enter the exact ADB serials,
  watch address, and BlueZ adapter, record Sys Info percent/millivolts, then run
  `uv sync --group hardware` and `uv run ptlab hardware accept --plan <plan>
  --battery-percent <n> --battery-mv <n>`.
- Last checkpoint: 2026-08-06 02:18 UTC

## Phase 1 - Lab foundation

- [x] P1-T1 package ptlab with uv (2026-08-04)
- [x] P1-T2 isolate process, port, flash, and artifact lifecycle (2026-08-04)
- [x] P1-T3 centralize GATT/control framing (2026-08-04)
- [x] GATE-P1 - unit tests pass (2026-08-04)

## Phase 2 - Scenario coverage

- [x] P2-T1 add bond/radio/auth/persistence/family suites (2026-08-04)
- [x] P2-T2 port protocol, DFU, and raw power-loss regressions (2026-08-04)
- [x] P2-T3 add JUnit, transcripts, and timeout-safe cleanup (2026-08-04)
- [x] GATE-P2 - all eight headless suites pass, 192 checks (2026-08-04)

## Phase 3 - CI and hardware adapters

- [x] P3-T1 add immutable cross-repository CI (2026-08-04)
- [x] P3-T2 add Bleak and ADB adapters (2026-08-04)
- [x] P3-T3 add acceptance, long-idle, and soak workflows (2026-08-04)
- [x] P3-T4 document release and fidelity gates (2026-08-04)
- [x] GATE-P3 - 46 tests, package build, protocol check, and all eight
  deterministic suites pass (2026-08-05)

## Phase 4 - Physical ship gate

- [x] P4-T0 enforce 40% reported floor and record millivolts (2026-08-05)
- [ ] P4-T1 run A-B-A family handoff
- [ ] P4-T2 run five-plus-six LRU acceptance
- [ ] P4-T3 run fresh-auth and CCCD probe
- [ ] P4-T4 run long-idle advertising and upstream/candidate soak
- [ ] GATE-P4 - archive JSON evidence for the release SHAs

## Blocked

- Physical tasks require the deployed watches, independent phones, ADB, and a
  Linux BlueZ adapter.
