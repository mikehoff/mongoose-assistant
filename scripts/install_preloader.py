#!/usr/bin/env python3
"""Immediate browser preloader for the macOS installer.

Why this exists: the real /install page is served by the Mongoose FastAPI app, but a fresh install
can spend minutes in Homebrew/Python/pip/cert setup before that app exists. Opening the real
https://127.0.0.1:<port>/install during that window gives the human a blank browser spinner, which
looks like the product is broken. This tiny stdlib-only HTTP server runs on a temporary *different*
loopback port, shows visible progress immediately, tails the installer log, and hands off to the real
/install page only after the real server proves that route is renderable.

The important UX contract: this page must never become a second blank spinner. It has a no-JavaScript
refresh fallback, it reads the wrapper log that always receives terminal output, and it notices when the
installer process has stopped before the real app becomes ready.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STARTED_AT = time.time()
READY_SINCE: float | None = None


def _read_tail_one(path: Path, max_lines: int = 10) -> list[str]:
    try:
        if not path.exists():
            return []
        # Installer logs can grow quickly during pip/brew; read a bounded chunk from the end.
        with path.open("rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 20000), os.SEEK_SET)
            except OSError:
                pass
            raw = f.read().decode("utf-8", "replace")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        return lines[-max_lines:]
    except Exception as exc:  # pragma: no cover - defensive UI only
        return [f"could not read installer log {path}: {exc}"]


def _read_tail(paths: list[Path], max_lines: int = 12) -> list[str]:
    """Return useful terminal output, preferring the first log but falling back to extras.

    The wrapper log is the best source because Install Mongoose.command tees *all* output there,
    including failures before bootstrap_macos.sh starts. Extra logs are still accepted for older callers
    or direct tests. Duplicate lines are suppressed so a bootstrap line that appears in both logs is not
    shown twice.
    """
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        for line in _read_tail_one(p, max_lines=max_lines):
            if line in seen:
                continue
            seen.add(line)
            out.append(line)
    return out[-max_lines:]


def _latest_mtime(paths: list[Path]) -> float | None:
    mtimes: list[float] = []
    for p in paths:
        try:
            if p.exists() and p.stat().st_size > 0:
                mtimes.append(p.stat().st_mtime)
        except OSError:
            pass
    return max(mtimes) if mtimes else None


def _pid_alive(pid: int | None) -> bool | None:
    if not pid or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _probe_url(base: str, path: str = "/install", timeout: float = 0.7) -> tuple[bool, str]:
    url = base.rstrip("/") + path
    ctx = ssl._create_unverified_context() if url.startswith("https://") else None
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 - loopback only
            data = r.read(50000).decode("utf-8", "replace")
            if r.status < 400 and ("Mongoose" in data and ("/install/progress" in data or "Setting things up" in data)):
                return True, url
            return False, url
    except Exception:
        return False, url


def _candidate_bases(main_port: int) -> list[str]:
    # 127.0.0.1 is preferred because it avoids macOS/browser IPv6 localhost surprises; localhost is kept
    # as a same-machine alternate origin because it can escape a stale service worker registered on 127.
    return [
        f"https://127.0.0.1:{main_port}",
        f"https://localhost:{main_port}",
        f"http://127.0.0.1:{main_port}",
        f"http://localhost:{main_port}",
    ]


def _append_from_marker(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "from=install-preloader"


def _status(log_paths: list[Path], main_port: int, watch_pid: int | None = None) -> dict[str, Any]:
    global READY_SINCE
    ready_url = ""
    fallbacks: list[str] = []
    for base in _candidate_bases(main_port):
        ok, url = _probe_url(base)
        if ok:
            if not ready_url:
                ready_url = _append_from_marker(url)
            else:
                fallbacks.append(_append_from_marker(url))
    ready = bool(ready_url)
    if ready and READY_SINCE is None:
        READY_SINCE = time.time()

    elapsed = int(time.time() - STARTED_AT)
    tail = _read_tail(log_paths)
    latest = _latest_mtime(log_paths)
    last_output_seconds_ago = int(time.time() - latest) if latest else None
    parent_alive = _pid_alive(watch_pid)

    state = "running"
    attention = ""
    current = tail[-1] if tail else "Starting the installer..."
    if ready:
        state = "ready"
        current = "The real Mongoose install screen is ready."
    elif parent_alive is False:
        state = "stopped"
        current = "The installer command stopped before Mongoose became ready."
        attention = "Look at the Terminal window and data/logs/install-command-wrapper.log for the actual error."
    elif not tail and elapsed >= 8:
        state = "no_output"
        current = "No installer output has appeared yet."
        attention = "The Terminal may be waiting for a prompt, or the installer may have failed before bootstrap logging started."
    elif tail and last_output_seconds_ago is not None and last_output_seconds_ago >= 60:
        state = "quiet"
        current = tail[-1]
        attention = f"No new terminal output for {last_output_seconds_ago}s. A download may be quiet, but check Terminal for a prompt."

    return {
        "ready": ready,
        "url": ready_url,
        "fallback_urls": fallbacks[:3],
        "elapsed_seconds": elapsed,
        "current": current,
        "tail": tail,
        "state": state,
        "attention": attention,
        "main_install_url": f"https://127.0.0.1:{main_port}/install",
        "log_paths": [str(p) for p in log_paths],
        "watch_pid_alive": parent_alive,
        "last_output_seconds_ago": last_output_seconds_ago,
    }


def _format_elapsed(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    return f"{sec // 60}m {sec % 60}s"


def _page(title: str, main_port: int, status: dict[str, Any]) -> str:
    safe_title = html.escape(title)
    current = html.escape(str(status.get("current") or "Working..."))
    elapsed = html.escape(_format_elapsed(int(status.get("elapsed_seconds") or 0)))
    tail = status.get("tail") or []
    log_text = "\n".join(html.escape(str(x)) for x in tail) or "Waiting for installer output..."
    ready = bool(status.get("ready") and status.get("url"))
    state = html.escape(str(status.get("state") or "running"))
    attention = html.escape(str(status.get("attention") or ""))
    attention_style = "block" if attention else "none"
    open_url = html.escape(str(status.get("url") or status.get("main_install_url") or f"https://127.0.0.1:{main_port}/install"), quote=True)
    fallback = ""
    fallbacks = status.get("fallback_urls") or []
    if fallbacks:
        fallback = html.escape(str(fallbacks[0]), quote=True)
    logs = "<br>".join(html.escape(str(p)) for p in (status.get("log_paths") or []))
    refresh_meta = f'<meta id="refreshFallback" http-equiv="refresh" content="{1 if ready else 5}{(";url=" + open_url) if ready else ""}">'  # no-JS / broken-JS fallback
    tpl = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
%%REFRESH_META%%
<title>Mongoose is installing</title>
<style>
:root { color-scheme: dark; --bg:#15120f; --panel:#211b16; --line:#3b3027; --gold:#d6b56d; --muted:#b7aaa0; --ok:#8ee0a1; --warn:#ffd37a; --bad:#ff9b80; }
* { box-sizing:border-box }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px; background:radial-gradient(circle at 30% 15%, #2b2219, var(--bg) 55%); color:#f6eee3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
.card { width:min(760px,100%); background:rgba(33,27,22,.92); border:1px solid var(--line); border-radius:22px; padding:28px; box-shadow:0 24px 80px rgba(0,0,0,.35); }
.brand { display:flex; align-items:center; gap:12px; margin-bottom:10px; }
.badge { width:14px; height:14px; border-radius:50%; background:var(--gold); box-shadow:0 0 0 0 rgba(214,181,109,.7); animation:pulse 1.5s infinite; }
.badge.ok { background:var(--ok); animation:none; box-shadow:none; }
.badge.bad { background:var(--bad); animation:none; box-shadow:none; }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(214,181,109,.7)} 70%{box-shadow:0 0 0 14px rgba(214,181,109,0)} 100%{box-shadow:0 0 0 0 rgba(214,181,109,0)} }
h1 { margin:0; font-size:26px; letter-spacing:.2px; }
p { color:var(--muted); line-height:1.5; margin:10px 0 0; }
.state { margin-top:20px; padding:15px 16px; background:#18130f; border:1px solid var(--line); border-radius:14px; }
.current { color:#fff; font-weight:650; margin-bottom:6px; }
.small { color:var(--muted); font-size:13px; }
.log { margin-top:16px; padding:12px 14px; background:#100d0a; border-radius:12px; border:1px solid #2b231d; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; line-height:1.45; color:#d8cfc5; max-height:210px; overflow:auto; white-space:pre-wrap; }
.actions { display:{'flex' if ready else 'none'}; margin-top:18px; gap:10px; flex-wrap:wrap; }
a.btn { display:inline-block; text-decoration:none; border-radius:12px; padding:12px 16px; font-weight:750; background:var(--gold); color:#18130f; }
a.alt { background:transparent; color:var(--gold); border:1px solid rgba(214,181,109,.45); }
.notice { display:%%ATTENTION_STYLE%%; margin-top:16px; padding:12px 14px; border:1px solid rgba(255,211,122,.45); border-radius:12px; background:rgba(255,211,122,.08); color:#ffe7af; font-size:13px; line-height:1.45; }
.oktxt { color:var(--ok); } .badtext { color:var(--bad); } code { color:#f1d899; }
</style></head>
<body><div class="card" data-state="%%STATE%%">
  <div class="brand"><span class="badge {'ok' if ready else ''}" id="badge"></span><h1>%%SAFE_TITLE%%</h1></div>
  <p id="intro">Mongoose is preparing the real app server. This page tails the Terminal output and will move on only when the real <code>/install</code> screen is actually reachable.</p>
  <div class="state">
    <div class="current" id="current">%%CURRENT%%</div>
    <div class="small" id="elapsed">Elapsed: %%ELAPSED%%</div>
    <div class="log" id="log">%%LOG_TEXT%%</div>
  </div>
  <div class="notice" id="notice">%%ATTENTION%%</div>
  <div class="actions" id="actions">
    <a class="btn" id="open" href="%%OPEN_URL%%">Open the live install screen</a>
    <a class="btn alt" id="fallback" href="%%FALLBACK%%" style="display:{'inline-block' if fallback else 'none'}">Try the alternate local URL</a>
    <a class="btn alt" href="javascript:location.reload()">Refresh this page</a>
  </div>
  <p class="small" style="margin-top:16px">Target screen: <code>https://127.0.0.1:%%MAIN_PORT%%/install</code>. If Safari shows a certificate page, choose Show Details, then visit the website.</p>
  <p class="small" style="margin-top:8px">Log being watched:<br><code>%%LOGS%%</code></p>
</div>
<script>
let redirected=false;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function fmt(sec){sec=Number(sec||0); if(sec<60) return sec+'s'; let m=Math.floor(sec/60), s=sec%60; return m+'m '+s+'s';}
function showAttention(text){const n=document.getElementById('notice'); if(!n) return; n.textContent=text||''; n.style.display=text?'block':'none';}
async function poll(){
  try{
    const r=await fetch('/status?ts='+Date.now(),{cache:'no-store'});
    const s=await r.json();
    const mf=document.getElementById('refreshFallback'); if(mf&&mf.parentNode) mf.parentNode.removeChild(mf);
    document.getElementById('current').textContent=s.current||'Working...';
    document.getElementById('elapsed').textContent='Elapsed: '+fmt(s.elapsed_seconds||0);
    document.getElementById('log').innerHTML=(s.tail&&s.tail.length?s.tail:['Waiting for installer output...']).map(esc).join('\n');
    showAttention(s.attention||'');
    if((s.state==='stopped'||s.state==='no_output'||s.state==='quiet') && !s.ready){
      document.getElementById('badge').className='badge bad';
    }
    if(s.ready&&s.url){
      document.getElementById('badge').className='badge ok';
      document.getElementById('intro').innerHTML='<span class="oktxt">The real Mongoose install screen is ready.</span> Opening it now.';
      const actions=document.getElementById('actions'); actions.style.display='flex';
      const open=document.getElementById('open'); open.href=s.url;
      if(s.fallback_urls&&s.fallback_urls.length){ const fb=document.getElementById('fallback'); fb.href=s.fallback_urls[0]; fb.style.display='inline-block'; }
      if(!redirected){ redirected=true; setTimeout(function(){ location.href=s.url; }, 900); }
      setTimeout(poll,2500); return;
    }
  }catch(e){
    document.getElementById('current').textContent='This temporary page lost contact with its status server.';
    showAttention('Check the Terminal window. If it has stopped or shows an error, close this tab and re-run Install Mongoose.command.');
  }
  setTimeout(poll,1000);
}
poll();
</script>
</body></html>"""
    # RAW string + token replacement, never an f-string: the f-string processed Python
    # escapes across the embedded JS and one swallowed backslash became a real newline
    # inside a JS string literal, a SyntaxError that killed the whole script and froze
    # the page at Elapsed: 0s. Raw means no escape or brace processing can ever touch
    # the JS; the served page is node-parsed by a permanent test.
    out = tpl
    for k, v in {"ATTENTION_STYLE": attention_style, "ATTENTION": attention,
                 "CURRENT": current, "ELAPSED": elapsed, "FALLBACK": fallback,
                 "LOG_TEXT": log_text, "LOGS": logs, "MAIN_PORT": str(main_port),
                 "OPEN_URL": open_url, "REFRESH_META": refresh_meta,
                 "SAFE_TITLE": safe_title, "STATE": state}.items():
        out = out.replace("%%" + k + "%%", str(v))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--main-port", type=int, required=True)
    parser.add_argument("--log", action="append", required=True)
    parser.add_argument("--extra-log", action="append", default=[])
    parser.add_argument("--watch-pid", type=int, default=0)
    parser.add_argument("--title", default="Mongoose is getting ready")
    parser.add_argument("--exit-after-ready", type=int, default=180)
    parser.add_argument("--max-seconds", type=int, default=1800)
    args = parser.parse_args()
    log_paths = [Path(p) for p in [*args.log, *args.extra_log]]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path in ("/", "/install", "/installing"):
                payload = _status(log_paths, args.main_port, args.watch_pid)
                self._send(_page(args.title, args.main_port, payload).encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/status":
                payload = _status(log_paths, args.main_port, args.watch_pid)
                self._send(json.dumps(payload).encode("utf-8"), "application/json")
                return
            self._send(b"not found", "text/plain", 404)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    def reaper() -> None:
        while True:
            time.sleep(2)
            # Once ready, self-exit after a grace period. Before readiness, stay alive while the watched
            # installer process is alive; if it died, keep the page around long enough for the user to read
            # the error instead of turning it into a browser connection failure.
            if READY_SINCE is not None and time.time() - READY_SINCE > args.exit_after_ready:
                server.shutdown()
                return
            alive = _pid_alive(args.watch_pid)
            if alive is False and time.time() - STARTED_AT > min(args.max_seconds, 900):
                server.shutdown()
                return
            if args.max_seconds > 0 and time.time() - STARTED_AT > args.max_seconds and alive is not True:
                server.shutdown()
                return

    threading.Thread(target=reaper, daemon=True).start()
    print(f"http://127.0.0.1:{args.port}/installing", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
