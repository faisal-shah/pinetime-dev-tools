# Project Context

## Overview

`ptlab` is the integration and acceptance owner for the four sibling PineTime
repositories. Implementation commit `bfeeb47` contains the multi-companion lab,
CI, and hardware tooling.

## Architecture

- CLI: `src/ptlab/cli.py`.
- Isolated scenarios: `scenario.py` and `scenarios.py`.
- Simulator lifecycle: `sim.py` and `processes.py`.
- Protocol transports: `gatt.py`, `control.py`, and `wire.py`.
- Artifacts: `artifacts.py` and `transcript.py`.
- Physical adapters: `hardware.py`.
- Canonical release gate: `RELEASE.md`.

## Tech Stack

Python 3.12+, uv/uv_build, pytest, orjson, optional Bleak 3, ADB, Node
regression scripts, CMake/CTest, and GitHub Actions.

## Invariants

- Repository directories are exactly `InfiniTime`, `InfiniSim`,
  `PineTimeCompanion`, and `pinetime-dev-tools`.
- Every scenario owns private ports, flash, run directory, logs, and cleanup.
- Timeout status must survive socket timeout handlers.
- Scenario skips never count as hardware proof.
- Physical results checkpoint atomically after every step/sample.
- Full-watch pairing requires explicit eviction permission.
- Power and advertising outputs state their fidelity limits.
- The InfiniTime 2.0.2 release imports no previous bond format; the release
  gate must state the required one-time re-pair.
- Physical acceptance uses a 40% reported floor and records millivolts because
  PineTime estimates charge from voltage without a hardware fuel gauge.

## Key Decisions

| Decision | Rationale | Date |
|---|---|---|
| InfiniTime owns protocol generation | Prevent four independent contracts | 2026-08-04 |
| CI resolves mutable refs to SHAs | Make cross-repository results reproducible | 2026-08-04 |
| Hardware Bleak dependency is optional | Keep simulator/CI installs lean | 2026-08-04 |
