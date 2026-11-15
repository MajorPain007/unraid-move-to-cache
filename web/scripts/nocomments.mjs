import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import ts from 'typescript';

const root = new URL('..', import.meta.url).pathname;
const found = [];

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(ts|svelte|css|mjs)$/.test(name)) check(p);
  }
}

function scanScript(file, text, offsetLine) {
  const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, ts.LanguageVariant.Standard, text);
  for (let k = scanner.scan(); k !== ts.SyntaxKind.EndOfFileToken; k = scanner.scan()) {
    if (k === ts.SyntaxKind.SingleLineCommentTrivia || k === ts.SyntaxKind.MultiLineCommentTrivia) {
      const line = text.slice(0, scanner.getTokenStart()).split('\n').length + offsetLine;
      found.push(`${file}:${line}: ${scanner.getTokenText().slice(0, 60)}`);
    }
  }
}

function scanCss(file, text, offsetLine) {
  let quote = '';
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote) {
      if (c === '\\') i++;
      else if (c === quote) quote = '';
    } else if (c === '"' || c === "'") quote = c;
    else if (text.startsWith('/*', i)) {
      const line = text.slice(0, i).split('\n').length + offsetLine;
      found.push(`${file}:${line}: ${text.slice(i, i + 60)}`);
      i = text.indexOf('*/', i + 2);
      if (i < 0) break;
    }
  }
}

function check(p) {
  const file = relative(root, p);
  const text = readFileSync(p, 'utf8');
  if (/\.(ts|mjs)$/.test(p)) return scanScript(file, text, 0);
  if (p.endsWith('.css')) return scanCss(file, text, 0);
  const blocks = [];
  const re = /<(script|style)\b[^>]*>([\s\S]*?)<\/\1>/g;
  let m;
  let markup = text;
  while ((m = re.exec(text))) {
    const start = m.index + m[0].indexOf('>') + 1;
    const line = text.slice(0, start).split('\n').length - 1;
    blocks.push([m[1], m[2], line]);
    markup = markup.replace(m[0], m[0].replace(/[^\n]/g, ' '));
  }
  for (const [kind, body, line] of blocks) {
    if (kind === 'script') scanScript(file, body, line);
    else scanCss(file, body, line);
  }
  let i = markup.indexOf('<!--');
  while (i >= 0) {
    found.push(`${file}:${markup.slice(0, i).split('\n').length}: HTML comment`);
    i = markup.indexOf('<!--', i + 4);
  }
}

walk(join(root, 'src'));
walk(join(root, 'scripts'));
if (found.length) {
  console.log('Comments found:\n  ' + found.join('\n  '));
  process.exit(1);
}
console.log('  PASS no comments in the web interface');
