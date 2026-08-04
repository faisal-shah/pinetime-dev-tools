# PineTime Lab Plan

## Goal

Provide reproducible cross-repository validation for firmware, simulator,
companion applications, and physical family-watch workflows.

## Architecture

- Discover four sibling repositories by exact directory name.
- Verify generated protocol outputs from InfiniTime's manifest.
- Run isolated simulator scenarios with dynamic ports and private artifacts.
- Share framed GATT and BLE-control clients across suites.
- Record repository SHAs, events, transcripts, results, and JUnit.
- Provide Linux/Bleak and Android/ADB physical acceptance adapters.
- Resolve cross-repository CI refs to immutable SHAs before checkout.

## Completed

1. uv package, workspace/process/artifact foundation.
2. Five deterministic policy suites plus protocol, DFU, and power-loss suites.
3. Cross-repository CI and software power-proxy gate.
4. Hardware handoff, LRU, auth, CCCD, advertising, and soak tooling.
5. Release procedure and fidelity documentation.

## Remaining

Execute the physical ship gate in `RELEASE.md` and preserve its JSON evidence
against the exact four release SHAs.
