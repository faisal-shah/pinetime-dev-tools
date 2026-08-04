# Family release gate

Use one immutable SHA for each repository:

| Repository | Required branch |
|---|---|
| `InfiniTime` | `family-features` |
| `InfiniSim` | `family-features` |
| `PineTimeCompanion` | `master` |
| `pinetime-dev-tools` | `main` |

Run `.github/workflows/cross-repository.yml` from `pinetime-dev-tools` with all
four full SHAs. The workflow records the selected revisions, verifies every
generated protocol target, runs firmware and simulator native tests, checks the
companion TypeScript/Kotlin/web targets, and uploads all deterministic ptlab
scenario artifacts and JUnit files.

The same gate can be reproduced from sibling local checkouts:

```sh
uv run ptlab protocol check

cmake -S ../InfiniTime/tests/host -B ../InfiniTime/build-host-tests -G Ninja
cmake --build ../InfiniTime/build-host-tests
ctest --test-dir ../InfiniTime/build-host-tests --output-on-failure

cmake -S ../InfiniSim -B ../InfiniSim/build \
  -G Ninja \
  -DInfiniTime_DIR="$(cd ../InfiniTime && pwd)" \
  -DENABLE_BLE_TEST_CONTROL=ON
cmake --build ../InfiniSim/build
ctest --test-dir ../InfiniSim/build --output-on-failure

(cd ../PineTimeCompanion && \
  npm ci && \
  npm run typecheck && \
  npm test && \
  npm run test:kotlin && \
  npm run web:export)

uv sync --frozen
uv run pytest -q
uv build
uv run ptlab run --target sim --suite all --filter fast
```

## Physical ship gate

Use independent central identities. Turn notification forwarding off unless the
step explicitly validates forwarding ownership.

1. Run `ptlab hardware accept` with at least two phones and one Linux adapter in
   an A-B-A sequence. Each peer must read the battery without routine re-pairing.
2. Fill the watch with five peers, touch the intended survivor, pair a sixth,
   and confirm the actual LRU peer is rejected without repairing while the
   survivor still verifies.
3. Run `ptlab hardware probe` from a fresh Linux identity to prove the
   authenticated verify gate and CCCD restore.
4. Run `ptlab hardware advertise` over the required long-idle interval.
5. Run `ptlab hardware soak` beside an upstream watch under the same placement,
   charging history, display use, and scan schedule.

The hardware JSON artifacts are release evidence. Advertising samples prove
visibility only, and battery percentages are coarse telemetry. Do not claim
absolute current or RF power without a controlled measurement fixture.

Promote a prerelease only after the cross-repository workflow and the applicable
physical gates pass for the exact four SHAs being shipped.
