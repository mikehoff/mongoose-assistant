#!/usr/bin/env node
/*
 * check_js.js , a zero-network JavaScript validity checker for Mongoose's embedded page scripts.
 *
 * Why this exists: the install/welcome/cockpit pages are large JS programs embedded in Python strings.
 * Nothing validated them, so a script that called esc() six times but never defined it shipped through
 * a fully-green Python suite and only showed up as a dead browser page. Parsing alone would NOT have
 * caught it (calling an undefined function is a runtime ReferenceError, not a syntax error), so this
 * does two things:
 *   1. PARSE each script with acorn (vendored, MIT) , catches syntax errors.
 *   2. SCOPE-WALK , collect every declared name (function declarations, var/let/const, params, catch
 *      bindings, function/class names, imports) across the whole script and the global allowlist, then
 *      flag any identifier that is READ or CALLED but never declared anywhere in scope. That is exactly
 *      the esc() class: "referenced but undefined".
 *
 * Usage: node check_js.js < script.js
 *   prints JSON: {"ok": true} or {"ok": false, "errors": [{type, name, line, col, message}, ...]}
 * Exit code is 0 on ok, 1 on any error, 2 on a checker/usage failure.
 *
 * Conservative by design: when in doubt it does NOT flag (a false "undefined" would block a good build).
 * The allowlist of browser/JS globals is broad. The goal is to catch genuine "function never defined"
 * mistakes, not to be a type checker.
 */
'use strict';

const path = require('path');
const acorn = require(path.join(__dirname, 'acorn.js'));

// ---- the global allowlist: names that legitimately exist at runtime without being declared in-script.
// Broad on purpose. Anything here is treated as "defined". JS language keywords/literals are handled by
// the parser and never reach identifier resolution (true/false/null/this/etc.), so they are not needed.
const GLOBALS = new Set([
  // core JS built-ins / constructors
  'Object', 'Array', 'String', 'Number', 'Boolean', 'Math', 'JSON', 'Date', 'RegExp', 'Error',
  'TypeError', 'RangeError', 'SyntaxError', 'Promise', 'Map', 'Set', 'WeakMap', 'WeakSet', 'Symbol',
  'Proxy', 'Reflect', 'BigInt', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'encodeURIComponent',
  'decodeURIComponent', 'encodeURI', 'decodeURI', 'Infinity', 'NaN', 'undefined', 'globalThis',
  'Intl', 'ArrayBuffer', 'Uint8Array', 'Int8Array', 'Float32Array', 'DataView', 'structuredClone',
  // timers / microtasks
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame',
  'cancelAnimationFrame', 'queueMicrotask',
  // DOM / BOM
  'window', 'document', 'navigator', 'location', 'history', 'screen', 'console', 'alert', 'confirm',
  'prompt', 'fetch', 'Headers', 'Request', 'Response', 'AbortController', 'AbortSignal', 'FormData',
  'URL', 'URLSearchParams', 'Blob', 'File', 'FileReader', 'WebSocket', 'EventSource', 'Event',
  'CustomEvent', 'MutationObserver', 'IntersectionObserver', 'ResizeObserver', 'Node', 'Element',
  'HTMLElement', 'Image', 'Audio', 'getComputedStyle', 'matchMedia', 'DOMParser', 'XMLSerializer',
  'XMLHttpRequest', 'TextEncoder', 'TextDecoder', 'btoa', 'atob', 'crypto', 'performance', 'CSS',
  'customElements', 'HTMLCanvasElement', 'Option', 'NodeList', 'self',
  // service worker / PWA globals (sw.js runs in a worker scope)
  'caches', 'Cache', 'CacheStorage', 'clients', 'skipWaiting', 'registration', 'importScripts',
  'ServiceWorkerGlobalScope', 'WorkerGlobalScope',
  // media / speech / device APIs
  'MediaRecorder', 'MediaStream', 'MediaSource', 'AudioContext', 'webkitAudioContext',
  'SpeechRecognition', 'webkitSpeechRecognition', 'speechSynthesis', 'SpeechSynthesisUtterance',
  'Notification', 'PushManager', 'Worker', 'SharedWorker', 'BroadcastChannel', 'Clipboard',
  'ClipboardItem', 'getSelection', 'scrollTo', 'scrollBy', 'open', 'close', 'print', 'focus', 'blur',
  // storage
  'localStorage', 'sessionStorage', 'indexedDB',
]);

function read(stream) {
  return new Promise((resolve, reject) => {
    let data = '';
    stream.setEncoding('utf8');
    stream.on('data', (c) => (data += c));
    stream.on('end', () => resolve(data));
    stream.on('error', reject);
  });
}

// Collect names bound by a binding pattern (Identifier, Object/Array patterns, defaults, rest).
function collectPatternNames(node, out) {
  if (!node) return;
  switch (node.type) {
    case 'Identifier':
      out.add(node.name);
      break;
    case 'ObjectPattern':
      for (const p of node.properties) {
        if (p.type === 'RestElement') collectPatternNames(p.argument, out);
        else collectPatternNames(p.value, out);
      }
      break;
    case 'ArrayPattern':
      for (const el of node.elements) if (el) collectPatternNames(el, out);
      break;
    case 'AssignmentPattern':
      collectPatternNames(node.left, out);
      break;
    case 'RestElement':
      collectPatternNames(node.argument, out);
      break;
    default:
      break;
  }
}

// A scope holds the set of names declared in it. Resolution walks from inner to outer.
function makeScope(parent) {
  return { parent, names: new Set() };
}
function declared(scope, name) {
  for (let s = scope; s; s = s.parent) if (s.names.has(name)) return true;
  return false;
}

// Hoist function declarations and var declarations into the given function-or-program scope, and
// also bring in let/const/class/function names (we do not model the temporal dead zone; for "is it
// defined at all" that is the correct, conservative choice).
function hoistInto(scope, body) {
  const stack = Array.isArray(body) ? body.slice() : [body];
  while (stack.length) {
    const n = stack.pop();
    if (!n || typeof n.type !== 'string') continue;
    switch (n.type) {
      case 'FunctionDeclaration':
        if (n.id) scope.names.add(n.id.name);
        break; // do not descend into nested function bodies for hoisting
      case 'VariableDeclaration':
        for (const d of n.declarations) collectPatternNames(d.id, scope.names);
        break;
      case 'ClassDeclaration':
        if (n.id) scope.names.add(n.id.name);
        break;
      case 'ImportDeclaration':
        for (const sp of n.specifiers) if (sp.local) scope.names.add(sp.local.name);
        break;
      // descend into blocks/control flow to hoist vars + nested function declarations
      case 'BlockStatement': case 'Program':
        for (const s of n.body) stack.push(s); break;
      case 'IfStatement':
        stack.push(n.consequent); if (n.alternate) stack.push(n.alternate); break;
      case 'ForStatement':
        if (n.init) stack.push(n.init); stack.push(n.body); break;
      case 'ForInStatement': case 'ForOfStatement':
        stack.push(n.left); stack.push(n.body); break;
      case 'WhileStatement': case 'DoWhileStatement':
        stack.push(n.body); break;
      case 'TryStatement':
        stack.push(n.block);
        if (n.handler) stack.push(n.handler.body);
        if (n.finalizer) stack.push(n.finalizer);
        break;
      case 'SwitchStatement':
        for (const c of n.cases) for (const s of c.consequent) stack.push(s); break;
      case 'LabeledStatement':
        stack.push(n.body); break;
      default:
        break;
    }
  }
}

// Walk the AST resolving identifier *references*. Reports reads of names that are not declared in any
// enclosing scope and not a known global. Handles member access (only the object is a reference, not
// the property), shorthand object props, and labels.
function analyze(ast) {
  const errors = [];

  // Names that appear anywhere as `typeof NAME` are treated as intentionally-optional (the standard
  // feature-detection idiom: `if (typeof loadTasks === "function") loadTasks()`). A call/reference to
  // such a name is guarded by the author and must NOT be flagged, otherwise the checker punishes the
  // correct pattern. This is script-wide and conservative: it only suppresses names the author
  // explicitly probed with typeof, so genuine "never defined, never probed" cases (esc, postAction)
  // are still caught.
  const typeofGuarded = new Set();
  (function collectTypeof(n) {
    if (!n || typeof n.type !== 'string') return;
    if (n.type === 'UnaryExpression' && n.operator === 'typeof' && n.argument && n.argument.type === 'Identifier') {
      typeofGuarded.add(n.argument.name);
    }
    for (const key of Object.keys(n)) {
      if (key === 'loc' || key === 'start' || key === 'end' || key === 'range') continue;
      const child = n[key];
      if (Array.isArray(child)) { for (const c of child) if (c && typeof c.type === 'string') collectTypeof(c); }
      else if (child && typeof child.type === 'string') collectTypeof(child);
    }
  })(ast);

  function visit(node, scope) {
    if (!node || typeof node.type !== 'string') return;

    switch (node.type) {
      case 'Program': {
        const s = makeScope(null);
        for (const g of GLOBALS) s.names.add(g);
        hoistInto(s, node.body);
        for (const st of node.body) visit(st, s);
        return;
      }
      case 'FunctionDeclaration':
      case 'FunctionExpression':
      case 'ArrowFunctionExpression': {
        const s = makeScope(scope);
        if (node.id && node.type !== 'FunctionDeclaration') s.names.add(node.id.name);
        for (const p of node.params) collectPatternNames(p, s.names);
        s.names.add('arguments');
        // default-value expressions in params can reference outer scope; visit them
        for (const p of node.params) {
          if (p.type === 'AssignmentPattern') visit(p.right, s);
        }
        if (node.body.type === 'BlockStatement') {
          hoistInto(s, node.body.body);
          for (const st of node.body.body) visit(st, s);
        } else {
          visit(node.body, s); // concise arrow body
        }
        return;
      }
      case 'BlockStatement': {
        const s = makeScope(scope);
        hoistInto(s, node.body);
        for (const st of node.body) visit(st, s);
        return;
      }
      case 'CatchClause': {
        const s = makeScope(scope);
        if (node.param) collectPatternNames(node.param, s.names);
        hoistInto(s, node.body.body);
        for (const st of node.body.body) visit(st, s);
        return;
      }
      case 'VariableDeclaration':
        for (const d of node.declarations) if (d.init) visit(d.init, scope);
        return;
      case 'UnaryExpression':
        // `typeof x` is legal even when x is undeclared (it yields "undefined", never throws). This is
        // the standard feature-detection idiom, so a bare identifier operand of typeof is NOT a
        // reference that must resolve. Other unary operators still resolve their operand normally.
        if (node.operator === 'typeof' && node.argument && node.argument.type === 'Identifier') return;
        visit(node.argument, scope);
        return;
      case 'MemberExpression':
        visit(node.object, scope);
        if (node.computed) visit(node.property, scope);
        return; // non-computed property is not a reference
      case 'Property':
        // { key: value } , key is not a reference unless computed; shorthand {x} means value===key===Identifier(x)
        if (node.computed) visit(node.key, scope);
        visit(node.value, scope);
        return;
      case 'LabeledStatement':
        visit(node.body, scope); return; // label name is not a variable
      case 'BreakStatement': case 'ContinueStatement':
        return; // label is not a variable
      case 'Identifier': {
        if (declared(scope, node.name)) return;
        if (typeofGuarded.has(node.name)) return; // author feature-detected this name; intentional
        errors.push({
          type: 'undefined-reference',
          name: node.name,
          line: node.loc ? node.loc.start.line : null,
          col: node.loc ? node.loc.start.column : null,
          message: `'${node.name}' is referenced but never declared in scope and is not a known global`,
        });
        return;
      }
      default: {
        // generic descent over child nodes
        for (const key of Object.keys(node)) {
          if (key === 'loc' || key === 'start' || key === 'end' || key === 'range') continue;
          const child = node[key];
          if (Array.isArray(child)) {
            for (const c of child) if (c && typeof c.type === 'string') visit(c, scope);
          } else if (child && typeof child.type === 'string') {
            visit(child, scope);
          }
        }
        return;
      }
    }
  }

  visit(ast, null);
  return errors;
}

(async function main() {
  let src;
  try {
    src = await read(process.stdin);
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, errors: [{ type: 'io', message: String(e) }] }));
    process.exit(2);
  }

  let ast;
  try {
    ast = acorn.parse(src, { ecmaVersion: 2022, locations: true, allowReturnOutsideFunction: false });
  } catch (e) {
    process.stdout.write(JSON.stringify({
      ok: false,
      errors: [{ type: 'syntax', message: e.message, line: e.loc ? e.loc.line : null, col: e.loc ? e.loc.column : null }],
    }));
    process.exit(1);
  }

  let errors;
  try {
    errors = analyze(ast);
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, errors: [{ type: 'checker-bug', message: String(e && e.stack || e) }] }));
    process.exit(2);
  }

  if (errors.length) {
    process.stdout.write(JSON.stringify({ ok: false, errors }));
    process.exit(1);
  }
  process.stdout.write(JSON.stringify({ ok: true }));
  process.exit(0);
})();
