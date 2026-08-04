#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec uv run ptlab run --target sim --suite raw-flash-power-loss "$@"
