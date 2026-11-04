// Fails if the build output could leak the Gemini key: the variable's name, anything
// shaped like a Google API key, or (when GEMINI_API_KEY is set where this runs) the
// key itself. Prints file names only, never what matched. Run after `next build`.

import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const ROOTS = [".next/static", ".next/server"];
const PATTERNS = [/GEMINI_API_KEY/, /AIza[0-9A-Za-z_-]{35}/];
const key = process.env.GEMINI_API_KEY?.trim();

async function* files(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* files(path);
    else yield path;
  }
}

let scanned = 0;
const leaks = [];
for (const root of ROOTS) {
  for await (const path of files(root)) {
    const text = await readFile(path, "latin1");
    scanned += 1;
    if (PATTERNS.some((pattern) => pattern.test(text)) || (key && text.includes(key))) {
      leaks.push(path);
    }
  }
}

if (scanned === 0) {
  console.error("No build output found. Run `npm run build` first.");
  process.exit(1);
}
if (leaks.length > 0) {
  console.error(`Possible Gemini key material in ${leaks.length} build file(s):`);
  for (const path of leaks) console.error(`  ${path}`);
  process.exit(1);
}
console.log(
  `OK: ${scanned} build files contain no Gemini key material` +
    (key ? " (including the configured key)." : "."),
);
