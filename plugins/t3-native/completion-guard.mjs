#!/usr/bin/env node
/** Backwards-compatible Node CLI; pure core is shared with the active wrapper. */
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { classifyCompletion } from './completion-core.mjs';
export { classifyCompletion, payload } from './completion-core.mjs';

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { process.stdout.write(JSON.stringify(classifyCompletion(JSON.parse(readFileSync(0, 'utf8'))), null, 2) + '\n'); }
  catch { process.stderr.write('Invalid JSON input. No completion decision made.\n'); process.exitCode = 2; }
}
