"""Execute every cockpit loadX() render function in a real JS runtime to catch RUNTIME crashes that
node --check (syntax-only) misses.

The nonce() bug was valid syntax that threw at render: `const n=nonce();` called a function that never
existed, so the Build tab crashed before its form appeared, while every Python test passed. This harness
loads the actual cockpit <script> into Node with the browser environment (document/window/fetch/...)
stubbed, then CALLS each loadX() and flags any that throw SYNCHRONOUSLY (before their first await) , the
exact failure mode that kills a tab on render. Async/network errors after the first await are tolerated
(they degrade gracefully in the real UI); a synchronous throw is a hard render crash.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

_NODE = shutil.which("node")


def _cockpit_script() -> str:
    import mongoose.cockpit as ck
    html = next(v for v in vars(ck).values()
                if isinstance(v, str) and v.lstrip().startswith("<!DOCTYPE") and "loadBuild" in v)
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
    return "\n".join(b for b in blocks if b.strip())


def _load_fn_names(script: str) -> list[str]:
    return sorted(set(re.findall(r"\basync\s+function\s+(load[A-Z][A-Za-z]*)\s*\(\)", script)))


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_every_loadX_renders_without_crash(tmp_path):
    script = _cockpit_script()
    fns = _load_fn_names(script)
    assert fns, "expected to find loadX render functions"

    # A permissive DOM/browser stub. Every accessor returns a chainable no-op element so the render
    # code can set innerHTML, addEventListener, style, etc. without throwing. fetch never resolves
    # (we only care about SYNCHRONOUS throws before the first await), but is awaitable.
    harness = r"""
const __errors = [];
const __noop = () => {};
function __el() {
  return new Proxy(function(){}, {
    get(t, p) {
      if (p === 'style') return {};
      if (p === 'classList') return { add: __noop, remove: __noop, toggle: __noop, contains: () => false };
      if (p === 'value') return '';
      if (p === 'checked') return false;
      if (p === 'textContent' || p === 'innerHTML' || p === 'innerText') return '';
      if (p === 'children' || p === 'childNodes') return [];
      if (p === 'dataset') return {};
      if (p === 'appendChild' || p === 'removeChild' || p === 'addEventListener'
          || p === 'removeEventListener' || p === 'setAttribute' || p === 'click'
          || p === 'focus' || p === 'remove' || p === 'querySelector') return () => __el();
      if (p === 'querySelectorAll' || p === 'getElementsByClassName') return () => [];
      if (p === Symbol.toPrimitive) return () => '';
      return __el();
    },
    set() { return true; },
    apply() { return __el(); }
  });
}
const document = {
  documentElement: { dataset: {}, style: { setProperty: __noop }, classList: { add: __noop, remove: __noop, toggle: __noop } },
  getElementById: () => __el(),
  querySelector: () => __el(),
  querySelectorAll: () => [],
  createElement: () => __el(),
  body: __el(),
  addEventListener: __noop,
  cookie: '',
};
const window = { location: { href: '', origin: 'http://127.0.0.1:8765', search: '', reload: __noop },
                 addEventListener: __noop, setTimeout: () => 0, clearTimeout: __noop,
                 matchMedia: () => ({ matches: false, addEventListener: __noop }) };
const location = window.location;
const history = { replaceState: __noop, pushState: __noop };
const URLSearchParams = globalThis.URLSearchParams;
const localStorage = { getItem: () => null, setItem: __noop, removeItem: __noop };
const navigator = { clipboard: { writeText: () => Promise.resolve() }, userAgent: 'node' };
function fetch() { return new Promise(() => {}); }   // never resolves: we only test sync throws
function setTimeout(){ return 0; } function clearTimeout(){} function setInterval(){ return 0; }
function clearInterval(){} function requestAnimationFrame(){ return 0; }
function alert(){} function confirm(){ return true; } function prompt(){ return ''; }
const console = { log: __noop, warn: __noop, error: __noop, info: __noop };
const URL = globalThis.URL; const Blob = function(){}; const FormData = function(){ return { append: __noop }; };
const EventSource = function(){ return { addEventListener: __noop, close: __noop }; };
const AbortController = function(){ return { abort: __noop, signal: {} }; };

// ---- the real cockpit script ----
__SCRIPT__

// ---- call each loadX(); an async function turns a render-time throw into a REJECTION, so we must
// inspect the rejection. We distinguish a real render crash (ReferenceError/TypeError , undefined
// var/function, the nonce() class) from a tolerable network/await error (our fetch never resolves, so
// genuine data-loading code simply hangs and is fine). Give each call a microtask tick to reject. ----
const __fns = __FNS__;
(async () => {
  for (const name of __fns) {
    try {
      const f = eval(name);
      const r = f();
      if (r && typeof r.then === 'function') {
        // Race the promise against a tick: a synchronous render throw rejects immediately; real data
        // loading hangs on our never-resolving fetch and wins the tick (treated as OK).
        const verdict = await Promise.race([
          r.then(() => null, (e) => e),
          new Promise(res => setImmediate(() => res('__pending__'))),
        ]);
        if (verdict && verdict !== '__pending__') {
          const msg = verdict && verdict.message ? verdict.message : String(verdict);
          // Only flag the render-crash classes; ignore anything that's clearly a network/data issue.
          if (/is not defined|is not a function|Cannot read prop|Cannot read properties|undefined is not/i.test(msg)) {
            __errors.push(name + ': ' + msg);
          }
        }
      }
    } catch (e) {
      __errors.push(name + ': ' + (e && e.message ? e.message : String(e)));
    }
  }
  process.stdout.write(JSON.stringify(__errors));
})();
"""
    src = (harness
           .replace("__SCRIPT__", script)
           .replace("__FNS__", json.dumps(fns)))
    js = tmp_path / "harness.js"
    js.write_text(src)
    r = subprocess.run([_NODE, str(js)], capture_output=True, text=True, timeout=60)
    # A non-zero exit with no JSON means the SCRIPT ITSELF failed to load (a top-level crash) , also a bug.
    out = (r.stdout or "").strip()
    if not out:
        pytest.fail(f"cockpit script failed to even load in node:\n{r.stderr[-1500:]}")
    errors = json.loads(out)
    assert not errors, (
        "cockpit tab render functions throw synchronously (would crash the tab before it renders):\n  "
        + "\n  ".join(errors))
