import fs from 'node:fs';
import path from 'node:path';

import { generateRuntimeFunction } from '../node_modules/@alpinejs/csp/src/parser.js';

const templatesRoot = path.resolve('src/mcp_agent_mail/templates');
const alpineSources = [
  ...fs.readdirSync(templatesRoot)
    .filter((name) => name.endsWith('.html'))
    .map((name) => path.join(templatesRoot, name)),
  path.resolve('src/mcp_agent_mail/viewer_assets/index.html'),
];
const alpineAttribute = /\s((?:x-[\w:.-]+)|(?:@[\w:.-]+)|(?::[\w:.-]+))\s*=\s*"([\s\S]*?)"/g;
const ignoredAttribute = /^(?:x-transition|x-ref|x-cloak)/;
const unsupportedSyntax = [
  [/=>/, 'arrow function'],
  [/^\s*`/, 'template literal'],
  [/\?\./, 'optional chaining'],
  [/^\s*(?:const|let|var|if|switch|try|class)\b/, 'statement syntax'],
];
const failures = [];

function decodeEntities(expression) {
  return expression
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&amp;', '&');
}

for (const sourcePath of alpineSources) {
  const filename = path.relative(process.cwd(), sourcePath);
  const source = fs.readFileSync(sourcePath, 'utf8');
  for (const match of source.matchAll(alpineAttribute)) {
    const [, attribute] = match;
    let expression = decodeEntities(match[2]);
    if (ignoredAttribute.test(attribute)) continue;

    for (const [pattern, description] of unsupportedSyntax) {
      if (pattern.test(expression)) failures.push(`${filename}: ${attribute} uses ${description}`);
    }

    if (attribute === 'x-for') {
      const separator = expression.includes(' in ') ? ' in ' : ' of ';
      expression = expression.split(separator).slice(1).join(separator);
    }
    if (!expression || expression.includes('{{') || expression.includes('{%')) continue;

    try {
      generateRuntimeFunction(expression);
    } catch (error) {
      failures.push(`${filename}: ${attribute} ${JSON.stringify(expression)}: ${error.message}`);
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else {
  console.log('All statically resolvable Alpine expressions are compatible with the CSP build.');
}
