import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

export const here = dirname(fileURLToPath(import.meta.url));
export const golden = JSON.parse(readFileSync(join(here, 'fixtures', 'golden.json'), 'utf8'));
export const repoRoot = join(here, '..', '..');
export const NOW = 1_800_000_000_000;
