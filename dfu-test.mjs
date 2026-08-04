#!/usr/bin/env node

import { spawnSync } from 'node:child_process';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const result = spawnSync(
  'uv',
  ['run', 'ptlab', 'run', '--target', 'sim', '--suite', 'dfu', ...process.argv.slice(2)],
  { cwd: root, stdio: 'inherit' },
);

if (result.error) {
  console.error(`error: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
