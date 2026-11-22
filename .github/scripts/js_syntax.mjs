// Kontrola syntaxe JavaScriptu bez spúšťania kódu (CI, Node 22).
// Kontroluje stats_ui.js, sw.js a inline <script> bloky ui.html a monitor/index.html.
// Skripty sa len kompilujú cez vm.Script (nevykonajú sa), takže nie sú potrebné DOM, self ani window.
// Externé skripty (src=) sa preskakujú. Exit code 1, ak je aspoň jedna chyba.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const STANDALONE_FILES = ['stats_ui.js', 'sw.js'];
const HTML_FILES = ['ui.html', 'monitor/index.html'];
// <script> s ďalším typom (napr. application/json) nie je JavaScript, preskočí sa.
const CLASSIC_TYPES = new Set(['', 'text/javascript', 'application/javascript']);
const SCRIPT_RE = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;

const errors = [];
const notes = [];
let checked = 0;

// Skompiluje kód ako klasický skript. lineOffset zarovná čísla riadkov na pozíciu v HTML súbore.
function checkCode(where, filename, code, lineOffset) {
  checked++;
  try {
    new vm.Script(code, { filename, lineOffset });
  } catch (err) {
    if (err?.name !== 'SyntaxError') throw err;
    const m = /^(.*?):(\d+)\r?\n/.exec(String(err.stack ?? ''));
    const line = m ? m[2] : String(lineOffset + 1);
    errors.push(`${where} (riadok ${line}): ${err.message}`);
  }
}

function readRepoFile(rel) {
  const full = path.join(ROOT, rel);
  if (!fs.existsSync(full)) {
    errors.push(`${rel}: súbor neexistuje`);
    return null;
  }
  return fs.readFileSync(full, 'utf8');
}

for (const rel of STANDALONE_FILES) {
  const code = readRepoFile(rel);
  if (code !== null) checkCode(rel, rel, code, 0);
}

for (const rel of HTML_FILES) {
  const html = readRepoFile(rel);
  if (html === null) continue;
  let index = 0;
  for (const m of html.matchAll(SCRIPT_RE)) {
    index++;
    const attrs = m[1];
    const where = `${rel}: script #${index}`;
    if (/(?:^|\s)src\s*=/i.test(attrs)) continue;
    const typeM = /(?:^|\s)type\s*=\s*["']?([^"'\s>]+)/i.exec(attrs);
    const type = typeM ? typeM[1].toLowerCase() : '';
    if (type === 'module') {
      notes.push(`${where}: type="module" nie je podporovaný, preskočené`);
      continue;
    }
    if (!CLASSIC_TYPES.has(type)) continue;
    // Kód začína hneď za otváracou značkou <script ...>; počet nového riadku pred ním je offset.
    const codeStart = m.index + 8 + attrs.length;
    const lineOffset = html.slice(0, codeStart).split('\n').length - 1;
    checkCode(where, `${rel}#script${index}`, m[2], lineOffset);
  }
}

for (const n of notes) console.log(`POZNÁMKA: ${n}`);
if (errors.length > 0) {
  for (const e of errors) console.error(`CHYBA: ${e}`);
  console.error(`Syntaktické chyby: ${errors.length} (z ${checked} skriptov)`);
  process.exitCode = 1;   // nie process.exit(), aby sa výstup stihol vypísať
} else {
  console.log(`OK: syntax je v poriadku (${checked} skriptov).`);
}
