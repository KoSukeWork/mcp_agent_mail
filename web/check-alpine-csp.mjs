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
const alpineAttributeStart = /\s((?:x-[\w:.-]+)|(?:@[\w:.-]+)|(?::[\w:.-]+))\s*=\s*(["'])/g;
const ignoredAttribute = /^(?:x-transition|x-ref|x-cloak)/;
const unsupportedSyntax = [
  [/=>/, 'arrow function'],
  [/^\s*`/, 'template literal'],
  [/\?\./, 'optional chaining'],
  [/^\s*(?:const|let|var|if|switch|try|class)\b/, 'statement syntax'],
];
const failures = [];
let checkedExpressions = 0;

function decodeEntities(expression) {
  return expression
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&amp;', '&');
}

function extractAlpineAttributes(source) {
  const attributes = [];
  alpineAttributeStart.lastIndex = 0;
  let match;
  while ((match = alpineAttributeStart.exec(source)) !== null) {
    const attribute = match[1];
    const quote = match[2];
    let cursor = alpineAttributeStart.lastIndex;
    let expression = '';
    while (cursor < source.length) {
      if (source.startsWith('{{', cursor)) {
        const end = source.indexOf('}}', cursor + 2);
        if (end === -1) break;
        expression += '"template"';
        cursor = end + 2;
        continue;
      }
      if (source.startsWith('{%', cursor)) {
        const end = source.indexOf('%}', cursor + 2);
        if (end === -1) break;
        cursor = end + 2;
        continue;
      }
      if (source[cursor] === quote) {
        attributes.push([attribute, expression]);
        cursor += 1;
        break;
      }
      expression += source[cursor];
      cursor += 1;
    }
    alpineAttributeStart.lastIndex = cursor;
  }
  return attributes;
}

const extractionProbe = extractAlpineAttributes(
  `<div :aria-label='{{ label | tojson }}.replace("x", () => value)'></div>`,
);
if (
  extractionProbe.length !== 1
  || extractionProbe[0][1] !== '"template".replace("x", () => value)'
) {
  failures.push('Alpine attribute scanner failed its single-quote/Jinja extraction probe.');
}

for (const sourcePath of alpineSources) {
  const filename = path.relative(process.cwd(), sourcePath);
  const source = fs.readFileSync(sourcePath, 'utf8');
  for (const [attribute, rawExpression] of extractAlpineAttributes(source)) {
    let expression = decodeEntities(rawExpression);
    if (ignoredAttribute.test(attribute)) continue;

    for (const [pattern, description] of unsupportedSyntax) {
      if (pattern.test(expression)) failures.push(`${filename}: ${attribute} uses ${description}`);
    }

    if (attribute === 'x-for') {
      const separator = expression.includes(' in ') ? ' in ' : ' of ';
      expression = expression.split(separator).slice(1).join(separator);
    }
    if (!expression) continue;

    try {
      generateRuntimeFunction(expression);
      checkedExpressions += 1;
    } catch (error) {
      failures.push(`${filename}: ${attribute} ${JSON.stringify(expression)}: ${error.message}`);
    }
  }
}

if (failures.length > 0) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else {
  console.log(`${checkedExpressions} Alpine expressions are compatible with the CSP build.`);
}
