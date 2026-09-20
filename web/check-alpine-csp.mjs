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
const registrationSources = [
  ...alpineSources,
  path.resolve('src/mcp_agent_mail/viewer_assets/viewer.js'),
];
const alpineAttributeStart = /(?:\s|%\})((?:x-[\w:.-]+)|(?:@[\w:.-]+)|(?::[\w:.-]+))\s*=\s*(["'])/g;
const alpineDataRegistration = /(?:window\.)?Alpine\.data\(\s*(["'])([$A-Z_a-z][$\w]*)\1/g;
const inlineEventHandler = /\s(on[a-z][\w:.-]*)\s*=\s*(["'])/gi;
const namedDataProvider = /^\s*([$A-Z_a-z][$\w]*)\s*(?:\(|$)/;
const ignoredAttribute = /^(?:x-transition|x-ref|x-cloak)/;
const unsupportedSyntax = [
  [/=>/, 'arrow function'],
  [/^\s*`/, 'template literal'],
  [/\?\./, 'optional chaining'],
  [/^\s*(?:const|let|var|if|switch|try|class)\b/, 'statement syntax'],
  [/\bMath\./, 'global Math reference'],
];
const failures = [];
const namedDataProviders = new Map();
const registeredDataProviders = new Set();
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

// Conditional attributes can immediately follow a Jinja block without whitespace.
// These must be checked too: the former card handler parsed only after a click.
const conditionalProbe = extractAlpineAttributes(
  `<div {% if active %}@click="if (!$event.target.closest('a,button,input,select')) window.location.href = {{ href | tojson | forceescape }}"{% endif %} x-show="visible"></div>`,
);
if (
  conditionalProbe.length !== 2
  || conditionalProbe[0][0] !== '@click'
  || conditionalProbe[1][0] !== 'x-show'
) {
  failures.push('Alpine attribute scanner missed a Jinja-conditional click handler.');
} else {
  try {
    generateRuntimeFunction(conditionalProbe[0][1]);
    failures.push('CSP parser unexpectedly accepted the unsupported conditional card handler.');
  } catch {
    // Expected: this is the exact regression the scanner previously missed.
  }
}

for (const sourcePath of registrationSources) {
  const source = fs.readFileSync(sourcePath, 'utf8');
  alpineDataRegistration.lastIndex = 0;
  let match;
  while ((match = alpineDataRegistration.exec(source)) !== null) {
    registeredDataProviders.add(match[2]);
  }
}

for (const sourcePath of alpineSources) {
  const filename = path.relative(process.cwd(), sourcePath);
  const source = fs.readFileSync(sourcePath, 'utf8');

  inlineEventHandler.lastIndex = 0;
  let inlineHandlerMatch;
  while ((inlineHandlerMatch = inlineEventHandler.exec(source)) !== null) {
    failures.push(`${filename}: inline ${inlineHandlerMatch[1]} handler is blocked by the mail CSP`);
  }

  for (const [attribute, rawExpression] of extractAlpineAttributes(source)) {
    let expression = decodeEntities(rawExpression);
    if (ignoredAttribute.test(attribute)) continue;

    if (attribute === 'x-data') {
      const providerMatch = expression.match(namedDataProvider);
      if (providerMatch) {
        const locations = namedDataProviders.get(providerMatch[1]) ?? new Set();
        locations.add(filename);
        namedDataProviders.set(providerMatch[1], locations);
      }
    }

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

for (const [provider, locations] of namedDataProviders) {
  if (!registeredDataProviders.has(provider)) {
    failures.push(
      `${[...locations].join(', ')}: x-data provider ${provider} is not registered with Alpine.data()`,
    );
  }
}

if (failures.length > 0) {
  console.error(failures.join('\n'));
  process.exitCode = 1;
} else {
  console.log(`${checkedExpressions} Alpine expressions are compatible with the CSP build.`);
}
