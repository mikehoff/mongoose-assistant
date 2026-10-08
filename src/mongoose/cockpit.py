"""Mongoose Cockpit: a small local control panel.

Answers the four questions a local assistant owner actually has: is it working, what
is it doing, what needs me, what did it do. Built as ONE self-contained HTML page
served at /ui that polls existing JSON endpoints (/doctor, /system/status,
/cockpit/*, /find, /bus/list). No build step, no SPA framework: server-rendered shell
plus minimal vanilla JS. Action buttons call REAL endpoints (approve/cancel a
WhatsApp draft, run doctor, install a capability); nothing here invents capability or
autonomy Mongoose doesn't already have.

Tabs: Overview, Setup & Repair, Activity, Approvals, Search, Models, Scheduler.
"""

COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mongoose Cockpit</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#1a1a1a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Mongoose">
<script>
/* Fetch concurrency gate. This page kicks off ~15-20 panel loads at once; the server is single
   threaded and its heavier endpoints do CPU/SQLite work that saturates the GIL, so a simultaneous
   herd starves the event loop and even /health times out (pages then "load only the header"). Cap
   in-flight fetches so the burst becomes a paced queue. Transparent when few requests are active. */
(function(){
  if(window.__mgFetchGate)return; window.__mgFetchGate=true;
  var MAX=4, active=0, q=[], _f=window.fetch.bind(window);
  function pump(){ while(active<MAX && q.length){ var t=q.shift(); active++;
    _f(t.a,t.b).then(function(r){active--;pump();t.res(r);},function(e){active--;pump();t.rej(e);}); } }
  window.fetch=function(a,b){ return new Promise(function(res,rej){ q.push({a:a,b:b,res:res,rej:rej}); pump(); }); };
})();
</script>
<script>if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('/sw.js').then(function(r){try{r.update();}catch(_e){}}).catch(function(){});});}</script>
<style>
/* Local-first fonts: bundled woff2 served by Mongoose, no third-party CDN. */
@font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}
@font-face{font-family:"Inter";font-weight:500;font-display:swap;src:url("/static/fonts/inter-latin-500-normal.woff2") format("woff2")}
@font-face{font-family:"Inter";font-weight:600;font-display:swap;src:url("/static/fonts/inter-latin-600-normal.woff2") format("woff2")}
@font-face{font-family:"Fraunces";font-weight:400;font-display:swap;src:url("/static/fonts/fraunces-latin-400-normal.woff2") format("woff2")}
@font-face{font-family:"Fraunces";font-weight:500;font-display:swap;src:url("/static/fonts/fraunces-latin-500-normal.woff2") format("woff2")}
@font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url("/static/fonts/fraunces-latin-600-normal.woff2") format("woff2")}
@font-face{font-family:"IBM Plex Mono";font-weight:400;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-400-normal.woff2") format("woff2")}
@font-face{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}
</style>
<style>
  /* Mongoose Cockpit: "the watch". A warm-charcoal instrument panel for a local-first
     household assistant, the opposite of cold cloud-SaaS dark. The one piece of motion
     that earns its place is the breathing "alive" pulse: the whole product is about
     whether the thing is alive and watching, so that status literally has a heartbeat.
     Everything else stays quiet. Display: Fraunces (a warm, characterful serif used
     with restraint). Body: Inter. Data/logs: IBM Plex Mono. Accent: a single restrained
     brass, the metal of a well-made instrument. */
  :root {
    --ink:#16140F;        /* warm near-black (brown-shifted, not blue-black) */
    --panel:#1F1C16;      /* raised warm charcoal */
    --panel2:#26221B;     /* inputs / wells */
    --line:#322D24;       /* warm hairline */
    --text:#ECE6DA;       /* warm off-white */
    --dim:#9C9486;        /* muted warm grey */
    --ok:#7CB893;         /* calm sage = alive/ready (not acid green) */
    --warn:#D9A05B;       /* warm amber = needs you (not alarm red) */
    --bad:#D0785E;        /* warm terracotta = problem (not fire-engine red) */
    --gold:#C9A35B;       /* the single brass accent */
    --gold-dim:#7d6736;
  }
  /* Hearth: a warm DAYLIGHT skin. Same brass/sage/terracotta hues, light
     substrate. Pure token swap, every rule below already reads these vars,
     so nothing structural changes. Activated by data-theme="light" on <html>
     (see the initTheme() script near boot()). */
  :root[data-theme="light"] {
    --ink:#F2E9D8; --panel:#FBF6EC; --panel2:#F0E7D4; --line:#E2D5BD;
    --text:#2C261C; --dim:#897F6B;
    --ok:#4E8C63; --warn:#A9762C; --bad:#B0553B;
    --gold:#9C7A2C; --gold-dim:#C9A35B;
  }
  /* Appearance toggle (sun/moon): lives in the header, see <header> below. */
  .theme-toggle{ width:54px; height:27px; border-radius:14px; background:var(--panel2);
    border:1px solid var(--line); position:relative; cursor:pointer; padding:0;
    flex:0 0 auto; display:flex; align-items:center; justify-content:space-between; }
  .theme-toggle .tg{ font-size:12px; line-height:1; width:21px; text-align:center;
    color:var(--dim); z-index:1; }
  .theme-toggle .tg.moon{ color:var(--gold); }
  :root[data-theme="light"] .theme-toggle .tg.moon{ color:var(--dim); }
  :root[data-theme="light"] .theme-toggle .tg.sun{ color:var(--gold); }
  .theme-toggle .knob{ position:absolute; top:2px; left:2px; width:21px; height:21px;
    border-radius:50%; background:var(--gold); box-shadow:0 1px 4px rgba(0,0,0,.4);
    transition:left .22s cubic-bezier(.4,0,.2,1); }
  :root[data-theme="light"] .theme-toggle .knob{ left:29px; }
  @media (prefers-reduced-motion: reduce){ .theme-toggle .knob{ transition:none; } }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.6 "Inter",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--ink); color:var(--text);
         background-image:radial-gradient(1200px 600px at 70% -10%, rgba(201,163,91,0.06), transparent 70%); }
  header { padding:20px 26px 16px; display:flex; align-items:center; gap:14px; }
  .brand { display:flex; align-items:baseline; gap:12px; }
  header h1 { font-family:"Fraunces",Georgia,serif; font-size:21px; margin:0; font-weight:600;
              letter-spacing:.2px; color:var(--text); }
  header h1 .sub { font-family:"IBM Plex Mono",monospace; font-size:11px; letter-spacing:.18em;
                   text-transform:uppercase; color:var(--gold); margin-left:2px; }
  /* the heartbeat: a sage ring that breathes when alive */
  .pulse { width:11px; height:11px; border-radius:50%; background:var(--ok); position:relative; flex:0 0 auto; }
  .pulse::after { content:""; position:absolute; inset:0; border-radius:50%; background:var(--ok);
                  animation:breathe 2.6s ease-in-out infinite; }
  @keyframes breathe { 0%{transform:scale(1);opacity:.55} 70%{transform:scale(2.6);opacity:0} 100%{opacity:0} }
  .pulse.warn{background:var(--warn)} .pulse.warn::after{background:var(--warn)}
  /* The pixel-art mongoose, rendered crisp (no smoothing) and pulsing from near-transparent so the
     header has a quiet heartbeat. .logo-pulse is reused for any logo that should breathe. */
  .logo-pulse{  image-rendering:crisp-edges; flex:0 0 auto;
    animation:logoBreath 2.4s ease-in-out infinite; }
  @keyframes logoBreath{
    0%,100%{ opacity:.6; transform:scale(.97); filter:drop-shadow(0 0 4px rgba(57,255,20,.45)); }
    50%{ opacity:1; transform:scale(1); filter:drop-shadow(0 0 10px rgba(57,255,20,.85)) drop-shadow(0 0 18px rgba(57,255,20,.4)); }
  }
  /* a stronger "materialise from transparent" entrance used on splash/welcome screens */
  .logo-emerge{  image-rendering:crisp-edges; animation:logoEmerge 1.1s ease-out both; }
  @keyframes logoEmerge{ 0%{opacity:0; transform:scale(.7)} 60%{opacity:1; transform:scale(1.05)} 100%{opacity:1; transform:scale(1)} }
  /* Sillygoose codename mascot. Appears in key screens as a delightful codename badge.
     The glow pulses between warm amber and a pale green accent: confident and a little silly. */
  .sillygoose-wrap {
    display:inline-flex; align-items:center; gap:10px;
    background:linear-gradient(135deg,rgba(57,255,20,.06),rgba(201,162,39,.07));
    border:1px solid rgba(201,162,39,.18); border-radius:14px;
    padding:8px 14px 8px 10px; margin:10px 0; cursor:default;
  }
  .sillygoose-img { image-rendering:auto;
    animation:goosePulse 3.2s ease-in-out infinite; flex:0 0 auto; }
  @keyframes goosePulse {
    0%,100% { opacity:.82; transform:scale(.98);
              filter:drop-shadow(0 0 4px rgba(201,162,39,.4)); }
    50%      { opacity:1;   transform:scale(1.02);
              filter:drop-shadow(0 0 10px rgba(201,162,39,.75)) drop-shadow(0 0 18px rgba(57,255,20,.28)); }
  }
  /* About Mongoose: the codename goose gets a gentle bob on top of the glow so it reads as a lively
     little mascot rather than a static badge. Scoped to About; other codename badges keep the calm pulse. */
  .about-goose { animation:aboutGoose 2.8s ease-in-out infinite !important; }
  @keyframes aboutGoose {
    0%,100% { opacity:.85; transform:translateY(0) scale(.99);
              filter:drop-shadow(0 0 4px rgba(201,162,39,.4)); }
    50%      { opacity:1;   transform:translateY(-4px) scale(1.03);
              filter:drop-shadow(0 0 10px rgba(201,162,39,.75)) drop-shadow(0 0 18px rgba(57,255,20,.28)); }
  }
  .sillygoose-label { font-size:11px; font-weight:600; letter-spacing:.08em;
                      color:var(--gold); text-transform:uppercase; line-height:1.2; }
  .sillygoose-sub   { font-size:10px; color:var(--dim); letter-spacing:.04em; line-height:1.4; }

  /* Poke system: hold-to-poke button with sillygoose waddling while the poke loads */
  .poke-btn { position:relative; overflow:hidden; background:var(--panel2);
    border:1px solid var(--line); border-radius:12px; padding:8px 14px;
    color:var(--dim); font-size:13px; cursor:pointer; transition:all .15s;
    display:inline-flex; align-items:center; gap:6px; user-select:none; }
  .poke-btn:hover { border-color:var(--gold); color:var(--text); }
  .poke-btn.holding { border-color:var(--gold); color:var(--gold); }
  .poke-btn.fired   { border-color:#39ff14; color:#39ff14; }
  .poke-progress { position:absolute; left:0; top:0; bottom:0; width:0%;
    background:rgba(201,162,39,.18); transition:width linear; pointer-events:none; }
  .poke-goose { display:inline-block; image-rendering:auto;
    transition:transform .1s; }
  .poke-btn.holding .poke-goose { animation:gooseWaddle .35s ease-in-out infinite; }
  .poke-btn.fired   .poke-goose { animation:gooseLaunch .5s ease-out forwards; }
  @keyframes gooseWaddle {
    0%,100% { transform:rotate(-8deg) scale(1); }
    50%      { transform:rotate(8deg) scale(1.15); }
  }
  @keyframes gooseLaunch {
    0%   { transform:scale(1) rotate(0); opacity:1; }
    60%  { transform:scale(1.6) rotate(-15deg); opacity:1; }
    100% { transform:scale(.8) rotate(10deg) translateX(30px); opacity:0; }
  }
  .poke-card { display:flex; align-items:center; gap:12px; padding:8px 0;
    border-bottom:1px solid var(--line); }
  .poke-card:last-child { border-bottom:none; }
  .poke-card .poke-name { flex:1; font-size:14px; color:var(--text); }
  .poke-card .poke-no   { font-size:11px; color:var(--dim); font-style:italic; }
  .poke-history-item { font-size:12px; color:var(--dim); padding:3px 0; }
  /* The version badge in the header */
  .brand-meta { display:flex; flex-direction:column; justify-content:center; }
  .brand-version { font-size:10px; color:var(--dim); letter-spacing:.04em;
                   line-height:1; margin-top:1px; }
  .brand-codename { font-size:10px; color:var(--gold); letter-spacing:.06em;
                    line-height:1.3; font-weight:500; }
  header .live { font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--dim);
                 letter-spacing:.04em; margin-left:auto; }
  nav { display:flex; gap:2px; padding:0 16px; flex-wrap:wrap; border-bottom:1px solid var(--line); align-items:center;
        position:sticky; top:0; z-index:40; background:var(--ink); }
  /* Narrow screens (phones / narrow windows): rather than wrapping the tabs into a tall multi-row
     block, keep the bar one row and scroll it horizontally with momentum. A thin scrollbar makes the
     scrollability visible (affordance), and show() keeps the active tab scrolled into view. Desktop
     keeps the wrap layout. */
  @media (max-width:760px){
    nav{ flex-wrap:nowrap; overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; scrollbar-width:thin; }
    nav > button, nav > .nav-more{ flex:0 0 auto; }
  }
  nav button { background:none; border:none; color:var(--dim); padding:12px 15px; cursor:pointer;
               font-size:13.5px; font-weight:500; border-bottom:2px solid transparent;
               transition:color .15s; }
  nav button:hover { color:var(--text); }
  nav button.active { color:var(--text); border-bottom-color:var(--gold); }
  /* Chat tab attention pulse: a soft lime glow + gentle scale that draws the eye to the terminal,
     until the user has opened Chat once (then .seen removes it). Honours reduced-motion below. */
  nav button.chat-pulse{ position:relative; color:#39ff14; }
  nav button.chat-pulse::after{ content:""; position:absolute; left:8px; right:8px; bottom:6px; height:2px;
    background:#39ff14; border-radius:2px; box-shadow:0 0 8px #39ff14; animation:chatGlow 1.6s ease-in-out infinite; }
  nav button.chat-pulse{ animation:chatBeat 1.6s ease-in-out infinite; }
  @keyframes chatGlow{ 0%,100%{opacity:.35; box-shadow:0 0 4px #39ff14} 50%{opacity:1; box-shadow:0 0 12px #39ff14, 0 0 20px rgba(57,255,20,.5)} }
  @keyframes chatBeat{ 0%,100%{text-shadow:0 0 4px rgba(57,255,20,.4)} 50%{text-shadow:0 0 10px rgba(57,255,20,.9)} }
  nav button.chat-pulse.seen{ animation:none; color:var(--dim); }
  nav button.chat-pulse.seen::after{ display:none; }
  /* Progressive disclosure: a "More" dropdown holds specialist tabs so the first-time
     user sees a handful of everyday tabs, not all eighteen at once. */
  .nav-more{ position:relative; display:inline-block; }
  .nav-more-btn{ background:none; border:none; color:var(--dim); padding:12px 15px; cursor:pointer;
    font:inherit; font-size:13px; border-bottom:2px solid transparent; display:inline-flex; align-items:center; gap:5px; }
  .nav-more-btn:hover{ color:var(--text); }
  .nav-more-btn.has-active{ color:var(--text); border-bottom-color:var(--gold); }
  .nav-more-btn .chev{ font-size:9px; transition:transform .15s; }
  .nav-more.open .nav-more-btn .chev{ transform:rotate(180deg); }
  .nav-more-menu{ position:absolute; top:100%; right:0; z-index:50; min-width:180px;
    background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:6px;
    box-shadow:0 8px 24px rgba(0,0,0,.35); display:none; flex-direction:column; gap:1px; }
  .nav-more.open .nav-more-menu{ display:flex; }
  .nav-more-menu button{ text-align:left; border-radius:7px; padding:9px 12px; width:100%;
    border-bottom:none !important; font-size:13px; }
  .nav-more-menu button:hover{ background:var(--panel2); }
  .nav-more-menu button.active{ background:var(--panel2); color:var(--gold); border-bottom:none; }
  .nav-more-menu .nav-more-section{ font-size:10px; text-transform:uppercase; letter-spacing:.07em;
    color:var(--dim); padding:8px 12px 4px; opacity:.7; }
  @media (prefers-reduced-motion:reduce){ .nav-more-btn .chev{ transition:none; } }
  /* Collapsible sub-sections inside System and RTM (merged tabs live here now) */
  .sys-section{ border:1px solid var(--line); border-radius:10px; margin-top:12px; background:var(--panel); }
  .sys-section>summary{ cursor:pointer; padding:12px 14px; font-weight:600; font-size:14px;
    list-style:none; display:flex; align-items:center; gap:8px; user-select:none; }
  .sys-section>summary::before{ content:"\25B8"; font-size:10px; color:var(--gold); transition:transform .15s; }
  .sys-section[open]>summary::before{ transform:rotate(90deg); }
  .sys-section>summary::-webkit-details-marker{ display:none; }
  .sys-section>*:not(summary){ padding:0 14px 14px; }
  @media (prefers-reduced-motion:reduce){ .sys-section>summary::before{ transition:none; } }
  main { padding:26px 22px 60px; max-width:940px; }
  .tab { display:none; animation:rise .35s ease both; } .tab.active { display:block; }
  @keyframes rise { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:none} }
  .card { background:linear-gradient(180deg, var(--panel), #1c1914);
          border:1px solid var(--line); border-radius:14px; padding:20px 22px; margin-bottom:16px;
          box-shadow:0 1px 0 rgba(255,255,255,0.02) inset, 0 10px 30px -22px rgba(0,0,0,0.9); }
  .card.bad { border-color:var(--bad); box-shadow:0 1px 0 rgba(255,255,255,0.02) inset,
              0 10px 30px -22px rgba(0,0,0,0.9), inset 0 0 0 1px rgba(208,120,94,.15); }
  .card.warn { border-color:var(--warn); }
  .card h2 { margin:0 0 14px; font-family:"IBM Plex Mono",monospace; font-size:11px;
             text-transform:uppercase; letter-spacing:.2em; color:var(--gold);
             display:flex; align-items:center; gap:10px; }
  .card h2::after { content:""; flex:1; height:1px;
                    background:linear-gradient(90deg, var(--gold-dim), transparent); }
  .big { font-family:"Fraunces",Georgia,serif; font-size:25px; font-weight:600; margin:2px 0 12px;
         letter-spacing:.2px; display:flex; align-items:center; gap:12px; }
  .row { display:flex; justify-content:space-between; gap:18px; padding:10px 0;
         border-bottom:1px solid var(--line); align-items:baseline; }
  .row:last-child { border-bottom:none; }
  .k { color:var(--text); font-size:14px; flex:0 0 auto; max-width:42%; }
  .v { text-align:right; color:var(--dim); flex:1 1 auto; min-width:0;
       font-size:13.5px; line-height:1.5; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:9px;
         vertical-align:middle; box-shadow:0 0 0 3px rgba(124,184,147,0.08); }
  .ok{background:var(--ok)} .warn{background:var(--warn)} .bad{background:var(--bad)}
  .unknown{background:var(--dim);box-shadow:none}
  button.act { background:var(--panel2); color:var(--text); border:1px solid var(--line);
               border-radius:9px; padding:8px 14px; cursor:pointer; font-size:12.5px; font-weight:500;
               margin-left:6px; transition:border-color .15s, background .15s; }
  button.act:hover { border-color:var(--gold); background:#2c2820; }
  button.act.go { border-color:var(--gold-dim); color:#e9d9b4; }
  button.act.go:hover { border-color:var(--gold); }
  button.act.no { border-color:#5a3b30; color:#e6b5a4; }
  input.q { width:100%; background:var(--panel2); border:1px solid var(--line); color:var(--text);
            border-radius:11px; padding:13px 15px; font-size:15px; font-family:inherit; }
  input.q:focus { outline:none; border-color:var(--gold); }
  input.q::placeholder { color:var(--dim); }
  .timeline .row { align-items:flex-start; }
  .time { color:var(--dim); font-family:"IBM Plex Mono",monospace; font-size:12px;
          font-variant-numeric:tabular-nums; min-width:128px; }
  .muted { color:var(--dim); font-size:13px; }
  .src { font-size:12px; color:var(--gold); font-family:"IBM Plex Mono",monospace; }
  pre { white-space:pre-wrap; word-break:break-word; background:#141109; color:#d6cfc0;
        padding:14px; border-radius:11px; border:1px solid var(--line);
        font-family:"IBM Plex Mono",monospace; font-size:12.5px; line-height:1.55; }
  a { color:var(--gold); }
  /* Setup wizard tiles: make expandable cards visibly clickable in the main Cockpit too. */
  .wiz-grid{ display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:10px; margin:6px 0 4px; }
  .sr-only{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
  .wiz-tile{ background:#13110d; border:1px solid var(--line); border-radius:12px; padding:12px 12px 14px;
    cursor:pointer; transition:border-color .15s, box-shadow .15s, transform .08s; min-height:78px; display:flex;
    flex-direction:column; justify-content:space-between; }
  .wiz-tile:hover, .wiz-tile:focus-visible{ border-color:var(--gold); box-shadow:0 0 0 1px rgba(212,160,74,.25); transform:translateY(-1px); outline:none; }
  .wiz-tile.configured{ border-color:#2c5a2c; }
  .wiz-tile.needsauth{ border-color:#6a5a1e; }
  .wiz-tile .tname{ font-weight:600; font-size:13.5px; display:flex; align-items:center; gap:7px; }
  .wiz-tile .tstat{ font-size:11px; margin-top:8px; }
  .wiz-tile .tstat .src{ color:#6fcf6f; } .wiz-tile .tstat .muted{ opacity:.6; }
  .tile-chev{ transition:transform .15s, opacity .15s; }
  .wiz-tile:hover .tile-chev{ opacity:1 !important; transform:translateX(2px); }
  .wiz-detail{ border:1px solid var(--gold); border-radius:12px; padding:14px; margin:4px 0 10px; background:#13110d; }
  .wiz-detail .wiz-close{ float:right; cursor:pointer; color:var(--dim); font-size:18px; line-height:1; }
  .stage-head:hover{ color:var(--gold); }
  .stage-chev{ color:var(--gold-dim); transition:color .15s; }
  .src.warn{ color:var(--gold); opacity:.95; }
  .dot.warn{ background:#d8b13a; }
  button.act.signin{ border-color:var(--gold-dim); color:#e9d9b4; }
  button.act.signin:hover:not(:disabled){ border-color:var(--gold); }
  button.act.signin:disabled{ opacity:.45; cursor:not-allowed; }
  .setup-help{ line-height:1.5; }
  .setup-help br{ content:""; display:block; margin:3px 0; }
  .result-row{ cursor:pointer; align-items:flex-start; border-radius:9px; padding:12px; margin:4px 0; border:1px solid var(--line); }
  .result-row:hover, .result-row:focus-visible{ border-color:var(--gold-dim); background:rgba(201,163,91,.045); outline:none; }
  .search-detail{ display:none; margin-top:8px; padding:8px; border-radius:8px; background:#141109; border:1px solid var(--line); }
  .search-detail.open{ display:block; }
  .email-body{ max-height:420px; overflow:auto; white-space:pre-wrap; word-break:break-word; background:#0f0d08; border:1px solid var(--line); border-radius:8px; padding:10px; color:var(--text); }
  .mail-hdr{ background:#0f0d08; border:1px solid var(--line); border-radius:8px; padding:11px 13px; }
  .mail-from{ font-weight:700; font-size:15px; color:var(--text); word-break:break-word; }
  .mail-subj{ font-size:16px; font-weight:600; margin:7px 0 3px; color:var(--text); word-break:break-word; }
  .mail-meta{ font-size:12px; margin-top:3px; word-break:break-word; }
  .mail-label{ display:inline-block; min-width:42px; opacity:0.75; font-weight:600; }
  .mail-read-pre{ margin-top:8px; }
  .result-meta{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--dim); word-break:break-all; }
  /* ===== Chat tab: ICQ/IRC console aesthetic, lime phosphor on black ===== */
  #chat{ --term:#39ff14; --term-dim:#1f9e0c; --term-bg:#020a02; --term-amber:#ffb000; }
  .irc-win{
    background:linear-gradient(180deg,#0a140a,#020a02);
    border:2px solid #2a2a2a; border-radius:4px;
    /* the 90s bevel: light top/left, dark bottom/right */
    box-shadow: inset 1px 1px 0 #444, inset -1px -1px 0 #000,
                0 0 0 1px #000, 0 14px 40px rgba(0,0,0,.55), 0 0 24px rgba(57,255,20,.06);
    overflow:hidden; font-family:"IBM Plex Mono",monospace;
  }
  .irc-title{
    display:flex; align-items:center; gap:8px;
    background:linear-gradient(90deg,#0c5c0c,#073f07);
    color:#cfffcf; font-size:12px; font-weight:600; letter-spacing:.06em;
    padding:5px 9px; border-bottom:2px solid #000; text-shadow:0 0 6px rgba(57,255,20,.5);
    user-select:none;
  }
  .irc-title .dots{display:flex;gap:5px;margin-left:auto}
  .irc-title .dots b{width:11px;height:11px;border-radius:50%;display:inline-block;
    border:1px solid #000; box-shadow:inset 1px 1px 0 rgba(255,255,255,.35)}
  .irc-title .dots .mn{background:#ffd23a}.irc-title .dots .mx{background:#46d160}.irc-title .dots .cl{background:#ec5a5a}
  .irc-screen{
    position:relative; background:var(--term-bg); height:48vh; min-height:300px;
    overflow-y:auto; padding:12px 14px; color:var(--term);
    text-shadow:0 0 4px rgba(57,255,20,.45); font-size:13.5px; line-height:1.55;
  }
  /* CRT scanlines + faint vignette, purely decorative, behind the text */
  .irc-screen::before{
    content:""; position:fixed; pointer-events:none; inset:0;
    background:repeating-linear-gradient(0deg, rgba(0,0,0,0) 0px, rgba(0,0,0,0) 2px, rgba(0,0,0,.22) 3px, rgba(0,0,0,0) 4px);
    mix-blend-mode:multiply; z-index:1;
  }
  .irc-screen::after{
    content:""; position:absolute; inset:0; pointer-events:none; z-index:1;
    background:radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,.45) 100%);
  }
  .irc-line{ position:relative; z-index:2; margin:3px 0; white-space:pre-wrap; word-break:break-word; animation:ircIn .12s ease both; }
  @keyframes ircIn{ from{opacity:0; transform:translateY(2px)} to{opacity:1; transform:none} }
  .irc-ts{ color:#1f7a12; }
  .irc-nick-you{ color:var(--term-amber); text-shadow:0 0 5px rgba(255,176,0,.5); font-weight:600; }
  .irc-nick-bot{ color:#54ff3a; font-weight:600; }
  .irc-sys{ color:#1f9e0c; font-style:italic; }
  .irc-mem{ color:#6b7a6b; font-style:italic; font-size:12px; opacity:.85; } /* memory note: muted grey, not lime */
  .irc-notice{ color:#e9d39a; } /* proactive surpriser notice: soft gold, distinct from replies */
  .irc-join{ color:#2bd40f; }
  /* the typing/working indicator */
  .irc-typing{ color:#54ff3a; }
  .irc-typing i{ animation:ircBlink 1s steps(1) infinite; font-style:normal; }
  .irc-typing i:nth-child(2){ animation-delay:.2s } .irc-typing i:nth-child(3){ animation-delay:.4s }
  /* the input row, prompt + field + steady blinking block cursor */
  .irc-input{ display:flex; align-items:center; gap:0; background:#041004; border-top:2px solid #000;
    padding:8px 10px; font-family:"IBM Plex Mono",monospace; }
  .irc-prompt{ color:var(--term); text-shadow:0 0 5px rgba(57,255,20,.5); margin-right:8px; white-space:nowrap; }
  .irc-field{ flex:1; background:transparent; border:none; outline:none; color:var(--term);
    font-family:"IBM Plex Mono",monospace; font-size:13.5px; text-shadow:0 0 4px rgba(57,255,20,.45);
    caret-color:transparent; }
  /* A real, visible, blinking lime BLOCK cursor (the ICQ/terminal look). The native caret is hidden
     (caret-color:transparent); we render a lime rectangle right after the typed text using a hidden
     mirror that measures the text width, so the block sits where the cursor is. caret-shape:block has
     poor browser support, hence this. */
  .irc-cursorwrap{ position:relative; flex:1; display:flex; align-items:center; }
  .irc-mirror{ position:absolute; left:0; top:50%; transform:translateY(-50%); visibility:hidden;
    white-space:pre; font-family:"IBM Plex Mono",monospace; font-size:13.5px; }
  .irc-block{ display:inline-block; width:8px; height:16px; background:var(--term); margin-left:1px;
    box-shadow:0 0 6px rgba(57,255,20,.7); animation:ircBlink 1.05s step-end infinite;
    vertical-align:middle; }
  @keyframes ircBlink{ 0%,50%{opacity:1} 50.01%,100%{opacity:0} }
  .irc-hint{ color:#1f7a12; font-size:11px; padding:6px 10px; background:#020a02; border-top:1px solid #0c2a0c;
    font-family:"IBM Plex Mono",monospace; display:flex; justify-content:space-between; }
  .irc-emoji-bar{ display:flex; gap:4px; flex-wrap:wrap; }
  .irc-emoji-bar button{ background:#0a1a0a; border:1px solid #144014; border-radius:3px; cursor:pointer;
    font-size:14px; padding:1px 5px; line-height:1.3; }
  .irc-emoji-bar button:hover{ background:#14401a; box-shadow:0 0 6px rgba(57,255,20,.3); }
  /* Accessibility: honour reduced-motion by disabling ANIMATION only (never the chat colours or
     layout, which must always apply). Self-contained, properly-closed block. */
  /* ===== UX refresh (proposals 0002-0006): additive, token-driven, deletable to revert ===== */
  /* 0002 brief-led Overview hero */
  .hero{ background:linear-gradient(160deg,var(--panel),var(--panel2));
    border:1px solid var(--line); border-radius:16px; padding:20px 22px; position:relative; margin-bottom:14px; }
  .hero h2{ margin:0 0 10px; font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:var(--dim); font-weight:600; }
  .brieftext{ font-family:"Fraunces",Georgia,serif; font-weight:400; font-size:19px; line-height:1.55; margin:0; color:var(--text); }
  .brieftext .hl{ color:var(--gold); font-weight:500; }
  .brieffoot{ margin-top:14px; font-family:"IBM Plex Mono",monospace; font-size:10.5px; letter-spacing:.06em; color:var(--dim); }
  /* 0003 scannable status strip + universal button affordance */
  .statrip{ display:flex; flex-wrap:wrap; gap:8px; margin:4px 0 2px; }
  .stat{ display:flex; align-items:center; gap:9px; background:var(--panel2);
    border:1px solid var(--line); border-radius:10px; padding:9px 13px; font-size:13px; }
  .stat .dot{ width:8px; height:8px; border-radius:50%; flex:0 0 auto; }
  .stat em{ color:var(--dim); font-size:11.5px; font-style:normal; }
  .btn{ display:inline-flex; align-items:center; gap:7px; background:var(--panel2);
    border:1px solid var(--line); color:var(--text); border-radius:9px; padding:9px 14px;
    font-size:13px; font-weight:600; cursor:pointer; transition:border-color .15s,transform .08s; font-family:inherit; }
  .btn:hover{ border-color:var(--gold); } .btn:active{ transform:translateY(1px); }
  .btn.gold{ border-color:var(--gold-dim); color:#e9d9b4;
    background:linear-gradient(180deg,rgba(201,163,91,.16),rgba(201,163,91,.05)); }
  :root[data-theme="light"] .btn.gold{ color:#5a4718; }
  .needsrow{ display:flex; justify-content:space-between; align-items:center; gap:12px; margin:8px 0;
    padding:10px 12px; background:var(--panel2); border:1px solid var(--line); border-radius:10px; }
  .needsrow .nx{ flex:1; font-size:14px; } .needsrow .nx b{ color:var(--text); }
  /* 0004 command-palette search */
  .filters{ display:flex; gap:7px; flex-wrap:wrap; margin:10px 0; }
  .chip{ font-family:"IBM Plex Mono",monospace; font-size:11px; padding:6px 12px;
    border-radius:999px; border:1px solid var(--line); color:var(--dim);
    background:var(--panel2); cursor:pointer; }
  .chip.on{ color:#1f1a10; background:var(--gold); border-color:var(--gold); }
  :root[data-theme="light"] .chip.on{ color:#fbf6ec; }
  .res{ display:flex; gap:14px; align-items:center; padding:12px 4px;
    border-bottom:1px solid var(--line); cursor:pointer; }
  .res:hover,.res.sel{ background:var(--panel2); }
  .res.sel{ box-shadow:inset 3px 0 0 var(--gold); }
  .restype{ font-family:"IBM Plex Mono",monospace; font-size:9.5px; letter-spacing:.1em;
    text-transform:uppercase; padding:5px 8px; border-radius:6px; min-width:58px; text-align:center; flex:0 0 auto; }
  .t-file{ background:rgba(124,184,147,.16); color:var(--ok); }
  .t-mail{ background:rgba(217,160,91,.18); color:var(--warn); }
  .t-note{ background:rgba(201,163,91,.18); color:var(--gold); }
  .t-voicemail{ background:rgba(124,184,147,.14); color:var(--ok); }
  .t-notification{ background:rgba(201,163,91,.12); color:var(--gold); }
  .t-other{ background:var(--panel2); color:var(--dim); }
  .pfoot{ display:flex; gap:18px; flex-wrap:wrap; font-family:"IBM Plex Mono",monospace; font-size:11px;
    color:var(--dim); padding-top:10px; }
  .kbd{ border:1px solid var(--line); border-radius:5px; padding:1px 6px; color:var(--text); }
  /* 0005 registry caption */
  .regnote{ display:flex; gap:9px; font-family:"IBM Plex Mono",monospace; font-size:11px;
    line-height:1.55; color:var(--dim); background:var(--panel2);
    border:1px solid var(--line); border-radius:10px; padding:10px 12px; margin-top:8px; }
  .regnote b{ color:var(--gold); font-weight:500; }
  /* 0006 welcome state cards */
  .step{ display:flex; gap:15px; padding:17px 18px; border:1px solid var(--line);
    border-radius:15px; background:var(--panel); margin-bottom:10px; }
  .step.done{ border-color:rgba(124,184,147,.45); }
  .step.active{ border-color:var(--gold); box-shadow:0 0 0 1px var(--gold-dim); }
  .stepnum{ width:34px; height:34px; border-radius:10px; display:flex; align-items:center;
    justify-content:center; font-family:"IBM Plex Mono",monospace; font-weight:600;
    border:1px solid var(--line); color:var(--dim); flex:0 0 auto; }
  .step.done .stepnum{ background:var(--ok); color:#0d1a10; border-color:var(--ok); }
  .step.active .stepnum{ background:var(--gold); color:#211a0d; border-color:var(--gold); }
  .steptitle{ font-family:"Fraunces",Georgia,serif; font-size:17px; font-weight:600; color:var(--text); }
  .stepdesc{ color:var(--dim); font-size:13px; margin-top:4px; }
  /* Light-skin overrides for the few hard-coded dark substrates the theme vars don't reach, code
     blocks, search-detail, email body, token boxes. (The ICQ #chat is intentionally left dark.) */
  :root[data-theme="light"] pre{ background:#F0E7D4; color:#2C261C; }
  :root[data-theme="light"] .search-detail{ background:#F0E7D4; }
  :root[data-theme="light"] .email-body{ background:#FBF6EC; color:#2C261C; }
  :root[data-theme="light"] .mail-hdr{ background:#FBF6EC; }
  :root[data-theme="light"] .tok{ background:#F0E7D4; color:#2C261C; }
  /* Light-mode contrast: the remaining surfaces that hardcode dark backgrounds or light text and
     don't read the theme tokens, the setup wizard tiles/detail, action buttons, and JS-rendered
     token/code boxes (inline styles win over classes, so those are caught by their literal values).
     The ICQ #chat console is deliberately left dark and is not touched here. */
  :root[data-theme="light"] .wiz-tile,
  :root[data-theme="light"] .wiz-detail{ background:#FBF6EC; }
  :root[data-theme="light"] button.act.go{ color:#5a4718; }
  :root[data-theme="light"] button.act:hover{ background:#EDE2C9; }
  :root[data-theme="light"] [style*="background:#141109"],
  :root[data-theme="light"] [style*="background:#13110d"],
  :root[data-theme="light"] [style*="background:#0f0d08"]{ background:#F0E7D4 !important; color:#2C261C !important; }
  :root[data-theme="light"] [style*="color:#d6cfc0"],
  :root[data-theme="light"] [style*="color:#e9d9b4"]{ color:#2C261C !important; }
  @media (prefers-reduced-motion: reduce){
    *, *::before, *::after{ animation-duration:0.001ms !important; animation-iteration-count:1 !important; transition-duration:0.001ms !important; }
    .irc-typing i{ animation:none !important; opacity:1 !important; }
    nav button.chat-pulse, nav button.chat-pulse::after{ animation:none !important; }
    .logo-pulse, .logo-emerge{ animation:none !important; opacity:1 !important; transform:none !important; }
  }
</style>
</head>
<body>
<header>
  <img class="logo-pulse" id="brandLogo" src="/static/img/mongoose-64.png" alt="Mongoose" width="46" height="46">
  <div class="brand">
    <h1>Mongoose<span class="sub">cockpit</span></h1>
    <div class="brand-meta">
      <span class="brand-version">__MONGOOSE_VERSION__</span>
      <span class="brand-codename" title="Codename Sillygoose"><img src="/static/img/sillygoose-256.png" alt="" width="16" height="16" style="vertical-align:-3px;margin-right:3px">sillygoose</span>
    </div>
  </div>
  <span class="live" id="live">checking...</span>
  <button id="themeToggle" class="theme-toggle" type="button"
          aria-label="Toggle light or dark appearance" title="Light / dark">
    <span class="tg moon" aria-hidden="true">&#9790;</span
    ><span class="tg sun" aria-hidden="true">&#9728;</span
    ><span class="knob"></span>
  </button>
</header>
<div id="watchdog" role="status" aria-live="polite" style="display:none"></div>
<nav id="nav"></nav>
<div id="navsearch" style="position:relative;padding:0 26px 8px">
  <input id="secsearch" type="search" placeholder="Find a menu, section or function&hellip;  ( press / )" aria-label="Find a menu, section or function"
    autocomplete="off" style="width:100%;max-width:340px;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:10px;padding:8px 12px;font-size:13px">
  <div id="secresults" role="listbox" style="display:none;position:absolute;z-index:50;left:26px;top:40px;max-width:340px;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden;box-shadow:0 12px 30px -16px rgba(0,0,0,.8)"></div>
</div>
<main>
  <section class="tab active" id="overview"></section>
  <section class="tab" id="setup"></section>
  <section class="tab" id="activity"></section>
  <section class="tab" id="approvals"></section>
  <section class="tab" id="search">
    <div class="card">
      <h2>Search your stuff</h2>
      <label for="q" class="sr-only">Search files, email, voicemails and notifications</label>
      <input class="q" id="q" placeholder="search files, email, voicemails, notifications...   (Enter, or ⌘K from anywhere)" aria-label="Search files, email, voicemails and notifications">
      <div class="filters" id="searchFilters">
        <span class="chip on" data-filter="all" onclick="setSearchFilter(this)">All</span>
        <span class="chip" data-filter="file" onclick="setSearchFilter(this)">Files</span>
        <span class="chip" data-filter="mail" onclick="setSearchFilter(this)">Email</span>
        <span class="chip" data-filter="voicemail" onclick="setSearchFilter(this)">Voicemails</span>
        <span class="chip" data-filter="notification" onclick="setSearchFilter(this)">Notifications</span>
        <span class="chip" data-filter="note" onclick="setSearchFilter(this)">Notes</span>
      </div>
      <div id="results" style="margin-top:12px"></div>
      <div class="pfoot" id="searchFoot" style="display:none">
        <span><span class="kbd">↑</span> <span class="kbd">↓</span> navigate</span>
        <span><span class="kbd">⏎</span> open</span>
        <span><span class="kbd">⌘K</span> anywhere</span>
        <span id="searchTiming"></span>
      </div>
    </div>
  </section>
  <section class="tab" id="chat"></section>
  <section class="tab" id="voice">
    <div class="card">
      <h2>Voice chat</h2>
      <p class="muted">Hold to record, release to send. Mongoose transcribes on the server and replies; the reply is spoken back on this device.</p>
      <p class="muted" id="voicewarn"></p>
      <button class="act go" id="rec" style="padding:14px 22px;font-size:15px">Hold to talk</button>
      <label style="margin-left:14px"><input type="checkbox" id="speak" checked> speak replies</label>
      <div id="vout" style="margin-top:14px"></div>
    </div>
  </section>
  <section class="tab" id="rtm"></section>
  <section class="tab" id="build"></section>
  <section class="tab" id="whatsapp"></section>
  <section class="tab" id="scheduler"></section>
  <section class="tab" id="devices"></section>
  <section class="tab" id="system"></section>
</main>
<script>
// Escape ALL untrusted values before they touch innerHTML. Content from WhatsApp,
// email, files, the bus and search snippets is attacker-influenced; unescaped it
// could inject script into this same-origin page and call privileged POSTs.
function esc(v){ return String(v==null?"":v)
  .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
  .replaceAll('"',"&quot;").replaceAll("'","&#39;"); }
// Escape first (safe), THEN turn URLs and bare domains into clickable links. Used for setup help
// text and field hints so links the user needs to visit are actually openable in a new tab.
function linkify(v){
  let s = esc(v);
  // full http(s):// URLs
  s = s.replace(/(https?:\/\/[^\s<]+[^\s<.,;:)])/g,
        '<a href="$1" target="_blank" rel="noopener" style="color:var(--gold);text-decoration:underline">$1</a>');
  // bare known-setup domains (console.cloud.google.com, entra.microsoft.com, etc.) not already linked
  s = s.replace(/(^|[\s(])((?:console|platform|aistudio|myaccount|entra|api)?\.?(?:cloud\.)?(?:google|anthropic|openai|microsoft)\.com[^\s<.,;:)]*)/g,
        (m,pre,dom)=> dom.includes('</a>')||/href=/.test(m)? m : `${pre}<a href="https://${dom}" target="_blank" rel="noopener" style="color:var(--gold);text-decoration:underline">${dom}</a>`);
  // Numbered steps "1) ... 2) ... 3) ..." become their own lines so instructions read as a list,
  // not a comma-jammed paragraph. Break BEFORE each "N)" that follows a separator.
  s = s.replace(/(^|[\s;.,])(\d\))\s*/g, (m,pre,num)=> (pre && !/^\s*$/.test(pre) && pre!=="("? "<br>" : "") + num + " ");
  // The help texts use ", " as a soft clause separator; render it as a normal comma+space
  // rather than the spaced comma that reads as a typo.
  s = s.replace(/\s+,\s+/g, ", ");
  return s;
}
// Per-process action nonce, fetched same-origin. Required on privileged POSTs so an
// injected/cross-origin script can't forge approve/install/cancel.
let NONCE=null;
async function ensureNonce(){ if(NONCE)return NONCE;
  try{ const r=await fetch("/cockpit/nonce"); if(r.ok){ NONCE=(await r.json()).cockpit_nonce; } }catch(e){}
  return NONCE; }
async function requestAction(url, body, {method="POST", timeoutMs=60000}={}){
  if(window.__cockpitStale){ alert("Mongoose restarted (or you have this open in another tab) since this page loaded. Nothing was lost, your conversation and data are all still there, but this tab is now out of sync and can't safely send actions. Reload the page and everything will be there."); throw new Error("tab superseded"); }
  const n=await ensureNonce();
  const ctrl=new AbortController();
  const to=setTimeout(()=>ctrl.abort(), timeoutMs);
  try{
    return await fetch(url,{method,headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n||""},
      body:JSON.stringify(Object.assign({cockpit_nonce:n},body||{})), signal:ctrl.signal});
  } finally { clearTimeout(to); }
}
async function postAction(url, body, timeoutMs=60000){
  if(window.__cockpitStale){ alert("Mongoose restarted (or you have this open in another tab) since this page loaded. Nothing was lost, your conversation and data are all still there, but this tab is now out of sync and can't safely send actions. Reload the page and everything will be there."); throw new Error("tab superseded"); }
  const n=await ensureNonce();
  const ctrl=new AbortController();
  const to=setTimeout(()=>ctrl.abort(), timeoutMs);
  try{
    return await fetch(url,{method:"POST",headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n||""},
      body:JSON.stringify(Object.assign({cockpit_nonce:n},body||{})), signal:ctrl.signal});
  } finally { clearTimeout(to); }
}
// Honest action: HTTP 200 with {ok:false} is a FAILURE, not a success. Fire-and-reload callers
// used to read only HTTP status and would clear a card / refresh as if done even when the server
// refused the action. postActionOk throws with the server's own reason so the invariant holds:
// an action completes truthfully, fails visibly, or stays pending, never quietly pretends.
async function postActionOk(url, body, timeoutMs=60000){
  const r=await postAction(url, body, timeoutMs);
  let d=null; try{ d=await r.json(); }catch(_e){ d=null; }
  if(!r.ok){ const m=(d&&(d.message||d.error||d.detail))||("HTTP "+r.status); const e=new Error(m); e.data=d; throw e; }
  if(d && d.ok===false){ const e=new Error(d.message||d.error||"That action did not complete."); e.data=d; throw e; }
  return d||{};
}
const TABS = ["overview","setup","activity","approvals","search","chat","voice","whatsapp","rtm","build","scheduler","devices","system"];
const LABELS = {overview:"Overview",setup:"Setup & Repair",activity:"Activity",
                approvals:"Approvals",search:"Search",chat:"Chat",voice:"Voice",whatsapp:"WhatsApp",rtm:"RTM",scheduler:"Scheduler",devices:"Phones & devices",system:"System",build:"Build & plugin"};

// Progressive disclosure. Chat leads (it's the point of the product). The everyday tabs sit in
// the primary bar; specialist tabs live under "More" grouped by purpose. Models, Benchmarks, and
// Logs are now SECTIONS inside System; Voicemail is a SECTION inside RTM (property business): so
// they are not top-level tabs at all anymore.
const PRIMARY_TABS = ["chat","overview","activity","search","devices","setup"];
const MORE_GROUPS = [
  { label:"Messages & voice", tabs:["whatsapp","voice"] },
  { label:"Approvals",        tabs:["approvals"] },
  { label:"Property (RTM)",   tabs:["rtm"] },
  { label:"System & advanced", tabs:["build","scheduler","system"] },
];

const nav = document.getElementById("nav");

function _mkTabButton(t){
  const b=document.createElement("button");
  b.textContent=LABELS[t]; b.dataset.tab=t;
  if(t==="chat"){ b.classList.add("chat-pulse"); }
  b.onclick=()=>show(t,b);
  return b;
}

// Primary bar
PRIMARY_TABS.forEach((t,i)=>{
  const b=_mkTabButton(t);
  if(i===0) b.classList.add("active");
  nav.appendChild(b);
});

// "More" dropdown holding the grouped specialist tabs
const moreWrap=document.createElement("div"); moreWrap.className="nav-more";
const moreBtn=document.createElement("button"); moreBtn.className="nav-more-btn";
moreBtn.setAttribute("aria-haspopup","true"); moreBtn.setAttribute("aria-expanded","false");
moreBtn.innerHTML='More <span class="chev" aria-hidden="true">&#9660;</span>';
const moreMenu=document.createElement("div"); moreMenu.className="nav-more-menu"; moreMenu.setAttribute("role","menu");
MORE_GROUPS.forEach(group=>{
  const sec=document.createElement("div"); sec.className="nav-more-section"; sec.textContent=group.label;
  moreMenu.appendChild(sec);
  group.tabs.forEach(t=>{
    const b=_mkTabButton(t);
    b.setAttribute("role","menuitem");
    // wrap onclick so picking from the menu also closes it
    b.onclick=()=>{ show(t,b); moreWrap.classList.remove("open"); moreBtn.setAttribute("aria-expanded","false"); };
    moreMenu.appendChild(b);
  });
});
moreBtn.onclick=(e)=>{ e.stopPropagation();
  const open=moreWrap.classList.toggle("open");
  moreBtn.setAttribute("aria-expanded", open?"true":"false"); };
moreWrap.appendChild(moreBtn); moreWrap.appendChild(moreMenu);
nav.appendChild(moreWrap);
// close the dropdown on outside click or Escape
document.addEventListener("click",(e)=>{ if(!moreWrap.contains(e.target)){ moreWrap.classList.remove("open"); moreBtn.setAttribute("aria-expanded","false"); } });
document.addEventListener("keydown",(e)=>{ if(e.key==="Escape"){ moreWrap.classList.remove("open"); moreBtn.setAttribute("aria-expanded","false"); } });

// ===== Section search ribbon (D27) =====
// Type to find any section (including the ones tucked under "More") and jump straight to it, so a
// long, multi-section console is navigable without hunting. Press "/" anywhere to focus it.
(function sectionSearch(){
  const inp=document.getElementById("secsearch"), res=document.getElementById("secresults"), wrap=document.getElementById("navsearch");
  if(!inp||!res||!wrap) return;
  // Index: every tab (menu item), plus curated sub-sections and functions, each pointing at the tab
  // that hosts it (and optionally an element to scroll to / a <details> to open). This surfaces menu
  // items, SECTIONS and FUNCTIONS, NOT documents. The Search tab / /find is the document search.
  function buildEntries(){
    const E=[];
    TABS.forEach(t=>E.push({label:LABELS[t]||t, tab:t, kw:((LABELS[t]||t)+" "+t).toLowerCase(), kind:"Menu"}));
    // Curated sections + functions. Keywords are written for the words a REAL person types ("stop a
    // job", "free memory", "guest", "pay", "find a file", "remote access", "turn off lights"), not
    // for internal field names. Each points at the tab that hosts it (and optionally an element to
    // scroll to). This surfaces menu items, SECTIONS and FUNCTIONS, NOT documents. The Search tab /
    // /find is the document search.
    [ {label:"Models", tab:"system", el:"sys-models", kw:"models residency ollama llm brain ai local model"},
      {label:"Benchmarks & tuning", tab:"system", el:"sys-benchmarks", kw:"benchmarks tuning performance speed slow fast latency lane"},
      {label:"Logs", tab:"system", kw:"logs log errors error output crash debug what happened"},
      {label:"Voicemail", tab:"rtm", kw:"voicemail voice messages calls phone message recordings"},
      {label:"RTM source of truth", tab:"rtm", kw:"rtm source truth requirements spec provenance claude build canonical"},
      {label:"RTM build plan", tab:"build", kw:"rtm build wordpress management plan chat ui scheduled product"},
      {label:"Protocol v3 coordination", tab:"system", kw:"v3 protocol pi peers chat code mongoose locks bus readiness coordination handoff"}
    ].forEach(s=>E.push({label:s.label, tab:s.tab, el:s.el, kw:(s.label+" "+s.kw).toLowerCase(), kind:"Section"}));
    [ {label:"Run doctor", tab:"system", kw:"doctor health diagnostics check repair fix broken problem wrong"},
      {label:"Index my files", tab:"search", kw:"index reindex files rebuild search index documents"},
      {label:"Search files & mail", tab:"search", kw:"search find file files document documents mail email voicemail look for finding"},
      {label:"Sync mail", tab:"search", kw:"sync mail email imap fetch gmail outlook inbox refresh messages"},
      {label:"Connectors", tab:"setup", kw:"connectors connect accounts google microsoft oauth gmail calendar drive integration integrations link unlink enable disable account settings"},
      {label:"Pair a device", tab:"devices", kw:"pair device phone tablet qr add new connect ipad watch revoke remove unpair"},
      {label:"Guests & invites", tab:"devices", el:"guestInvite", kw:"guest guests invite invites visitor share access revoke trusted add guest"},
      {label:"Reset a password", tab:"devices", el:"pwResetBox", kw:"reset password repair pair forgot forgotten locked out lockout log in login signin sign in credentials new password change password guest sam cannot log in cant login home screen pwa"},
      {label:"Deploy / update", tab:"build", kw:"deploy update build release upgrade staged version new install ship"},
      {label:"Approvals", tab:"approvals", kw:"approve approvals cancel pending whatsapp drafts review queue"},
      {label:"Scheduler & jobs", tab:"scheduler", kw:"scheduler jobs cron schedule stop start pause job task background free memory unattended automation routine"},
      {label:"RTM receipts & reconcile", tab:"rtm", kw:"rtm receipt receipts reconcile reconciliation reserve trust pay payment demand service charge leaseholder rent money bank arrears {{RTM_NAME_LOWER}}"},
      {label:"RTM intake & cases", tab:"rtm", kw:"rtm intake case cases email reply draft leaseholder maintenance request"},
      {label:"Build a plan", tab:"build", kw:"build plan plans rtm build trigger run approve overnight"},
      {label:"Remote access (away from home)", tab:"setup", kw:"remote access away from home ddns dyndns duckdns internet outside public url reach connect remotely travel city"},
      {label:"Backup & restore", tab:"system", kw:"backup restore save export brain data snapshot recover drive"},
      {label:"Security & HTTPS", tab:"setup", kw:"security https certificate cert tls ssl secure password token nonce safe encryption privacy webhook webhook secret generate regenerate signature hmac vapid push key"},
      {label:"Home devices & control", tab:"devices", kw:"home assistant ha lights light turn off on devices control smart thermostat alexa echo plugs scenes hue"},
      {label:"Notifications & push", tab:"system", kw:"notification notifications push alerts ntfy reminders ping phone pwa device web push"},
      {label:"Voice & speech", tab:"voice", kw:"voice speech talk say speak microphone mic listen audio"},
      {label:"WhatsApp drafts", tab:"whatsapp", kw:"whatsapp message drafts chat reply send"},
      {label:"Chat with Mongoose", tab:"chat", kw:"chat talk ask question assistant conversation"},
      {label:"Log or diagnose a bug", tab:"chat", kw:"bug bugs log bug add bug detail diagnose bug attempt fix apply fix patch verifier peers v3"},
      {label:"Data dump / export", tab:"chat", kw:"data dump export download backup journal support bug mongoose dump private zip"},
      {label:"Wellness journal", tab:"chat", kw:"wellness mindful mindfulness journal journaling self journal checkin check-in mood structured brief rituals"},
      {label:"System resources", tab:"system", kw:"resources resource memory ram rss model stuck ollama looping loop high cpu pressure process thread runaway exhausting"},
      {label:"File crawler freshness", tab:"search", kw:"file crawl crawler crawling index reindex stale deleted folders search refresh filesystem"},
      {label:"Surpriser nudges", tab:"system", kw:"surpriser surprise nudges delight recommendations cadence not creepy stale noisy"},
      // Chat-driven functions: no dedicated UI, so these open chat with a ready-to-send draft that
      // the intent resolver routes to the right handler. `chat` is the prepopulated draft.
      {label:"Invite a guest (pair link)", tab:"chat", chat:"Give me a guest invite link for ", kw:"invite guest pair repair link access friend visitor share add someone new person sam"},
      {label:"Prepare me for something", tab:"chat", chat:"Prepare me for ", kw:"prepare prep get ready meeting call appointment trustee before"},
      {label:"Surprise me / a useful nudge", tab:"chat", chat:"Surprise me", kw:"surprise suggestion suggest nudge idea anything for me something useful"},
      {label:"RTM arrears & money position", tab:"chat", chat:"How are the arrears looking?", kw:"rtm arrears owed overdue who owes money position service charge leaseholder unpaid balance {{RTM_NAME_LOWER}}"},
      {label:"Build the RTM plugin", tab:"chat", chat:"Build the next RTM plugin iteration", kw:"build rtm plugin wordpress site rebuild author deploy ship portal {{RTM_NAME_LOWER}}"},
      {label:"Protocol v3 / peer coordination", tab:"chat", chat:"What is protocol v3?", kw:"v3 protocol peers bus chat code coordination handshake tailscale ssh pi home assistant sync catch up"},
      {label:"Play music", tab:"chat", chat:"Play some music", kw:"music play song songs jazz tunes put on lounge"}
    ].forEach(f=>E.push({label:f.label, tab:f.tab, el:f.el, chat:f.chat, kw:(f.label+" "+f.kw).toLowerCase(), kind:"Function"}));
    return E;
  }
  const ENTRIES=buildEntries(); let _m=[];
  function jump(e){ if(!e) return;
    // Chat-driven functions have no dedicated UI element: open the chat tab and PREPOPULATE the input
    // with a draft utterance that invokes the function (the user reviews/edits, then sends). We do NOT
    // auto-send: the draft is a starting point, and some need the user to finish it (such as a name).
    if(e.chat){
      if(typeof activateTab==="function") activateTab("chat"); else show("chat");
      inp.value=""; res.style.display="none";
      setTimeout(()=>{ const ci=document.querySelector("textarea#msg")||document.getElementById("msg");
        if(ci&&("value" in ci)){ ci.value=e.chat; try{ci.focus();}catch(_){}
          try{ci.setSelectionRange(ci.value.length,ci.value.length);}catch(_){}
          try{ci.dispatchEvent(new Event("input",{bubbles:true}));}catch(_){} } }, 250);
      return;
    }
    if(typeof activateTab==="function") activateTab(e.tab); else show(e.tab); inp.value=""; res.style.display="none";
    if(e.el){ setTimeout(()=>{ const t=document.getElementById(e.el); if(t){ const d=t.closest&&t.closest("details"); if(d) d.open=true; if(t.scrollIntoView){ try{ t.scrollIntoView({block:"start",behavior:"smooth"}); }catch(_){}} } }, 260); } }
  function render(q){
    q=(q||"").trim().toLowerCase();
    if(!q){ res.style.display="none"; res.innerHTML=""; return; }
    const terms=q.split(/\s+/);
    _m=ENTRIES.filter(e=>terms.every(t=>e.kw.indexOf(t)>=0)).slice(0,40);
    if(!_m.length){ res.innerHTML='<div style="padding:8px 12px;color:var(--dim);font-size:13px">No menu, section or function matches.</div>'; res.style.display="block"; return; }
    res.innerHTML=_m.map((e,i)=>`<div class="secopt" data-i="${i}" role="option" tabindex="-1" style="padding:9px 12px;cursor:pointer;font-size:13px;display:flex;justify-content:space-between;gap:10px;align-items:center;${i===0?'background:rgba(201,163,91,.12)':''}"><span>${esc(e.label)}</span><span style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);border-radius:99px;padding:1px 7px">${esc(e.kind)}</span></div>`).join("");
    res.style.display="block";
    res.querySelectorAll(".secopt").forEach(el=>{ el.onclick=()=>jump(_m[parseInt(el.dataset.i,10)]); });
  }
  inp.addEventListener("input",()=>render(inp.value));
  inp.addEventListener("keydown",(e)=>{
    if(e.key==="Enter"){ if(_m.length){ e.preventDefault(); jump(_m[0]); } }
    else if(e.key==="Escape"){ inp.value=""; res.style.display="none"; inp.blur(); }
  });
  document.addEventListener("click",(e)=>{ if(!wrap.contains(e.target)){ res.style.display="none"; } });
  document.addEventListener("keydown",(e)=>{ if(e.key==="/" && !/INPUT|TEXTAREA|SELECT/.test((e.target&&e.target.tagName)||"") && !e.metaKey && !e.ctrlKey){ e.preventDefault(); inp.focus(); } });
})();

function show(t,btn){ TABS.forEach(x=>document.getElementById(x).classList.remove("active"));
  document.getElementById(t).classList.add("active");
  // clear active on all nav buttons (primary + inside the menu)
  nav.querySelectorAll("button").forEach(c=>c.classList.remove("active"));
  if(btn) btn.classList.add("active");
  // keep the active tab visible when the nav scrolls horizontally (narrow screens), so you can
  // always see where you are.
  if(btn && btn.scrollIntoView){ try{ btn.scrollIntoView({inline:"nearest", block:"nearest", behavior:"smooth"}); }catch(_){} }
  // if the chosen tab lives under More, mark the More button as carrying the active tab
  const inMore = MORE_GROUPS.some(g=>g.tabs.includes(t));
  moreBtn.classList.toggle("has-active", inMore);
  if(t==="chat"){ if(btn){ btn.classList.add("seen"); btn.classList.remove("chat-pulse"); } }
  load(t); }

// Find a tab's nav button by name, wherever it lives (primary bar or the More menu).
function _btnForTab(t){ return nav.querySelector('button[data-tab="'+t+'"]'); }
// Activate a tab by name only (used by deep-links and programmatic show("chat") calls), so
// callers don't need to know whether the tab is primary or under More.
function activateTab(t){ const b=_btnForTab(t); show(t, b); }
// Jump to Setup and open the DuckDNS (remote-reach) form, used by the early "use away from home"
// prompt on the pairing card, so remote access is reachable early rather than buried.
function gotoDdnsSetup(){
  // Only switch (and reload) tabs if not already on Setup: this button only ever appears
  // WITHIN the Setup tab's own rendered HTML, so activateTab("setup") here was unconditionally
  // re-triggering a full rebuild of the pane it was already sitting in -- clearing remoteChase
  // (reading as "the box collapsed") and resetting scroll to the top (the "page jump"), on
  // every single click, for no reason.
  const already = document.getElementById("setup") && document.getElementById("setup").classList.contains("active");
  if(!already) activateTab("setup");
  // wait for the setup tab to render, then find the ddns tile's stage and expand its form
  let tries=0;
  const iv=setInterval(()=>{
    const tile=document.querySelector('.wiz-tile[onclick*="ddns"], [data-slug="ddns"]');
    const store=[...document.querySelectorAll('[id^="wizforms-"]')].find(s=>s.querySelector('.wiz-formsrc[data-slug="ddns"]'));
    if(store){
      const sid=store.id.replace("wizforms-","");
      const already=document.getElementById("wizdetail-"+sid);
      const inView = already && already.getBoundingClientRect().top >= 0 &&
                     already.getBoundingClientRect().bottom <= window.innerHeight;
      wizExpand(sid, "ddns", {forceOpen:true, scroll: !inView});
      clearInterval(iv);
    } else if(++tries>20){ clearInterval(iv); }
  }, 150);
}
async function chaseRemoteAccess(){
  // Chase DDNS during setup: read-only check of the EXACT prerequisites guest invites demand
  // (HTTPS + Dynamic DNS + remote access). When they're missing, surface a prominent card naming
  // the specific gaps and pointing at the DDNS form. When ready, the placeholder stays empty.
  const box=document.getElementById("remoteChase");
  if(!box) return;
  let r;
  try{ r=await j("/cockpit/guests/readiness",{},6000); }catch(e){ box.innerHTML=""; return; }
  if(!r || r.ok){ box.innerHTML=""; return; }
  const probs=Array.isArray(r.problems)?r.problems:[];
  const codes=probs.map(p=>p&&p.code).filter(Boolean);
  const has=c=>codes.indexOf(c)>=0;
  const rows=[];
  if(has("no_dynamic_dns")) rows.push(["Dynamic DNS","A stable public hostname so your phone still finds home when your broadband IP changes. This is the keystone, set it up first."]);
  if(has("no_https")) rows.push(["HTTPS","A secure https:// address (on by default now) so guest links and remote sessions are encrypted."]);
  if(has("remote_restart_needed")) rows.push(["Remote access","Configured and saved, this Mongoose process just hasn't picked it up yet."]);
  if(has("server_not_network_bound")) rows.push(["Remote access","Turn on reaching Mongoose from outside your home Wi-Fi."]);
  if(has("remote_unknown")) rows.push(["Remote access","Mongoose could not confirm the server is reachable from the network."]);
  if(!rows.length) rows.push(["Remote access","A few remote-access prerequisites are still missing."]);
  // The explanation must name the ACTUAL gap, not a generic assumption: if DDNS+HTTPS are already
  // fine (codes not containing either), saying "Dynamic DNS + HTTPS are what let you..." falsely
  // implies those specific things are still missing when the real remaining gap is narrower.
  const ddnsHttpsMissing = has("no_dynamic_dns") || has("no_https");
  const needsRestartOnly = !ddnsHttpsMissing && has("remote_restart_needed") && !has("server_not_network_bound");
  const onlyRemoteToggle = !ddnsHttpsMissing && (has("server_not_network_bound") || has("remote_unknown")) && !has("remote_restart_needed");
  const explain = needsRestartOnly
    ? "Dynamic DNS, HTTPS, and LAN binding are all correctly saved, that part's done. This running Mongoose process just hasn't restarted to pick up the saved settings yet."
    : onlyRemoteToggle
    ? "Dynamic DNS and HTTPS are already set up correctly, that part's done. The one thing left is turning on remote access itself, so guest invite links can be generated."
    : "Dynamic DNS + HTTPS are what let you use Mongoose on your phone away from home, and they are <b>required before any guest invite link can be generated</b>. Until they're in place, guest invites stay locked.";
  const inner=rows.map(p=>`<div class="row"><span class="k"><span class="dot bad"></span>${esc(p[0])}</span><span class="v muted">${esc(p[1])}</span></div>`).join("");
  box.innerHTML=`<div class="card" style="border-color:var(--gold)">
    <h2>Set up remote access, to go anywhere and to invite guests</h2>
    <div class="muted" style="margin-bottom:10px">${explain}</div>
    ${inner}
    <div style="margin-top:12px"><button class="act go" onclick="gotoDdnsSetup()">Set up Dynamic DNS now</button></div></div>`;
}
async function j(url, opts={}, timeoutMs=25000){
  // Resilient fetch+parse with a hard timeout. A missing timeout is what made search/setup actions
  // look like permanent spinners when the server was waiting on OAuth, IMAP, DNS, or a cold model.
  // Attach the cockpit nonce automatically: several GET endpoints are nonce-gated (e.g. whatsapp/state),
  // and calling them via a bare fetch returned 403. Endpoints that don't check it simply ignore it.
  async function once(){
    const ctrl=new AbortController();
    const to=setTimeout(()=>ctrl.abort(), timeoutMs);
    let r;
    try{
      let n = (typeof NONCE!=="undefined" && NONCE) ? NONCE : "";
      if(!n && typeof ensureNonce==="function"){
        try{ n=await ensureNonce(); }catch(_e){}
      }
      const baseHeaders = Object.assign({}, (opts && opts.headers) || {});
      if(n && !baseHeaders["X-Cockpit-Nonce"]) baseHeaders["X-Cockpit-Nonce"]=n;
      r=await fetch(url, Object.assign({}, opts||{}, {headers:baseHeaders, signal:ctrl.signal}));
    } finally { clearTimeout(to); }
    if(!r.ok) throw r.status;
    const txt=await r.text();
    if(!txt) throw "empty response";
    let data; try{ data=JSON.parse(txt); }catch(e){ throw "bad json"; }
    if(data===null || data===undefined) throw "null response";
    return data;
  }
  try{ return await once(); }
  catch(e){
    // Self-heal a stale cockpit nonce after a server restart: the per-process nonce changed, so our
    // cached one 403s. Re-fetch a fresh nonce ONCE and retry, so a restart no longer leaves owner-gated
    // panels stuck on "owner only" until a manual reload.
    if(e===403){
      NONCE=null;
      try{ if(typeof ensureNonce==="function") await ensureNonce(); }catch(_e){}
      return await once();
    }
    if(typeof e==="number" && e>=400 && e<500) throw e;
    await new Promise(r=>setTimeout(r,400));
    return await once();
  }
}
function humanizeKey(key){
  return String(key||"").replace(/_/g," ").replace(/\b\w/g,c=>c.toUpperCase());
}
async function toggleCloudEscalation(btn,enable){
  btn.disabled=true; const orig=btn.textContent; btn.textContent="...";
  try{
    const r=await postAction("/cockpit/cloud-escalation",{enabled:!!enable});
    const d=await r.json();
    if(d.ok){ loadOverview(); }
    else { btn.disabled=false; btn.textContent=orig; alert(d.error||"Could not change cloud escalation."); }
  }catch(e){ btn.disabled=false; btn.textContent=orig; alert("Could not reach Mongoose: "+esc(e)); }
}
function dotFor(status){ if(status===true||status==="ok"||status==="ready"||status==="running"||status==="configured")return"ok";
  if(status===false||status==="missing"||status==="stopped")return"bad";
  if(status==="unknown"||status==null)return"unknown"; return"warn"; }
// A failure card with a working Retry button, so a tab that couldn't load (e.g. a transient
// null/timeout under load) is never a dead end. loaderName is the function to re-run.
function retryCard(what, err, loaderName){
  return `<div class="card"><h2>${esc(what)} couldn't load</h2>
    <div class="muted" style="margin-bottom:10px">${esc(String(err||"")) || "The server didn't answer in time."} This is usually transient under load.</div>
    <button class="act go" onclick="(${loaderName})()">Retry</button></div>`;
}
function safeArray(v){ return Array.isArray(v)? v : []; }
function normaliseWizard(wiz){
  wiz = (wiz && typeof wiz === "object")? wiz : {_error:"empty wizard response"};
  const core = (wiz.core_status && typeof wiz.core_status === "object")? wiz.core_status : {ready:false,items:[]};
  const stages = safeArray(wiz.stages).map((stage, si)=>{
    stage = (stage && typeof stage === "object")? stage : {};
    return {
      id: String(stage.id || stage.key || ("s"+si)),
      key: String(stage.key || stage.id || ("s"+si)),
      title: String(stage.title || "Setup"),
      blurb: String(stage.blurb || ""),
      integrations: safeArray(stage.integrations).map(ig=>{
        ig = (ig && typeof ig === "object")? ig : {};
        return Object.assign({slug:"unknown", name:"Unnamed integration", help:"", fields:[], configured:false}, ig,
          {fields:safeArray(ig.fields)});
      })
    };
  });
  return Object.assign({}, wiz, {stages, core_status:Object.assign({ready:false,items:[]}, core, {items:safeArray(core.items)})});
}
function setupDegradedCard(err){
  return `<div class="card" style="border-color:var(--warn)"><h2>Setup loaded in repair mode</h2>
    <div class="muted" style="margin-bottom:10px">One setup source failed, but the tab is still usable. Error: ${esc(String(err||"unknown"))}</div>
    <button class="act go" onclick="loadSetup()">Retry setup</button></div>`;
}
function rows(obj){ return Object.entries(obj).map(([k,v])=>
  `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join(""); }

async function syncMail(){
  const out=document.getElementById("syncout"); if(out)out.textContent="starting sync...";
  let polling=true;
  // While the sync runs (off-thread on the server), poll mail status for the live stage so the user
  // sees "connecting / syncing" instead of a spinner that looks frozen.
  async function pollProgress(){
    while(polling){
      try{
        const s=await j("/cockpit/mail");
        const p=s && s.sync_progress;
        if(p && p.running && out){ out.textContent = (p.message||p.stage||"syncing")+"..."; }
      }catch(e){}
      await new Promise(r=>setTimeout(r,1200));
    }
  }
  pollProgress();
  try{
    const r=await postAction("/cockpit/mail/sync",{},10*60*1000); const d=await r.json();
    polling=false;
    if(out) out.textContent = d.ok? `+${d.new_messages} new (${d.message_count} total)` : ("failed: "+esc(d.error||""));
    if(d.ok) loadOverview();
  }catch(e){ polling=false; if(out)out.textContent="sync failed: "+esc(e); }
}

async function loadSystem(){
  const el=document.getElementById("system");
  try{
    const [m, live, sup, cfg]=await Promise.all([
      j("/cockpit/system-map"),
      j("/cockpit/liveness").catch(()=>null),
      j("/cockpit/supervisor").catch(()=>null),
      j("/cockpit/config-health").catch(()=>null)
    ]);
    let html=`<div class="card" style="padding-bottom:12px">
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
        <img src="/static/img/mongoose-64.png" alt="Mongoose" width="48" height="48"
             style="image-rendering:crisp-edges;flex:0 0 auto;">
        <div>
          <div style="font-size:18px;font-weight:700;letter-spacing:-.01em">Mongoose</div>
          <div class="big" style="margin:2px 0 0"><span class="dot ok"></span>${esc(m.release||m.version||"version unknown")}</div>
          <div class="muted" style="margin-top:4px;font-size:12px">running build on this machine</div>
        </div>
        <div class="sillygoose-wrap" style="margin-left:auto">
          <img class="sillygoose-img about-goose" src="/static/img/sillygoose-256.png" alt="Sillygoose mascot" width="48" height="48">
          <div>
            <div class="sillygoose-label">Codename</div>
            <div class="sillygoose-sub">Sillygoose</div>
          </div>
        </div>
      </div></div>`;
    html+=`<div class="card"><h2>What Mongoose is</h2>
      <div class="muted" style="margin-bottom:6px">${esc(m.headline||"")}</div></div>`;
    // Files & staging: the heavyweight lane for large files (up to 200MB), separate from the quick
    // 30MB chat attachment. Upload here, see what's staged and what each file is available for.
    html+=`<div class="card"><h2>Files &amp; staging</h2>
      <div class="muted" style="font-size:13px;margin-bottom:10px">Upload large files here, up to 200MB. Build packages become available to apply from the Build tab; other files are kept for reference and RTM. For small files, the 📎 in chat handles up to 30MB.</div>
      <input type="file" id="stagefile" style="width:100%;font:inherit;color:var(--text);margin-bottom:8px">
      <button class="act go" id="stagebtn" onclick="uploadStaged(this)" style="width:100%">Upload to staging</button>
      <div id="stageout" class="body" style="font-size:13px;margin-top:10px"></div>
      <div id="stagelist" class="body" style="font-size:13px;margin-top:10px"><span class="muted">Loading staged files…</span></div>
    </div>`;
    // Liveness / rot, is the system actually fresh, or quietly stale? Shown high because a
    // stale subsystem means the brief is working from old data.
    if(live){
      const col = live.healthy? 'var(--ok)' : 'var(--gold-dim)';
      html+=`<div class="card" style="border-color:${col}"><h2>System health</h2>
        <div style="font-size:15px;color:${live.healthy?'var(--ok)':'var(--gold)'};margin-bottom:10px">${esc(live.headline||"")}</div>`;
      for(const c of (live.checks||[])){
        if(c.state==='not_configured') continue;
        const dot = c.state==='ok'?'ok':'warn';
        html+=`<div class="row"><span class="k"><span class="dot ${dot}"></span>${esc(c.subsystem)}
          <div class="muted">${esc(c.detail)}${c.fix?' &middot; fix: '+esc(c.fix):''}</div></span>
          <span class="v muted">${esc(c.state)}</span></div>`;
      }
      html+=`</div>`;
    }
    // Config health, configured settings checked against the ACTUAL machine (the drift the installer/
    // updater never validated): a role pointing at a model you don't have, a feature whose backend is
    // down (web search / voicemail transcriber), a wired-but-off transport. Each row carries the fix.
    if(cfg){
      const issues=cfg.issues||[];
      const col = (cfg.high>0)?'var(--gold)':(issues.length?'var(--gold-dim)':'var(--ok)');
      html+=`<div class="card" style="border-color:${col}"><h2>Config health</h2>
        <div class="muted" style="margin-bottom:8px">Configured settings vs the real machine. ${issues.length?esc(cfg.summary||''):'Everything configured matches what is installed.'}</div>`;
      if(!issues.length){
        html+=`<div class="row"><span class="k"><span class="dot ok"></span>config matches the machine</span></div>`;
      } else {
        for(const i of issues){
          const dot=(i.severity==='low')?'':'warn';
          html+=`<div class="row"><span class="k"><span class="dot ${dot}"></span>${esc(i.key)}
            <div class="muted">${esc(i.detail)}${i.fix?'<br>fix: '+esc(i.fix):''}</div></span>
            <span class="v muted">${esc(i.severity)}</span></div>`;
        }
      }
      html+=`</div>`;
    }
    // Supervisor (watchdog) liveness: if nothing supervises the server, a crash or a failed restart
    // won't auto-recover (the failure that left the phone on "connection refused"). Warn and offer start.
    if(sup && sup.running===false){
      html+=`<div class="card" style="border-color:var(--gold)">
        <h2 style="color:var(--gold)">No supervisor is running</h2>
        <div class="muted" style="margin-bottom:10px">Nothing is watching the server. If Mongoose crashes or a restart fails it won't come back on its own; you would have to start it from the Mac. Starting the supervisor lets it and the server keep each other alive.</div>
        <button onclick="startSupervisor(this)" style="background:var(--gold);color:#1a1407;border:none;border-radius:8px;padding:8px 14px;font-weight:700;cursor:pointer">Start supervisor</button>
      </div>`;
    }
    // Connections, LIVE health of every connector (not "is it configured", but "does it work right
    // now"). Broken-but-configured connectors (token revoked, host down) are surfaced loudly here,
    // because a false green is the whole problem this panel exists to kill.
    html+=`<div class="card"><h2>Connections</h2>
      <div class="muted" style="margin-bottom:8px">Live check: does each connector actually work right now, not just whether it's set up. Rows also show the next real-life step.</div>
      <div id="connBox" class="muted">probing connections...</div></div>`;
    // What leaves this machine, the privacy-critical part, FIRST and prominent.
    const eg=m.egress||{};
    const leaves=eg.anything_leaves;          // leaves on its own (passive)
    const onAction=eg.anything_leaves_on_action;  // leaves only when you act (search)
    const borderCol = leaves? 'var(--gold-dim)' : (onAction? 'var(--gold-dim)' : 'var(--ok)');
    const sumCol = leaves? 'var(--gold)' : (onAction? 'var(--gold)' : 'var(--ok)');
    html+=`<div class="card" style="border-color:${borderCol}">
      <h2>What leaves this machine</h2>
      <div style="font-size:15px;color:${sumCol};margin-bottom:10px">${esc(eg.summary||"")}</div>`;
    for(const f of (eg.flows||[])){
      const out=f.leaves_machine;
      html+=`<div class="row"><span class="k">
        <span class="dot ${out?'warn':'ok'}"></span>${esc(f.name)}
        <div class="muted">${esc(f.note||"")}</div></span>
        <span class="v ${out?'':'muted'}" style="text-align:right">${out?'leaves &rarr; ':'stays local'}${out?esc(f.destination):''}</span></div>`;
    }
    html+=`</div>`;
    // What it holds, real counts.
    html+=`<div class="card"><h2>What it holds (on this machine)</h2>`;
    for(const c of (m.data_categories||[])){
      html+=`<div class="row"><span class="k">${esc(c.category)}<div class="muted">${esc(c.what)}</div></span>
        <span class="v ${c.present?'':'muted'}">${c.present?esc(String(c.count)):'none'}</span></div>`;
    }
    const st=m.storage||{};
    if(st.location){
      const encState = st.encrypted_at_rest===true? 'ok' : (st.encrypted_at_rest===false? 'warn' : 'warn');
      const encLabel = st.encrypted_at_rest===true? 'encrypted at rest' : (st.encrypted_at_rest===false? 'NOT encrypted at rest' : 'at-rest encryption unknown');
      html+=`<div class="muted" style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
        Stored at <span style="font-family:'IBM Plex Mono',monospace;color:var(--text)">${esc(st.location)}</span>
        ${st.size?'('+esc(st.size)+')':''}. ${esc(st.note||"")}</div>
        <div class="row"><span class="k"><span class="dot ${encState}"></span>${esc(encLabel)}
        <div class="muted">${esc(st.at_rest_note||"")}</div></span>
        <span class="v muted">${esc(st.encryption_mechanism||"")}</span></div>`;
    }
    html+=`</div>`;
    // Surfaces.
    html+=`<div class="card"><h2>Surfaces</h2>`;
    for(const s of (m.surfaces||[])){
      html+=`<div class="row"><span class="k"><span class="dot ${s.role==='brain'?'ok':'gold'}"></span><b>${esc(s.name)}</b>
        <div class="muted">${esc((s.can||[]).join(', '))}</div></span>
        <span class="v muted">${esc(s.status||"")}</span></div>`;
    }
    html+=`</div>`;
    // Brain wakeup health, TLS, and lock status: fetched in parallel, rendered if available
    html+=`<div id="sys-extra"></div>`;
    // Merged sections: Models, Benchmarks, and Logs now live inside System as collapsible
    // sections rather than separate top-level tabs (keeps the nav lean).
    html+=`<details class="sys-section"><summary>Models</summary><div id="sys-models"></div></details>`;
    html+=`<details class="sys-section"><summary>Benchmarks &amp; tuning</summary><div id="sys-benchmarks"></div></details>`;
    html+=`<details class="sys-section"><summary>Logs</summary>
      <div class="card"><p class="muted">Durable logs from data/logs (the scheduler's output when run as a service). Tail only, last 64KB.</p>
      <div id="loglist" style="margin-bottom:10px"></div>
      <pre id="logview" style="max-height:420px;overflow:auto">Select a log above.</pre></div></details>`;
    html+=`<details class="sys-section" open><summary>Knowledge &amp; insights</summary><div id="sys-knowledge" class="muted">Loading imports, review queue and insights...</div></details>`;
    html+=`<details class="sys-section"><summary>Data export</summary><div class="card">
      <div class="muted" style="margin-bottom:8px">Download a scoped, owner-only export of your data. Secrets are never included; the broad export redacts private journal wording, so use the journal export if you want that.</div>
      <div style="display:flex;gap:8px;align-items:center"><select id="dumpKind" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text)"></select><button class="act go" onclick="dumpCreate(this)">Create export</button></div>
      <div id="dump-out" class="muted" style="margin-top:8px"></div>
      <div id="dump-list" class="muted" style="margin-top:8px"></div></div></details>`;
    html+=`<details class="sys-section"><summary>Staged self-update</summary><div class="card">
      <div class="muted" style="margin-bottom:8px">Fetch the next version from your configured source and stage it. Safety checks run before anything applies; applying stays a deliberate step (mongoose update apply).</div>
      <button class="act go" onclick="suFetch(this)">Check for an update</button>
      <div id="su-msg" class="muted" style="margin-top:8px"></div>
      <div id="su-stages" class="muted" style="margin-top:8px">Loading&hellip;</div></div></details>`;
    html+=`<details class="sys-section"><summary>What Mongoose suggested</summary><div class="card">
      <div class="muted" style="margin-bottom:8px">Recent proactive nudges and how you rated them. A dismiss backs that nudge off for a good while.</div>
      <div id="sugg-list" class="muted">Loading&hellip;</div></div></details>`;
    el.innerHTML=html;
    if(typeof loadKnowledge==="function") loadKnowledge();
    if(document.getElementById("connBox")) loadConnections();
    loadSystemExtras();
    refreshStagedList();
    // populate the merged sections, pointing the existing loaders at the in-System containers
    try{ loadModels("sys-models"); }catch(e){}
    try{ loadBenchmarks("sys-benchmarks"); }catch(e){}
    try{ loadLogs(); }catch(e){}
    try{ dumpLoadList(); suLoadStages(); suggLoad(); }catch(e){}
  }catch(e){ el.innerHTML=retryCard("System", e, "loadSystem"); }
}
// ---- System manage panel handlers (desktop copy) ----
async function api(path,opts){
  // Desktop shim: the launchpad/phone documents define api() natively; the desktop document's
  // primitives are j() (nonce GET) and postAction() (nonce POST returning a Response).
  opts = opts || {};
  if ((opts.method||'GET').toUpperCase() === 'POST'){
    const body = opts.body ? JSON.parse(opts.body) : {};
    const r = await postAction(path, body, opts.timeoutMs||30000);
    return await r.json();
  }
  return await j(path);
}
async function dumpOpen(id){
  try{
    const d=await api('/cockpit/dump/redeem',{method:'POST',body:JSON.stringify({dump_id:id})});
    if(d && d.ok && d.download_url){ window.open(d.download_url,'_blank'); }
    else { alert((d&&d.error)||'Could not fetch a download link.'); }
  }catch(e){ alert('Download failed.'); }
}
async function suFetch(btn){ btn.disabled=true; const m=document.getElementById('su-msg'); if(m)m.textContent='Checking your update source\u2026';
  try{ const d=await api('/cockpit/update/fetch',{method:'POST',body:JSON.stringify({}),timeoutMs:60000}); if(m)m.textContent=(d&&d.ok)?('Staged '+(d.version||d.stage_id||'a candidate')+'.'):((d&&(d.error||d.detail))||'No update found.'); }
  catch(e){ if(m)m.textContent='Could not reach the update source.'; } finally{ btn.disabled=false; suLoadStages(); } }
async function suLoadStages(){ const el=document.getElementById('su-stages'); if(!el) return;
  try{ const d=await api('/cockpit/update-stage/list',{method:'POST',body:JSON.stringify({})}); const st=(d&&d.stages)||[];
    if(!st.length){ el.innerHTML='<div class="muted">No staged updates.</div>'; return; }
    let h=''; st.forEach(function(s){ const id=s.stage_id||s.id||''; const v=s.version||s.target_version||''; const ap=s.approved?'approved':(s.checks_passed?'checked':'staged');
      h+='<div class="row"><span class="k"><b>'+esc(v||id)+'</b><div class="muted" style="font-size:11px">'+esc(id)+' &middot; '+esc(ap)+'</div></span><span class="v"><button class="act" onclick="suCheck(\''+esc(id)+'\',this)">Check</button> <button class="act" onclick="suDrill(\''+esc(id)+'\',this)">Rollback drill</button> <button class="act go" onclick="suApprove(\''+esc(id)+'\',this)">Approve</button></span></div><div id="su-r-'+esc(id)+'" class="muted" style="font-size:11px;margin:-4px 0 8px"></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load staged updates.'; } }
async function suCheck(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Running checks\u2026';
  try{ const d=await api('/cockpit/update-stage/check',{method:'POST',body:JSON.stringify({stage_id:id}),timeoutMs:120000}); if(r)r.textContent=(d&&d.ok)?'Checks passed.':('Checks: '+JSON.stringify((d&&d.checks)||d).slice(0,150)); }
  catch(e){ if(r)r.textContent='Check failed.'; } finally{ if(btn)btn.disabled=false; } }
async function suDrill(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Dry-running rollback\u2026';
  try{ const d=await api('/cockpit/update-stage/rollback-drill',{method:'POST',body:JSON.stringify({stage_id:id})}); if(r)r.textContent=(d&&d.ok)?'Rollback path verified. You could roll back.':((d&&d.error)||'Rollback drill failed.'); }
  catch(e){ if(r)r.textContent='Rollback drill failed.'; } finally{ if(btn)btn.disabled=false; } }
async function suApprove(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Approving\u2026';
  try{ const d=await api('/cockpit/update-stage/approve',{method:'POST',body:JSON.stringify({stage_id:id})}); if(r)r.textContent=(d&&d.ok)?('Approved. Apply from a terminal with: mongoose update apply '+id):((d&&d.error)||'Approve failed.'); }
  catch(e){ if(r)r.textContent='Approve failed.'; } finally{ if(btn)btn.disabled=false; suLoadStages(); } }
async function dumpLoadList(){ const el=document.getElementById('dump-list'); const sel=document.getElementById('dumpKind');
  try{ const d=await api('/cockpit/dump/list',{method:'POST',body:JSON.stringify({})});
    if(sel && !sel.options.length){ ((d&&d.kinds)||['everything']).forEach(function(k){ const o=document.createElement('option'); o.value=k; o.textContent=k; if(k==='everything')o.selected=true; sel.appendChild(o); }); }
    if(!el) return; const dumps=(d&&d.dumps)||[];
    if(!dumps.length){ el.innerHTML='<div class="muted">No exports yet.</div>'; return; }
    let h='<div class="muted" style="margin-bottom:4px">Recent exports:</div>'; dumps.forEach(function(x){ h+='<div class="row"><span class="k">'+esc(x.kind||'')+'<div class="muted" style="font-size:11px">'+esc((x.created_at||'').slice(0,16))+' &middot; '+Math.round((x.size_bytes||0)/1024)+' KB</div></span><span class="v"><button class="act" onclick="dumpOpen(\''+esc(x.dump_id||'')+'\')">Download</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ if(el)el.textContent='Could not load exports.'; } }
async function dumpCreate(btn){ const sel=document.getElementById('dumpKind'); const kind=(sel&&sel.value)||'everything'; const out=document.getElementById('dump-out'); btn.disabled=true; if(out)out.textContent='Building your export\u2026';
  try{ const d=await api('/cockpit/dump/create',{method:'POST',body:JSON.stringify({kind:kind}),timeoutMs:60000});
    if(d&&d.ok){ const rc=Object.keys(d.row_counts||{}).map(function(k){return k+':'+d.row_counts[k];}).join(', '); if(out)out.innerHTML='Ready. <a href="'+esc(d.download_url||('/dump/'+d.dump_id+'/download'))+'" target="_blank" rel="noopener" style="color:var(--accent)">Download '+esc(d.filename||'export')+'</a><div class="muted" style="font-size:11px;margin-top:4px">'+esc(rc)+'</div>'; }
    else { if(out)out.textContent='Export failed: '+((d&&d.error)||'unknown'); }
  }catch(e){ if(out)out.textContent='Export failed.'; } finally{ btn.disabled=false; dumpLoadList(); } }
async function suggLoad(){ const el=document.getElementById('sugg-list'); if(!el) return;
  try{ const d=await api('/cockpit/suggestions'); const ss=(d&&d.suggestions)||[];
    if(!ss.length){ el.innerHTML='<div class="muted">Nothing surfaced yet.</div>'; return; }
    let h=''; ss.forEach(function(s){ const fb=s.feedback?(' &middot; you said: '+esc(s.feedback)):''; h+='<div class="row"><span class="k">'+esc((s.text||'').slice(0,100))+'<div class="muted" style="font-size:11px">'+esc(s.kind||'')+fb+'</div></span><span class="v"><button class="act" onclick="suggFb(\''+esc(s.signature||'')+'\',\'useful\',this)">Useful</button> <button class="act" onclick="suggFb(\''+esc(s.signature||'')+'\',\'dismissed\',this)">Dismiss</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load suggestions.'; } }
async function suggFb(sig,fb,btn){ if(!sig) return; if(btn)btn.disabled=true;
  try{ await api('/cockpit/suggestions/feedback',{method:'POST',body:JSON.stringify({signature:sig,feedback:fb})}); }catch(e){} finally{ suggLoad(); } }

async function startSupervisor(btn){
  if(btn){ btn.disabled=true; btn.textContent="Starting…"; }
  try{
    const r=await postAction("/cockpit/supervisor/start",{},20000); const d=await r.json();
    if(d && d.ok!==false){ if(btn) btn.textContent="Started"; _reloadSoon(loadSystem, 1400); }
    else { if(btn){ btn.disabled=false; btn.textContent="Start supervisor"; } alert("Could not start supervisor"+(d&&d.error?(": "+d.error):"")); }
  }catch(e){ if(btn){ btn.disabled=false; btn.textContent="Start supervisor"; } }
}
async function loadConnections(){
  const box=document.getElementById("connBox"); if(!box) return;
  try{
    const s=await j("/connectors/status");
    let journey={journeys:[]}; try{ journey=await j("/connectors/journey"); }catch(_e){}
    const byName={}; (journey.journeys||[]).forEach(j=>{ byName[String(j.name||"")]=j; });
    const sum=s.summary||{};
    const label={ollama:"Local brain (Ollama)",home_assistant:"Home Assistant",gmail:"Gmail",
      google_calendar:"Google Calendar",google_drive:"Google Drive (backups)",microsoft:"Microsoft To Do",
      imap:"IMAP mailbox",alexa_bridge:"Alexa fallback"};
    let head="";
    if(sum.broken>0){
      head=`<div style="color:var(--bad);font-weight:600;margin-bottom:8px">⚠ ${sum.broken} connector(s) set up but NOT working: ${(sum.broken_names||[]).map(n=>esc(label[n]||n)).join(", ")}</div>`;
    } else if(sum.usable>0){
      head=`<div style="color:var(--ok);margin-bottom:8px">${sum.usable} connector(s) live and working.</div>`;
    } else {
      head=`<div class="muted" style="margin-bottom:8px">No connectors set up yet.</div>`;
    }
    let rows="";
    for(const c of (s.connectors||[])){
      let dot, state;
      if(!c.configured){ continue; }  // don't clutter with things not set up
      if(c.usable===true){ dot="ok"; state="working"; }
      else if(c.usable===false){ dot="bad"; state="not working"; }
      else { dot="warn"; state="set up (not live-tested)"; }
      const lat=c.latency_ms!=null?` · ${c.latency_ms}ms`:"";
      const jn=byName[c.name]||{};
      const next=jn.next_action?`<div class="muted">Next: ${esc(jn.next_action)}</div>`:"";
      rows+=`<div class="row"><span class="k"><span class="dot ${dot}"></span>${esc(label[c.name]||c.name)}
        <div class="muted">${esc(c.detail||"")}${c.evidence?` · ${esc(c.evidence)}`:""}</div>${next}</span>
        <span class="v muted">${esc(state)}${lat}</span></div>`;
    }
    box.innerHTML=head+(rows||'<div class="muted">Nothing set up to check yet.</div>')
      +`<div style="margin-top:8px"><button class="act" onclick="loadConnections()">Re-check now</button></div>`;
    box.classList.remove("muted");
  }catch(e){ box.innerHTML=`<div class="muted">Connection check unavailable (${esc(e)}).</div>`; }
}
async function loadDevices(){
  const el=document.getElementById("devices");
  try{
    const d=await j("/cockpit/devices");
    let html=`<div class="card"><h2>Add a device</h2>
      <div class="muted" style="margin-bottom:10px">Pair a phone or tablet as a Mongoose surface. The Mac stays the brain; the device gets approvals, voice, and (later) notification ingest. Pick a profile, then scan the QR with the device.</div>
      <button class="act go" onclick="newDevice('primary_phone')">Add phone</button>
      <button class="act" onclick="newDevice('home_tablet')">Add tablet</button>
      <button class="act" id="guestInvite" onclick="showGuestInviteForm()" title="Create a normal HTTPS invite link for a trusted friend. No QR scan needed.">Generate guest URL invite</button>
      <button class="act" onclick="newDevice('trusted_guest')" title="Fallback: short guest QR code.">Add guest QR fallback</button>
      <div id="guestInviteBox" style="margin-top:14px"></div>
      <div id="pwResetBox" style="margin-top:12px;border-top:1px solid var(--line);padding-top:10px">
        <div class="muted" style="font-size:12px;margin-bottom:6px">Someone locked out (for example logged out after adding Mongoose to their home screen)? Reset their password and read them the new one.</div>
        <input id="pwResetUser" placeholder="their username, e.g. sam" autocapitalize="none" spellcheck="false" style="padding:8px;border:1px solid var(--line);border-radius:8px;width:190px">
        <button class="act" onclick="resetGuestPassword()">Reset password</button>
        <div id="pwResetOut" style="margin-top:8px;font-size:13px"></div>
      </div>
      <div id="qrbox" style="margin-top:14px"></div>
      <div class="muted" style="margin-top:14px;font-size:12px;border-top:1px solid var(--line);padding-top:10px">
        <b>On your home Wi-Fi this just works</b>: the QR uses the Mac's local address, no setup needed. Scan it with the phone and you're paired.
        <br><b>Guest URL invites</b> need <b>HTTPS + Dynamic DNS</b> so a friend gets a real link rather than a LAN-only QR. <b>Guest access</b> gives them their own private conversation with no tools and no access to your data.
        <br><span style="opacity:.85">Want to use it away from home too? That's optional: <button class="act" style="margin-left:2px;font-size:12px;padding:4px 10px" onclick="gotoDdnsSetup()">set up remote access →</button> (adds a public address so the QR works anywhere).</span>
      </div></div>`;
    html+=`<div class="card"><h2>Paired devices</h2>`;
    if(!d.devices.length){
      html+=`<div class="muted">No devices paired yet. Add one above. (On a fresh network, the Mac also advertises itself for auto-discovery via the companion app.)</div>`;
    } else {
      for(const dev of d.devices){
        const fresh = dev.freshness||{state:"unknown",detail:""};
        const st = dev.revoked? "bad" : (fresh.state==="stale"?"warn":"ok");
        html+=`<div class="row" style="align-items:flex-start"><span class="k" style="flex:1">
          <span class="dot ${st}"></span><b>${esc(dev.name)}</b>
          <div class="muted">${esc(dev.device_type)} · ${dev.profile==='trusted_guest'?'<span style="color:var(--gold)">guest</span>':esc(dev.profile||"")} · ${dev.revoked?"revoked":esc(fresh.detail||"")}</div>`;
        // per-capability state chips
        html+=`<div style="margin-top:8px">`;
        for(const cs of (dev.capability_status||[])){
          const col = cs.state==="enabled"? "var(--ok)" : (cs.state==="off_by_design"? "var(--dim)" : "var(--gold-dim)");
          const txt = cs.state==="enabled"? "✓" : (cs.state==="off_by_design"? ", " : "○");
          const title = cs.state==="off_by_design"? "off by design for this role" : (cs.state==="available"?"available, not enabled yet":"enabled");
          html+=`<span title="${esc(title)}" style="display:inline-block;font-size:11px;font-family:'IBM Plex Mono',monospace;color:${col};border:1px solid ${col};border-radius:20px;padding:2px 8px;margin:0 4px 4px 0">${txt} ${esc(cs.label)}</span>`;
        }
        html+=`</div>`;
        if(dev.next_step && !dev.revoked){
          html+=`<div class="muted" style="margin-top:6px;color:var(--gold)">→ ${esc(dev.next_step)}</div>`;
        }
        html+=`</span>
          <span class="v">${dev.revoked?'<span class="muted">revoked</span>':`<button class="act no" data-device-id="${esc(dev.device_id)}" onclick="revokeDevice(this.dataset.deviceId)">Revoke</button>`}</span></div>`;
      }
    }
    html+=`</div>`;
    // Same person paired twice? Fold the duplicate into the survivor. Only shown when there
    // are at least two live devices to merge. Same-profile enforcement is on the server.
    const liveDevs=(d.devices||[]).filter(x=>!x.revoked);
    if(liveDevs.length>=2){
      const opts=liveDevs.map(x=>`<option value="${esc(x.device_id)}" data-name="${esc(x.name)}">${esc(x.name)} (${x.profile==='trusted_guest'?'guest':esc(x.profile||'')})</option>`).join('');
      html+=`<div class="card"><h2>Same person, two devices?</h2>
        <div class="muted" style="margin-bottom:10px">If one person ended up paired twice (say they re-paired), merge the duplicate into the one you want to keep. The duplicate's notifications, history and messages move across, then it is revoked. Only devices of the same kind can merge (owner with owner, guest with guest). Nothing is deleted.</div>
        <div class="row"><span class="k" style="flex:1">Fold in (the duplicate)</span><span class="v"><select id="mergeSrc">${opts}</select></span></div>
        <div class="row"><span class="k" style="flex:1">Keep (the survivor)</span><span class="v"><select id="mergeTgt">${opts}</select></span></div>
        <button class="act no" onclick="mergeDevices(this)">Merge duplicate into survivor</button>
        <div id="mergeMsg" class="muted" style="margin-top:8px"></div></div>`;
    }
    // pairing receipts
    if(d.receipts && d.receipts.length){
      html+=`<div class="card"><h2>Pairing receipts</h2>`;
      for(const r of d.receipts.slice(0,10)){
        html+=`<div class="row"><span class="time">${esc((r.at||"").replace("T"," ").slice(0,16))}</span>
          <span class="v muted" style="text-align:left;flex:1">${esc(r.event)} · ${esc(r.device_name||r.device_id||"")}${r.detail?" ("+esc(r.detail)+")":""}</span></div>`;
      }
      html+=`</div>`;
    }

    // Moved here from the System tab: HTTPS, push and router reachability are device
    // concerns, and their only home is now this tab.
    const n=await ensureNonce();
  // TLS / guest HTTPS readiness
  try{
    const t=await fetch("/cockpit/tls/status",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(t && (t.certificate || t.self_signed)){
      const ss=t.self_signed||{};
      const c=t.certificate||{};
      html+=`<div class="card"><h2>HTTPS (unlocks phone voice &amp; notifications)</h2>`;
      if(ss.serving_https){
        html+=`<div class="big"><span class="dot ok"></span>Serving HTTPS</div>`;
        if(ss.expires) html+=`<div class="row"><span class="k">Certificate expires</span><span class="v">${esc(ss.expires)}</span></div>`;
      } else if(ss.present){
        html+=`<div class="big"><span class="dot warn"></span>Certificate ready: currently serving plain http</div>
          <div class="muted" style="font-size:12px;margin-top:4px">HTTPS is on by default, so this usually means Mongoose was started with <span style="font-family:monospace">--no-tls</span> (or MONGOOSE_TLS=off). Restart it normally to serve HTTPS, then open <span style="font-family:monospace">https://${esc(ss.lan_ip||"")}:${esc(String(ss.port||""))}</span> on your phone and accept the one-time security warning. Voice and notifications then work.</div>`;
      } else {
        html+=`<div class="muted" style="font-size:13px;margin-bottom:8px">Phones need HTTPS for the microphone (voice) and for push notifications: a browser security rule. One click generates a certificate for this Mac, no command line needed.</div>
          <button class="act go" onclick="genSelfSigned()">Set up HTTPS (one click)</button>
          <div id="tls-out" class="muted" style="margin-top:8px;font-size:12px"></div>`;
      }
      html+=`</div>`;
    }
  }catch(e){}

  // Web Push status
  try{
    const p=await fetch("/cockpit/push/status",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(p && p.ok){
      const saved = p.settings_saved || p.vapid_ready;
      const testLine = p.last_test_ok_at
        ? '<span class="ok">✓ delivered</span> <span class="muted" style="font-size:11px">('+esc(String(p.last_test_ok_at).slice(0,10))+')</span>'
        : (p.last_test_at ? '<span style="color:var(--warn)">last test reached no device</span>' : '<span class="muted">not tested yet</span>');
      html+=`<div class="card"><h2>Push notifications</h2>
        <div class="muted" style="font-size:12px;margin-bottom:8px">Three separate things: whether the server can send push at all, how many devices are subscribed, and whether a test actually arrived. Each phone turns notifications on from its own launchpad Status tab.</div>
        <div class="row"><span class="k">Notifications set up</span><span class="v">${saved?'<span class="ok">✓ yes</span>':'<span style="color:var(--warn)">not yet</span>'}</span></div>
        <div class="row"><span class="k">Subscribed devices</span><span class="v">${esc(String(p.subscriptions||0))}</span></div>
        <div class="row"><span class="k">Last test</span><span class="v">${testLine}</span></div>`;
      if(p.subscriptions>0){
        html+=`<button class="act" onclick="testPush()" style="margin-top:8px">Send a test notification</button>
          <span id="push-test-out" class="muted" style="margin-left:8px;font-size:12px"></span>`;
      } else if(saved){
        html+=`<div class="muted" style="font-size:12px;margin-top:8px">No devices subscribed yet. Open the launchpad on a phone and turn notifications on there.</div>`;
      }
      html+=`</div>`;
    }
  }catch(e){}
  // Remote access / router (DDNS + port forwarding)
  try{
    const rt=await fetch("/cockpit/remote-access/router",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    // FIX: this card checked for rt.ddns/rt.ports/rt.headline, which port_forward_card()
    // never actually produced, so it could never render. Now driven by the real setup_state
    // remote_away diagnosis (same one the pairing page and phone repair already use), with
    // a working Repair button instead of a dead status display.
    const ra = (rt && rt.remote_away) || {};
    if(rt && rt.ok && (ra.status || (rt.fields))){
      const dotClass = ra.ok ? "ok" : (ra.severity==="warn" ? "warn" : "info");
      html+=`<div class="card"><h2>Remote access</h2>`;
      html+=`<div class="row"><span class="k"><span class="dot ${dotClass}"></span>Status</span>`
           +`<span class="v muted">${esc(ra.detail || ra.status || "not configured")}</span></div>`;
      if(ra.facts && ra.facts.public_url) html+=`<div class="row"><span class="k">Address</span><span class="v">${esc(ra.facts.public_url)}</span></div>`;
      if(!ra.ok && ra.status && ra.status !== "not_configured"){
        html+=`<div id="raRepairBox" style="margin-top:8px">`
             +`<button class="act" onclick="repairRemoteAccess(this)">Repair now</button></div>`;
      }
      if(rt.fields && !ra.ok){
        html+=`<div class="muted" style="font-size:12px;margin-top:8px">Router fields, if a manual port-forward is what's missing:</div>`;
        for(const [k,v] of Object.entries(rt.fields)) html+=`<div class="row"><span class="k">${esc(k)}</span><span class="v">${esc(String(v))}</span></div>`;
      }
      html+=`</div>`;
    }
  }catch(e){}
  // Guest access via Cloudflare Tunnel: the no-terminal journey. Each stage renders as one
  // button; the only human step is Cloudflare's own browser approval, shown as a link.
  try{
    const ts=await fetch("/cockpit/tunnel/state",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(ts && ts.ok){
      const st=ts.state||{}, proof=ts.proof;
      html+=`<div class="card"><h2>Guest access (Cloudflare Tunnel)</h2>`;
      html+=`<div class="muted" style="font-size:12px;margin-bottom:8px">A normal https link guests can open on any phone. Works behind CGNAT; no port-forward, no app for guests.</div>`;
      if(proof && proof.ok){
        html+=`<div class="row"><span class="k"><span class="dot ok"></span>Live</span><span class="v">https://${esc(st.hostname)}</span></div>`
             +`<div class="muted" style="font-size:12px;margin-top:6px">Proven end-to-end over the public internet: the same path a guest's phone uses.</div>`;
        if(!st.reboot_persistent){
          html+=`<div style="margin-top:8px;padding:8px 10px;border:1px solid var(--warn,#d8a200);border-radius:8px;font-size:12px;color:var(--dim)">`
               +`<b style="color:var(--text)">Won't survive a restart yet:</b> nothing currently starts Mongoose after this Mac reboots, so the tunnel would stay down too. `
               +`<button class="act" style="margin-top:6px" onclick="serviceInstall(this)">Start Mongoose automatically after reboot</button></div>`;
        }
      } else if(st.configured){
        html+=`<div class="row"><span class="k"><span class="dot warn"></span>Configured, not reachable yet</span><span class="v muted">${esc((proof&&proof.error)||"cloudflared may not be running")}</span></div>`
             +`<div style="margin-top:8px"><button class="act" onclick="tunnelProvision(this, '${esc(st.hostname)}')">Repair tunnel</button></div>`;
      } else if(!st.installed){
        html+=`<div class="row"><span class="k"><span class="dot info"></span>Step 1 of 3</span><span class="v muted">Get the connector (downloads automatically, ~20s)</span></div>`
             +`<div style="margin-top:8px"><button class="act" onclick="tunnelInstall(this)">Set up guest access</button></div>`;
      } else if(!st.logged_in){
        html+=`<div class="row"><span class="k"><span class="dot info"></span>Step 2 of 3</span><span class="v muted">Approve this Mac in your Cloudflare account</span></div>`
             +`<div id="tunnelLoginBox" style="margin-top:8px"><button class="act" onclick="tunnelLogin(this)">Connect Cloudflare account</button></div>`;
      } else {
        html+=`<div class="row"><span class="k"><span class="dot info"></span>Step 3 of 3</span><span class="v muted">Choose the address guests will use</span></div>`
             +`<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">`
             +`<input id="tunnelHost" placeholder="mongoose.yourdomain.com" style="flex:1;min-width:220px;padding:8px 10px;border-radius:8px;border:1px solid var(--line,#444);background:transparent;color:var(--text)">`
             +`<button class="act" onclick="tunnelProvision(this, document.getElementById('tunnelHost').value)">Turn on</button></div>`
             +`<div class="muted" style="font-size:12px;margin-top:6px">Use a subdomain of a domain that's on Cloudflare DNS (e.g. mongoose.{{site.domain}}). Your existing website is untouched: Mongoose only adds one subdomain record. If the domain isn't on Cloudflare yet, the Turn on step will tell you exactly how to move it (free, one-time, existing records imported).</div>`;
      }
      html+=`<div id="tunnelMsg"></div></div>`;
    }
  }catch(e){}

    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Devices", e, "loadDevices"); }
}
async function resetGuestPassword(){
  const el=document.getElementById('pwResetUser'); const out=document.getElementById('pwResetOut');
  const u=((el&&el.value)||'').trim();
  if(!u){ if(out) out.textContent='Enter their username first.'; return; }
  if(out) out.textContent='Resetting...';
  try{
    const d=await api('/cockpit/guests/reset-password',{method:'POST',body:JSON.stringify({username:u})});
    if(d&&d.ok){ out.innerHTML='New password for <b>'+esc(d.username||u)+'</b>: <code style="font-size:15px">'+esc(d.password)+'</code><br><span class="muted">Read this to them once. They sign in with their username and this password.</span>'; }
    else { out.textContent=(d&&d.error)||'Could not reset that account.'; }
  }catch(e){ if(out) out.textContent='Reset failed. Try again.'; }
}
function showGuestInviteForm(){
  const box=document.getElementById("guestInviteBox"); if(!box) return;
  box.innerHTML=`<div style="border:1px solid var(--line);border-radius:12px;padding:14px;background:rgba(255,255,255,.025)">
    <div style="font-weight:650;margin-bottom:4px">Generate a guest URL invite</div>
    <div class="muted" style="font-size:12px;margin-bottom:10px">This creates a single-use HTTPS/DDNS link. What you call the guest is remembered internally and never placed in the URL.</div>
    <label style="display:block;font-size:12px;color:var(--dim);margin:8px 0 4px">What do you call this guest internally?</label>
    <input id="guestInviteLabel" class="q" type="text" placeholder="e.g. Sam, Mum, Toby's favourite human" style="width:100%;background:#141109;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px">
    <label style="display:block;font-size:12px;color:var(--dim);margin:8px 0 4px">Name they'll see (they can change it)</label>
    <input id="guestInviteName" class="q" type="text" placeholder="Prefills their name on the invite; defaults to the label above" style="width:100%;background:#141109;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px">
      <div><label style="display:block;font-size:12px;color:var(--dim);margin:0 0 4px">Valid for days</label><input id="guestInviteDays" class="q" type="number" min="1" max="30" value="5" style="width:100%;background:#141109;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px"></div>
      <div><label style="display:block;font-size:12px;color:var(--dim);margin:0 0 4px">Minimum Mongoose version</label><input id="guestInviteVersion" class="q" type="text" value="1.1" style="width:100%;background:#141109;color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px"></div>
    </div>
    <button class="act go" style="margin-top:12px" onclick="makeGuestInvite(this)">Create invite link</button>
    <div id="guestInviteResult" style="margin-top:10px"></div>
  </div>`;
  setTimeout(()=>{ const i=document.getElementById("guestInviteLabel"); if(i)i.focus(); },50);
}
async function makeGuestInvite(btn){
  const out=document.getElementById("guestInviteResult");
  const owner_label=(document.getElementById("guestInviteLabel")||{}).value||"";
  const valid_days=(document.getElementById("guestInviteDays")||{}).value||"5";
  const min_version=(document.getElementById("guestInviteVersion")||{}).value||"1.1";
  const suggested_name=((document.getElementById("guestInviteName")||{}).value||"").trim();
  if(!owner_label.trim()){ if(out)out.innerHTML='<span class="bad">Give the guest an internal label first.</span>'; return; }
  if(btn){ btn.disabled=true; btn.textContent="Creating..."; }
  if(out) out.innerHTML='<div class="muted">Checking HTTPS, Dynamic DNS, and invite settings...</div>';
  try{
    const r=await postAction("/cockpit/guests/invite",{owner_label, valid_days, min_version, suggested_name});
    const d=await r.json();
    if(!d.ok){
      const problems=(d.problems||[]).map(p=>`<li><b>${esc(p.code||"setup")}</b>: ${esc(p.message||"")}<div class="muted">${esc(p.action||"")}</div></li>`).join("");
      if(out) out.innerHTML=`<div style="color:var(--gold);font-size:13px">Guest invites are not safe to send yet.</div><ul style="margin:8px 0 0 18px;padding:0;color:var(--dim);font-size:12px">${problems||'<li>'+esc(d.message||d.error||'Not ready')+'</li>'}</ul><button class="act" style="margin-top:10px" onclick="gotoDdnsSetup()">Open remote-access setup →</button>`;
      return;
    }
    if(out) out.innerHTML=`<div style="color:var(--ok);font-weight:650">Invite ready.</div>
      <div class="tok" style="font-family:'IBM Plex Mono',monospace;font-size:12px;word-break:break-all;background:#141109;border:1px solid var(--line);border-radius:9px;padding:10px;margin-top:6px">${esc(d.invite_url)}</div>
      <div class="muted" style="font-size:12px;margin-top:6px">Expires ${esc((d.expires_at||'').replace('T',' ').slice(0,16))} · minimum Mongoose ${esc(d.min_version||'1.1')} · HTTPS/DDNS required ✓</div>
      <button class="act" style="margin-top:8px" onclick="navigator.clipboard&&navigator.clipboard.writeText('${String(d.invite_url||'').replace(/'/g,"\'")}')">Copy invite link</button>`;
  }catch(e){ if(out) out.innerHTML='<span class="bad">Invite failed: '+esc(e)+'</span>'; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Create invite link"; } }
}

async function newDevice(profile){
  const box=document.getElementById("qrbox");
  // Show spinner immediately
  box.innerHTML=`<div style="display:flex;flex-direction:column;align-items:center;gap:10px;padding:20px">
    <svg width="36" height="36" viewBox="0 0 40 40" fill="none"><circle cx="20" cy="20" r="16" stroke="rgba(255,255,255,0.15)" stroke-width="4"/>
    <path d="M20 4a16 16 0 0 1 16 16" stroke="var(--gold)" stroke-width="4" stroke-linecap="round"><animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20" dur="0.9s" repeatCount="indefinite"/></path></svg>
    <div class="muted" style="font-size:12px">${profile==='trusted_guest'?'Creating guest access code…':'Creating pairing code…'}</div>
  </div>`;
  try{
    const r=await postAction("/cockpit/devices/pair-code",{profile,auto_repair:true}); const d=await r.json();
    if(!d.code){ box.innerHTML='<span class="bad">Could not create a pairing code.</span>'; return; }
    const mins=Math.round((d.expires_in||300)/60);
    // Show DDNS status for all profiles: owners can scan from outside home too
    // DDNS uses the right ADDRESS, but if the server isn't actually listening on the network
    // interface (reachability_warning below), that address can't be reached yet either -- same
    // missing listener, whether the traffic arrives via LAN or port-forwarded DDNS. Don't claim
    // "scannable from anywhere" when it very likely is not, yet.
    const ddnsLine = d.ddns_used
     ? (d.reachability_warning
        ? `<div class="muted" style="font-size:12px;margin-top:6px">DDNS address is set correctly, but won't be reachable until the server is listening on the network (see below).</div>`
        : `<div class="muted" style="font-size:12px;margin-top:6px">✓ QR uses your DDNS address, scannable from anywhere.</div>`)
      : (d.ddns_hint===null? '' : `<div style="color:var(--gold);font-size:12px;margin-top:6px">ℹ Set up DDNS (Setup tab → Remote access) to make this QR work from outside home.</div>`);
    const guestNote = (profile==='trusted_guest'
     ? '<div class="muted" style="font-size:12px;margin-top:4px">Guest: own private conversation · no tools · no access to your data.</div>'
      : '') + ddnsLine;
    const qrBlocked = !!d.qr_blocked;
    let qr = d.qr_svg? `<div style="background:#fff;display:inline-block;padding:10px;border-radius:10px;max-width:230px">${d.qr_svg}</div>`
                      : (qrBlocked? '' : `<div class="muted">QR unavailable (install 'qrcode' on the Mac). Open this on the device:</div>`);
    const reachMsg = d.reachability_warning==="not_lan_reachable"
      ? "The QR address is right, but this running server is not reachable from the network yet."
      : d.reachability_warning;
    const ar = d.auto_repair||{};
    // DEFENSE IN DEPTH (external triage): blocking the QR was necessary but not the fix.
    // Mongoose already tried to repair this (auto_repair:true above); show that plainly and
    // poll for it coming back, rather than dumping the owner straight into manual buttons.
    const warn = d.reachability_warning? `<div id="dev-reachwarn" style="margin-top:10px;padding:8px 10px;border:1px solid var(--warn);border-radius:8px;background:rgba(255,176,0,.08);color:var(--warn);font-size:12px">⚠ ${esc(reachMsg)}
      ${ar.restarting
        ? `<div style="margin-top:8px;color:var(--ok)">Mongoose is repairing this now: applying LAN settings and restarting the listener.</div><div id="dev-restartwait" class="muted" style="margin-top:6px;font-size:11px">Waiting for Mongoose to come back...</div>`
        : `<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
            <button onclick="enablePhone(this,false)" style="background:var(--gold);color:#1a1a1a;border:none;border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer">Enable LAN access</button>
            <button onclick="enablePhone(this,true)" style="background:transparent;color:var(--gold);border:1px solid var(--gold-dim);border-radius:8px;padding:7px 12px;font-weight:600;cursor:pointer">Repair / restart</button>
          </div>
          <div class="muted" style="margin-top:6px;font-size:11px">If the phone gets "connection refused", Mongoose is still listening only on this Mac. Repair / restart updates the saved host settings and restarts onto the LAN.</div>`}</div>` : "";
    if(ar.restarting){ setTimeout(()=>waitForMongooseThenReload("dev-restartwait"), 400); }
    const altLinks = (d.alternate_pair_urls||[]).map(a =>
      `<div class="muted" style="margin-top:8px;font-size:12px">${esc(a.label||"Alternate route")}:<div class="tok" style="font-family:'IBM Plex Mono',monospace;font-size:12px;word-break:break-all;background:#141109;border:1px solid var(--line);border-radius:9px;padding:8px;margin-top:4px">${esc(a.url||"")}</div><div style="font-size:11px;margin-top:3px">${esc(a.note||"")}</div></div>`
    ).join('');
    const openBlock = qrBlocked ? '' : `<div class="muted" style="margin-top:10px">Scan with the device's camera, or open:</div>
      <div class="tok" style="font-family:'IBM Plex Mono',monospace;font-size:12px;word-break:break-all;background:#141109;border:1px solid var(--line);border-radius:9px;padding:10px;margin-top:6px">${esc(d.pair_url)}</div>${altLinks}`;
    box.innerHTML=`${qr}${guestNote||''}
      ${warn}
      ${openBlock}
      <div class="muted" style="margin-top:8px">Code expires in ${mins} min · fingerprint ${esc(d.fingerprint)} · profile ${esc(d.profile)}</div>`;
    // Single-use: the moment a device claims this code, stop showing a scannable QR. Poll the
    // code's status; on consumption swap the QR for a confirmation so a second person can't scan
    // a dead code, and the owner sees it landed. A newer code (dataset change) cancels this poll.
    const myCode=d.code; box.dataset.pairCode=myCode;
    (async function pollPair(){
      await new Promise(res=>setTimeout(res,2500));
      if(box.dataset.pairCode!==myCode) return;            // superseded by a newer code
      let s=null; try{ s=await j("/cockpit/devices/pair-status?code="+encodeURIComponent(myCode)); }
      catch(e){ if(box.dataset.pairCode===myCode) pollPair(); return; }
      if(box.dataset.pairCode!==myCode) return;
      if(s && s.consumed){
        box.dataset.pairCode='';
        box.innerHTML='<div style="text-align:center;padding:22px 16px">'
          +'<div style="font-size:34px;color:var(--ok);line-height:1">✓</div>'
          +'<div style="font-weight:600;margin-top:8px">This device paired.</div>'
          +'<div class="muted" style="font-size:13px;margin-top:4px">The code is now used up. To add another device, create a new one.</div>'
          +'<button class="act" style="margin-top:14px" onclick="loadDevices()">Done</button></div>';
        try{ loadDevices(); }catch(e){}
        return;
      }
      if(s && (s.found===false || (s.expired && !s.consumed))){
        box.dataset.pairCode='';
        box.innerHTML='<div class="muted" style="padding:16px">This code expired before any device used it. Add a device again to get a fresh code.</div>';
        return;
      }
      pollPair();
    })();
  }catch(e){ box.innerHTML='<span class="bad">Failed: '+esc(e)+'</span>'; }
}
async function revokeDevice(id){
  if(!confirm("Revoke this device? Its access stops immediately.")) return;
  try{ await postActionOk("/cockpit/devices/revoke",{device_id:id}); loadDevices(); }
  catch(e){ alert("Revoke did NOT complete: "+(e&&e.message||e)+". The device may still have access, please check."); }
}
async function mergeDevices(btn){
  const src=document.getElementById("mergeSrc"), tgt=document.getElementById("mergeTgt");
  if(!src||!tgt) return;
  const sId=src.value, tId=tgt.value;
  const sName=src.options[src.selectedIndex].dataset.name||"";
  const tName=tgt.options[tgt.selectedIndex].dataset.name||"";
  const msg=document.getElementById("mergeMsg"); if(msg) msg.textContent="";
  if(sId===tId){ if(msg) msg.textContent="Pick two different devices."; return; }
  if(!confirm('Fold "'+sName+'" into "'+tName+'"?\n\n"'+sName+'" will be revoked. Its notifications, history and messages move to "'+tName+'". This is not easily undone.')) return;
  btn.disabled=true;
  try{ await postActionOk("/cockpit/devices/merge",{source_id:sId,target_id:tId,confirm_source_name:sName,confirm_target_name:tName}); loadDevices(); }
  catch(e){ btn.disabled=false; if(msg) msg.textContent="Merge did NOT complete: "+(e&&e.message||e); }
}
async function toggleBenchmark(want,btn){
  const msg=document.getElementById("benchToggleMsg"); if(msg) msg.textContent="";
  if(btn) btn.disabled=true;
  try{
    await postActionOk("/cockpit/benchmarks/enable",{enabled:want});
    if(msg) msg.textContent = want ? "On. The first run happens overnight (5-8am), not now." : "Off.";
    setTimeout(()=>{ try{ loadBenchmarks("sys-benchmarks"); }catch(_e){} }, 600);
  }catch(e){ if(btn) btn.disabled=false; if(msg) msg.textContent="Could not change it: "+(e&&e.message||e); }
}

async function prepareNow(){
  const topic=(document.getElementById("prepTopic")||{}).value||"today";
  const box=document.getElementById("preparebox"); if(box)box.textContent="Gathering local context...";
  try{
    const d=await j("/cockpit/prepare?q="+encodeURIComponent(topic));
    const ctx=d.useful_context||[]; const limits=d.known_limits||[];
    box.innerHTML=`<div style="color:var(--text)">${esc(d.why_now||d.topic||topic)}</div>
      <div class="muted" style="margin-top:6px">next: ${esc(d.suggested_next_step||"")}</div>`+
      (ctx.length?`<div style="margin-top:8px">${ctx.slice(0,5).map(x=>`<div class="row"><span class="v" style="text-align:left;flex:1">• ${esc(x)}</span></div>`).join("")}</div>`:
        `<div class="muted" style="margin-top:8px">No local evidence yet.</div>`)+
      (limits.length?`<div class="muted" style="margin-top:8px;font-size:12px">${limits.map(esc).join(" · ")}</div>`:"");
  }catch(e){ box.textContent="Could not prepare ("+esc(e)+")."; }
}

async function askSurprise(){
  const box=document.getElementById("surprisebox"); if(box)box.textContent="Let me look across everything...";
  try{
    const d=await j("/cockpit/suggest");
    if(!d.surfaced){ box.textContent="Nothing worth interrupting you for right now ("+esc(d.reason||"")+")."; return; }
    const s=d.suggestion;
    box.innerHTML=`<div style="color:var(--text)">${esc(s.text)}</div>
      <div class="muted" style="margin-top:4px;font-size:12px">why: ${esc(s.why)}</div>
      <div style="margin-top:8px">
        <button class="act" onclick="suggestFeedback('${esc(s.signature)}','useful',this)">Useful</button>
        <button class="act no" onclick="suggestFeedback('${esc(s.signature)}','not_useful',this)">Not useful</button></div>`;
  }catch(e){ box.textContent="Could not check ("+esc(e)+")."; }
}
async function suggestFeedback(sig, fb, btn){
  try{ await postAction("/cockpit/suggest/feedback",{signature:sig,feedback:fb});
    const p=btn.parentElement; if(p)p.innerHTML='<span class="muted">Thanks, noted.</span>';
  }catch(e){ /* non-critical */ }
}
async function loadOverview(){
  const el=document.getElementById("overview");
  try{
    const [doc,sys,release,dop,tune,rdy] = await Promise.all([
      j("/doctor"),
      j("/system/status").catch(()=>({})),
      j("/release-check").catch(()=>null),
      j("/cockpit/daily").catch(()=>null),
      j("/cockpit/tuning").catch(()=>null),
      j("/cockpit/readiness").catch(()=>null)]);
    const alertsData = await j("/cockpit/alerts").catch(()=>null);
    const tutData = await j("/cockpit/tutorial").catch(()=>null);
    const checks = doc.checks||{};
    const aliveDot = doc.ok!==false? "ok":"warn";
    let html = "";
    // ===== 0002 brief-led Overview: the cross-domain "Today" picture is the USP, so it paints FIRST,
    // as a hero, before any health/diagnostic card. Built from data already fetched (dop). The
    // time-sensitive spans (next step, connections) are highlighted so the eye lands on what matters.
    if(dop){
      const shape=dop.todays_shape||{};
      const evs=shape.events_ahead||[];
      const links=dop.cross_domain_links||[];
      const step=dop.next_smallest_step||"";
      let hero = `<div class="hero"><h2>Today${shape.date?" · "+esc(shape.date):""}</h2><p class="brieftext">`;
      const bits=[];
      if(shape.weather) bits.push(esc(shape.weather));
      if(evs.length){
        const ev=evs[0];
        const when=ev.when||(ev.all_day?"all day":"");
        bits.push(`${when?'<span class="hl">'+esc(when)+'</span> ':''}${esc(ev.title||"")}` +
                  (evs.length>1?` <span class="hl">+${evs.length-1} more</span>`:""));
      } else { bits.push("No timed events ahead today."); }
      if(step) bits.push(`Next smallest step: <span class="hl">${esc(step)}</span>`);
      hero += bits.join(". ") + `</p>`;
      if(links.length){
        hero += `<p class="brieftext" style="font-size:15px;margin-top:12px;color:var(--dim)">`;
        hero += links.slice(0,3).map(l=>"• "+esc(l.text)).join("<br>");
        hero += `</p>`;
      }
      hero += `<div class="brieffoot">cross-domain brief · the synthesis Mongoose exists for</div></div>`;
      html += hero;
    }
    // ===== 0003 scannable status strip: connector/health state as one at-a-glance row of chips,
    // instead of a long stack of rows. Built from the doctor checks already fetched.
    {
      const keys=Object.keys(checks);
      if(keys.length){
        html += `<div class="statrip">`;
        html += `<span class="stat"><span class="dot ${aliveDot}"></span><b>Mongoose</b><em>${doc.ok!==false?"alive":"degraded"}</em></span>`;
        for(const key of keys){
          const c=checks[key]; const ok=(c&&c.ok);
          const label = humanizeKey(key);
          const detail = (c&&c.detail)||"";
          let extra="";
          if(key==="cloud_escalation"){
            // The one chip in this strip with an actual missing lever: this was previously an
            // env-only setting with no UI control anywhere. Configuring OpenAI/Gemini keys does
            // NOT turn it on (deliberate safety gate); give the real toggle right where the
            // status is shown, instead of leaving it as a terminal-only setting.
            extra = ok
              ? `<button class="act" style="margin-left:8px;font-size:11px;padding:2px 8px" onclick="toggleCloudEscalation(this,false)">turn off</button>`
              : `<button class="act go" style="margin-left:8px;font-size:11px;padding:2px 8px" onclick="toggleCloudEscalation(this,true)">turn on</button>`;
          }
          html += `<span class="stat"><span class="dot ${dotFor(ok)}"></span><b>${esc(label)}</b>${detail?` <em>${esc(detail)}</em>`:""}${extra}</span>`;
        }
        html += `</div>`;
      }
    }
    // First-run tutorial card: Mongoose introducing itself + tappable examples. Dismissible.
    if(tutData && tutData.try_now && !window.__tutDismissed){
      html += `<div class="card" style="border-color:var(--gold-dim)">
        <h2>${esc(tutData.headline)} <button class="act" style="float:right;font-size:12px" onclick="window.__tutDismissed=true;loadOverview()">Dismiss</button></h2>
        <div class="muted" style="margin-bottom:10px">${esc(tutData.subhead)}</div>
        <div class="muted" style="font-size:13px;margin-bottom:4px">Try saying:</div>`;
      for(const x of tutData.try_now.slice(0,4)){
        html += `<div style="font-family:'IBM Plex Mono',monospace;font-size:13px;margin:4px 0;color:var(--gold)">"${esc(x.try)}"</div>`;
      }
      if(tutData.unlocks && tutData.unlocks.length){
        const u=tutData.unlocks.map(x=>esc(x.unlocks_with||x.id)).filter((v,i,a)=>a.indexOf(v)===i);
        html += `<div class="muted" style="font-size:12px;margin-top:10px;opacity:.75">Connect more to unlock: ${u.join(" · ")}</div>`;
      }
      html += `<button class="act go" style="margin-top:12px" onclick="doSomethingCool()">Do something cool</button>
        <div id="coolout" style="margin-top:10px"></div></div>`;
    }
    // What can I use now?, the readiness truth layer as a glanceable card. Honest: shows
    // exactly what's ready, what needs you, and what's just not set up. Escapes all labels.
    if(rdy && rdy.items){
      const sev=(it)=>({critical:3,warning:2,info:1,ok:0,none:0})[it.severity]||0;
      const attention=rdy.items.filter(it=>it.severity==="critical"||it.severity==="warning")
                               .sort((a,b)=>sev(b)-sev(a));
      const working=rdy.items.filter(it=>it.status==="ready"||it.status==="has_data");
      const head={ready:"Core brain up, nothing configured is failing",degraded:"Usable, with gaps",not_ready:"Not ready for daily use"}[rdy.overall]||"Readiness";
      const dotc=rdy.overall==="ready"?"ok":(rdy.overall==="not_ready"?"bad":"warn");
      html += `<div class="card"><h2>What can I use now? <button class="act" style="float:right;font-size:12px" onclick="refreshReadiness()">Check now</button></h2>
        <div class="big"><span class="dot ${dotc}"></span>${esc(head)}</div>`;
      if(attention.length){
        html += `<div class="muted" style="margin:10px 0 6px">Needs you:</div>`;
        for(const it of attention.slice(0,5)){
          const na=it.next_action&&it.next_action.label?`, ${esc(it.next_action.label)}`:"";
          html += `<div style="margin:6px 0;font-size:14px"><b>${esc(it.label)}</b>: ${esc(it.why||"")}${na}</div>`;
        }
      }
      if(working.length){
        html += `<div class="muted" style="margin:12px 0 4px">Ready: ${working.map(it=>esc(it.label)).join(" · ")}</div>`;
      }
      const notset=rdy.items.filter(it=>it.status==="not_configured").map(it=>esc(it.label));
      if(notset.length) html += `<div class="muted" style="font-size:12px;opacity:.7">Not set up: ${notset.join(" · ")}</div>`;
      html += `</div>`;
    }
    // Upcoming alarms / timers / reminders, with cancel + honest flagging of native-unverified.
    if(alertsData && alertsData.count>0){
      html += `<div class="card"><h2>Upcoming alarms & timers</h2>`;
      for(const a of alertsData.alerts.slice(0,8)){
        let when="";
        try{ const d=new Date(a.fire_at); when=d.toLocaleString([], {weekday:"short",hour:"2-digit",minute:"2-digit"}); }catch(e){ when=a.fire_at; }
        const rec=a.recurrence?` (every ${esc(a.recurrence)})`:"";
        const lbl=a.label?(": "+esc(a.label)):"";
        const unv=a.status==="accepted_unverified"?` <span style="color:var(--bad);font-size:12px">unverified</span>`:"";
        html += `<div style="display:flex;justify-content:space-between;align-items:center;margin:6px 0;font-size:14px">
          <span>${esc(a.kind)} ${esc(when)}${rec}${lbl}${unv}</span>
          <button class="act" style="font-size:12px" onclick="cancelAlert('${esc(a.id)}')">Cancel</button></div>`;
      }
      html += `</div>`;
    }
    // Lane 2: a quiet, dismissible tuning card, appears ONLY once tuning has vested (enough
    // real use + benchmark evidence) and there's an actual better setup the user isn't on.
    if(tune && tune.vested && tune.actionable && !tune.dismissed){
      const rec=tune.recommendation||{};
      const proposed=tune.proposed||{};
      const lines=Object.entries(proposed).map(([r,m])=>`${r}: ${esc(m)}`).join(" · ");
      const auto = tune.auto_applied;
      html += `<div class="card" style="border-color:var(--gold-dim)">
        <h2>${auto?"Tuned your models":"A better setup for how you use Mongoose"}</h2>
        <div class="muted" style="margin-bottom:10px">${auto
         ? "Based on real benchmarks, I switched to a clearly better, safe setup. You can undo this."
          : "Enough real use and benchmarks to suggest the best models for you."}</div>
        <div style="font-family:'IBM Plex Mono',monospace;font-size:13px;margin-bottom:12px">${lines}</div>
        <div class="row2">
          ${auto
           ? `<button onclick="tuneAct('revert')">Undo</button>`
            : `<button class="go" onclick="tuneAct('apply')">Switch to this</button>
               <button onclick="tuneAct('dismiss')">Not now</button>`}
        </div></div>`;
    }
    // The cross-domain "Today" picture now leads as the HERO above (0002). Here we keep the
    // remaining DOP cards: on-demand surpriser, prepare-me, the real "Needs you" queue, and changes.
    if(dop){
      // The surpriser, on demand: ask Mongoose if it's noticed anything worth surfacing.
      html += `<div class="card"><h2>Mongoose noticed</h2>
        <div id="surprisebox" class="muted">Ask Mongoose if there's anything worth your attention right now.</div>
        <button class="act go" style="margin-top:8px" onclick="askSurprise()">Anything for me?</button></div>`;
      html += `<div class="card"><h2>Prepare me</h2>
        <div class="muted">Build a compact context card from local calendar, mail, files, WhatsApp and memory.</div>
        <input id="prepTopic" placeholder="event, person, project or tomorrow morning" style="margin-top:8px">
        <button class="act go" style="margin-top:8px" onclick="prepareNow()">Prepare</button>
        <div id="preparebox" class="muted" style="margin-top:8px">Ask for a grounded context pack.</div></div>`;
      // What needs you, the triage queue, now with a real affordance per item (0003). The "act"
      // text is the REAL suggested action from the DOP; no invented actions.
      const needs=dop.what_needs_you||[];
      if(needs.length){
        html += `<div class="card"><h2>Needs you</h2>`;
        for(const n of needs){
          const dt = n.urgency==="now"? "bad" : "warn";
          const act = n.act? `<button class="btn gold" onclick="activateTab('chat')">${esc(n.act)}</button>` : "";
          html += `<div class="needsrow"><span class="nx"><span class="dot ${dt}"></span> <b>${esc(n.text)}</b></span>${act}</div>`;
        }
        html += `</div>`;
      }
      // What changed since you last looked
      const changed=dop.what_changed||[];
      if(changed.length){
        html += `<div class="card"><h2>Since you last looked</h2>`;
        for(const c of changed){ html += `<div class="row"><span class="v" style="text-align:left;flex:1">• ${esc(c)}</span></div>`; }
        html += `</div>`;
      }
    }
    // Release readiness card: make known limits VISIBLE, not hidden in docs.
    if(release){
      const lines=[];
      for(const [title,items] of (release.sections||[])){
        for(const ln of items){ if(ln.state!=="ready") lines.push(ln); }
      }
      const ready = release.verdict==="ready";
      const vmark = ready? "ok" : "warn";
      const verdictText = ready? "ready" : esc(release.verdict||"not ready");
      html += `<div class="card"><h2>Release readiness</h2>
        <div class="big"><span class="dot ${vmark}"></span>${verdictText}</div>`;
      if(!lines.length) html += `<div class="muted">All checks ready.</div>`;
      else { html += `<div class="muted" style="margin-bottom:6px">Known gaps (configure these; not bugs):</div>`;
        for(const ln of lines){
          const d = ln.state==="missing"? "bad" : (ln.state==="unknown"?"unknown":"warn");
          html += `<div class="row"><span class="k"><span class="dot ${d}"></span>${esc(ln.label)}:</span>
                   <span class="v muted">${esc(ln.detail||"")}</span></div>`;
        }
      }
      html += `</div>`;
    }
    // Detailed status is DEMOTED below a Details divider, the at-a-glance strip at the top (0003)
    // is the primary view; this stays reachable for drill-down without leading the page.
    html += `<div style="margin:18px 0 8px;border-top:1px solid var(--line);padding-top:6px"></div>`;
    html += `<div class="card"><h2>Details · status</h2>
      <div class="big"><span class="dot ${aliveDot}"></span>Mongoose is ${doc.ok!==false?"alive":"degraded"}</div>`;
    for(const key of Object.keys(checks)){
      const c=checks[key]; const ok=(c&&c.ok); 
      html+=`<div class="row"><span class="k"><span class="dot ${dotFor(ok)}"></span>${esc(key)}</span>
             <span class="v muted">${esc((c&&c.detail)||"")}</span></div>`;
    }
    html+=`</div>`;
    // Mail sync card: make real-data sync visible + a sync-now action.
    try{
      const mail=await j("/cockpit/mail");
      html+=`<div class="card"><h2>Mail sync</h2>
        <div class="muted" style="margin-bottom:6px">${(mail.message_count||0)} messages indexed</div>`;
      for(const p of (mail.providers||[])){
        const st = p.authorized? "ok" : (p.configured? "warn" : "unknown");
        const label = p.authorized? ("synced "+(p.last_completed||"")) : (p.note||"not set up");
        html+=`<div class="row"><span class="k"><span class="dot ${st}"></span>${esc(p.provider)}</span>
               <span class="v muted">${esc(label)}</span></div>`;
      }
      html+=`<button class="act go" onclick="syncMail()" style="margin-top:8px">Sync now</button>
             <span id="syncout" class="muted" style="margin-left:8px"></span></div>`;
    }catch(e){ /* mail card optional */ }
    // Users & pokes card
    html += `<div class="card"><h2>Users &amp; pokes</h2>
      <div class="muted" style="margin-bottom:10px;font-size:12px">Hold the button to poke someone. They'll get a notification with 👆🪿</div>
      <div id="poke-panel"><div class="muted">Loading...</div></div>
      <div style="margin-top:10px">
        <a href="#" onclick="showPokeSetup();return false" style="font-size:12px;color:var(--gold)">Set your display name &amp; poke preferences</a>
      </div></div>`;
    // Song exchange card
    html += `<div class="card"><h2>🎵 Song exchange</h2>
      <div class="muted" style="margin-bottom:8px;font-size:12px">Mongoose asks users to recommend songs to each other on Sunday evenings. Feedback is collected a day or two later. Use chat: "recommend [song] for [name]" or "feedback [id]: [thoughts]"</div>
      <div id="song-exchange-panel"><div class="muted" style="font-size:12px">Loading...</div></div></div>`;
    document.getElementById("live").textContent = doc.ok!==false? "alive" : "needs attention";
    const pulse=document.getElementById("pulse"); if(pulse) pulse.classList.toggle("warn", doc.ok===false);
    el.innerHTML=html;
    loadPokePanel();
    loadSongExchangePanel();
  }catch(e){ el.innerHTML=`<div class="card">Could not reach Mongoose (${esc(e)}). Is it running?</div>`; }
}

// A small, tasteful win moment when something connects, a brief gold pulse, not confetti spam.
function celebrateSetup(root){
  try{
    if(root){ root.style.transition="box-shadow .5s"; const o=root.style.boxShadow;
      root.style.boxShadow="0 0 0 2px var(--gold)"; setTimeout(()=>{ root.style.boxShadow=o||"none"; }, 700); }
    const hdr=document.getElementById("setup-progress-headline");
    if(hdr){ hdr.style.transition="transform .25s"; hdr.style.transform="scale(1.03)"; setTimeout(()=>{ hdr.style.transform="scale(1)"; },260); }
  }catch(e){}
}
// Recompute the warm header (count + bar + headline) in place after a save, without reloading the tab.
function refreshSetupProgress(integrations){
  // integrations is a flat list with .configured; but the header counts ALL stages, so re-fetch lightly.
  const bar=document.getElementById("setup-progress-bar");
  const head=document.getElementById("setup-progress-headline");
  const sub=document.getElementById("setup-progress-sub");
  if(!bar && !head) return;
  j("/cockpit/wizard").then(wiz=>{
    wiz=normaliseWizard(wiz);
    let c=0,t=0; safeArray(wiz.stages).forEach(s=>safeArray(s.integrations).forEach(i=>{t++; if(i.configured)c++;}));
    if(bar) bar.style.width = (t?Math.round(100*c/t):0)+"%";
    if(head && sub){
      if(c===0){ head.textContent="Let's make Mongoose yours"; }
      else if(c<3){ head.textContent=`Nice, ${c} ${c===1?'thing':'things'} connected`; sub.textContent="That's a great start. Add more whenever you like, or stop here."; }
      else { head.textContent="You're in great shape"; sub.textContent=`${c} things connected. Mongoose has plenty to work with now.`; }
    }
  }).catch(()=>{});
}
async function loadSetup(){
  const el=document.getElementById("setup");
  try{
    // Resilient: if wizard endpoint fails, show a degraded but loadable setup tab. A 500 from
    // wizard_schema() (e.g. run_doctor() crash) must not blank the tab.
    const rawWiz=await j("/cockpit/wizard").catch(e=>({stages:[],core_done:false,core_status:{items:[{ok:false,label:"Setup connection error",detail:"Server returned "+e+". Try refreshing or check the server log."}]},configured_count:0,_error:e}));
    const wiz=normaliseWizard(rawWiz);
    let oauthCfg={google_redirect_uri:"http://localhost:8766/",google_redirect_port:8766};
    try{ oauthCfg=await j("/cockpit/oauth-config",{},5000); }catch(e){}
    let html="";
    if(wiz._error){ html+=setupDegradedCard(wiz._error); }
    // Front-door framing. The CORE is the local brain + database, not any integration.
    // If core isn't ready, that's a local-runtime issue (show status); the stages below
    // are all OPTIONAL domains you can connect in any order, or skip entirely.
    if(!wiz.core_done && wiz.core_status){
      let coreRows="";
      for(const it of (wiz.core_status.items||[])){
        coreRows+=`<div class="row"><span class="k"><span class="dot ${it.ok?'ok':'bad'}"></span>${esc(it.label)}</span><span class="v muted">${esc(it.detail||"")}</span></div>`;
      }
      html+=`<div class="card"><h2>Almost there</h2>
        <div class="big"><span class="dot warn"></span>Just getting the brain online</div>
        <div class="muted" style="margin-bottom:10px">Mongoose runs entirely on your machine, so it needs its local brain ready first (usually just: start Ollama and pull the model). Once these are green you're off to the races, everything else is optional.</div>
        ${coreRows}</div>`;
    } else {
      // Warm, progress-aware header. Count what's connected across all stages so the user feels
      // momentum (and gets a first-win nudge / a gentle "you're all set" rather than a flat prompt).
      let __connected = 0, __total = 0;
      safeArray(wiz.stages).forEach(st=>{ safeArray(st.integrations).forEach(i=>{ __total++; if(i.configured) __connected++; }); });
      let headline, sub;
      if(__connected===0){
        headline = "Let's make Mongoose yours";
        sub = "Mongoose already works, connect a thing or two below and it gets genuinely useful. Pick whatever matters to you; skip the rest. There's no wrong order and nothing you can break.";
      } else if(__connected < 3){
        headline = `Nice, ${__connected} ${__connected===1?'thing':'things'} connected`;
        sub = "That's a great start. Add more whenever you like, or stop here, Mongoose stays quiet about anything you haven't set up, never broken.";
      } else {
        headline = "You're in great shape";
        sub = `${__connected} things connected. Mongoose has plenty to work with now. Everything else below is there if you want it, never required.`;
      }
      html+=`<div class="card" style="border-color:var(--gold-dim)">
        <h2 id="setup-progress-headline" style="margin-bottom:4px;display:inline-block">${esc(headline)}</h2>
        <div class="muted" id="setup-progress-sub">${esc(sub)}</div>
        <div style="margin-top:10px;height:6px;border-radius:99px;background:var(--line);overflow:hidden">
          <div id="setup-progress-bar" style="height:100%;width:${__total?Math.round(100*__connected/__total):0}%;background:linear-gradient(90deg,var(--gold-dim),var(--gold));transition:width .4s"></div></div></div>`;
    }
    // Remote-access chase: HTTPS + Dynamic DNS unlock using Mongoose away from home AND inviting
    // guests by link. Filled by chaseRemoteAccess() after render (read-only readiness check); shown
    // prominently here, near the top, whenever it isn't ready yet.
    html+=`<div id="remoteChase"></div>`;
    let __firstOpenUsed = false;
    safeArray(wiz.stages).forEach((stage,si)=>{
      const integrations=safeArray(stage.integrations);
      const done=integrations.filter(i=>i.configured).length;
      const total=integrations.length;
      // Collapse a stage by default when it's fully configured, or when it's an all-optional stage
      // the user hasn't touched, so you're not scrolling through everything. Keep the FIRST
      // not-fully-done stage open so there's an obvious place to act.
      const fullyDone = done>=total && total>0;
      let collapsed = fullyDone;
      if(!collapsed && !__firstOpenUsed){ __firstOpenUsed = true; }      // this one stays open
      else if(!collapsed && __firstOpenUsed){ collapsed = false; }        // later incomplete stages: open too
      const sid = stage.id || ('s'+si);
      const chev = collapsed? '▸' : '▾';
      html+=`<div class="card stage-card" data-stage="${esc(sid)}">
        <h2 class="stage-head" onclick="toggleStage('${esc(sid)}')" role="button" tabindex="0" aria-expanded="false" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleStage('${esc(sid)}')}" style="cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:space-between">
          <span><span class="stage-chev" data-stage="${esc(sid)}" style="display:inline-block;width:1em">${chev}</span> Stage ${si+1} · ${esc(stage.title)}</span>
          <span class="muted" style="font-size:12px;font-weight:400;color:${fullyDone?'var(--ok)':'var(--gold)'}">${done}/${total} configured</span>
        </h2>
        <div class="stage-body" data-stage="${esc(sid)}" style="${collapsed?'display:none':''}">
        <div class="muted" style="margin:-2px 0 12px">${esc(stage.blurb)}</div>`;
      html+=`<div class="wiz-grid">`;
      // Per-integration display state. OAuth integrations carry auth_state
      // ("not_set"|"needs_signin"|"signed_in"); everything else uses the binary configured flag.
      // This is what stops a tile saying "configured" when only the client JSON / client ID exists.
      function tileState(ig){
        if(ig.auth_state==="signed_in") return {cls:" configured", dot:"ok", label:"signed in", warn:false};
        if(ig.auth_state==="needs_signin") return {cls:" needsauth", dot:"warn", label:"sign in needed", warn:true};
        if(ig.auth_state==="not_set") return {cls:"", dot:"unknown", label:"not set", warn:false};
        return ig.configured? {cls:" configured", dot:"ok", label:"configured", warn:false}
                             : {cls:"", dot:"unknown", label:"not set", warn:false};
      }
      for(const ig of integrations){
        const ts = tileState(ig);
        const stat = ts.warn? `<span class="src warn">${ts.label}</span>`
                   : (ts.dot==="ok"? `<span class="src">${ts.label}</span>` : `<span class="muted">${ts.label}</span>`);
        html+=`<div class="wiz-tile${ts.cls}" onclick="wizExpand('${esc(stage.id||('s'+si))}','${esc(ig.slug)}')" role="button" tabindex="0" aria-label="Set up ${esc(ig.name)}" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();wizExpand('${esc(stage.id||('s'+si))}','${esc(ig.slug)}')}" title="Click to set up ${esc(ig.name)}">
          <div class="tname"><span class="dot ${ts.dot}"></span>${esc(ig.name)}<span class="tile-chev" style="margin-left:auto;opacity:.5;font-size:12px">▸</span></div>
          <div class="tstat">${stat}</div></div>`;
      }
      html+=`</div><div class="wiz-stage-detail" id="wizdetail-${esc(stage.id||('s'+si))}"></div>`;
      // Build the hidden form store for this stage (one form per integration).
      html+=`<div class="wiz-forms" id="wizforms-${esc(stage.id||('s'+si))}" style="display:none">`;
      for(const ig of integrations){
        if(ig.slug==="ddns"){ html+=`<div class="wiz-formsrc" data-slug="ddns">${ddnsCardShell()}</div>`; continue; }
        const ts = tileState(ig);
        const hdrBadge = ts.warn? `<span class="src warn">${ts.label}</span>`
                       : (ts.dot==="ok"? `<span class="src">${ts.label}</span>` : `<span class="muted">${ts.label}</span>`);
        let f1=`<div class="wiz-formsrc" data-slug="${esc(ig.slug)}"><div class="wiz-int" data-slug="${esc(ig.slug)}" style="padding:2px 0">
          <div class="row" style="border:none;padding:0 0 6px">
            <span class="k"><span class="dot ${ts.dot}"></span><b>${esc(ig.name)}</b></span>
            <span class="v">${hdrBadge}</span>
          </div>
          <div class="muted setup-help" style="margin-bottom:10px">${linkify(ig.help)}</div>`;
        for(const f of ig.fields){
          // Google OAuth client JSON: an UPLOAD control, not a path text field. The user downloaded
          // a file from Google Cloud Console; asking them to type a filesystem path is hostile.
          if(f.key==="GOOGLE_CLIENT_SECRETS_PATH"){
            const savedNote = (ig.auth_state && ig.auth_state!=="not_set")
             ? `<div class="muted" style="font-size:11px;opacity:.75;margin-top:4px">OAuth client JSON is saved. ${ig.auth_state==="signed_in"?"Signed in.":"Now sign in below."}</div>` : "";
            // The registered redirect URI is a fixed target now (no port drift), so always show it.
            const useUri = (oauthCfg.google_redirect_uri||"http://localhost:8766/");
            const portWarn = oauthCfg.warning
             ? `<div class="warn" style="font-size:11px;margin:4px 0;padding:6px 8px;border-radius:6px;background:rgba(209,168,90,.12);border:1px solid var(--warn)">⚠ ${esc(oauthCfg.warning)}</div>` : "";
            f1+=`<div style="margin-bottom:10px">
              <label class="muted" style="display:block;margin-bottom:3px">OAuth client JSON</label>
              <div class="muted" style="font-size:11px;opacity:.85;margin:2px 0 4px">Upload the JSON file you downloaded from Google Cloud Console (OAuth client ID, type <b>Desktop app</b>). Add this exact redirect URI to the Google client: <code>${esc(useUri)}</code></div>
              ${portWarn}
              <input type="file" accept="application/json,.json" class="wiz-gjson" data-slug="${esc(ig.slug)}" onchange="uploadGoogleJson(this)">
              <span class="wiz-gjson-msg muted" style="margin-left:8px;font-size:12px"></span>
              ${savedNote}</div>`;
            continue;
          }
          const ph = f.is_set && f.secret? "(set, leave blank to keep)" : (f.default||"");
          const type = f.secret? "password" : "text";
          const optLabel = (f.optional && !/optional/i.test(f.prompt))? " (optional)" : "";
          const hintHtml = f.hint? `<div class="muted" style="font-size:11px;opacity:.75;margin:2px 0 4px">${linkify(f.hint)}</div>` : "";
          const defNote = (f.default && !f.is_set)? `<div class="muted" style="font-size:11px;opacity:.6">prefilled with <code>${esc(f.default)}</code>, the safe default; change only if you know you need to</div>` : "";
          f1+=`<div style="margin-bottom:10px">
            <label class="muted" style="display:block;margin-bottom:3px">${esc(f.prompt)}${optLabel}</label>
            ${hintHtml}
            <input class="q wiz-field" data-key="${esc(f.key)}" type="${type}" value="${esc(f.value||"")}" placeholder="${esc(ph)}" autocomplete="off">
            ${defNote}</div>`;
        }
        const extNote = (ig.has_verifier && ig.external_check)
         ? `<span class="muted" style="margin-left:10px;font-size:12px">⤴ test contacts ${esc(ig.name)} (leaves this machine)</span>` : "";
        f1+=`<button class="act go" onclick="saveIntegration(this,'${esc(ig.slug)}')">Save${ig.has_verifier?" & test":""}</button>${extNote}`;
        // 0005: a self-evident caption, the fields and sign-in method above are whatever Mongoose's
        // connector registry declares for this integration, not a hard-coded form. Rendered for every
        // integration so the method-agnostic design is visible and stays correct if a connector changes
        // (zero UI edit). Secrets are never sent to the browser (shown as set/not-set above).
        f1+=`<div class="regnote"><span>&#8505;</span><span>Sign-in method and fields come from Mongoose's <b>connector registry</b>. ${ig.has_verifier?"<b>Save &amp; test</b> verifies the connection.":"There's no live test for this one, so it just saves."}</span></div>`;
        // Clickable OAuth sign-in for Google / Microsoft. Shown whenever the integration uses an
        // auth_state (i.e. it's an OAuth source) and isn't already signed in. This is the button
        // that was previously inert command text.
        if(ig.auth_state && ig.auth_state!=="signed_in" && (ig.slug==="google_calendar" || ig.slug==="microsoft")){
          const prov = ig.slug==="microsoft"? "microsoft" : "google";
          const provLabel = prov==="microsoft"? "Microsoft" : "Google";
          const dis = ig.auth_state==="not_set"? " disabled title='Add the OAuth client details first'" : "";
          f1+=`<button class="act signin" onclick="oauthSignIn(this,'${prov}')"${dis} style="margin-left:8px">Sign in with ${provLabel}</button>`;
        }
        // The one-click native ntfy install this card's help text promises. Reuses the same
        // already-working /cockpit/setup-ntfy endpoint the dedicated Push panel uses, but reports
        // status into THIS card's own .wiz-msg sibling (found by DOM proximity, not a global id),
        // so it can never collide with that other panel's #ntfymsg element.
        if(ig.slug==="push"){
          f1+=`<button class="act go" onclick="setupNtfyFromCard(this)" style="margin-left:8px">Set up native push</button>`;
        }
        f1+=`<span class="wiz-msg muted" style="margin-left:10px"></span></div></div>`;
        html+=f1;
      }
      html+=`</div>`;
      html+=`</div>`;
      html+=`</div>`;
    });
    // Keep the capability repair tools available below the wizard.
    html+=`<div class="card"><h2>Phone push notifications</h2>
      <div class="muted" style="margin-bottom:10px">Optional. Mongoose can push alerts to your phone. This uses a local push service (ntfy). It can run in Docker, or natively without Docker.</div>
      <div id="pushbox" class="muted">checking push setup...</div></div>`;
    html+=`<div class="card"><h2>Backups</h2>
      <div class="muted" style="margin-bottom:10px">Mongoose backs up your brain + config every day, keeping the newest 7 daily, 4 weekly, and 12 monthly copies (grandfather-father-son). Connect Google Drive to keep copies safely off this Mac too.</div>
      <div id="backupBox" class="muted">checking backups...</div></div>`;
    html+=`<div class="card"><h2>Brain snapshots</h2>
      <div class="muted" style="margin-bottom:10px">Daily local restore points of your brain, taken automatically. They're safe but add up (roughly 0.8 GB each). This shows how much disk they use and lets you prune the oldest, no terminal needed.</div>
      <div id="snapshotBox" class="muted">checking snapshots…</div></div>`;
    html+=`<div class="card"><h2>Household coordination (advanced)</h2>
      <div class="muted" style="margin-bottom:10px">Optional. Lets Mongoose join the multi-agent protocol that coordinates your Home Assistant with the other Claude agents. This won't change anything live, it only checks that Mongoose <em>could</em> join when you're ready.</div>
      <div id="v3box" class="muted">checking...</div></div>`;
    html+=`<div class="card"><h2>Voice on phones (HTTPS)</h2>
      <div class="muted" style="margin-bottom:10px"><b>HTTPS is on by default.</b> Browsers only allow microphone and notifications over a secure (HTTPS) connection, so Mongoose serves https with a self-signed certificate and phone voice + notifications work on your network out of the box. The first time each phone or guest opens a Mongoose link the browser shows a one-time \u201cnot secure\u201d warning, tap through (on iPhone, trust it in Settings) and you're set. Below you can check or regenerate the certificate, or install a mkcert-trusted one for a warning-free experience.</div>
      <div id="trustBox" class="muted">checking...</div></div>`;
    html+=`<div class="card"><h2>Voice transcription (Whisper)</h2>
      <div class="muted" style="margin-bottom:10px">Push-to-talk on the phone and voice chat in the cockpit both need Whisper (speech-to-text) and ffmpeg on the Mac. Click Install to set them up, no terminal needed.</div>
      <div id="voiceSetupBox" class="muted">checking...</div>
      <button class="act go" id="voiceInstallBtn" style="display:none" onclick="installVoice(this)">Install voice toolchain</button>
      <div id="voiceInstallOut" class="muted" style="margin-top:8px"></div></div>`;
    html+=`<div class="card"><h2>Voice &amp; reply voice</h2>
      <div class="muted" style="margin-bottom:10px">Choose the voice used to speak Mongoose's replies on this device (browser text-to-speech). Your choice is saved locally.</div>
      <select id="voicePicker" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:10px;color:var(--text);padding:9px;font:inherit;margin-top:4px">
        <option value="">Loading voices…</option></select>
      <div id="voiceSavedFlash" style="font-size:12px;color:var(--ok,#7BAA6E);opacity:0;transition:opacity .3s;margin-top:6px"></div>
      <div style="margin-top:12px;display:flex;align-items:center;gap:10px">
        <label for="voiceRate" style="font-size:13px;white-space:nowrap">Speaking speed</label>
        <input type="range" id="voiceRate" min="0.7" max="1.8" step="0.05" value="1.15" style="flex:1">
        <span id="voiceRateLabel" class="muted" style="font-size:12px;min-width:42px;text-align:right">1.15×</span>
      </div>
      <button class="act" onclick="testVoice()" style="margin-top:10px">Test voice</button>
      <div class="muted" style="font-size:12px;margin-top:8px">Voices are provided by the OS, the list varies by device. On iPhone/Mac, 'Siri' voices give the most natural result. On Android, choose a Google voice.</div></div>`;
    html+=`<div class="card"><h2>Webhook ({{site.domain}} → Mongoose)</h2>
      <div class="muted" style="margin-bottom:10px;font-size:13px">Mongoose's DDNS address lets external sites like {{site.domain}} report events back to the brain. Generate a secret, paste it and the ingest URL into the WordPress plugin settings, and Mongoose will receive form submissions, publish events, and leaseholder queries.</div>
      <div id="webhookBox" class="muted">checking…</div>
      <div id="webhookOut" style="margin-top:8px"></div></div>`;
    html+=`<div class="card"><h2>Maintenance</h2>
      <button class="act" onclick="runDoctor()">Run doctor</button>
      <button class="act go" onclick="installMissing()">Install missing capabilities</button>
      <div id="setupout" class="muted" style="margin-top:10px"></div></div>`;
    html+=`<div class="card"><h2>Saved routines</h2>
      <div class="muted" style="margin-bottom:8px;font-size:12px">Multi-step actions Mongoose can run on command.</div>
      <div id="routines-box"><div class="muted" style="font-size:12px">Loading…</div></div></div>`;
    el.innerHTML=html;
    loadRoutines();
    if(document.getElementById("ddnsBox")) loadDdns();
    chaseRemoteAccess();
    if(document.getElementById("pushbox")) loadPush();
    if(document.getElementById("v3box")) loadV3();
    if(document.getElementById("trustBox")) loadDeviceTrust();
    if(document.getElementById("backupBox")) loadBackups();
    if(document.getElementById("snapshotBox")) loadSnapshots();
    if(document.getElementById("voiceSetupBox")) loadVoiceSetup();
    if(document.getElementById("webhookBox")) loadWebhook();
    if(document.getElementById("voicePicker")) initVoicePicker();
  }catch(e){ el.innerHTML=setupDegradedCard(e); }
}
// Expand one integration's form into its stage's detail area when its tile is tapped. The form
// markup lives hidden in #wizforms-<stage>; we clone it into #wizdetail-<stage>. Tapping the same
// tile again collapses it. saveIntegration still works because the form keeps its .wiz-int wrapper.
function toggleStage(sid){
  const body=document.querySelector(`.stage-body[data-stage="${sid}"]`);
  const chev=document.querySelector(`.stage-chev[data-stage="${sid}"]`);
  if(!body) return;
  const hidden = body.style.display==="none";
  body.style.display = hidden? "" : "none";
  if(chev) chev.textContent = hidden? "▾" : "▸";
}
function wizExpand(stageId, slug, opts){
  opts=opts||{};
  const detail=document.getElementById("wizdetail-"+stageId);
  const store=document.getElementById("wizforms-"+stageId);
  if(!detail||!store) return;
  // If this stage is collapsed, open it first so the form is visible.
  const body=document.querySelector(`.stage-body[data-stage="${stageId}"]`);
  if(body && body.style.display==="none"){
    body.style.display="";
    const chev=document.querySelector(`.stage-chev[data-stage="${stageId}"]`);
    if(chev) chev.textContent="▾";
  }
  // toggle off if the same one is already open, UNLESS forceOpen is set (a programmatic deep
  // link, e.g. gotoDdnsSetup, should always land OPEN, never close what it just opened).
  if(detail.dataset.open===slug){
    if(opts.forceOpen){ if(opts.scroll!==false) detail.scrollIntoView({behavior:"smooth", block:"nearest"}); return; }
    detail.innerHTML=""; detail.dataset.open=""; return;
  }
  const src=store.querySelector('.wiz-formsrc[data-slug="'+slug+'"]');
  if(!src) return;
  detail.innerHTML=`<div class="wiz-detail"><button type="button" class="wiz-close" aria-label="Close setup panel" onclick="this.closest('.wiz-detail').parentElement.innerHTML='';this.closest('.wiz-detail').parentElement.dataset.open=''">×</button>${src.innerHTML}</div>`;
  detail.dataset.open=slug;
  if(slug==="ddns" && document.getElementById("ddnsBox")) loadDdns();
  if(opts.scroll!==false) detail.scrollIntoView({behavior:"smooth", block:"nearest"});
}
async function saveIntegration(btn, slug){
  const root=btn.closest(".wiz-int"); const msg=root.querySelector(".wiz-msg");
  const values={};
  root.querySelectorAll(".wiz-field").forEach(inp=>{ values[inp.dataset.key]=inp.value; });
  msg.textContent="saving..."; msg.style.color="var(--dim)";
  try{
    const r=await postAction("/cockpit/wizard/save",{slug,values}); const d=await r.json();
    if(!d.ok){ msg.textContent=esc(d.error||"save failed"); msg.style.color="var(--bad)"; return; }
    const igName = (root.dataset.slug||slug);
    if(d.verified===true){ msg.innerHTML="✓ "+esc(d.detail?("saved and verified: "+d.detail):"saved and verified, nicely done."); msg.style.color="var(--ok)"; celebrateSetup(root); }
    else if(d.verified===false){ msg.textContent="saved · "+esc(d.detail||"could not verify"); msg.style.color="var(--warn)"; }
    else { msg.innerHTML="✓ "+esc("saved, that's connected."); msg.style.color="var(--ok)"; celebrateSetup(root); }
    // Do NOT reload the whole setup tab here, that wipes the result the user is reading. Update
    // just this integration's configured badge in place if the save reported it.
    if(d.integrations){
      const me=d.integrations.find(x=>x.slug===slug);
      if(me){ const badge=root.querySelector(".wiz-badge");
        if(badge){ badge.textContent=me.configured?"configured":"not set";
          badge.className="wiz-badge "+(me.configured?"ok":"off"); } }
      // refresh the warm progress header without disturbing the form the user is in
      if(typeof refreshSetupProgress==="function"){ try{ refreshSetupProgress(d.integrations); }catch(e){} }
    }
  }catch(e){ msg.textContent="save failed: "+esc(e); msg.style.color="var(--bad)"; }
}

// Upload the Google OAuth client JSON from the browser (reads the file's text, posts it as JSON so
// there's no multipart dependency). Saving it does NOT sign in, it enables the Sign in button.
async function uploadGoogleJson(input){
  const root=input.closest(".wiz-int");
  const msg=input.parentElement.querySelector(".wiz-gjson-msg");
  const file=input.files && input.files[0];
  if(!file){ return; }
  msg.textContent="uploading..."; msg.style.color="var(--dim)";
  try{
    const text=await file.text();
    const r=await postAction("/cockpit/wizard/upload-google-json",{content:text},30000);
    const d=await r.json();
    if(!d.ok){ msg.textContent=esc(d.error||"upload failed"); msg.style.color="var(--bad)"; return; }
    msg.textContent="saved · now click Sign in with Google"; msg.style.color="var(--ok)";
    // Enable the sign-in button in this form (state moved not_set -> needs_signin) without a reload.
    const sib=root.querySelector("button.signin");
    if(sib){ sib.disabled=false; sib.removeAttribute("title"); }
  }catch(e){ msg.textContent="upload failed: "+esc(e); msg.style.color="var(--bad)"; }
}

// Clickable OAuth sign-in. Google opens the consent flow on the Mac; Microsoft starts the
// device-code flow (the message returned explains the next step). Runs server-side in a worker
// thread so it doesn't block. On success, nudges the user to refresh the tab to see "signed in".
async function oauthSignIn(btn, provider){
  const root=btn.closest(".wiz-int"); const msg=root.querySelector(".wiz-msg");
  const old=btn.textContent; btn.disabled=true; btn.textContent="signing in...";
  msg.style.color="var(--dim)";
  if(provider==="microsoft"){
    // Device-code flow: get the code + URL and SHOW them here, then poll until the user finishes.
    msg.textContent="starting Microsoft sign-in...";
    try{
      const r=await postAction("/cockpit/microsoft/signin-begin",{todo_only:true}); const d=await r.json();
      if(!d.ok){ msg.textContent=esc(d.error||"could not start sign-in"); msg.style.color="var(--bad)"; btn.disabled=false; btn.textContent=old; return; }
      const url=d.verification_uri||"https://microsoft.com/devicelogin";
      msg.innerHTML=`<div style="border:1px solid var(--gold-dim);border-radius:8px;padding:10px;margin-top:6px">
        <div>1. Open <a href="${esc(url)}" target="_blank" style="color:var(--gold)">${esc(url)}</a> on any device</div>
        <div style="margin-top:6px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">2. Enter this code:
          <b id="ms-code" style="font-family:'IBM Plex Mono',monospace;font-size:17px;letter-spacing:2px;background:rgba(212,175,90,.12);padding:2px 8px;border-radius:6px">${esc(d.user_code||"")}</b>
          <button onclick="(function(b){navigator.clipboard&&navigator.clipboard.writeText('${esc(d.user_code||"")}');b.textContent='copied ✓';setTimeout(()=>b.textContent='copy',1500);})(this)" style="background:transparent;border:1px solid var(--gold-dim);color:var(--gold);border-radius:6px;padding:3px 9px;cursor:pointer;font-size:12px">copy</button>
        </div>
        <div style="margin-top:6px">3. Sign in with your Microsoft account and approve <span class="muted">Tasks.Read</span></div>
        <div class="muted" id="ms-poll" style="margin-top:8px">waiting for you to finish...</div></div>`;
      msg.style.color="var(--text)";
      // poll status until done or timeout
      const deadline=Date.now()+((d.expires_in||900)*1000);
      while(Date.now()<deadline){
        await new Promise(r=>setTimeout(r,2500));
        try{
          const s=await (await fetch("/cockpit/microsoft/signin-status")).json();
          const poll=document.getElementById("ms-poll");
          if(s.state==="syncing"){ if(poll){ poll.textContent=s.message||"Signed in. Pulling your tasks..."; poll.style.color="var(--ok)"; } continue; }
          if(s.state==="done"){
            if(s.ok){ msg.innerHTML=`<span style="color:var(--ok)">✓ ${esc(s.message||"Signed in to Microsoft To Do.")}</span>`; btn.textContent="signed in ✓";
              if(typeof loadTasks==="function"){ try{ loadTasks(); }catch(e){} } }
            else { if(poll){poll.textContent="sign-in failed: "+esc(s.error||"");poll.style.color="var(--bad)";} btn.disabled=false; btn.textContent=old; }
            return;
          }
        }catch(e){}
      }
      const poll=document.getElementById("ms-poll"); if(poll){poll.textContent="the code expired, click sign in again";poll.style.color="var(--bad)";}
      btn.disabled=false; btn.textContent=old;
    }catch(e){ msg.textContent="sign-in failed: "+esc(e); msg.style.color="var(--bad)"; btn.disabled=false; btn.textContent=old; }
    return;
  }
  // Google: opens consent in the system browser on the Mac.
  msg.textContent="opening Google sign-in in your browser on the Mac...";
  try{
    const r=await postAction("/cockpit/wizard/authorize/"+provider,{},210000); const d=await r.json();
    if(d.ok){ msg.textContent="signed in: "+esc(d.message||""); msg.style.color="var(--ok)";
      btn.textContent="signed in ✓"; }
    else { msg.textContent=esc(d.error||d.message||"sign-in did not complete"); msg.style.color="var(--bad)";
      btn.disabled=false; btn.textContent=old; }
  }catch(e){ msg.textContent="sign-in failed: "+esc(e); msg.style.color="var(--bad)";
    btn.disabled=false; btn.textContent=old; }
}
function ddnsCardShell(){
  // placeholder filled by loadDdns() after loadSetup paints; keeps the heavy fetch off the main render
  return `<div class="wiz-int" data-slug="ddns" style="border-top:1px solid var(--line);padding:14px 0">
    <div id="ddnsBox" class="muted">Loading remote-reach options…</div></div>`;
}
let DDNS_OPTS=null, DDNS_SEL=null;
async function loadV3(){
  const box=document.getElementById("v3box"); if(!box) return;
  try{
    const s=await j("/cockpit/protocol-v3/status");
    const ready=s.can_take_pi_locks;
    const dot = ready? '<span class="dot ok"></span>' : '<span class="dot warn"></span>';
    let status;
    if(!ready){ status="Not connected to your Pi yet."; }
    else if(s.pi_ssh_last_verified){
      const mins=Math.max(0, Math.round((Date.now()-new Date(s.pi_ssh_last_verified).getTime())/60000));
      const when = mins<1?"just now" : mins<60?(mins+" min ago") : mins<1440?(Math.round(mins/60)+"h ago") : (Math.round(mins/1440)+"d ago");
      status="Pi connection verified working ("+when+"). Test again any time.";
    } else {
      status="Pi connection set, ready to test.";
    }
    let domains="";
    (s.proposed_domains||[]).forEach(d=>{ domains+=`<li style="margin:3px 0"><b>${esc(d.domain)}</b><div class="muted" style="font-size:12px">${esc(d.why||"")}</div></li>`; });
    box.innerHTML=`
      <div style="margin-bottom:8px">${dot}${esc(status)}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:8px">
        <label style="font-size:12px;color:var(--dim)">Your Pi's SSH address
          <input id="v3-host" placeholder="pi@raspberrypi.local" value="${esc((s.pi_transport&&s.pi_transport.host)||'')}" style="display:block;margin-top:3px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);min-width:230px"></label>
        <button class="act" onclick="saveV3(this)">Save</button>
        <button class="act go" onclick="testV3Handshake(this)">Test connection</button>
      </div>
      <details style="margin-bottom:8px"><summary class="muted" style="cursor:pointer;font-size:12px">Set it up automatically (mongoose makes + installs an SSH key)</summary>
        <div class="muted" style="font-size:12px;margin:6px 0">mongoose will create its own dedicated key and authorise it on the Pi, so SSH is passwordless afterwards. Enter the Pi password just once (used immediately, never stored). Leave it blank if you'd rather authorise the key yourself, mongoose will show you the exact one-line command.</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
          <label style="font-size:12px;color:var(--dim)">Pi password (once, optional)
            <input id="v3-pass" type="password" placeholder="used once, never stored" style="display:block;margin-top:3px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);min-width:200px"></label>
          <button class="act go" onclick="setupV3Ssh(this)">Set up SSH automatically</button>
        </div>
      </details>
      <details style="margin-bottom:8px" open><summary class="muted" style="cursor:pointer;font-size:12px">Simplest: just username and password (no keys)</summary>
        <div class="muted" style="font-size:12px;margin:6px 0">No key setup at all, mongoose connects to the Pi with the address and password every time. Easiest to set up. The password is saved on this Mac (in .env, like your email password), and this needs a small helper called <code>sshpass</code> (mongoose will tell you how to install it if it's missing). A key is a little more secure if you ever want one.</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
          <label style="font-size:12px;color:var(--dim)">Pi password (saved)
            <input id="v3-pass-keyless" type="password" placeholder="saved on this Mac" style="display:block;margin-top:3px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);min-width:200px"></label>
          <button class="act go" onclick="setupV3SshPassword(this)">Connect with password</button>
        </div>
      </details>
      <div class="muted" style="font-size:12px;margin-bottom:6px">The test only reads, it never changes your Home Assistant or goes live. You can skip it entirely; Mongoose works fully without it.</div>
      <div id="v3-result" style="margin-top:6px"></div>
      <details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:12px">What Mongoose would help with (proposed, you decide)</summary>
        <ul style="margin:8px 0 0 0;padding-left:18px">${domains}</ul>
        <div class="muted" style="font-size:11px;margin-top:6px">Nothing here is active. Going live needs your explicit approval and the other agents' handshakes.</div></details>`;
    box.classList.remove("muted");
  }catch(e){ try{ box.classList.remove("muted"); box.innerHTML=`<div style="color:var(--muted);font-size:12px">⚠ Temporarily unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadV3()">Retry</button></div>`; console.warn("loadV3 failed:", e); }catch(_){} }
}
async function saveV3(btn){
  const host=(document.getElementById("v3-host")||{}).value||"";
  const out=document.getElementById("v3-result");
  if(!host.trim()){ if(out)out.innerHTML='<span style="color:var(--warn)">Add your Pi\'s address first (like pi@raspberrypi.local).</span>'; return; }
  btn.disabled=true; const o=btn.textContent; btn.textContent="saving...";
  try{
    const r=await postAction("/cockpit/protocol-v3/save",{pi_ssh_host:host.trim()}); const d=await r.json();
    if(out) out.innerHTML = d.ok? '<span style="color:var(--ok)">Saved. Now hit \u201cTest connection\u201d when you\u2019re ready.</span>' : `<span style="color:var(--bad)">${esc(d.error||"could not save")}</span>`;
  }catch(e){ if(out)out.innerHTML=`<span style="color:var(--bad)">save failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function testV3Handshake(btn){
  const out=document.getElementById("v3-result"); if(!out) return;
  btn.disabled=true; const o=btn.textContent; btn.textContent="testing...";
  out.innerHTML='<div class="muted">Checking the connection (read-only)...</div>';
  try{
    const r=await postAction("/cockpit/protocol-v3/test-handshake",{}); const d=await r.json();
    let steps="";
    (d.steps||[]).forEach(st=>{
      const mark = st.ok? (st.soft?'<span style="color:var(--gold)">\u25CB</span>':'<span style="color:var(--ok)">\u2713</span>') : '<span style="color:var(--bad)">\u2717</span>';
      steps+=`<div style="margin:3px 0">${mark} ${esc(st.step)}<span class="muted" style="font-size:12px">, ${esc(st.detail||"")}</span></div>`;
    });
    const color = d.ok? (d.ready?"var(--ok)":"var(--warn)") : "var(--bad)";
    out.innerHTML=`<div style="border:1px solid var(--line);border-radius:8px;padding:10px">
      ${steps}
      <div style="margin-top:8px;color:${color};font-weight:600">${esc(d.summary||"")}</div></div>`;
  }catch(e){ out.innerHTML=`<span style="color:var(--bad)">test failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function setupV3Ssh(btn){
  const host=(document.getElementById("v3-host")||{}).value||"";
  const pass=(document.getElementById("v3-pass")||{}).value||"";
  const out=document.getElementById("v3-result"); if(!out) return;
  if(!host.trim()){ out.innerHTML='<span style="color:var(--warn)">Add your Pi\'s address first (like pi@raspberrypi.local).</span>'; return; }
  btn.disabled=true; const o=btn.textContent; btn.textContent="setting up...";
  out.innerHTML='<div class="muted">Creating a key and authorising it on the Pi...</div>';
  try{
    const r=await postAction("/cockpit/protocol-v3/setup-ssh",{pi_ssh_host:host.trim(),password:pass||undefined});
    const d=await r.json();
    const pw=document.getElementById("v3-pass"); if(pw) pw.value="";  // clear the password field immediately
    let steps="";
    (d.steps||[]).forEach(st=>{
      const mark = st.ok? '<span style="color:var(--ok)">\u2713</span>' : (st.soft?'<span style="color:var(--gold)">\u25CB</span>':'<span style="color:var(--bad)">\u2717</span>');
      steps+=`<div style="margin:3px 0">${mark} ${esc(st.step)}<span class="muted" style="font-size:12px">, ${esc(st.detail||"")}</span></div>`;
      if(st.manual_command) steps+=`<div style="margin:3px 0 6px 18px"><code style="font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--gold)">${esc(st.manual_command)}</code> <button onclick="navigator.clipboard&&navigator.clipboard.writeText(${JSON.stringify(st.manual_command)});this.textContent='copied \u2713';setTimeout(()=>this.textContent='copy',1500)" style="background:transparent;border:1px solid var(--gold-dim);color:var(--gold);border-radius:5px;padding:1px 6px;cursor:pointer;font-size:11px">copy</button>${st.warning?`<div class="warn" style="font-size:11px;margin-top:4px;padding:5px 7px;border-radius:6px;background:rgba(208,120,94,.12);border:1px solid var(--bad)">\u26A0 ${esc(st.warning)}</div>`:""}</div>`;
    });
    if(d.needs_sshpass && d.install_command){
      steps+=`<div style="margin:6px 0 0 18px"><span class="muted" style="font-size:12px">Make it fully automatic next time:</span> <code style="font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--gold)">${esc(d.install_command)}</code> <button onclick="navigator.clipboard&&navigator.clipboard.writeText(${JSON.stringify(d.install_command)});this.textContent='copied \u2713';setTimeout(()=>this.textContent='copy',1500)" style="background:transparent;border:1px solid var(--gold-dim);color:var(--gold);border-radius:5px;padding:1px 6px;cursor:pointer;font-size:11px">copy</button></div>`;
    }
    const color = d.ok? (d.ready?"var(--ok)":"var(--warn)") : (d.needs_manual?"var(--gold)":"var(--bad)");
    out.innerHTML=`<div style="border:1px solid var(--line);border-radius:8px;padding:10px">${steps}<div style="margin-top:8px;color:${color};font-weight:600">${esc(d.summary||"")}</div></div>`;
  }catch(e){ out.innerHTML=`<span style="color:var(--bad)">setup failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function setupV3SshPassword(btn){
  const host=(document.getElementById("v3-host")||{}).value||"";
  const pass=(document.getElementById("v3-pass-keyless")||{}).value||"";
  const out=document.getElementById("v3-result"); if(!out) return;
  if(!host.trim()){ out.innerHTML='<span style="color:var(--warn)">Add your Pi\'s address first (like pi@raspberrypi.local).</span>'; return; }
  if(!pass){ out.innerHTML='<span style="color:var(--warn)">Add the Pi password.</span>'; return; }
  btn.disabled=true; const o=btn.textContent; btn.textContent="connecting...";
  out.innerHTML='<div class="muted">Connecting to the Pi with the password...</div>';
  try{
    const r=await postAction("/cockpit/protocol-v3/setup-ssh-password",{pi_ssh_host:host.trim(),password:pass});
    const d=await r.json();
    let steps="";
    (d.steps||[]).forEach(st=>{
      const mark = st.ok? '<span style="color:var(--ok)">\u2713</span>' : '<span style="color:var(--bad)">\u2717</span>';
      steps+=`<div style="margin:3px 0">${mark} ${esc(st.step)}<span class="muted" style="font-size:12px">, ${esc(st.detail||"")}</span></div>`;
    });
    if(d.needs_sshpass && d.install_command){
      steps+=`<div style="margin:6px 0 0 18px"><code style="font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--gold)">${esc(d.install_command)}</code> <button onclick="navigator.clipboard&&navigator.clipboard.writeText(${JSON.stringify(d.install_command)});this.textContent='copied \u2713';setTimeout(()=>this.textContent='copy',1500)" style="background:transparent;border:1px solid var(--gold-dim);color:var(--gold);border-radius:5px;padding:1px 6px;cursor:pointer;font-size:11px">copy</button></div>`;
    }
    const color = d.ok? (d.ready?"var(--ok)":"var(--warn)") : (d.needs_sshpass?"var(--gold)":"var(--bad)");
    out.innerHTML=`<div style="border:1px solid var(--line);border-radius:8px;padding:10px">${steps}<div style="margin-top:8px;color:${color};font-weight:600">${esc(d.summary||"")}</div></div>`;
  }catch(e){ out.innerHTML=`<span style="color:var(--bad)">setup failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function loadDeviceTrust(){
  const box=document.getElementById("trustBox"); if(!box) return;
  try{
    const s=await j("/cockpit/device-trust/status");
    const ready=s.tls_configured && s.cert_exists;
    const dot = ready? '<span class="dot ok"></span>' : '<span class="dot warn"></span>';
    const status = ready? "HTTPS certificate ready." : "Not set up yet, phones can't use the mic over the network without it.";
    const methodNote = s.mkcert_available
     ? "mkcert is installed, so devices can trust mongoose's certificate fully (no warnings after a one-time install)."
      : "mkcert isn't installed, mongoose will use a self-signed certificate (devices show a one-time warning you accept). For the cleaner path, install mkcert (brew install mkcert) and set up again.";
    box.innerHTML=`
      <div style="margin-bottom:8px">${dot}${esc(status)}</div>
      <div class="muted" style="font-size:12px;margin-bottom:8px">${esc(methodNote)}</div>
      <button class="act go" onclick="setupDeviceTrust(this)">Set up phone HTTPS</button>
      <div id="trust-result" style="margin-top:10px"></div>`;
    box.classList.remove("muted");
  }catch(e){ try{ box.classList.remove("muted"); box.innerHTML=`<div style="color:var(--muted);font-size:12px">⚠ Temporarily unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadDeviceTrust()">Retry</button></div>`; console.warn("loadDeviceTrust failed:", e); }catch(_){} }
}
async function setupDeviceTrust(btn){
  const out=document.getElementById("trust-result"); if(!out) return;
  btn.disabled=true; const o=btn.textContent; btn.textContent="generating...";
  out.innerHTML='<div class="muted">Generating the certificate...</div>';
  try{
    const r=await postAction("/cockpit/device-trust/setup",{}); const d=await r.json();
    if(!d.ok){ out.innerHTML=`<span style="color:var(--bad)">${esc(d.error||"setup failed")}</span>`; btn.disabled=false; btn.textContent=o; return; }
    renderTrustPlan(out, d.plan, d.method, d.lan_ip);
  }catch(e){ out.innerHTML=`<span style="color:var(--bad)">setup failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
function renderTrustPlan(out, plan, method, lanIp){
  if(!plan){ out.innerHTML='<span class="muted">No plan returned.</span>'; return; }
  let tabs="";
  if(plan.method==="mkcert"){
    ["ios","android","macos"].forEach(p=>{
      tabs+=`<button class="act" data-plat="${p}" onclick="switchTrustPlatform(this,'${p}')" style="${p===(plan.platform||'ios')?'border-color:var(--gold);color:var(--gold)':''}">${p==='ios'?'iPhone / iPad':p==='android'?'Android':'this Mac'}</button> `;
    });
  }
  let qr="";
  if(plan.qr_svg){
    qr=`<div style="margin:10px 0;text-align:center"><div style="background:#fff;display:inline-block;padding:10px;border-radius:10px;max-width:200px">${plan.qr_svg}</div><div class="muted" style="font-size:11px;margin-top:4px">Scan on the device, or open <code>${esc(plan.qr_target||"")}</code></div></div>`;
  } else if(plan.qr_target){
    qr=`<div style="margin:10px 0"><div class="muted" style="font-size:12px">Open this on the device:</div><code style="font-family:'IBM Plex Mono',monospace;font-size:13px;color:var(--gold)">${esc(plan.qr_target)}</code></div>`;
  }
  let steps="<ol style=\"margin:8px 0 0 0;padding-left:20px\">";
  (plan.steps||[]).forEach(st=>{ steps+=`<li style="margin:4px 0">${esc(st)}</li>`; });
  steps+="</ol>";
  out.innerHTML=`<div style="border:1px solid var(--line);border-radius:8px;padding:12px">
    <div class="muted" style="font-size:12px;margin-bottom:8px">${esc(plan.summary||"")}</div>
    ${tabs?`<div id="trust-tabs" style="margin-bottom:8px">${tabs}</div>`:""}
    ${qr}
    <div id="trust-steps">${steps}</div>
    <div class="muted" style="font-size:11px;margin-top:10px">After installing, restart Mongoose with HTTPS on so phones connect securely.</div></div>`;
}
async function switchTrustPlatform(btn, plat){
  const tabs=document.getElementById("trust-tabs");
  if(tabs) Array.from(tabs.children).forEach(b=>{ b.style.borderColor=""; b.style.color=""; });
  btn.style.borderColor="var(--gold)"; btn.style.color="var(--gold)";
  try{
    const plan=await j("/cockpit/device-trust/plan?platform="+encodeURIComponent(plat));
    const stepsEl=document.getElementById("trust-steps");
    if(stepsEl){
      let steps="<ol style=\"margin:8px 0 0 0;padding-left:20px\">";
      (plan.steps||[]).forEach(st=>{ steps+=`<li style="margin:4px 0">${esc(st)}</li>`; });
      steps+="</ol>";
      stepsEl.innerHTML=steps;
    }
  }catch(e){}
}
async function loadBackups(){
  const box=document.getElementById("backupBox"); if(!box) return;
  try{
    const s=await j("/cockpit/backup-status");
    const connected=s.drive_connected;
    const dot = connected? '<span class="dot ok"></span>' : '<span class="dot warn"></span>';
    const last = s.last_ok_at? `Last backup: ${esc(new Date(s.last_ok_at).toLocaleString())}` : "No backup recorded yet.";
    const err = s.last_error? `<div style="color:var(--warn);font-size:12px;margin-top:4px">Last error: ${esc(s.last_error)}</div>` : "";
    // local + drive GFS breakdown
    const lg = s.local_gfs||{};
    const localLine = `On this Mac: ${s.local_count||0} backup(s), ${lg.daily||0} daily · ${lg.weekly||0} weekly · ${lg.monthly||0} monthly`;
    let driveBlock="";
    if(connected){
      const dg=s.drive_gfs||{};
      const folderLink = s.folder_url? `<a href="${esc(s.folder_url)}" target="_blank" rel="noopener" style="color:var(--gold)">open “${esc(s.folder_name||"Mongoose Backups")}” in Drive ↗</a>` : esc(s.folder_name||"Mongoose Backups");
      driveBlock=`<div style="margin-top:8px;padding:8px 10px;border:1px solid var(--gold-dim);border-radius:8px">
        <div style="font-size:13px"><span class="dot ok"></span>Google Drive connected, ${folderLink}</div>
        <div class="muted" style="font-size:12px;margin-top:4px">In Drive: ${s.drive_count!=null?s.drive_count:"?"} backup(s), ${dg.daily||0} daily · ${dg.weekly||0} weekly · ${dg.monthly||0} monthly</div>
        ${s.scope_note?`<div class="muted" style="font-size:11px;margin-top:4px">${esc(s.scope_note)}</div>`:""}</div>`;
    } else {
      driveBlock=`<div style="margin-top:8px;padding:8px 10px;border:1px solid var(--line);border-radius:8px">
        <div class="muted" style="font-size:12px;margin-bottom:6px">Google Drive isn't connected. Connecting keeps copies safely off this Mac (Mongoose only ever sees its own backup folder, never the rest of your Drive).</div>
        <button class="act go" onclick="connectDrive(this)">Connect Google Drive</button></div>`;
    }
    box.innerHTML=`
      <div style="margin-bottom:6px">${dot}${esc(last)}</div>
      ${err}
      <div class="muted" style="font-size:12px">${esc(localLine)}</div>
      <div class="muted" style="font-size:11px;margin-top:2px">${esc(s.policy||"")}</div>
      ${driveBlock}
      <div style="margin-top:10px"><button class="act" onclick="backupNow(this)">Back up now</button></div>
      <div id="backup-result" style="margin-top:8px"></div>`;
    box.classList.remove("muted");
  }catch(e){ try{ box.classList.remove("muted"); box.innerHTML=`<div style="color:var(--muted);font-size:12px">⚠ Temporarily unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadBackups()">Retry</button></div>`; console.warn("loadBackups failed:", e); }catch(_){} }
}
async function connectDrive(btn){
  const out=document.getElementById("backup-result");
  btn.disabled=true; const o=btn.textContent; btn.textContent="opening Google...";
  if(out) out.innerHTML='<div class="muted">Opening Google sign-in in this Mac browser (approve there)...</div>';
  try{
    // This is the desktop cockpit, running ON the Mac, so use the loopback flow directly: its
    // localhost redirect is already registered in the Google client, so it needs no Console change
    // (unlike the phone web flow). A browser window opens on the Mac for consent.
    const r=await postAction("/cockpit/drive-connect",{}); const d=await r.json();
    if(d.ok){ if(out) out.innerHTML=`<span style="color:var(--ok)">${esc(d.detail||"Drive connected.")}</span>`; _reloadSoon(loadBackups, 800); }
    else { if(out) out.innerHTML=`<span style="color:var(--bad)">${esc(d.error||"connection failed")}</span>`; }
  }catch(e){ if(out) out.innerHTML=`<span style="color:var(--bad)">connect failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function backupNow(btn){
  const out=document.getElementById("backup-result");
  btn.disabled=true; const o=btn.textContent; btn.textContent="backing up...";
  if(out) out.innerHTML='<div class="muted">Creating a backup...</div>';
  try{
    const r=await postAction("/cockpit/backup-now",{}); const d=await r.json();
    const okLocal = d.local_backup && d.local_backup.ok;
    const drive = d.drive_uploaded? "uploaded to Drive" : (d.drive&&d.drive.skipped? "kept locally (Drive not connected)" : "kept locally");
    if(d.ok || okLocal){ if(out) out.innerHTML=`<span style="color:var(--ok)">Backup done, ${esc(drive)}.</span>`; _reloadSoon(loadBackups, 800); }
    else { if(out) out.innerHTML=`<span style="color:var(--bad)">${esc((d.local_backup&&d.local_backup.error)||"backup failed")}</span>`; }
  }catch(e){ if(out) out.innerHTML=`<span style="color:var(--bad)">backup failed: ${esc(e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function loadSnapshots(){
  const box=document.getElementById("snapshotBox"); if(!box) return;
  try{
    const s=await j("/cockpit/snapshots");
    const snaps=s.snapshots||[];
    const totalGB=((s.total_bytes||0)/1073741824).toFixed(2);
    let rows="";
    for(const sn of snaps.slice(0,12)){
      const gb=((sn.db_bytes||0)/1073741824).toFixed(2);
      rows+=`<div class="row"><span class="k">${esc(sn.name||"?")}</span><span class="v muted">${gb} GB</span></div>`;
    }
    if(snaps.length>12) rows+=`<div class="muted" style="font-size:12px">…and ${snaps.length-12} older</div>`;
    box.innerHTML=`
      <div style="margin-bottom:6px">${s.count||0} snapshot(s), ${totalGB} GB total</div>
      ${rows}
      <div style="margin-top:10px"><button class="act" onclick="pruneSnapshots(this)">Prune to newest 14</button></div>
      <div id="snapshot-result" style="margin-top:8px"></div>`;
    box.classList.remove("muted");
  }catch(e){ try{ box.classList.remove("muted"); box.innerHTML=`<div style="color:var(--muted);font-size:12px">⚠ Temporarily unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadSnapshots()">Retry</button></div>`; console.warn("loadSnapshots failed:", e); }catch(_){} }
}
async function pruneSnapshots(btn){
  const out=document.getElementById("snapshot-result");
  if(!confirm("Delete all but the newest 14 brain snapshots?")) return;
  btn.disabled=true; const o=btn.textContent; btn.textContent="pruning...";
  if(out) out.innerHTML='<div class="muted">Pruning snapshots...</div>';
  try{
    const d=await postActionOk("/cockpit/snapshots/prune",{keep:14});
    const pr=d.pruned||{};
    if(out) out.innerHTML=`<span style="color:var(--ok)">Removed ${pr.deleted||0}, kept ${pr.kept||0}.</span>`;
    _reloadSoon(loadSnapshots, 600);
  }catch(e){ if(out) out.innerHTML=`<span style="color:var(--bad)">${esc(e.message||e)}</span>`; }
  btn.disabled=false; btn.textContent=o;
}
async function loadDdns(){
  try{
  const box=document.getElementById("ddnsBox"); if(!box) return;
  try{ DDNS_OPTS=await j("/cockpit/ddns/options"); }
  catch(e){ box.innerHTML=`<span class="muted">Couldn't load remote-reach options (${esc(e)}).</span>`; return; }
  const st=DDNS_OPTS.status||{};
  DDNS_SEL = DDNS_SEL || st.provider || (DDNS_OPTS.providers[0]&&DDNS_OPTS.providers[0].slug);
  renderDdns();
  }catch(e){ try{ const b=document.getElementById('ddnsBox'); if(b) b.innerHTML='<div style="color:var(--muted);font-size:12px">⚠ DDNS status unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadDdns()">Retry</button></div>'; console.warn('loadDdns failed:',e); }catch(_){} }
}
function renderDdns(){
  const box=document.getElementById("ddnsBox"); if(!box||!DDNS_OPTS) return;
  const st=DDNS_OPTS.status||{};
  const cfgLine = st.provider
   ? `<span class="src">${esc(st.provider_name||st.provider)}</span>${st.enabled?' · auto-update on':' · auto-update off'}${st.last_ip?` · last IP ${esc(st.last_ip)}`:''}`
    : `<span class="muted">not set up</span>`;
  let h=`<div class="row" style="border:none;padding:0 0 6px">
      <span class="k"><span class="dot ${st.provider?'ok':'unknown'}"></span><b>Reachable by a stable name</b></span>
      <span class="v">${cfgLine}</span></div>
    <div class="muted" style="margin-bottom:10px">Pick how Mongoose stays reachable from outside your home when your internet IP changes. Tailscale is the private, no-fuss choice; the dynamic-DNS options need a router port-forward and publish your home IP.</div>`;
  // provider picker
  h+=`<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px">`;
  for(const p of DDNS_OPTS.providers){
    const on = p.slug===DDNS_SEL;
    const tag = p.recommended? ' ★' : '';
    h+=`<button class="act${on?' go':''}" style="font-size:12px" onclick="ddnsPick('${esc(p.slug)}')">${esc(p.name.split(' (')[0])}${tag}</button>`;
  }
  h+=`</div>`;
  // selected provider detail
  const sel=DDNS_OPTS.providers.find(p=>p.slug===DDNS_SEL);
  if(sel){
    h+=`<div style="border:1px solid var(--line);border-radius:8px;padding:10px;margin-bottom:8px">
      <div style="margin-bottom:6px">${esc(sel.blurb)}</div>`;
    if(sel.needs_portforward){
      h+=`<div class="muted" style="font-size:11px;color:var(--warn);margin-bottom:8px">⚠ Needs a router port-forward, and publishes your home's public IP. DDNS only keeps the name pointed at your IP, to actually reach Mongoose you also need to run Enable phone access and forward the port on your router.</div>`;
    } else if(sel.kind==="vpn"){
      h+=`<div class="muted" style="font-size:11px;color:var(--ok);margin-bottom:8px">✓ No port-forwarding, no open ports, your home IP is never exposed.</div>`;
    }
    if(sel.setup_hint){ h+=`<div class="muted" style="font-size:11px;margin-bottom:8px">${linkify(sel.setup_hint)}</div>`; }
    // fields (ddns providers) or tailscale action
    if(sel.kind==="vpn"){
      h+=`<button class="act go" onclick="ddnsTailscale(this)">Set up Tailscale</button>
          <span class="ddns-msg muted" style="margin-left:10px"></span>`;
    } else {
      for(const f of sel.fields){
        const ph = f.is_set && f.secret? "(set, leave blank to keep)" : "";
        h+=`<div style="margin-bottom:8px">
          <label class="muted" style="display:block;margin-bottom:3px">${esc(f.label)}</label>
          ${f.hint?`<div class="muted" style="font-size:11px;opacity:.75;margin:2px 0 4px">${linkify(f.hint)}</div>`:''}
          <input class="q ddns-field" data-key="${esc(f.key)}" type="${f.secret?'password':'text'}" placeholder="${esc(ph)}" autocomplete="off"></div>`;
      }
      h+=`<button class="act go" onclick="ddnsSave(this)">Save & update now</button>`;
      if(st.provider && st.enabled){ h+=`<button class="act" style="margin-left:6px" onclick="ddnsUpdate(this)">Update IP now</button>`; }
      if(st.enabled){ h+=`<button class="act" style="margin-left:6px" onclick="ddnsDisable(this)">Turn off auto-update</button>`; }
      h+=`<span class="ddns-msg muted" style="margin-left:10px"></span>`;
    }
    h+=`</div>`;
  }
  box.innerHTML=h;
}
function ddnsPick(slug){ DDNS_SEL=slug; renderDdns(); }
function _ddnsMsg(btn,txt,col){ const m=btn.parentElement.querySelector(".ddns-msg")||document.querySelector(".ddns-msg"); if(m){ m.textContent=txt; m.style.color=col||"var(--dim)"; } }
async function ddnsSave(btn){
  const box=document.getElementById("ddnsBox");
  const values={}; box.querySelectorAll(".ddns-field").forEach(i=>{ if(i.value.trim()) values[i.dataset.key]=i.value.trim(); });
  btn.disabled=true; _ddnsMsg(btn,"saving & updating…");
  try{
    const r=await postAction("/cockpit/ddns/save",{provider:DDNS_SEL,values},70000); const d=await r.json();
    _ddnsMsg(btn, d.detail||(d.ok?"saved":"failed"), d.ok?"var(--ok)":"var(--bad)");
    if(d.ok){ DDNS_SEL=DDNS_SEL; await loadDdns(); }
  }catch(e){ _ddnsMsg(btn,"save failed: "+esc(e),"var(--bad)"); }
  btn.disabled=false;
}
async function ddnsUpdate(btn){
  btn.disabled=true; _ddnsMsg(btn,"updating…");
  try{ const r=await postAction("/cockpit/ddns/update",{},70000); const d=await r.json();
    _ddnsMsg(btn, d.detail||(d.ok?"updated":"failed"), d.ok?"var(--ok)":"var(--bad)"); await loadDdns(); }
  catch(e){ _ddnsMsg(btn,"update failed: "+esc(e),"var(--bad)"); }
  btn.disabled=false;
}
async function ddnsDisable(btn){
  btn.disabled=true; _ddnsMsg(btn,"…");
  try{ const r=await postAction("/cockpit/ddns/disable",{}); const d=await r.json();
    _ddnsMsg(btn, d.detail||"done", "var(--ok)"); await loadDdns(); }
  catch(e){ _ddnsMsg(btn,"failed: "+esc(e),"var(--bad)"); }
  btn.disabled=false;
}
async function ddnsTailscale(btn){
  btn.disabled=true; _ddnsMsg(btn,"checking…");
  try{ const r=await postAction("/cockpit/ddns/tailscale-up",{}); const d=await r.json();
    _ddnsMsg(btn, d.detail||(d.ok?"done":"see terminal"), d.ok?"var(--ok)":"var(--warn)"); await loadDdns(); }
  catch(e){ _ddnsMsg(btn,"failed: "+esc(e),"var(--bad)"); }
  btn.disabled=false;
}

// ── Voice transcription setup ──────────────────────────────────────────────────
async function loadVoiceSetup(){
  const box=document.getElementById('voiceSetupBox'); const btn=document.getElementById('voiceInstallBtn');
  if(!box) return;
  try{
    const d=await j('/cockpit/voice/status');
    const stt=d.stt||{}, tts=d.tts||{}, cfg=d.config||{}, hf=d.hands_free||{};
    const dot=(ok)=>'<span style="color:'+(ok?'var(--ok)':'var(--warn)')+'">'+(ok?'✓':'○')+'</span>';
    let html='<div style="font-size:13px;line-height:1.8">';
    html+='<div>'+dot(tts.available)+' Speech output ('+esc(tts.command||'say')+')</div>';
    html+='<div>'+dot(stt.ready)+' Speech input '+dot(stt.whisper)+'Whisper '+dot(stt.ffmpeg)+'ffmpeg '+dot(stt.sox)+'sox</div>';
    html+='<div>'+dot(!!d.browser_push_to_talk)+' Push-to-talk in the cockpit</div>';
    html+='<div class="muted" style="margin-top:6px;font-size:12px">Wake word: '+esc(cfg.wake_word||'mongoose')+' · model: '+esc(cfg.stt_model||'')+'</div>';
    html+='</div>';
    html+='<div class="muted" style="margin-top:8px;font-size:12px;border-top:1px solid var(--line);padding-top:8px"><b>Hands-free roadmap:</b> '+esc(hf.roadmap||'')+'</div>';
    box.innerHTML=html;
    if(btn) btn.style.display = stt.ready ? 'none' : '';
  }catch(e){ try{ box.innerHTML='<div style="color:var(--muted);font-size:12px">⚠ Voice status unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadVoiceSetup()">Retry</button></div>'; console.warn('loadVoiceSetup failed:',e); }catch(_){} }
}
async function installVoice(btn){
  const out=document.getElementById('voiceInstallOut'); const box=document.getElementById('voiceSetupBox');
  btn.disabled=true; btn.textContent='Installing…';
  out.innerHTML='<div class="muted">Installing sox, ffmpeg, and openai-whisper, this can take a few minutes. The page will tell you when it\'s done.</div>';
  try{
    const d=await postAction('/setup/voice',{});
    const data=await d.json();
    if(data.ok){
      out.innerHTML=`<span class="ok">${esc(data.message)}</span>`;
      if(box) box.innerHTML='<span class="ok">✓ Voice toolchain ready.</span>';
      btn.style.display='none';
    } else {
      out.innerHTML=`<span class="bad">${esc(data.message)}</span>`;
      btn.disabled=false; btn.textContent='Retry install';
    }
  }catch(e){ out.textContent='Install failed: '+e; btn.disabled=false; btn.textContent='Retry'; }
}

// ── Voice picker (browser TTS) ─────────────────────────────────────────────────
const VOICE_PREF_KEY='mongoose_tts_voice';
const VOICE_RATE_KEY='mongoose_tts_rate';
function ttsRate(){
  const v=parseFloat(localStorage.getItem(VOICE_RATE_KEY)||'1.15');
  return (isFinite(v) && v>=0.5 && v<=2.0)? v : 1.15;  // default a touch faster than 1.0
}
function initVoicePicker(){
  const sel=document.getElementById('voicePicker'); if(!sel) return;
  function populate(){
    let voices=speechSynthesis.getVoices(); if(!voices.length) return;
    const _en=voices.filter(v=>/^en(-|_|$)/i.test(v.lang||'')); if(_en.length) voices=_en;
    let saved=localStorage.getItem(VOICE_PREF_KEY)||'';
    if(!saved || !voices.some(v=>v.voiceURI===saved)){ saved=(voices[0]||{}).voiceURI||''; try{ if(saved) localStorage.setItem(VOICE_PREF_KEY,saved); }catch(_e){} }
    sel.innerHTML=voices.map((v,i)=>`<option value="${esc(v.voiceURI)}"${v.voiceURI===saved?' selected':''}>${esc(v.name)} (${esc(v.lang)})</option>`).join('');
    if(saved) sel.value=saved;
  }
  populate();
  if(speechSynthesis.onvoiceschanged!==undefined) speechSynthesis.onvoiceschanged=populate;
  sel.addEventListener('change',()=>{
    localStorage.setItem(VOICE_PREF_KEY,sel.value);
    const c=document.getElementById('voiceSavedFlash');
    if(c){ c.textContent='Saved \u2713'; c.style.opacity='1';
      setTimeout(()=>{ c.style.opacity='0'; }, 1800); }
  });
  // Speed slider (if present): persist and live-preview.
  const rate=document.getElementById('voiceRate');
  const rateLabel=document.getElementById('voiceRateLabel');
  if(rate){
    rate.value=String(ttsRate());
    if(rateLabel) rateLabel.textContent=ttsRate().toFixed(2)+'×';
    rate.addEventListener('input',()=>{
      localStorage.setItem(VOICE_RATE_KEY,rate.value);
      if(rateLabel) rateLabel.textContent=parseFloat(rate.value).toFixed(2)+'×';
    });
  }
}
function testVoice(){
  const sel=document.getElementById('voicePicker'); if(!sel) return;
  const voices=speechSynthesis.getVoices();
  const picked=voices.find(v=>v.voiceURI===sel.value);
  const u=new SpeechSynthesisUtterance("Hi, I'm Mongoose. How can I help?");
  if(picked){ u.voice=picked; u.lang=picked.lang; }
  u.rate=ttsRate();
  speechSynthesis.cancel(); speechSynthesis.speak(u);
}
// Shared helper: read preferred voice for use in chat/voice replies
function preferredVoice(){
  const saved=localStorage.getItem(VOICE_PREF_KEY)||'';
  if(!saved||!window.speechSynthesis) return null;
  return speechSynthesis.getVoices().find(v=>v.voiceURI===saved)||null;
}

async function loadWebhook(){
  const box=document.getElementById('webhookBox');
  const out=document.getElementById('webhookOut');
  if(!box) return;
  try{
    const d=await j('/webhook/setup');
    if(d.ddns_needed){
      box.innerHTML='<span class="warn">⚠ DDNS not configured, Mongoose needs a public address before {{site.domain}} can reach it. Set up DuckDNS or Cloudflare above first.</span>';
      return;
    }
    if(!d.configured){
      box.innerHTML=`<div style="margin-bottom:8px">Ingest URL: <code style="font-size:12px;color:var(--gold)">${esc(d.ingest_url||'(set up DDNS first)')}</code></div>
        <div class="muted small" style="margin-bottom:10px">No webhook secret yet. Generate one, then paste it and the ingest URL into the WordPress plugin settings.</div>
        <button class="act go" onclick="generateWebhookSecret()">Generate webhook secret</button>`;
    } else {
      box.innerHTML=`<span class="ok">✓ Webhook active</span>
        <div style="margin-top:8px"><label class="muted small">Ingest URL</label>
          <div style="display:flex;gap:6px;margin-top:4px">
            <input id="wh-url-val" readonly value="${esc(d.ingest_url||'')}"
              style="flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px;font-size:13px">
            <button class="act" onclick="copyText('wh-url-val')" style="padding:8px 12px">Copy</button>
          </div>
        </div>
        <div style="margin-top:8px">
          <button class="act" onclick="generateWebhookSecret()">Regenerate secret</button>
          <span class="muted small" style="margin-left:8px">The WordPress plugin must be updated with the new secret if regenerated.</span>
        </div>`;
    }
  }catch(e){ try{ box.innerHTML='<div style="color:var(--muted);font-size:12px">⚠ Webhook status unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadWebhook()">Retry</button></div>'; console.warn('loadWebhook failed:',e); }catch(_){} }
}
async function generateWebhookSecret(){
  const out=document.getElementById('webhookOut'); if(!out) return;
  out.textContent='Generating…';
  try{
    const r=await postAction('/webhook/generate-secret',{});
    const d=await r.json();
    if(d.ok){
      out.innerHTML=`<div style="margin-top:10px;background:var(--panel2);border:1px solid var(--gold);border-radius:10px;padding:12px">
        <div class="muted small" style="margin-bottom:6px">✓ Webhook secret generated. Copy both values into the WordPress plugin Settings → RTM Settings:</div>
        <div style="margin-bottom:8px"><label class="muted small">Ingest URL</label>
          <div style="display:flex;gap:6px;margin-top:4px">
            <input id="wh-url-gen" readonly value="${esc(d.ingest_url||'')}"
              style="flex:1;background:var(--ink);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:7px;font-size:13px">
            <button class="act go" onclick="copyText('wh-url-gen')">Copy</button>
          </div>
        </div>
        <div><label class="muted small">Webhook secret</label>
          <div style="display:flex;gap:6px;margin-top:4px">
            <input id="wh-sec-gen" readonly value="${esc(d.secret||'')}"
              style="flex:1;background:var(--ink);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:7px;font-size:12px;font-family:monospace">
            <button class="act go" onclick="copyText('wh-sec-gen')">Copy</button>
          </div>
        </div>
        <div class="muted small" style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
          <b>Don't have the plugin on {{site.domain}} yet?</b><br>
          1. Build it: go to the <b>Build</b> tab and click <i>Build plugin</i> (or run the RTM build plan) to produce the plugin zip.<br>
          2. Download the built zip from the Build tab.<br>
          3. On {{site.domain}}: <b>WP Admin → Plugins → Add New → Upload Plugin</b>, choose the zip, Install, then Activate.<br>
          4. In WP Admin open <b>Settings → RTM Settings</b> and paste the Ingest URL and Webhook secret above.<br>
          On activation the plugin reports its version back here, and the RTM screen's sync strip turns green.
        </div>
      </div>`;
      loadWebhook();
    } else {
      out.innerHTML=`<span class="bad">✗ ${esc(d.error||'failed')}</span>`;
    }
  }catch(e){ out.textContent='Error: '+e; }
}
function copyText(id){
  const el=document.getElementById(id); if(!el) return;
  el.select(); document.execCommand('copy');
  const orig=el.nextElementSibling?.textContent;
  if(el.nextElementSibling) el.nextElementSibling.textContent='Copied!';
  setTimeout(()=>{ if(el.nextElementSibling && orig) el.nextElementSibling.textContent=orig; },1500);
}

async function runDoctor(){ const out=document.getElementById("setupout");
  out.textContent="running doctor...";
  try{ const d=await j("/doctor");
    let lines=[];
    const checks=d.checks||d.results||{};
    for(const [k,v] of Object.entries(checks)){
      const ok = (v && (v.ok===true || v.status==="ok" || v.status==="configured"));
      const detail = (v && (v.detail||v.message||v.status)) || "";
      lines.push((ok?"\u2713 ":"\u2022 ")+k+(detail?": "+detail:""));
    }
    const head = "doctor: "+(d.ok!==false?"no blocking issues":"issues found");
    out.innerHTML = '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:12px;white-space:pre-wrap;line-height:1.5">'
      + esc(head+(lines.length?"\n"+lines.join("\n"):"")) + '</div>'; }
  catch(e){ out.textContent="doctor failed: "+esc(e); } }
async function installMissing(){ const o=document.getElementById("setupout");
  // Plan/confirm flow: show WHAT will be installed and WHERE before doing it.
  o.textContent="checking what's missing...";
  try{
    const plan=await j("/setup/install-missing?plan=1");
    const items=plan.plan||[];
    if(!items.length){ o.textContent="nothing missing to install."; return; }
    const lines=items.map(p=>"  "+esc(p.capability)+": "+esc((p.missing_pip||[]).join(", "))).join("\n");
    if(!confirm("Will install:\n"+lines+"\n\nInto: "+(plan.python||"the active venv")+"\n\nProceed?")) { o.textContent="cancelled."; return; }
    o.textContent="installing...";
    const r=await postAction("/setup/install-missing"); const data=await r.json();
    o.textContent="install: "+esc(data.detail||JSON.stringify(data));
  }catch(e){ o.textContent="install endpoint unavailable ("+e+"); use: mongoose update --install-missing"; }
}
function repair(){ const o=document.getElementById("setupout"); if(o)o.textContent="see the per-capability hint above; many repairs are: mongoose setup --capability <name>"; }
// Phone-push setup status + actions. Surfaces the Docker state so a user whose Docker isn't running
// has an in-app path (start Docker, or use native ntfy) instead of being stuck.
async function loadPush(){
  const box=document.getElementById("pushbox"); if(!box) return;
  try{
    const s=await j("/cockpit/push-status");
    let h="";
    if(s.docker_running){
      h=`<div><span class="dot ok"></span>Docker is running, push containers can run here. Push is good to go.</div>`;
    } else if(s.native_ntfy){
      h=`<div><span class="dot ok"></span>Native push (ntfy) is installed, no Docker needed. Push is good to go.</div>`;
    } else {
      h=`<div style="margin-bottom:10px"><span class="dot ${s.docker_installed?'unknown':'bad'}"></span>${s.docker_installed?"Docker is installed but not running.":"Docker isn't running."} Push isn't active yet. Pick one:</div>`;
      if(s.docker_installed){
        h+=`<div class="muted" style="font-size:11px;opacity:.75;margin:-4px 0 8px">The push/search containers (ntfy + SearXNG) are already defined in your install folder's compose file, they just need the Docker daemon running to launch. Starting Docker below brings them up automatically.</div>`;
      }
      h+=`<div style="display:flex;flex-direction:column;gap:8px">`;
      if(s.can_start_docker){
        h+=`<button class="act go" onclick="startDocker(this)">Start Docker now</button>`;
      } else if(!s.docker_installed){
        h+=`<div class="muted" style="font-size:12px">Docker Desktop isn't installed. <a href="https://www.docker.com/products/docker-desktop/" target="_blank" rel="noopener noreferrer" style="color:var(--gold)">Get Docker</a>, or use native push below (recommended, no Docker needed).</div>`;
      }
      h+=`<div class="muted" style="font-size:12px">Or set up native push (no Docker, no Rosetta). This installs a local ntfy and a background service for you:</div>
      <button class="act go" onclick="setupNtfy(this)" style="margin-top:6px">Set up native push</button>
      <span id="ntfymsg" class="muted" style="font-size:11px;margin-left:8px"></span>`;
      h+=`</div>`;
    }
    box.innerHTML=h;
  }catch(e){ try{ box.classList.remove("muted"); box.innerHTML=`<div style="color:var(--muted);font-size:12px">⚠ Temporarily unavailable. <button style="background:none;border:none;color:var(--gold);cursor:pointer;font-size:12px;padding:0" onclick="loadPush()">Retry</button></div>`; console.warn("loadPush failed:", e); }catch(_){} }
}
async function setupNtfyFromCard(btn){
  const msg=btn.closest(".wiz-formsrc")?.querySelector(".wiz-msg");
  btn.disabled=true; const orig=btn.textContent; btn.textContent="setting up native push (can take a minute)...";
  if(msg){ msg.textContent="installing ntfy and registering the background service..."; msg.className="wiz-msg muted"; }
  try{
    const r=await postAction("/cockpit/setup-ntfy",{}); const d=await r.json();
    if(d.ok){ btn.textContent="\u2713 native push ready";
      if(msg){ msg.innerHTML=`<span style="color:var(--ok)">Subscribe in the ntfy phone app${d.topic?(" to topic: "+esc(d.topic)):""}.</span>`; }
    } else { btn.disabled=false; btn.textContent=orig;
      if(msg){ msg.innerHTML=`<span style="color:var(--warn)">${esc(d.error||"could not set up native push")}</span>`; } }
  }catch(e){ btn.disabled=false; btn.textContent=orig; if(msg){ msg.innerHTML=`<span style="color:var(--bad)">setup failed: ${esc(e)}</span>`; } }
}
async function setupNtfy(btn){
  const msg=document.getElementById("ntfymsg");
  btn.disabled=true; const orig=btn.textContent; btn.textContent="setting up native push (can take a minute)...";
  if(msg){ msg.textContent="installing ntfy and registering the background service..."; msg.style.color="var(--dim)"; }
  try{
    const r=await postAction("/cockpit/setup-ntfy",{}); const d=await r.json();
    if(d.ok){ btn.textContent="✓ native push ready";
      if(msg){ msg.textContent="Subscribe in the ntfy phone app"+(d.topic?(" to topic: "+esc(d.topic)):"")+"."; msg.style.color="var(--ok)"; }
      _reloadSoon(loadPush, 1500);
    } else { btn.disabled=false; btn.textContent=orig;
      if(msg){ msg.textContent=esc(d.error||"could not set up native push"); msg.style.color="var(--warn)"; } }
  }catch(e){ btn.disabled=false; btn.textContent=orig; if(msg){ msg.textContent="setup failed: "+esc(e); msg.style.color="var(--bad)"; } }
}
async function startDocker(btn){
  btn.disabled=true; const orig=btn.textContent; btn.textContent="starting Docker (can take ~30s)...";
  try{
    const r=await postAction("/cockpit/start-docker",{}); const d=await r.json();
    if(d.ok){ btn.textContent="✓ "+(d.detail||"Docker is up"); _reloadSoon(loadPush, 1500); }
    else { btn.disabled=false; btn.textContent=orig;
      const box=document.getElementById("pushbox");
      if(box) box.insertAdjacentHTML("beforeend", `<div class="muted" style="color:var(--warn);font-size:12px;margin-top:8px">${esc(d.error||"could not start Docker")}</div>`); }
  }catch(e){ btn.disabled=false; btn.textContent=orig; }
}

async function loadActivity(){
  const el=document.getElementById("activity");
  try{
    const [chg,data,rcp]=await Promise.all([
      j("/cockpit/what-changed").catch(()=>({changes:[]})),
      j("/bus/list?limit=40").catch(()=>({messages:[]})),
      j("/cockpit/receipts?limit=30").catch(()=>({entries:[]}))]);
    let html="";
    // Body data card: upload Alpha Progression (sessions/sets/body-comp) and optional MyFitnessPal
    // exports so per-muscle progression and nutrition become real. Mongoose also asks for this in chat
    // occasionally; this is the matching place to satisfy it. Status fetched live so it shows what's
    // present and what's still wanted.
    try{
      const bd=await j("/body/data/status").catch(()=>null);
      if(bd && bd.data){
        const d=bd.data;
        const has=(b)=>b?'<span style="color:var(--ok)">✓</span>':'<span style="color:var(--dim)">, </span>';
        let ask="";
        if(bd.request){ ask=`<div class="muted" style="margin:6px 0 8px">${esc(bd.request.ask)}</div>`; }
        html+=`<div class="card"><h2>Body data</h2>
          ${ask}
          <div class="muted" style="font-size:13px;margin-bottom:8px">
            Alpha Progression: ${has(d.alpha_present)} sessions/sets${d.alpha_stale?' <span style="color:var(--warn)">(a few weeks old, a fresh export would help)</span>':''} ·
            MyFitnessPal: ${has(d.mfp_present)} nutrition <span style="color:var(--dim)">(optional)</span>
          </div>
          <input type="file" id="bdfile" accept=".csv,text/csv" multiple style="width:100%;font:inherit;color:var(--text);margin-bottom:8px">
          <div class="muted" style="font-size:12px;margin-bottom:8px">Pick your Alpha sessions and sets CSVs (and body-comp / a MyFitnessPal export if you have them). Mongoose detects each kind automatically.</div>
          <button class="act go" id="bdbtn" onclick="uploadBodyData(this)" style="width:100%">Import body data</button>
          <div id="bdout" class="body" style="font-size:13px;margin-top:10px"></div></div>`;
      }
    }catch(_e){}
    // The receipts ledger leads: one honest answer to "what did Mongoose do?".
    const rec=rcp.entries||[];
    html+=`<div class="card"><h2>What Mongoose did</h2>`;
    if(!rec.length) html+=`<div class="muted">Nothing recorded yet. As Mongoose syncs, runs jobs, and drafts or sends on your approval, those actions show up here, an honest trace, not a guess.</div>`;
    else { html+=`<div class="timeline">`;
      for(const e of rec){
        const t=(e.at||"").replace("T"," ").slice(0,16);
        const st=e.status?` · ${esc(e.status)}`:"";
        html+=`<div class="row"><span class="time">${esc(t)}</span><span class="v" style="text-align:left;flex:1">${esc(e.source)}: ${esc(e.text)}${st}</span></div>`;
      }
      html+=`</div>`;
    }
    html+=`</div>`;
    const changes=chg.changes||[];
    html+=`<div class="card"><h2>What changed since you last looked</h2>`;
    if(!changes.length) html+=`<div class="muted">Nothing new across WhatsApp, email, files or notifications.</div>`;
    else html+=changes.map(c=>`<div class="row"><span class="v" style="text-align:left;flex:1">${esc(c)}</span></div>`).join("");
    html+=`</div>`;
    const msgs=data.messages||data.items||[];
    html+=`<div class="card"><h2>Recent activity (bus)</h2><div class="timeline">`;
    if(!msgs.length) html+=`<div class="muted">No recent bus events.</div>`;
    for(const m of msgs){
      const t=(m.created_at||m.ts||"").toString().replace("T"," ").slice(0,19);
      const text=m.subject||m.title||m.body||m.text||JSON.stringify(m).slice(0,120);
      html+=`<div class="row"><span class="time">${esc(t)}</span><span class="v" style="text-align:left;flex:1">${esc(text)}</span></div>`;
    }
    html+=`</div></div>`;
    // Guest message threads
    try{
      const n=await ensureNonce();
      const mm=await fetch("/cockpit/messages",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
      if(mm && mm.ok && mm.messages && mm.messages.length){
        html+=`<div class="card"><h2>Messages with guests${mm.unread?` <span style="color:var(--gold);font-size:13px">(${mm.unread} unread)</span>`:""}</h2>`;
        for(const msg of mm.messages.slice(-15)){
          const who=esc(msg.from||msg.sender||"?");
          const body=esc((msg.body||msg.text||"").slice(0,100));
          const when=esc(msg.at||msg.timestamp||"");
          html+=`<div class="row" style="margin:4px 0"><span class="k">${who}</span>
            <span class="v" style="text-align:left;flex:1">${body} <span class="muted" style="font-size:11px">${when}</span></span></div>`;
        }
        html+=`</div>`;
      }
    }catch(e){}
    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Activity", e, "loadActivity"); }
}

async function loadApprovals(){
  const el=document.getElementById("approvals");
  try{
    const data=await j("/cockpit/approvals"); const items=data.approvals||[]; const c=data.counts||{};
    window.__approvalActions=[];
    let html=`<div class="card"><h2>Needs approval${items.length?` (${items.length})`:""}</h2>`;
    if(!items.length) html+=`<div class="muted">Nothing waiting. Mongoose only queues real decisions: WhatsApp drafts, bugs (including ones your guests raise), and imported-knowledge choices.</div>`;
    for(const a of items){
      let actions=a.actions;
      if(!actions){ actions=[]; if(a.approve) actions.push({label:"Approve",style:"go",url:a.approve}); if(a.cancel) actions.push({label:"Cancel",style:"no",url:a.cancel}); }
      const tag=a.from_guest?`<span style="font-size:11px;color:var(--accent);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-left:6px">guest</span>`:"";
      let btns="";
      for(const ac of actions){ const idx=window.__approvalActions.length; window.__approvalActions.push(ac);
        btns+=`<button class="act ${ac.style==='no'?'no':'go'}" onclick="runApproval(${idx})">${esc(ac.label)}</button> `; }
      html+=`<div class="row"><span class="k"><b>${esc(a.label||a.kind)}</b>${tag}
             <div class="muted" style="white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;margin-top:5px;padding:8px 10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;font-size:13px">${esc(a.body||"")}</div></span>
             <span class="v">${btns}</span></div>`;
    }
    if(c.knowledge_more) html+=`<div class="muted" style="margin-top:8px;font-size:12px">+${c.knowledge_more} more knowledge items to review in the Knowledge tab.</div>`;
    html+=`</div>`;
    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Approvals", e, "loadApprovals"); }
}
async function runApproval(idx){
  const ac=(window.__approvalActions||[])[idx]; if(!ac) return;
  const row=document.querySelector('[onclick="runApproval('+idx+')"]');
  const card=row&&(row.closest(".row")||row.closest(".card"));
  if(card) card.style.opacity=.5;
  try{ await postActionOk(ac.url, ac.body||{}); loadApprovals(); }
  catch(e){ if(card) card.style.opacity=1; alert("Action not completed: "+(e&&e.message||e)); }
}
async function tuneAct(action){ try{ await postAction("/cockpit/tuning/"+action); loadOverview(); }catch(e){ alert("tuning "+action+" failed: "+e); } }
async function refreshReadiness(){ try{ await postAction("/cockpit/readiness/refresh",{}); loadOverview(); }catch(e){ alert("readiness refresh failed: "+e); } }
async function cancelAlert(id){ try{ await postAction("/cockpit/alerts/cancel/"+id); loadOverview(); }catch(e){ alert("cancel failed: "+e); } }
async function doSomethingCool(){
  const out=document.getElementById("coolout");
  if(out) out.innerHTML='<span class="muted">Thinking...</span>';
  try{
    const r=await (await fetch("/cockpit/tutorial/cool",{method:"POST"})).json();
    const tag=r.is_sample?'<div style="font-size:11px;color:var(--dim);margin-bottom:4px">EXAMPLE (not your real data)</div>':"";
    if(out) out.innerHTML=tag+'<div style="font-size:14px;line-height:1.6">'+esc(r.speech)+'</div>';
  }catch(e){ if(out) out.innerHTML='<span class="muted">Could not run the demo: '+esc(e)+'</span>'; }
}


async function loadRoutines(){
  const el=document.getElementById("routines-box"); if(!el) return;
  try{
    const n=await ensureNonce();
    const d=await fetch("/cockpit/routines",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(!d.ok || !d.routines || !d.routines.length){
      el.innerHTML='<div class="muted" style="font-size:12px">No routines saved yet.</div>';
      return;
    }
    let html="";
    for(const r of d.routines){
      const steps=Array.isArray(r.steps)?r.steps.length:(r.step_count||0);
      html+=`<div class="row"><span class="k">${esc(r.name||r.id||"routine")}</span>
        <span class="v muted">${esc(r.description||(steps+" step"+(steps===1?"":"s")))}</span></div>`;
    }
    el.innerHTML=html;
  }catch(e){ el.innerHTML='<div class="muted">Could not load routines.</div>'; }
}

async function loadKnowledge(){
  const box=document.getElementById("sys-knowledge"); if(!box) return;
  const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}};
  try{
    const d=await j("/cockpit/knowledge/dashboard",hdr);
    const c=(d&&d.counts)||{};
    let h='<div class="card"><h2>Import a knowledge dump</h2>'
      +'<div class="muted" style="margin-bottom:8px">Upload the dump ZIP in Files &amp; staging, then paste its asset id (or a path). Only user-stated, high-confidence records reach memory; health, medication, supplement and assistant-suggested items are held for review.</div>'
      +'<input id="kiAsset" placeholder="asset id or path" style="width:100%;background:#141109;border:1px solid var(--line);border-radius:9px;padding:9px;color:var(--text)">'
      +'<button class="act go" style="margin-top:8px" onclick="kiImport()">Import</button> '
      +'<button class="act" style="margin-top:8px" onclick="kiReflect()">Run reflection</button>'
      +'<div id="kiImportMsg" class="muted" style="margin-top:8px"></div></div>';
    h+='<div class="card"><h2>Knowledge graph</h2><div class="muted">'
      +`${c.imports||0} imports &middot; ${c.claims||0} claims &middot; ${c.entities||0} entities &middot; ${c.edges||0} edges &middot; ${c.insights||0} insights &middot; ${c.review_open||0} to review &middot; ${c.research_queued||0} research queued`
      +'</div></div>';
    const items=(d&&d.review)||[];
    h+=`<div class="card"><h2>Needs review (${items.length})</h2>`;
    if(!items.length){ h+='<div class="muted">Nothing waiting.</div>'; }
    for(const it of items){
      const rid=it.review_id||it.id;
      h+=`<div class="row"><span class="k"><span class="dot ${it.severity==='high'?'gold':''}"></span>${esc(it.reason||it.object_type||'review')}<div class="muted" style="font-size:11px">${esc(it.object_type||'')} &middot; ${esc(it.severity||'')}</div></span>`
        +`<span class="v"><button class="act go" onclick="kiResolve('${esc(rid)}','approve')">Approve</button> `
        +`<button class="act" onclick="kiResolve('${esc(rid)}','keep_searchable')">Keep searchable</button> `
        +`<button class="act" onclick="kiResolve('${esc(rid)}','needs_research')">Research</button> `
        +`<button class="act" onclick="kiResolve('${esc(rid)}','reject')">Reject</button></span></div>`;
    }
    h+='</div>';
    const insights=(d&&d.insights)||[];
    h+=`<div class="card"><h2>Insights (${insights.length})</h2>`;
    if(!insights.length){ h+='<div class="muted">No insight candidates yet. Import a dump to generate some.</div>'; }
    for(const ic of insights){
      const iid=ic.insight_id||ic.id; const risky=(ic.risk_level==='high');
      h+=`<div class="row"><span class="k"><b>${esc(ic.title||'')}</b>${risky?' <span style="color:var(--gold);font-size:11px">consent-gated</span>':''}`
        +`<div class="muted" style="font-size:11px">${esc(ic.domain||'')} &middot; ${esc(ic.actionability||'')} &middot; ${esc(ic.status||'')}${ic.research_status==='needed'?' &middot; research needed':''}</div></span>`
        +`<span class="v"><button class="act" onclick="kiWhy('${esc(iid)}')">Why?</button> `
        +`<button class="act" onclick="kiFeedback('${esc(iid)}','helpful')">Helpful</button> `
        +`<button class="act" onclick="kiFeedback('${esc(iid)}','too_much')">Too much</button></span></div>`;
    }
    h+='</div>';
    h+='<div class="card"><h2>Search your knowledge</h2><div style="display:flex;gap:8px"><input id="ki-search-q" placeholder="claims, insights, entities, care, synthesis..." style="flex:1;background:#141109;border:1px solid var(--line);border-radius:9px;padding:9px;color:var(--text)" onkeydown="if(event.key===\'Enter\')kiSearch()"><button class="act go" onclick="kiSearch()">Search</button></div><div id="ki-search-res" class="muted" style="margin-top:8px"></div></div>';
    h+='<div class="card"><h2>Knowledge graph map</h2><div id="ki-graph" class="muted">Loading map&hellip;</div></div>';
    h+='<div class="card"><h2>Care suggestions</h2><div class="muted" style="margin-bottom:6px">Gentle, contextual routines you can turn on. Sensitive health and psychological items never appear here automatically.</div><div id="ki-care" class="muted">Loading&hellip;</div></div>';
    h+='<div class="card"><h2>Duplicate entities <button class="act" style="float:right;font-size:12px" onclick="kiSuggestFusions(this)">Find duplicates</button></h2><div class="muted" style="margin-bottom:6px">Same real thing under two names (e.g. vitamin D and cholecalciferol). Merging is always your call.</div><div id="ki-fusion" class="muted">Loading&hellip;</div></div>';
    h+='<div class="card"><h2>Synthesis cards <button class="act" style="float:right;font-size:12px" onclick="kiRunSynthesis(this)">Run synthesis</button></h2><div class="muted" style="margin-bottom:6px">Cross-claim summaries, reviewable and never turned into advice on their own.</div><div id="ki-synth" class="muted">Loading&hellip;</div></div>';
    h+='<div class="card"><h2>Research findings</h2><div class="muted" style="margin-bottom:6px">Snippet-only evidence cards, gathered for high-risk or externally-changing insights. Review before acting.</div><div id="ki-research" class="muted">Loading&hellip;</div></div>';
    box.innerHTML=h;
    kiLoadGraph(); kiLoadCare(); kiLoadFusion(); kiLoadSynth(); kiLoadResearch();
  }catch(e){ box.innerHTML='<div class="muted">Could not load knowledge layer ('+esc(String(e))+').</div>'; }
}
async function kiImport(){
  const v=(document.getElementById("kiAsset")||{}).value||""; const m=document.getElementById("kiImportMsg");
  if(!v.trim()){ if(m) m.textContent="Enter an asset id or path first."; return; }
  if(m) m.textContent="Importing...";
  const body = v.indexOf("/")>=0 ? {path:v.trim()} : {asset_id:v.trim()};
  try{ const r=await postAction("/cockpit/knowledge/import",body); const d=await r.json();
    if(m) m.textContent=d.ok?`Imported ${d.records_imported||0} records, ${d.canonical_memories_created||0} to memory, ${d.records_needing_review||0} to review.`:("Failed: "+(d.error||"unknown")); loadKnowledge();
  }catch(e){ if(m) m.textContent="Failed: "+e; }
}
async function kiReflect(){ const m=document.getElementById("kiImportMsg"); if(m) m.textContent="Reflecting...";
  try{ const r=await postAction("/cockpit/knowledge/reflect",{}); await r.json(); if(m) m.textContent="Reflection cycle run."; loadKnowledge(); }catch(e){ if(m) m.textContent="Failed: "+e; } }
async function kiResolve(id,action){ try{ const r=await postAction("/cockpit/knowledge/review/resolve",{review_id:id,action:action}); await r.json(); loadKnowledge(); }catch(e){} }
async function kiFeedback(id,fb){ try{ const r=await postAction("/cockpit/knowledge/insight/feedback",{insight_id:id,feedback:fb}); await r.json(); loadKnowledge(); }catch(e){} }
async function kiWhy(id){ try{ const r=await postAction("/cockpit/knowledge/research",{topic:"",insight_id:id}); }catch(e){}
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const d=await j("/cockpit/knowledge/insights",hdr); const found=((d&&d.insights)||[]).find(x=>(x.insight_id||x.id)===id); alert(found?(found.why||found.hypothesis||found.title||"No explanation."):"No explanation available."); }catch(e){ alert("Could not load explanation."); } }
async function kiLoadGraph(){ const el=document.getElementById('ki-graph'); if(!el) return;
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const g=await j('/cockpit/knowledge/graph',hdr); el.innerHTML=kiGraphSvg(g); }
  catch(e){ el.textContent='Could not load the graph.'; } }
function kiGraphSvg(g){
  const nodes=(g&&g.nodes)||[]; const edges=(g&&g.edges)||[];
  if(!nodes.length) return '<div class="muted">No entities yet. Import a dump to build the graph.</div>';
  const W=320,H=270,cx=W/2,cy=H/2,R=Math.min(W,H)/2-34; const n=nodes.length; const pos={};
  nodes.forEach(function(nd,i){ const a=2*Math.PI*i/n - Math.PI/2; pos[nd.id]={x:cx+R*Math.cos(a),y:cy+R*Math.sin(a)}; });
  const maxDeg=Math.max.apply(null,nodes.map(function(x){return x.degree||1;}))||1;
  let s='<svg viewBox="0 0 '+W+' '+H+'" width="100%" style="max-width:360px;background:var(--panel2,#0e0c08);border-radius:12px">';
  edges.forEach(function(e){ const a=pos[e.source],b=pos[e.target]; if(a&&b){ s+='<line x1="'+a.x.toFixed(1)+'" y1="'+a.y.toFixed(1)+'" x2="'+b.x.toFixed(1)+'" y2="'+b.y.toFixed(1)+'" stroke="var(--line,#444)" stroke-width="0.6" opacity="0.55"/>'; } });
  nodes.forEach(function(nd){ const p=pos[nd.id]; const r=3+6*Math.sqrt((nd.degree||1)/maxDeg); const col=(nd.sensitivity==='high')?'var(--gold,#e0b000)':'var(--accent,#25D366)';
    s+='<circle cx="'+p.x.toFixed(1)+'" cy="'+p.y.toFixed(1)+'" r="'+r.toFixed(1)+'" fill="'+col+'" opacity="0.9"><title>'+esc((nd.name||'')+' ('+(nd.degree||0)+' links)')+'</title></circle>'; });
  nodes.slice(0,6).forEach(function(nd){ const p=pos[nd.id]; s+='<text x="'+p.x.toFixed(1)+'" y="'+(p.y-9).toFixed(1)+'" font-size="8" fill="var(--text,#ddd)" text-anchor="middle">'+esc((nd.name||'').slice(0,14))+'</text>'; });
  s+='</svg><div class="muted" style="font-size:11px;margin-top:4px">'+nodes.length+' of '+((g&&g.total_entities)||nodes.length)+' entities shown (most-connected). Gold = sensitive.</div>';
  return s;
}
async function kiLoadCare(){ const el=document.getElementById('ki-care'); if(!el) return;
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const d=await j('/cockpit/knowledge/care',hdr);
    const cand=(d&&d.candidates)||[]; const act=(d&&d.active)||[]; let h='';
    if(act.length){ h+='<div class="muted" style="margin-bottom:6px">Active plans ('+act.length+'):</div>';
      act.forEach(function(p){ h+='<div class="row"><span class="k">'+esc(p.title||p.insight_id||'plan')+'<div class="muted" style="font-size:11px">'+esc(p.domain||'')+' &middot; '+esc(p.cadence||'contextual')+'</div></span><span class="v"><button class="act" onclick="kiPauseCare(\''+esc(p.plan_id||'')+'\')">Pause</button></span></div>'; }); }
    if(!cand.length){ h+='<div class="muted">Nothing to suggest right now.</div>'; }
    cand.forEach(function(c){ h+='<div class="row"><span class="k"><b>'+esc(c.title||'')+'</b><div class="muted" style="font-size:11px">'+esc(c.domain||'')+' &middot; '+esc(c.risk_level||'')+' risk &middot; score '+esc(String(c.score||''))+'</div><div class="muted" style="font-size:11px;margin-top:2px">'+esc((c.text||c.why||'').slice(0,120))+'</div></span><span class="v"><button class="act go" onclick="kiActivateCare(\''+esc(c.insight_id||'')+'\')">Activate</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load care suggestions.'; } }
async function kiActivateCare(id){ if(!id) return; try{ const r=await postAction('/cockpit/knowledge/care/activate',{insight_id:id}); await r.json(); loadKnowledge(); }catch(e){} }
async function kiPauseCare(id){ if(!id) return; try{ const r=await postAction('/cockpit/knowledge/care/pause',{plan_id:id}); await r.json(); loadKnowledge(); }catch(e){} }
async function kiLoadResearch(){ const el=document.getElementById('ki-research'); if(!el) return;
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const d=await j('/cockpit/knowledge/research/list',hdr);
    const rs=(d&&d.research)||[]; if(!rs.length){ el.innerHTML='<div class="muted">No research yet.</div>'; return; }
    let h='';
    rs.forEach(function(r){ h+='<div style="margin-bottom:10px"><div><b>'+esc((r.topic||'').slice(0,70))+'</b> <span class="muted" style="font-size:11px">'+esc(r.domain||'')+' &middot; '+esc(r.status||'')+'</span></div>';
      const ev=(r.evidence||[]);
      if(!ev.length){ h+='<div class="muted" style="font-size:11px">No source cards'+(r.status==='failed'?' (search unavailable)':'')+'.</div>'; }
      ev.forEach(function(c){ h+='<div class="row" style="align-items:flex-start"><span class="k" style="max-width:100%"><a href="'+esc(c.url||'#')+'" target="_blank" rel="noopener" style="color:var(--accent)">'+esc((c.title||'source').slice(0,64))+'</a><div class="muted" style="font-size:11px">'+esc(c.source||'')+' &middot; '+esc(c.source_quality||'')+'</div><div class="muted" style="font-size:11px;margin-top:2px">'+esc((c.snippet||'').slice(0,150))+'</div></span></div>'; });
      h+='</div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load research findings.'; } }
async function kiSearch(){ const q=(document.getElementById('ki-search-q')||{}).value||''; const el=document.getElementById('ki-search-res'); if(!el) return;
  if(!q.trim()){ el.textContent='Type something to search.'; return; }
  el.textContent='Searching\u2026';
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const d=await j('/cockpit/knowledge/search?q='+encodeURIComponent(q.trim()),hdr);
    const rs=(d&&d.results)||[]; if(!rs.length){ el.innerHTML='<div class="muted">No matches.</div>'; return; }
    let h='<div class="muted" style="margin-bottom:6px">'+rs.length+' match(es):</div>';
    rs.forEach(function(r){ h+='<div class="row"><span class="k"><b>'+esc((r.title||'').slice(0,60))+'</b> <span class="muted" style="font-size:11px">'+esc((r.kind||'').replace('knowledge_',''))+'</span><div class="muted" style="font-size:11px;margin-top:2px">'+esc((r.body||'').slice(0,140))+'</div></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Search failed.'; } }
async function kiLoadFusion(){ const el=document.getElementById('ki-fusion'); if(!el) return;
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const d=await j('/cockpit/knowledge/fusion/proposals',hdr);
    const sug=(d&&d.suggestions)||[]; const act=(d&&d.active)||[]; let h='';
    if(act.length){ h+='<div class="muted" style="margin-bottom:6px">'+act.length+' merge(s) already applied.</div>'; }
    const open=sug.filter(function(s){ return (s.status||'open')==='open'; });
    if(!open.length){ h+='<div class="muted">No duplicate suggestions right now. Use Find duplicates to scan.</div>'; }
    open.forEach(function(s){ h+='<div class="row"><span class="k"><b>'+esc(s.canonical_name||'')+'</b> = '+esc(s.duplicate_name||'')+'<div class="muted" style="font-size:11px">'+esc(s.strategy||'')+' &middot; confidence '+esc(String(s.confidence||''))+'</div></span><span class="v"><button class="act go" onclick="kiResolveFusion(\''+esc(s.suggestion_id||'')+'\',\'approve\')">Merge</button> <button class="act" onclick="kiResolveFusion(\''+esc(s.suggestion_id||'')+'\',\'reject\')">Keep separate</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load duplicate suggestions.'; } }
async function kiResolveFusion(id,action){ if(!id) return; try{ const r=await postAction('/cockpit/knowledge/fusion/resolve',{suggestion_id:id,action:action}); await r.json(); kiLoadFusion(); }catch(e){} }
async function kiSuggestFusions(btn){ if(btn) btn.disabled=true; const el=document.getElementById('ki-fusion'); if(el) el.textContent='Scanning for duplicates\u2026';
  try{ const r=await postAction('/cockpit/knowledge/entity/suggest-fusions',{}); await r.json(); }catch(e){} finally{ if(btn) btn.disabled=false; } kiLoadFusion(); }
async function kiLoadSynth(){ const el=document.getElementById('ki-synth'); if(!el) return;
  try{ const hdr={headers:{'X-Cockpit-Nonce': await ensureNonce()}}; const d=await j('/cockpit/knowledge/synthesis',hdr);
    const cards=(d&&d.cards)||[]; if(!cards.length){ el.innerHTML='<div class="muted">No synthesis cards yet. Run synthesis to generate some.</div>'; return; }
    let h=''; cards.forEach(function(c){ const risky=(c.risk_level==='high'); h+='<div class="row"><span class="k"><b>'+esc((c.title||'').slice(0,64))+'</b>'+(risky?' <span style="color:var(--gold);font-size:11px">sensitive</span>':'')+'<div class="muted" style="font-size:11px">'+esc(c.domain||'')+' &middot; '+esc(c.actionability||'')+' &middot; conf '+esc(String(c.confidence||''))+'</div><div class="muted" style="font-size:11px;margin-top:2px">'+esc((c.summary||'').slice(0,150))+'</div></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load synthesis cards.'; } }
async function kiRunSynthesis(btn){ if(btn) btn.disabled=true; const el=document.getElementById('ki-synth'); if(el) el.textContent='Running synthesis\u2026';
  try{ const r=await postAction('/cockpit/knowledge/synthesis/run',{}); await r.json(); }catch(e){} finally{ if(btn) btn.disabled=false; } kiLoadSynth(); }

async function loadSystemExtras(){
  const el=document.getElementById("sys-extra"); if(!el) return;
  let html="";
  const n=await ensureNonce();
  // FAST local extras first, in parallel, rendered immediately. The two protocol-v3 cards SSH to the Pi at
  // about 0.6s each, so they are DEFERRED below and never gate the Status tab from populating.
  const [b, lk]=await Promise.all([
    fetch("/cockpit/brain/wakeup",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json()).catch(()=>null),
    fetch("/cockpit/lock/status").then(r=>r.json()).catch(()=>null)
  ]);
  // Brain wake-up health
  if(b && (b.ok!==undefined || b.working_memory!==undefined || b.last_wakeup)){
    const dot=b.ok===false?"warn":"ok";
    html+=`<div class="card"><h2>Brain wake-up</h2>
      <div class="big"><span class="dot ${dot}"></span>${esc(b.summary||(b.ok===false?"degraded":"came back with full working memory"))}</div>`;
    if(b.last_wakeup) html+=`<div class="row"><span class="k">Last wake</span><span class="v">${esc(b.last_wakeup)}</span></div>`;
    if(b.repaired && b.repaired.length) html+=`<div class="muted" style="margin-top:6px">Repaired: ${esc(b.repaired.join(", "))}</div>`;
    html+=`</div>`;
  }
  // Lock / session config
  if(lk && (lk.lock_local!==undefined || lk.session_ttl_hours!==undefined)){
    html+=`<div class="card"><h2>Session &amp; lock</h2>
      <div class="row"><span class="k">Require login on the Mac</span><span class="v">${lk.lock_local?"yes":"no"}</span></div>`;
    if(lk.session_ttl_hours!=null) html+=`<div class="row"><span class="k">Session timeout</span><span class="v">${esc(String(lk.session_ttl_hours))}h</span></div>`;
    if(lk.remember_ttl_days!=null) html+=`<div class="row"><span class="k">"Remember me"</span><span class="v">${esc(String(lk.remember_ttl_days))} days</span></div>`;
    html+=`</div>`;
  }
  el.innerHTML=html;
  const base=html;
  // DEFERRED: the two Pi-over-SSH cards. Fetched in parallel AFTER paint and appended when they arrive, so a
  // slow or dead Pi link can never stall the Status tab. No await here on purpose.
  Promise.all([
    fetch("/cockpit/protocol-v3/bus",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json()).catch(()=>null),
    fetch("/cockpit/protocol-v3/voicemail",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json()).catch(()=>null)
  ]).then(([bus, vm])=>{
    let extra="";
    // Protocol v3 bus tail (tri-agent coordination): only renders when v3 is active
    if(bus && bus.ok && bus.messages && bus.messages.length){
      extra+=`<div class="card"><h2>Agent bus</h2>
        <div class="muted" style="margin-bottom:8px;font-size:12px">Recent messages addressed to Mongoose on the shared Chat/Code/Mongoose bus.</div>`;
      for(const msg of bus.messages.slice(-6)){
        extra+=`<div class="row" style="margin:4px 0"><span class="k">${esc(msg.from||"?")} &#8594; ${esc(msg.to||"?")}</span>
          <span class="v muted" style="font-size:12px">${esc((msg.topic||"")+": "+(msg.body||"").slice(0,50))}</span></div>`;
      }
      extra+=`</div>`;
    }
    // Protocol v3 voicemail staging (RTM)
    if(vm && vm.ok && vm.voicemails && vm.voicemails.length){
      extra+=`<div class="card"><h2>Voicemail staging</h2>
        <div class="muted" style="margin-bottom:8px;font-size:12px">{{rtm.name}} voicemails waiting on the bus (peek only, not acknowledged).</div>`;
      for(const v of vm.voicemails.slice(0,5)){
        extra+=`<div class="poke-history-item">${esc((v.from||"unknown")+": "+(v.summary||v.transcript||"").slice(0,60))}</div>`;
      }
      extra+=`</div>`;
    }
    if(extra) el.innerHTML=base+extra;
  });
}

async function genSelfSigned(){
  const out=document.getElementById("tls-out");
  if(out) out.textContent="generating certificate…";
  try{
    const n=await ensureNonce();
    const d=await fetch("/cockpit/tls/self-signed",{method:"POST",headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(out){
      if(d.ok) out.innerHTML='<span class="ok">Certificate created. Restart Mongoose with HTTPS, then open <b>https://'+esc(d.lan_ip)+':'+esc(String(d.port||8765))+'</b> on your phone (accept the one-time warning). Voice and notifications will then work.</span>';
      else out.textContent=d.error||"could not generate certificate";
    }
  }catch(e){ if(out) out.textContent="error: "+e; }
}

async function testPush(){
  const out=document.getElementById("push-test-out");
  if(out) out.textContent="sending…";
  try{
    const n=await ensureNonce();
    const d=await fetch("/push/test",{method:"POST",headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(out) out.innerHTML = d.sent>0 ? '<span class="ok">Sent to '+d.sent+' device(s)</span>' : ('No devices received it'+(d.reason?(' ('+esc(d.reason)+')'):''));
  }catch(e){ if(out) out.textContent="error"; }
}

async function loadMusic(){
  const el=document.getElementById("music"); if(!el) return;
  el.innerHTML='<div class="card muted">Loading music…</div>';
  try{
    const m=await fetch("/cockpit/music").then(r=>r.json());
    let html="";

    if(m.opted_out){
      // The user turned proactive music OFF. Offer to turn it back on. (Chat-time music curiosity is
      // unaffected by this either way; this card is only about Mongoose bringing music up on his own.)
      html+=`<div class="card" style="border:1px solid var(--gold)">
        <h2>🎵 Mongoose's music streak</h2>
        <div class="muted" style="margin:8px 0 14px">Proactive music is currently <b>off</b>, so Mongoose won't bring music up on his own (he'll still chat about it if you do). Turn it back on and he'll occasionally drop a playful music question and surface recommendations over time.</div>
        <button class="act go" onclick="musicOpt(true)">Let Mongoose bring up music again</button></div>`;
      el.innerHTML=html;
      return;
    }

    // Default (proactive music on): show profile + next question + recommend
    html+=`<div class="card"><h2>🎵 Your music profile</h2>`;
    if(m.summary){
      html+=`<div class="muted" style="margin-bottom:10px">${esc(m.summary)}</div>`;
    }
    const prof=m.profile||{};
    const facetLabels={fave_singers:"Favourite singers",fave_female_singers:"Female vocalists",
      fave_male_singers:"Male vocalists",fave_bands:"Bands",fave_genres:"Genres",
      fave_decades:"Decades",soundtracks:"Soundtracks",childhood_hits:"Childhood hits",
      teenage_obsessions:"Teenage obsessions",recent_discoveries:"Recent discoveries"};
    let any=false;
    for(const [facet,label] of Object.entries(facetLabels)){
      const v=prof[facet];
      if(v && (Array.isArray(v)?v.length:v)){
        any=true;
        const disp=Array.isArray(v)?v.join(", "):v;
        html+=`<div class="row"><span class="k">${esc(label)}</span><span class="v">${esc(disp)}</span></div>`;
      }
    }
    if(!any) html+=`<div class="muted">Nothing learned yet: answer a question below to get started.</div>`;
    html+=`<div style="margin-top:10px"><a href="#" onclick="musicOpt(false);return false" style="font-size:12px;color:var(--dim)">Stop bringing up music on your own</a></div>`;
    html+=`</div>`;

    // Next question card
    if(m.next_question && m.next_facet){
      html+=`<div class="card"><h2>Mongoose wants to know</h2>
        <div style="margin:8px 0 12px;font-style:italic;color:var(--text)">"${esc(m.next_question)}"</div>
        <input id="music-answer" type="text" placeholder="your answer..." style="width:100%;margin-bottom:10px">
        <button class="act go" onclick="musicAnswer('${esc(m.next_facet)}')">Tell Mongoose</button>
        <div id="music-answer-msg" class="muted" style="margin-top:8px"></div></div>`;
    } else if(m.onboarded){
      html+=`<div class="card"><div class="muted">Mongoose knows your taste well now. He'll surface recommendations as he learns more about new music.</div></div>`;
    }

    // A fresh recommendation nudge
    html+=`<div class="card"><h2>Ask for a nudge</h2>
      <div class="muted" style="margin-bottom:8px;font-size:12px">Mongoose phrases a playful music thought. He never invents fake songs.</div>
      <button class="act" onclick="musicNudge()">Say something musical</button>
      <div id="music-nudge-box" class="muted" style="margin-top:10px"></div></div>`;

    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Music", e, "loadMusic"); }
}

async function musicOpt(enabled){
  try{
    const n=await ensureNonce();
    await fetch("/cockpit/music/opt?enabled="+(enabled?"true":"false"),
      {method:"POST",headers:{"X-Cockpit-Nonce":n}});
    loadMusic();
  }catch(e){}
}

async function musicAnswer(facet){
  const inp=document.getElementById("music-answer");
  const msg=document.getElementById("music-answer-msg");
  const val=(inp&&inp.value||"").trim();
  if(!val){ if(msg) msg.textContent="type an answer first"; return; }
  try{
    const n=await ensureNonce();
    const r=await fetch("/cockpit/music/answer",{method:"POST",
      headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n},
      body:JSON.stringify({facet:facet,value:val})});
    const d=await r.json();
    if(d.ok){ if(msg) msg.innerHTML='<span class="ok">Got it!</span>'; _reloadSoon(loadMusic, 900); }
    else { if(msg) msg.textContent=d.error||"could not save"; }
  }catch(e){ if(msg) msg.textContent="error"; }
}

async function musicNudge(){
  const box=document.getElementById("music-nudge-box");
  if(box) box.textContent="...";
  try{
    const d=await fetch("/cockpit/music/recommend").then(r=>r.json());
    if(box) box.innerHTML='<span style="color:var(--text);font-style:italic">"'+esc(d.text||"")+'"</span>';
  }catch(e){ if(box) box.textContent="couldn't think of anything"; }
}

async function loadBenchmarks(targetId){
  const el=document.getElementById(targetId||"benchmarks"); if(!el) return;
  el.innerHTML='<div class="card muted">Loading benchmarks…</div>';
  try{
    const m=await fetch("/cockpit/benchmarks").then(r=>r.json());
    let html="";

    // Overnight benchmark lane on/off: honest state plus the previously-missing enable
    // journey. Before this the card claimed "runs automatically 5-8am" even when the lane
    // was off and would never run, with no way to see that or switch it on.
    const benchOn = !!m.enabled;
    const win = esc(m.window||"05:00-08:00 local");
    html+=`<div class="card"><h2>Overnight benchmarking</h2>
      <div class="big"><span class="dot ${benchOn?"ok":"warn"}"></span>${benchOn?"On":"Off"}</div>
      <div class="muted" style="margin-top:6px">${benchOn
        ? "Mongoose benchmarks your models in the background ("+win+") and suggests a better setup once it has evidence. It runs overnight, not on demand."
        : "Overnight benchmarking is off, so Mongoose is not measuring your models or suggesting setups. Turn it on and it benchmarks in the quiet "+win+" window (not right now)."}</div>
      <button class="act ${benchOn?"no":"go"}" style="margin-top:10px" onclick="toggleBenchmark(${benchOn?"false":"true"},this)">${benchOn?"Turn off":"Turn on overnight benchmarking"}</button>
      <div id="benchToggleMsg" class="muted" style="margin-top:8px"></div></div>`;

    // Lane 2 readiness card
    const l2ready = m.lane2_ready;
    const l2dot = l2ready ? "ok" : "warn";
    const l2label = l2ready ? "Lane 2 available" : "Still building a baseline";
    html+=`<div class="card"><h2>Model tuning (Lane 2)</h2>
      <div class="big"><span class="dot ${l2dot}"></span>${esc(l2label)}</div>
      <div class="muted" style="margin-top:6px">${esc(m.lane2_summary||"Mongoose benchmarks itself 5-8am in the background. Once enough real-use data and benchmark evidence has vested, it surfaces the best model setup for your patterns.")}</div></div>`;

    // Last benchmark run
    if(m.last_run_at){
      html+=`<div class="card"><h2>Last benchmark run</h2>
        <div class="row"><span class="k">Ran at</span><span class="v">${esc(m.last_run_at)}</span></div>`;
      if(m.last_run_duration_s) html+=`<div class="row"><span class="k">Duration</span><span class="v">${esc(String(m.last_run_duration_s)+"s")}</span></div>`;
      if(m.tasks_run!=null) html+=`<div class="row"><span class="k">Tasks run</span><span class="v">${esc(String(m.tasks_run))}</span></div>`;
      html+="</div>";
    } else {
      html+=`<div class="card"><h2>Last benchmark run</h2><div class="muted">${m.enabled?("No benchmark run recorded yet. The first run happens overnight in the "+esc(m.window||"05:00-08:00 local")+" window."):"No runs yet: overnight benchmarking is off (turn it on above)."}</div></div>`;
    }

    // Current model setup
    if(m.current_model || m.models){
      const models = m.models || {default: m.current_model};
      html+=`<div class="card"><h2>Current model setup</h2>`;
      for(const [role,name] of Object.entries(models)){
        html+=`<div class="row"><span class="k">${esc(role)}</span><span class="v">${esc(name||"not set")}</span></div>`;
      }
      html+="</div>";
    }

    // Last revelation
    if(m.last_revelation_at){
      html+=`<div class="card"><h2>Last tuning suggestion</h2>
        <div class="row"><span class="k">Surfaced</span><span class="v">${esc(m.last_revelation_at)}</span></div>
        ${m.last_revelation_body?`<div class="muted" style="margin-top:6px">${esc(m.last_revelation_body)}</div>`:""}
        </div>`;
    }

    // Benchmark history: every dated run, its model(s), and each captured task's prompt + outcome, so
    // the owner can see all runs and their conclusions (not just the latest). Read-only.
    if(m.history && m.history.length){
      html+=`<div class="card"><h2>Benchmark history</h2>
        <div class="muted" style="font-size:12px;margin-bottom:8px">Every recorded benchmark run, newest first. Tasks shown are the ones Mongoose captured a diagnosis for.</div>`;
      for(const run of m.history){
        const models = (run.models||[]).join(", ");
        html+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
          <div class="row"><span class="k">${esc(run.date||"?")}</span><span class="v muted">${esc(models)}</span></div>`;
        for(const t of (run.tasks||[])){
          html+=`<div style="margin:6px 0 6px 8px;padding-left:8px;border-left:2px solid var(--line)">
            <div style="font-size:13px"><b>${esc(t.task||"?")}</b> <span class="muted">${esc(t.model||"")}</span></div>`;
          if(t.prompt) html+=`<div class="muted" style="font-size:12px;margin-top:2px">Prompt: ${esc(t.prompt)}</div>`;
          if(t.summary) html+=`<div class="muted" style="font-size:12px;margin-top:2px">Output: ${esc(t.summary)}</div>`;
          if(t.outcome) html+=`<div class="muted" style="font-size:12px;margin-top:2px">${esc(t.outcome)}</div>`;
          html+=`</div>`;
        }
        if(!(run.tasks||[]).length) html+=`<div class="muted" style="font-size:12px;margin-left:8px">No per-task diagnoses recorded for this run.</div>`;
        html+=`</div>`;
      }
      html+="</div>";
    }

    // Embeddings (semantic search) benchmark, exposed alongside the model benchmarks.
    try{
      const eb=await j("/cockpit/embeddings-benchmark");
      html+=`<div class="card"><h2>Embeddings (semantic search)</h2>`;
      if(eb.available){
        html+=`<div class="big"><span class="dot ok"></span>Available</div>
          <div class="row"><span class="k">Dimensions</span><span class="v">${esc(String(eb.dimensions||"?"))}</span></div>
          <div class="row"><span class="k">Speed</span><span class="v">${esc(String(eb.ms_per_text||"?"))} ms/text (${esc(String(eb.batch_size||0))} texts in ${esc(String(eb.ms_total||"?"))} ms)</span></div>`;
        if(eb.crosslingual_similarity!=null) html+=`<div class="row"><span class="k">Cross-lingual match (EN/PL)</span><span class="v">${esc(String(eb.crosslingual_similarity))}</span></div>`;
        html+=`<div class="muted" style="margin-top:6px;font-size:12px">Local multilingual model (MiniLM). Powers fuzzy/semantic matching so reworded or cross-language phrasing still matches. One local model, so this is a health and speed check, not a comparison.</div>`;
      } else {
        const filesOk = eb.files===true;
        const detail = filesOk
          ? "The model is already downloaded. It just needs two small runtime libraries (onnxruntime, tokenizers) to activate."
          : (eb.hint||"Semantic search model is not present; matching falls back to word overlap.");
        html+=`<div class="big"><span class="dot warn"></span>Not installed</div>
          <div class="muted" style="margin-top:6px">${esc(detail)}</div>
          <button class="act go" onclick="installEmbeddings(this)" style="margin-top:10px">${filesOk?"Activate semantic search":"Install semantic search (~500MB)"}</button>
          <div id="embInstallMsg" class="muted" style="margin-top:6px;font-size:12px"></div>`;
      }
      html+=`</div>`;
    }catch(_e){}

    if(!html) html=`<div class="card"><div class="muted">No benchmark data yet.</div></div>`;
    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Benchmarks", e, "loadBenchmarks"); }
}

async function loadModels(targetId){
  const el=document.getElementById(targetId||"models"); if(!el) return;
  try{
    const m=await j("/cockpit/models");
    let html="";
    // Model roles: what each role is for, which model is in effect, and where it came from.
    if(m.roles && m.roles.length){
      html+=`<div class="card"><h2>Model roles</h2>
        <div class="muted" style="margin-bottom:10px">Mongoose uses a different local model for different jobs. Each starts on a sensible default; overnight benchmarking can suggest or apply a better one (reversible), and you can pin any role yourself in your .env.</div>`;
      for(const r of m.roles){
        const col = r.source==="benchmark"?"var(--gold)":(r.source==="env"?"var(--ok)":"var(--dim)");
        const inst = r.installed===false?` <span style="color:var(--bad)" title="this model is not pulled on this machine">not installed</span> <button class="act" data-model="${esc(r.model)}" onclick="installModel(this)" style="font-size:11px;padding:2px 8px">Install</button>`:(r.installed===true?` <span style="color:var(--ok)">installed</span>`:"");
        html+=`<div class="row" style="align-items:flex-start"><span class="k" style="flex:1">
          <b>${esc(r.label)}</b>
          <div class="muted" style="font-size:12px">${esc(r.purpose)}</div>
          <div style="margin-top:4px;font-family:'IBM Plex Mono',monospace;font-size:12px">${esc(r.model)}${inst}</div></span>
          <span class="v"><span title="where this model came from: ${esc(r.env_var)}" style="display:inline-block;font-size:11px;border:1px solid ${col};color:${col};border-radius:20px;padding:2px 8px;white-space:nowrap">${esc(r.source_label)}</span></span></div>`;
      }
      if(m.how_to_change) html+=`<div class="muted" style="margin-top:10px;font-size:12px">${esc(m.how_to_change)}</div>`;
      html+=`</div>`;
    }
    html+=`<div class="card"><h2>Model architecture</h2>
      <div class="row"><span class="k">conversation owner (speaks)</span><span class="v">${esc(m.owner)}</span></div>
      <div class="row"><span class="k">research worker (silent)</span><span class="v">${esc(m.worker)}</span></div>
      <div class="row"><span class="k">pair co-resident</span><span class="v"><span class="dot ${dotFor(m.pair_resident)}"></span>${esc(m.pair_resident)}</span></div>`;
    if(m.loaded) html+=`<div class="row"><span class="k">currently loaded</span><span class="v">${esc((m.loaded||[]).join(", "))||"(none)"}</span></div>`;
    html+=`<div class="muted" style="margin-top:8px">${esc(m.note||"")}</div></div>`;
    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Models", e, "loadModels"); }
}

async function installModel(btn){
  const model = btn && btn.dataset ? btn.dataset.model : "";
  if(!model) return;
  if(!confirm('Download "'+model+'"?\n\nThis can be several GB and runs in the background. You can leave this page; it keeps going.')) return;
  btn.disabled=true; btn.textContent="Starting...";
  try{
    await postActionOk("/cockpit/models/pull",{model:model});
    pollInstall(model, btn, {onDone:function(){ try{loadModels("sys-models");}catch(_e){} }});
  }catch(e){ btn.disabled=false; btn.textContent="Retry"; }
}
async function installEmbeddings(btn){
  if(!confirm("Install semantic search?\n\nThis installs two small CPU libraries and (if missing) downloads the model, in the background. You can leave this page.")) return;
  btn.disabled=true; btn.textContent="Starting...";
  const msg=document.getElementById("embInstallMsg"); if(msg) msg.textContent="";
  try{
    await postActionOk("/cockpit/embeddings/install",{});
    pollInstall("embeddings", btn, {msgId:"embInstallMsg", onDone:function(){ try{loadBenchmarks("sys-benchmarks");}catch(_e){} }});
  }catch(e){ btn.disabled=false; btn.textContent="Retry"; if(msg) msg.textContent="Could not start: "+(e&&e.message||e); }
}
// One hardened poller for every install journey. Survives a card refresh (detached button),
// a mid-install server restart (state goes 'idle' -> stop and let the user re-check), reports
// errors honestly, and hard-stops after ~2h so it can never spin forever.
function pollInstall(model, btn, opts){
  opts = opts || {};
  const setMsg = function(t){ if(opts.msgId){ const m=document.getElementById(opts.msgId); if(m) m.textContent=t; } };
  let tries=0, idleSeen=0;
  const timer=setInterval(async function(){
    tries++;
    if(btn && !document.body.contains(btn)){ clearInterval(timer); return; }
    try{
      const s=await j("/cockpit/models/pull-status?model="+encodeURIComponent(model));
      if(s.state==="running"){
        idleSeen=0;
        const pct=(s.pct!=null && s.pct>0)? (Math.round(s.pct)+"%") : (s.status_text||"working");
        if(btn) btn.textContent="Installing: "+pct;
        setMsg(s.status_text||"");
      } else if(s.state==="done"){
        clearInterval(timer);
        const needRestart=(s.status_text||"").indexOf("restart")>=0;
        if(btn){ btn.textContent = needRestart? "Installed (restart to activate)" : "Installed"; }
        setMsg(s.status_text||"done");
        if(opts.onDone) setTimeout(opts.onDone, 1400);
      } else if(s.state==="error"){
        clearInterval(timer);
        if(btn){ btn.disabled=false; btn.textContent="Retry"; }
        setMsg("Failed: "+((s.error||"error").slice(0,160)));
      } else if(s.state==="idle"){
        idleSeen++;
        if(idleSeen>=3){ clearInterval(timer); if(btn){ btn.disabled=false; btn.textContent="Re-check"; }
          setMsg("Lost track of the install (Mongoose may have restarted). It may still be running; re-check in a moment."); }
      }
    }catch(_e){}
    if(tries>3600){ clearInterval(timer); }
  }, 2500);
}
async function exportRTMPack(){
  const out=document.getElementById("rtmExportOut"); if(out)out.textContent="exporting...";
  try{
    const r=await postAction("/rtm/manage/export-pack",{dir:"data/rtm_management_export"});
    const d=await r.json();
    if(out) out.textContent = d.ok? `exported ${d.file_count||0} files to ${d.dir}` : ("failed: "+esc(d.error||""));
  }catch(e){ if(out)out.textContent="failed: "+esc(e); }
}
function spToggle(){ const f=document.getElementById('sp-fields'); if(f) f.style.display=(f.style.display==='none'?'block':'none'); }
async function genFirstStage(){
  const out=document.getElementById('sp-out');
  const g=id=>((document.getElementById(id)||{}).value||'').trim();
  const flat=g('spFlat');
  if(!flat){ if(out)out.textContent='Enter a flat first.'; return; }
  if(out)out.textContent='Generating the cover letter and summary…';
  const body={flat:flat};
  if(g('spSeller'))body.seller_name=g('spSeller');
  if(g('spFirm'))body.conveyancer_firm=g('spFirm');
  if(g('spContact'))body.conveyancer_contact=g('spContact');
  if(g('spEmail'))body.conveyancer_email=g('spEmail');
  if(g('spTheirRef'))body.their_ref=g('spTheirRef');
  if(g('spReqDate'))body.request_date=g('spReqDate');
  const sv=document.getElementById('spSave'); if(sv&&sv.checked)body.save=true;
  try{
    const d=await postAction('/cockpit/rtm/seller-pack/first-stage',body,120000).then(x=>x.json());
    if(!d||!d.ok){ if(out)out.textContent='Could not generate: '+((d&&(d.error||d.reason))||'unknown'); return; }
    let h='<div style="margin-bottom:6px">'+esc(d.premises||'')+' &middot; '+esc(d.our_ref||'')+' &middot; fee '+esc(d.fee||'')+'</div>';
    h+='<div style="display:flex;flex-wrap:wrap;gap:10px;margin:8px 0">';
    if(d.cover_letter&&d.cover_letter.download_url) h+='<a class="act go" href="'+esc(d.cover_letter.download_url)+'" target="_blank" rel="noopener">Cover letter (PDF)</a>';
    if(d.admin_charge_summary&&d.admin_charge_summary.download_url) h+='<a class="act" href="'+esc(d.admin_charge_summary.download_url)+'" target="_blank" rel="noopener">Admin-charge summary (PDF)</a>';
    h+='</div>';
    if((d.flags||[]).length){ h+='<div style="color:var(--warn);font-size:12px;margin-top:6px"><b>Fix before issue:</b><ul style="margin:4px 0 0 16px">'+d.flags.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul></div>'; }
    if((d.before_issue||[]).length){ h+='<div class="muted" style="font-size:12px;margin-top:6px"><ul style="margin:4px 0 0 16px">'+d.before_issue.map(f=>'<li>'+esc(f)+'</li>').join('')+'</ul></div>'; }
    if(out)out.innerHTML=h;
  }catch(e){ if(out)out.textContent='Generate failed.'; }
}

async function dmPreview(){
  const out=document.getElementById("dm-out"); if(!out) return; out.textContent="Loading preview...";
  try{
    const d=await j("/cockpit/demand-mailing");
    if(!d||!d.ok){ out.textContent="Preview failed: "+esc((d&&d.error)||"unknown"); return; }
    let h=`<div>Would email <b>${d.recipient_count}</b> for ${esc(d.period)} (${esc(d.period_end)}); <b>${d.excluded_count}</b> excluded.</div>`;
    if((d.problems||[]).length) h+=`<div style="color:#b23"><span class="dot bad"></span>Problems: ${esc(d.problems.join("; "))}</div>`;
    h+=`<table style="width:100%;margin-top:8px;font-size:13px;border-collapse:collapse">`;
    for(const b of (d.bundles||[])){ h+=`<tr><td>${esc(b.unit)}</td><td style="word-break:break-all">${esc(b.to)}</td><td style="text-align:right">${esc(b.amount)}</td><td style="text-align:center">${b.ready?"&#10003;":"&#10007;"}</td></tr>`; }
    h+=`</table>`;
    h+=`<details style="margin-top:8px"><summary class="muted">Excluded (${d.excluded_count})</summary>`;
    for(const e of (d.excluded||[])){ h+=`<div class="row"><span class="k">${esc(e.unit||e.flat_id)}</span><span class="v muted">${esc(e.reason)}</span></div>`; }
    h+=`</details>`;
    out.innerHTML=h;
  }catch(e){ out.textContent="Preview error."; }
}
async function dmSend(priority){
  let n=0;
  try{ const d=await j("/cockpit/demand-mailing"); n=(d&&d.recipient_count)||0; }catch(e){}
  const hi=(priority==="high");
  const typed=prompt("This SENDS "+n+" real demand emails to leaseholders"+(hi?" as HIGH PRIORITY":"")+", one each with its demand PDF. Type SEND to confirm.");
  if(typed!=="SEND") return;
  const out=document.getElementById("dm-out"); if(out)out.textContent="Sending"+(hi?" (high priority)":"")+", one at a time...";
  try{
    const r=await postAction("/cockpit/demand-mailing/send",{confirm:true,priority:(hi?"high":"normal")},120000).then(x=>x.json());
    if(!r.ok && r.error){ if(out)out.textContent="Refused: "+esc(r.error); return; }
    let h=`<div>Sent <b>${r.sent}</b>, skipped ${r.skipped}, failed <b>${r.failed}</b>.</div>`;
    h+=(r.results||[]).map(x=>`<div class="row"><span class="k">${esc(x.to)}</span><span class="v muted">${esc(x.outcome)}${x.reason?(" &middot; "+esc(x.reason)):""}</span></div>`).join("");
    if(out)out.innerHTML=h;
  }catch(e){ if(out)out.textContent="Send error (it may still have partly sent, check the log)."; }
}
async function dmLog(){
  const out=document.getElementById("dm-out"); if(!out) return; out.textContent="Loading log...";
  try{
    const d=await j("/cockpit/demand-mailing/log");
    const L=(d&&d.log)||[];
    if(!L.length){ out.textContent="Nothing sent yet."; return; }
    out.innerHTML=L.map(x=>`<div class="row"><span class="k">${esc(x.to_email)}</span><span class="v muted">${esc(x.status)} &middot; ${esc((x.sent_at||"").slice(0,16))}</span></div>`).join("");
  }catch(e){ out.textContent="Log error."; }
}

async function loadRTM(){
  const el=document.getElementById("rtm");
  try{
    const [dash,demands,payables,bankFeed]=await Promise.all([
      j("/rtm/manage/dashboard"),
      j("/rtm/manage/demands?limit=8").catch(()=>({demands:[]})),
      j("/rtm/manage/payables").catch(()=>({payables:[]})),
      j("/rtm/bank-feed/status").catch(()=>({accounts:[],providers:[],next_actions:[]})),
    ]);
    let html=`<div class="card"><h2>{{rtm.name}} RTM</h2>
      <div class="big">Director dashboard</div>
      <div class="muted">Demand register, Metro reconciliation, arrears, payables, budget actuals and evidence. Review-only: Mongoose does not send demands or move money.</div>
      <button class="act go" style="margin-top:10px" onclick="exportRTMPack()">Export management pack</button>
      <span id="rtmExportOut" class="muted" style="margin-left:8px"></span></div>
      <div class="card" id="rtm-seller-pack"><h2>Seller pack (LPE1)</h2>
        <div class="muted">Generate the first-stage reply to an LPE1 request: the branded cover letter plus the statutory administration-charge summary, ready to print or attach. Artifacts only. Mongoose sends nothing and does not release the completed form until the fee is paid.</div>
        <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center">
          <input id="spFlat" placeholder="Flat (e.g. 7 or {{RTM_BLOCK_A}}F7)" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);min-width:170px">
          <button class="act" onclick="spToggle()">Conveyancer details</button>
          <button class="act go" onclick="genFirstStage()">Generate first-stage response</button>
        </div>
        <div id="sp-fields" style="display:none;margin-top:10px;flex-wrap:wrap;gap:8px">
          <input id="spSeller" placeholder="Seller name (e.g. Miss Jane Doe)" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);width:100%;margin-bottom:6px">
          <input id="spFirm" placeholder="Conveyancer firm" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);width:100%;margin-bottom:6px">
          <input id="spContact" placeholder="Contact (FAO)" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);width:100%;margin-bottom:6px">
          <input id="spEmail" placeholder="Conveyancer email" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);width:100%;margin-bottom:6px">
          <input id="spTheirRef" placeholder="Their reference" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);width:100%;margin-bottom:6px">
          <input id="spReqDate" placeholder="Request date (YYYY-MM-DD)" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text);width:100%;margin-bottom:6px">
          <label class="muted" style="font-size:12px"><input type="checkbox" id="spSave" style="vertical-align:middle"> Remember these on the job</label>
        </div>
        <div id="sp-out" class="muted" style="margin-top:10px">Enter a flat and generate. If the flat already has a saved job (like Flat 7), its details are prefilled.</div></div>
      <div id="rtm-director"><div class="card"><div class="muted">Loading director financials&hellip;</div></div></div>`;
    html+=`<div class="card"><h2>Next actions</h2>`;
    const _acts=(dash.next_actions||[]).filter(a=>a.type!=="ok");
    if(_acts.length){
      for(const a of _acts){
        const dot=a.urgency==="high"?"bad":(a.urgency==="medium"?"warn":"unknown");
        html+=`<div class="row"><span class="k"><span class="dot ${dot}"></span>${esc(a.title)}</span><span class="v muted">${esc(a.action||"")}</span></div>`;
      }
    } else {
      html+=`<div class="row"><span class="k"><span class="dot ok"></span>All caught up. Reconcile weekly to keep it that way.</span></div>`;
    }
    html+=`</div>`;
    const pos=dash.status||{};
    html+=`<div class="card"><h2>Money position</h2>
      <div class="row"><span class="k">Demands raised</span><span class="v">${esc(pos.demands_raised||"£0.00")}</span></div>
      <div class="row"><span class="k">Payments allocated</span><span class="v">${esc(pos.payments_allocated||"£0.00")}</span></div>
      <div class="row"><span class="k">Arrears outstanding</span><span class="v">${esc(pos.arrears_outstanding||"£0.00")}</span></div>
      <div class="row"><span class="k">Committed outgoings</span><span class="v">${esc(pos.committed_outgoings||"£0.00")}</span></div></div>`;
    html+=`<div class="card"><h2>Recent demands</h2>`;
    const ds=(demands.demands||[]);
    if(!ds.length) html+=`<div class="muted">No demands recorded yet. Seed flats and import/create demands first.</div>`;
    for(const d of ds){
      const dot=d.status==="Paid"?"ok":(d.status==="Overdue"||d.status==="In recovery"?"bad":"warn");
      html+=`<div class="row"><span class="k"><span class="dot ${dot}"></span>${esc(d.flat_id)} ${esc(d.document_number)}</span><span class="v">${esc(d.outstanding)} outstanding · ${esc(d.status)}</span></div>`;
    }
    html+=`</div>`;
    html+=`<div class="card" id="rtm-demand-mailing"><h2>Payment-due mailing</h2>
      <div class="muted">Email every unpaid flat that has an email on file its own demand PDF plus a payment-now-due cover. Individual sends, never BCC. Preview is safe; sending needs a typed confirm and skips anyone already sent this period.</div>
      <div style="margin-top:10px">
        <button class="act" onclick="dmPreview()">Preview</button>
        <button class="act go" onclick="dmSend('normal')" style="margin-left:6px">Send now</button>
        <button class="act" onclick="dmSend('high')" style="margin-left:6px">Send high priority</button>
        <button class="act" onclick="dmLog()" style="margin-left:6px">Send log</button>
      </div>
      <div id="dm-out" class="muted" style="margin-top:10px">Tap Preview to see who would be emailed and who is excluded.</div></div>`;
    const rq=(dash.review_queue||{}).transactions||[];
    html+=`<div class="card"><h2>Payment review</h2>`;
    if(!rq.length) html+=`<div class="muted">No partial/unmatched/duplicate Metro credits waiting for review.</div>`;
    for(const t of rq.slice(0,8)){ html+=`<div class="row"><span class="k">${esc(t.bank_date)} ${esc(t.amount)}</span><span class="v muted">${esc(t.match_status)} · ${esc(t.reference||t.description||"")}</span></div>`; }
    html+=`</div>`;
    html+=`<div class="card"><h2>Payables due soon</h2>`;
    const pays=(dash.payables_due_soon||payables.payables||[]);
    if(!pays.length) html+=`<div class="muted">No scheduled or approved payables due in the next 30 days.</div>`;
    for(const p of pays.slice(0,8)){ html+=`<div class="row"><span class="k">${esc(p.due_date)} ${esc(p.supplier)}</span><span class="v">${esc(p.amount)} · ${esc(p.status)} · ${esc(p.budget_line||"")}</span></div>`; }
    html+=`</div>`;
    // Metro bank-feed verification playbook (read-only / account-information only).
    html+=`<div class="card"><h2>Metro bank-feed playbook</h2>
      <div class="muted">Provider tests for Metro service-charge and reserve accounts. Read-only/account-information only: Mongoose records the playbook and evidence, but never moves money or silently connects to a bank.</div>
      <button class="act go" style="margin-top:10px" onclick="exportBankFeedPack()">Export bank-feed evidence pack</button>
      <span id="bankFeedExportOut" class="muted" style="margin-left:8px"></span>`;
    const bfActs=(bankFeed.next_actions||[]);
    if(bfActs.length){
      html+=`<h3 style="margin-top:14px">Bank-feed next actions</h3>`;
      for(const a of bfActs.slice(0,5)){
        const dot=a.urgency==="high"?"bad":(a.urgency==="medium"?"warn":"unknown");
        html+=`<div class="row"><span class="k"><span class="dot ${dot}"></span>${esc(a.title)}</span><span class="v muted">${esc(a.action||"")}</span></div>`;
      }
    }
    const accts=(bankFeed.accounts||[]);
    html+=`<h3 style="margin-top:14px">Metro account inventory</h3>`;
    if(!accts.length) html+=`<div class="muted">No account inventory recorded yet. Record exact product/platform for the service-charge and reserve accounts before testing providers.</div>`;
    for(const a of accts){
      html+=`<div class="row"><span class="k">${esc(a.purpose||"account")}</span><span class="v">${esc(a.account_type_label||a.account_type||"unknown")} · ${esc(a.online_banking_platform||"platform missing")}</span></div>`;
    }
    const provs=(bankFeed.providers||[]).slice(0,10);
    html+=`<h3 style="margin-top:14px">Recommended order</h3>`;
    for(const p of provs){ html+=`<div class="row"><span class="k">${esc(p.rank)}. ${esc(p.name)}</span><span class="v muted">${esc(p.expectation||"")}</span></div>`; }
    html+=`</div>`;
    // Voicemails (phone messages Mongoose spots in email) are property business for M, so they
    // live inside the RTM tab as a collapsible section rather than a separate top-level tab.
    html+=`<details class="sys-section"><summary>📞 Voicemails</summary><div id="rtm-voicemail"></div></details>`;
    el.innerHTML=html;
    try{ loadVoicemail("rtm-voicemail"); }catch(e){}
    try{ preserveScrollAcrossReload(loadRtmDirector); }catch(e){}
  }catch(e){ el.innerHTML=retryCard("RTM dashboard", e, "loadRTM"); }
}

// Director financials: itemised budget, paid-vs-demanded pie, svc-charge-vs-reserves bar, and the
// arrears chasing shortlist with one-click chaser generation. Inline SVG charts (no external lib).
function _rtmPieSVG(pct){
  pct=Math.max(0,Math.min(100,Number(pct)||0));
  const r=52, c=2*Math.PI*r, off=c*(1-pct/100);
  return '<svg viewBox="0 0 130 130" width="130" height="130" role="img" aria-label="paid '+pct+'%">'
    +'<circle cx="65" cy="65" r="'+r+'" fill="none" stroke="#e6e0d4" stroke-width="18"/>'
    +'<circle cx="65" cy="65" r="'+r+'" fill="none" stroke="#3ecf8e" stroke-width="18" stroke-linecap="round"'
    +' stroke-dasharray="'+c.toFixed(1)+'" stroke-dashoffset="'+off.toFixed(1)+'" transform="rotate(-90 65 65)"/>'
    +'<text x="65" y="60" text-anchor="middle" font-size="20" font-weight="800" fill="var(--fg,#ddd)">'+pct+'%</text>'
    +'<text x="65" y="80" text-anchor="middle" font-size="10" fill="#8a8477">paid</text></svg>';
}
function _rtmBarSVG(a, b, aLabel, bLabel){
  const max=Math.max(a,b,1), h=110;
  const ah=Math.round((a/max)*h), bh=Math.round((b/max)*h);
  function bar(x,bh,color,label,val){
    return '<g><rect x="'+x+'" y="'+(120-bh)+'" width="64" height="'+bh+'" rx="6" fill="'+color+'"/>'
      +'<text x="'+(x+32)+'" y="134" text-anchor="middle" font-size="11" fill="#8a8477">'+esc(label)+'</text>'
      +'<text x="'+(x+32)+'" y="'+(114-bh)+'" text-anchor="middle" font-size="11" font-weight="700" fill="var(--fg,#ddd)">'+esc(val)+'</text></g>';
  }
  return '<svg viewBox="0 0 190 145" width="190" height="145" role="img" aria-label="fund balances">'
    +bar(20,ah,'#4a90d9',aLabel,arguments[4]||'')+bar(106,bh,'#c98ab4',bLabel,arguments[5]||'')+'</svg>';
}
async function loadRtmDirector(){
  const box=document.getElementById("rtm-director"); if(!box) return;
  let d, mem; try{ [d,mem]=await Promise.all([j("/rtm/director/summary"), j("/rtm/director/members").catch(()=>({members:[]}))]); }catch(e){ box.innerHTML='<div class="card"><div class="muted">Could not load director financials.</div></div>'; return; }
  const pv=d.paid_vs_demanded||{}, bal=d.balances||{}, bud=d.budget||{}, lines=(bud.lines||[]), tot=(bud.total||{});
  let h='<div class="card"><h2>Director financials</h2>';
  // charts row
  h+='<div style="display:flex;gap:24px;flex-wrap:wrap;align-items:flex-start;margin:6px 0 4px">';
  h+='<div><div class="muted" style="font-weight:700;margin-bottom:4px">Half-year paid vs demanded</div>'+_rtmPieSVG(pv.pct_paid||0)
    +'<div class="muted" style="font-size:12px">'+esc(pv.paid||"£0.00")+' of '+esc(pv.demanded||"£0.00")+' &middot; '+esc(pv.outstanding||"£0.00")+' outstanding</div></div>';
  h+='<div><div class="muted" style="font-weight:700;margin-bottom:4px">Fund balances</div>'
    +_rtmBarSVG(bal.service_charge_pence||0, bal.reserve_pence||0, "Svc charge", "Reserves", esc(bal.service_charge||"£0.00"), esc(bal.reserve||"£0.00"))+'</div>';
  h+='</div></div>';
  // Record a payment (mark paid): the quick lever to clear a flat's demand from money already received.
  h+='<div class="card"><h2>Record a payment (mark paid)</h2>'
    +'<div class="muted" style="margin-bottom:8px">Records a received payment (a bank credit and allocation) so the flat clears and the demand shows Paid. Records money already in the bank; it never moves money.</div>'
    +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
    +'<input id="rtm-mp-flat" placeholder="Flat id, e.g. {{RTM_BLOCK_B}}F9" style="flex:1;min-width:150px;padding:6px 8px">'
    +'<input id="rtm-mp-amount" placeholder="Amount £ (blank = full)" style="width:150px;padding:6px 8px">'
    +'<input id="rtm-mp-date" placeholder="Date YYYY-MM-DD (blank = today)" style="width:190px;padding:6px 8px">'
    +'<button class="act" onclick="rtmMarkPaid(this)">Mark paid</button></div>'
    +'<div id="rtm-mp-msg" class="muted" style="font-size:12px;margin-top:8px"></div></div>';
  // Access requests: leaseholder sign-ups that validated (correct payment ref + demand number) but
  // could not be auto-verified by email, awaiting the owner's approve/reject. Loaded async below.
  h+='<div class="card" id="rtm-access-requests"><h2>Access requests</h2>'
    +'<div class="muted" style="margin-bottom:8px">Sign-ups whose payment reference and demand number matched, but whose email is not the one on file. Approve to create their portal account (they get a set-password email); reject to decline. Sign-ups whose email matches on file are approved automatically and never appear here.</div>'
    +'<div id="rtm-review-claims" class="muted">Loading&hellip;</div></div>';
  // Schedules & apportionment (two-schedule model): per-schedule budget/apportionment integrity + actions.
  const scheds=d.schedules||[];
  if(scheds.length){
    h+='<div class="card"><h2>Schedules &amp; apportionment</h2>';
    h+='<table class="rtm-tbl" style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>'
      +'<th style="text-align:left">Schedule</th><th style="text-align:right">Annual budget</th><th style="text-align:right">Half-year</th><th style="text-align:right">Demanded</th><th style="text-align:right">Apportionment</th><th style="text-align:right">Creditable</th></tr></thead><tbody>';
    for(const s of scheds){ const okc=s.apportionment_ok?"#3ecf8e":"#e06666";
      h+='<tr><td>'+esc(s.name||s.code||"")+'</td><td style="text-align:right">'+esc(s.budgeted||"")+'</td><td style="text-align:right">'+esc(s.period||"")+'</td><td style="text-align:right">'+esc(s.demanded||"")+'</td>'
        +'<td style="text-align:right;color:'+okc+'">'+esc(s.apportionment_pct||"")+'% ('+esc(s.participants||0)+')</td><td style="text-align:right">'+esc(s.creditable||"")+'</td></tr>';
    }
    h+='</tbody></table>';
    h+='<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center">'
      +'<input id="rtm-appn-year" placeholder="Year" value="2027" style="width:110px;padding:6px 8px">'
      +'<input id="rtm-appn-period" placeholder="Period" value="2026-H2" style="width:140px;padding:6px 8px">'
      +'<button class="act" onclick="rtmReconcile(this)">Reconcile</button>'
      +'<button class="act" onclick="rtmDeriveDemands(this)" title="Generate per-flat per-schedule demands">Derive demands</button></div>'
      +'<div id="rtm-appn-msg" class="muted" style="font-size:12px;margin-top:8px"></div>';
    h+='<div style="margin-top:10px;border-top:1px solid var(--line,#2a2a2a);padding-top:10px"><div class="muted" style="font-weight:700;margin-bottom:4px">Issue a credit note (schedule over-recovery)</div>'
      +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
      +'<select id="rtm-cn-sched" style="padding:6px 8px">'+scheds.map(function(s){return '<option value="'+esc(s.code)+'">'+esc(s.name||s.code)+'</option>';}).join("")+'</select>'
      +'<input id="rtm-cn-actual" placeholder="Actual spend £" style="width:130px;padding:6px 8px">'
      +'<input id="rtm-cn-reason" placeholder="Reason" style="flex:1;min-width:160px;padding:6px 8px">'
      +'<button class="act" onclick="rtmCreditNote(this,true)">Preview</button>'
      +'<button class="act" onclick="rtmCreditNote(this,false)">Issue</button></div>'
      +'<div id="rtm-cn-msg" class="muted" style="font-size:12px;margin-top:8px"></div></div>';
    h+='</div>';
  }
  // itemised budget
  h+='<div class="card"><h2>Itemised budget'+(bud.year?(' '+esc(bud.year)):'')+'</h2>';
  if(lines.length){
    h+='<table class="rtm-tbl" style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>'
      +'<th style="text-align:left">Schedule</th><th style="text-align:left">Budget line</th><th style="text-align:right">Budgeted</th><th style="text-align:right">Spent</th><th style="text-align:right">Committed</th><th style="text-align:right">Variance</th></tr></thead><tbody>';
    for(const r of lines){ const neg=(r.variance_pence||0)<0;
      h+='<tr><td>'+esc(r.schedule||"")+'</td><td>'+esc(r.budget_line||"")+'</td><td style="text-align:right">'+esc(r.budgeted||"")+'</td><td style="text-align:right">'+esc(r.spent||"")+'</td><td style="text-align:right">'+esc(r.committed||"")+'</td><td style="text-align:right;color:'+(neg?'#e06666':'#3ecf8e')+'">'+esc(r.variance||"")+'</td></tr>'; }
    h+='<tr style="font-weight:800;border-top:2px solid var(--line,#333)"><td colspan="2">Total</td><td style="text-align:right">'+esc(tot.budgeted||"")+'</td><td style="text-align:right">'+esc(tot.spent||"")+'</td><td style="text-align:right">'+esc(tot.committed||"")+'</td><td style="text-align:right">'+esc(tot.variance||"")+'</td></tr>';
    h+='</tbody></table>';
  } else { h+='<div class="muted">No budget lines yet. Add them via the RTM management tools; they will chart here.</div>'; }
  h+='</div>';
  // chasing shortlist
  h+='<div class="card"><h2>Chasing shortlist'+(d.shortlist_count?(' ('+d.shortlist_count+')'):'')+'</h2>';
  const sl=d.chasing_shortlist||[];
  if(sl.length){
    h+='<div class="muted" style="margin-bottom:6px">Overdue demands, largest first. Generate a chaser to review before sending.</div>';
    h+='<table class="rtm-tbl" style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left">Flat</th><th style="text-align:left">Leaseholder</th><th style="text-align:right">Outstanding</th><th style="text-align:right">Days overdue</th><th>Next action</th><th></th></tr></thead><tbody>';
    for(const it of sl){
      const doc=esc(it.document_number||"");
      h+='<tr><td>'+esc(it.flat_id||"")+'</td><td>'+esc(it.leaseholder_name||"")+'</td><td style="text-align:right">'+esc(it.amount_outstanding||"")+'</td><td style="text-align:right">'+esc(it.days_overdue||0)+'</td><td>'+esc((it.next_action||{}).label||"")+'</td>'
        +'<td style="text-align:right"><button class="act" onclick="rtmChase(\''+doc+'\',this)">Generate chaser</button></td></tr>';
    }
    h+='</tbody></table><div id="rtm-chase-out" style="margin-top:10px"></div>';
  } else { h+='<div class="muted">No overdue demands. Nothing to chase.</div>'; }
  h+='</div>';
  // flats & members: the persisted RTM-member toggle (used to show active votes to members)
  const ms=(mem&&mem.members)||[];
  const onboarded=(mem&&mem.onboarded_count!=null)?mem.onboarded_count:null;
  h+='<div class="card"><div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap">'
    +'<h2 style="margin:0">Flats &amp; members'+(mem&&mem.member_count!=null?(' &middot; '+mem.member_count+' member'+(mem.member_count===1?'':'s')):'')+(onboarded!=null?(' &middot; '+onboarded+' onboarded'):'')+'</h2>'
    +'<button class="act" onclick="rtmSyncAccounts(this)" title="Pull last-login and linked WordPress accounts from the portal">Sync WP logins</button></div>';
  if(ms.length){
    h+='<div class="muted" style="margin:6px 0">Toggle who is an RTM member. It is remembered, and drives who sees active votes (directors are members too). Last login and WordPress account come from the portal via Sync WP logins.</div>';
    h+='<div id="rtm-acct-msg" class="muted" style="font-size:12px;margin-bottom:6px"></div>';
    h+='<table class="rtm-tbl" style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr><th style="text-align:left">Flat</th><th style="text-align:left">Leaseholder</th><th style="text-align:left">Email</th><th style="text-align:left">WP account</th><th style="text-align:left">Last login</th><th style="text-align:center">RTM member</th></tr></thead><tbody>';
    for(const r of ms){ const fid=esc(r.flat_id||""); const acct=(r.wp_account_count>0)?esc(r.wp_usernames||("yes ("+r.wp_account_count+")")):'<span class="muted">not onboarded</span>';
      h+='<tr><td>'+fid+'</td><td>'+esc(r.leaseholder_name||"")+'</td><td>'+esc(r.email||"-")+'</td><td>'+acct+'</td><td class="muted">'+esc(r.last_login||"-")+'</td>'
        +'<td style="text-align:center"><input type="checkbox" '+(r.is_member?"checked":"")+' onchange="rtmToggleMember(\''+fid+'\',this)" style="width:18px;height:18px;cursor:pointer" aria-label="RTM member"></td></tr>';
    }
    h+='</tbody></table>';
  } else { h+='<div class="muted">No flats yet. They appear here after the RTM data import, with a member toggle each.</div>'; }
  h+='</div>';
  // member votes: create a vote, see live tallies, pull casts from the portal, close a vote
  h+='<div class="card" id="rtm-votes"><h2>Member votes</h2>'
    +'<div class="muted" style="margin-bottom:8px">Open a vote for RTM members. Members (and directors) cast on the leaseholder portal; casts pull back here and tallies show live. One vote per flat.</div>'
    +'<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-start;margin-bottom:10px">'
    +'<input id="rtm-vote-q" placeholder="Question, e.g. Appoint managing agent?" style="flex:1;min-width:240px;padding:7px 9px">'
    +'<textarea id="rtm-vote-opts" placeholder="One option per line&#10;Yes&#10;No" style="width:180px;height:64px;padding:7px 9px;font:13px/1.4 inherit"></textarea>'
    +'<button class="act" onclick="rtmCreateVote(this)">Open vote</button>'
    +'<button class="act" onclick="rtmPullCasts(this)" title="Fetch member casts from the portal">Pull casts</button></div>'
    +'<div id="rtm-vote-msg" class="muted" style="font-size:12px;margin-bottom:8px"></div>'
    +'<div id="rtm-votes-list"></div></div>';
  box.innerHTML=h;
  try{ renderRtmVotes(); }catch(e){}
  try{ rtmLoadReviewClaims(); }catch(e){}
}
async function rtmLoadReviewClaims(){
  const el=document.getElementById("rtm-review-claims"); if(!el) return;
  let d; try{ d=await j("/rtm/portal/review-claims"); }catch(e){ el.innerHTML='<div class="muted">Could not load access requests.</div>'; return; }
  const cs=(d&&d.claims)||[];
  if(!cs.length){ el.innerHTML='<div class="muted">No access requests waiting.</div>'; return; }
  let h='<table class="rtm-tbl" style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr>'
    +'<th style="text-align:left">Flat</th><th style="text-align:left">On file</th><th style="text-align:left">They gave</th><th></th></tr></thead><tbody>';
  for(const c of cs){
    const cid=parseInt(c.claim_id,10);
    const onfile=esc(c.on_file_name||"")+(c.on_file_email?(' <span class="muted">&lt;'+esc(c.on_file_email)+'&gt;</span>'):'');
    const gave=esc(c.name||"")+' <span class="muted">&lt;'+esc(c.email||"")+'&gt;</span>';
    h+='<tr><td>'+esc(c.flat_id||"")+'</td><td>'+onfile+'</td><td>'+gave+'</td>'
      +'<td style="text-align:right;white-space:nowrap">'
      +'<button class="act go" onclick="rtmApproveClaim('+cid+',this)">Approve</button> '
      +'<button class="act" onclick="rtmRejectClaim('+cid+',this)">Reject</button></td></tr>';
  }
  h+='</tbody></table><div id="rtm-review-msg" class="muted" style="font-size:12px;margin-top:8px"></div>';
  el.innerHTML=h;
}
async function rtmApproveClaim(claimId, btn){
  const msg=document.getElementById("rtm-review-msg");
  if(!confirm("Approve this access request and create their portal account?")) return;
  if(btn){ btn.disabled=true; btn.textContent="Approving…"; }
  try{ const r=await postAction("/rtm/portal/review-claims/approve",{claim_id:claimId}); const d=await r.json();
    if(msg) msg.textContent=d.ok?("Approved. Account created for "+(d.email||"")+", set-password email sent."):("Could not approve: "+(d.error||"unknown"));
    if(d.ok) rtmLoadReviewClaims();
  }catch(e){ if(msg) msg.textContent="Approve failed: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Approve"; } }
}
async function rtmRejectClaim(claimId, btn){
  const msg=document.getElementById("rtm-review-msg");
  if(!confirm("Reject this access request? No account will be created.")) return;
  if(btn){ btn.disabled=true; btn.textContent="Rejecting…"; }
  try{ const r=await postAction("/rtm/portal/review-claims/reject",{claim_id:claimId}); const d=await r.json();
    if(msg) msg.textContent=d.ok?"Rejected.":("Could not reject: "+(d.error||"unknown"));
    if(d.ok) rtmLoadReviewClaims();
  }catch(e){ if(msg) msg.textContent="Reject failed: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Reject"; } }
}
function _rtmVoteBar(label, count, total){
  const pct = total>0 ? Math.round((count/total)*100) : 0;
  return '<div style="display:flex;align-items:center;gap:8px;margin:3px 0">'
    +'<div style="width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(label)+'">'+esc(label)+'</div>'
    +'<div style="flex:1;background:var(--line,#2a2a2a);border-radius:4px;height:16px;overflow:hidden"><div style="width:'+pct+'%;height:16px;background:var(--gold,#caa15a)"></div></div>'
    +'<div style="width:64px;text-align:right" class="muted">'+count+' ('+pct+'%)</div></div>';
}
async function renderRtmVotes(){
  const list=document.getElementById("rtm-votes-list"); if(!list) return;
  let d; try{ d=await j("/rtm/votes/list"); }catch(e){ list.innerHTML='<div class="muted">Could not load votes.</div>'; return; }
  const votes=(d&&d.votes)||[];
  if(!votes.length){ list.innerHTML='<div class="muted">No votes yet. Open one above.</div>'; return; }
  let h='';
  for(const v of votes){
    const open=v.status==="open";
    h+='<div style="border:1px solid var(--line,#2a2a2a);border-radius:8px;padding:10px 12px;margin-bottom:8px">'
      +'<div style="display:flex;justify-content:space-between;gap:10px;align-items:baseline">'
      +'<div style="font-weight:700">'+esc(v.question)+'</div>'
      +'<div class="muted" style="font-size:12px">'+(open?'open':'closed')+' &middot; '+ (v.total_cast||0) +' of '+(v.member_count||0)+' cast ('+(v.turnout_pct||0)+'%)</div></div>';
    const counts=v.counts||{};
    for(const opt of (v.options||[])){ h+=_rtmVoteBar(opt, counts[opt]||0, v.total_cast||0); }
    // Director-only per-flat audit (this Cockpit is owner-gated; members never see who voted what).
    const casts=v.casts||[];
    if(casts.length){
      h+='<details style="margin-top:6px"><summary class="muted" style="font-size:12px;cursor:pointer">Who voted, by flat (director audit) &middot; '+casts.length+'</summary>'
        +'<div style="font-size:12px;margin-top:4px;columns:2">'
        +casts.map(function(c){ return esc(c.flat_id)+': '+esc(c.choice); }).join('<br>')
        +'</div></details>';
    }
    if(open){ h+='<div style="margin-top:6px"><button class="act" onclick="rtmCloseVote('+v.vote_id+',this)">Close vote</button></div>'; }
    h+='</div>';
  }
  list.innerHTML=h;
}
async function rtmCreateVote(btn){
  const q=(document.getElementById("rtm-vote-q")||{}).value||"";
  const opts=(document.getElementById("rtm-vote-opts")||{}).value||"";
  const msg=document.getElementById("rtm-vote-msg");
  if(!q.trim()||opts.split("\n").filter(function(x){return x.trim();}).length<2){ if(msg) msg.textContent="A question and at least two options are required."; return; }
  if(btn){ btn.disabled=true; btn.textContent="Opening…"; }
  try{
    const r=await postAction("/rtm/votes/create",{question:q,options:opts}); const d=await r.json();
    if(d&&d.ok){ if(msg) msg.textContent="Vote opened. Publish to the portal so members can cast."; document.getElementById("rtm-vote-q").value=""; document.getElementById("rtm-vote-opts").value=""; renderRtmVotes(); }
    else { if(msg) msg.textContent="Could not open vote: "+((d&&d.error)||"unknown"); }
  }catch(e){ if(msg) msg.textContent="Could not open vote: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Open vote"; } }
}
async function rtmCloseVote(voteId, btn){
  if(btn){ btn.disabled=true; btn.textContent="Closing…"; }
  try{ const r=await postAction("/rtm/votes/close",{vote_id:voteId}); await r.json(); renderRtmVotes(); }
  catch(e){ if(btn){ btn.disabled=false; btn.textContent="Close vote"; } }
}
async function rtmPullCasts(btn){
  const msg=document.getElementById("rtm-vote-msg");
  if(btn){ btn.disabled=true; btn.textContent="Pulling…"; }
  try{
    const r=await postAction("/rtm/votes/pull-casts",{}); const d=await r.json();
    if(d&&d.ok){ if(msg) msg.textContent="Pulled "+(d.pulled||0)+" cast(s): "+(d.accepted||0)+" recorded, "+(d.rejected||0)+" rejected."; renderRtmVotes(); }
    else { if(msg) msg.textContent="Could not pull casts: "+((d&&d.error)||"unknown"); }
  }catch(e){ if(msg) msg.textContent="Could not pull casts: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Pull casts"; } }
}
async function rtmMarkPaid(btn){
  const flat=((document.getElementById("rtm-mp-flat")||{}).value||"").trim();
  const amount=((document.getElementById("rtm-mp-amount")||{}).value||"").trim();
  const date=((document.getElementById("rtm-mp-date")||{}).value||"").trim();
  const msg=document.getElementById("rtm-mp-msg");
  if(!flat){ if(msg) msg.textContent="Enter a flat id (e.g. {{RTM_BLOCK_B}}F9)."; return; }
  if(btn){ btn.disabled=true; btn.textContent="Recording…"; }
  try{
    const body={flat:flat};
    if(amount) body.amount=amount;
    if(date) body.received_date=date;
    const r=await postActionOk("/rtm/manage/mark-paid", body);
    if(msg) msg.textContent = r.already_paid
      ? ("Already paid: "+(r.flat_id||flat)+" is clear.")
      : ("Marked paid: "+(r.flat_id||flat)+" "+(r.amount||"")+" · balance "+(r.balance||"")+" ("+(r.position||"")+").");
    if(typeof loadRtmDirector==="function") _reloadSoon(loadRtmDirector, 700);
  }catch(e){ if(msg) msg.textContent="Could not record: "+(e&&e.message||e); }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Mark paid"; } }
}
async function rtmReconcile(btn){
  const y=(document.getElementById("rtm-appn-year")||{}).value||"", p=(document.getElementById("rtm-appn-period")||{}).value||"";
  const msg=document.getElementById("rtm-appn-msg");
  if(btn){ btn.disabled=true; btn.textContent="Reconciling…"; }
  try{ const d=await j("/rtm/apportionment/reconcile?year="+encodeURIComponent(y)+"&period="+encodeURIComponent(p)+"&published_total_pence={{RTM_PUBLISHED_TOTAL_PENCE}}");
    let t="Reconcile "+(d.ok?"PASS":"ISSUES")+" · period total "+((d.period_total_pence||0)/100).toFixed(2)+" · cross-total "+(d.cross_total_ok?"ok":"off");
    (d.schedules||[]).forEach(function(s){ t+=" · "+s.schedule+" appn "+(s.apportionment_ok?"ok":"BAD")+" demand "+(s.demand_matches_apportionment?"ok":"BAD"); });
    if(msg) msg.textContent=t;
  }catch(e){ if(msg) msg.textContent="Reconcile failed: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Reconcile"; } }
}
async function rtmDeriveDemands(btn){
  const y=(document.getElementById("rtm-appn-year")||{}).value||"", p=(document.getElementById("rtm-appn-period")||{}).value||"";
  const msg=document.getElementById("rtm-appn-msg");
  if(!confirm("Generate per-flat per-schedule demands for "+p+"? Re-running supersedes prior demands (no double charge).")) return;
  if(btn){ btn.disabled=true; btn.textContent="Deriving…"; }
  try{ const r=await postAction("/rtm/apportionment/derive",{year:y,period:p}); const d=await r.json();
    if(d&&d.ok){ if(msg) msg.textContent="Derived: "+(d.created||0)+" demands ("+(d.superseded||0)+" superseded) across "+((d.schedules||[]).length)+" schedules."; preserveScrollAcrossReload(loadRtmDirector); }
    else { if(msg) msg.textContent="Derive refused: "+JSON.stringify((d&&d.refused)||(d&&d.error)||"unknown"); }
  }catch(e){ if(msg) msg.textContent="Derive failed: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Derive demands"; } }
}
async function rtmCreditNote(btn, dry){
  const sch=(document.getElementById("rtm-cn-sched")||{}).value||"", actual=(document.getElementById("rtm-cn-actual")||{}).value||"";
  const reason=(document.getElementById("rtm-cn-reason")||{}).value||"";
  const y=(document.getElementById("rtm-appn-year")||{}).value||"", p=(document.getElementById("rtm-appn-period")||{}).value||"";
  const msg=document.getElementById("rtm-cn-msg");
  const pence=Math.round(parseFloat(actual||"0")*100);
  if(isNaN(pence)){ if(msg) msg.textContent="Enter the actual spend in pounds."; return; }
  if(!dry && !confirm("Issue credit notes for schedule "+sch+"? This posts credits to the ledger.")) return;
  if(btn){ btn.disabled=true; btn.textContent=dry?"Previewing…":"Issuing…"; }
  try{ const r=await postAction("/rtm/apportionment/credit-note",{schedule:sch,year:y,period:p,actual_pence:pence,reason:reason,dry_run:!!dry}); const d=await r.json();
    if(d&&d.ok){ if(msg) msg.textContent=(dry?"Preview: ":"Issued: ")+"pool "+d.pool+" across "+((d.lines||[]).length)+" flats"+(d.note?(" · "+d.note):""); if(!dry) preserveScrollAcrossReload(loadRtmDirector); }
    else { if(msg) msg.textContent="Credit note: "+((d&&d.error)||"unknown"); }
  }catch(e){ if(msg) msg.textContent="Credit note failed: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent=dry?"Preview":"Issue"; } }
}
async function rtmToggleMember(flatId, cb){
  const want=!!cb.checked; cb.disabled=true;
  try{ const r=await postAction("/rtm/director/member-toggle",{flat_id:flatId,is_member:want}); const d=await r.json(); if(!(d&&d.ok)) cb.checked=!want; }
  catch(e){ cb.checked=!want; }
  finally{ cb.disabled=false; }
}
async function rtmSyncAccounts(btn){
  const msg=document.getElementById("rtm-acct-msg");
  if(btn){ btn.disabled=true; btn.textContent="Syncing…"; }
  try{
    const r=await postAction("/rtm/director/pull-accounts",{}); const d=await r.json();
    if(d&&d.ok){ if(msg) msg.textContent="Synced WP accounts for "+(d.flats||0)+" flat(s)."; preserveScrollAcrossReload(loadRtmDirector); }
    else { if(msg) msg.textContent="Could not sync: "+((d&&d.error)||"unknown"); }
  }catch(e){ if(msg) msg.textContent="Could not sync: "+e; }
  finally{ if(btn){ btn.disabled=false; btn.textContent="Sync WP logins"; } }
}
async function rtmChase(doc, btn){
  if(btn){ btn.disabled=true; btn.textContent='Generating…'; }
  const out=document.getElementById("rtm-chase-out");
  try{
    const r=await postAction("/rtm/director/chase",{document_number:doc});
    const d=await r.json();
    if(btn){ btn.disabled=false; btn.textContent='Generate chaser'; }
    if(d&&d.ok){
      const rem=d.reminder||{}; const txt=rem.text||rem.body||rem.message||JSON.stringify(rem,null,2);
      if(out) out.innerHTML='<div class="card" style="border-color:var(--gold,#caa15a)"><h3>Chaser for '+esc(doc)+'</h3>'
        +'<div class="muted" style="margin-bottom:6px">'+esc((rem.action_type||rem.next_action||"")+(rem.recipient?(' &middot; '+rem.recipient):''))+'</div>'
        +'<textarea readonly style="width:100%;min-height:180px;font:13px/1.5 ui-monospace,monospace">'+esc(txt)+'</textarea>'
        +'<div class="muted" style="font-size:12px;margin-top:6px">Review-only draft. Sending is a deliberate step (staged).</div></div>';
    } else { if(out) out.innerHTML='<div class="muted">Could not generate chaser: '+esc((d&&d.error)||"unknown")+'</div>'; }
  }catch(e){ if(btn){ btn.disabled=false; btn.textContent='Generate chaser'; } if(out) out.textContent='Chaser failed: '+esc(e); }
}
async function exportBankFeedPack(){
  const out=document.getElementById("bankFeedExportOut"); if(out)out.textContent="exporting...";
  try{
    const r=await postAction("/rtm/bank-feed/export-pack",{dir:"data/rtm_bank_feed_playbook"});
    const d=await r.json();
    if(out) out.textContent = d.ok? `exported ${d.file_count||0} files to ${d.dir}` : ("failed: "+esc(d.error||""));
  }catch(e){ if(out)out.textContent="failed: "+esc(e); }
}

async function loadBuild(){
  const el=document.getElementById("build");
  el.innerHTML='<div class="card muted">Loading build status…</div>';
  try{
    const [status,wpStatus]=await Promise.all([
      j("/build/status"),
      j("/build/wordpress/status").catch(()=>({ok:false,connection:{configured:false}})),
    ]);

    // ── changelog strip ──
    const addenda=status.recent_addenda||[];
    const changeHtml=addenda.slice(0,5).map(a=>`<div class="row">
      <span class="small" style="font-weight:600">${esc(a.title)}</span>
      <span class="muted small" style="max-width:52%;text-align:right">${esc(a.summary)}</span></div>`).join('')
      || '<div class="muted">No addenda found.</div>';

    // ── WP connection ──
    const conn=wpStatus.connection||{};
    const wpOk=conn.ok||false;
    const wpForm=`<div class="wp-connect" style="margin-top:6px">
       <div class="muted small" style="margin-bottom:6px">Connect Mongoose to your WordPress site. In WordPress open Users, then your Profile, then Application Passwords, and create one for Mongoose.</div>
       <input id="wp-url" placeholder="https://your-site.uk" style="display:block;width:100%;margin:3px 0;padding:6px;border:1px solid var(--line,#444);border-radius:6px;background:transparent;color:inherit" />
       <input id="wp-user" placeholder="WordPress username" style="display:block;width:100%;margin:3px 0;padding:6px;border:1px solid var(--line,#444);border-radius:6px;background:transparent;color:inherit" />
       <input id="wp-pass" type="password" placeholder="Application password" style="display:block;width:100%;margin:3px 0;padding:6px;border:1px solid var(--line,#444);border-radius:6px;background:transparent;color:inherit" />
       <button class="act go" onclick="connectWordpress()" style="margin-top:4px">Save and connect</button>
       <span id="wp-connect-out" class="small" style="margin-left:8px"></span>
      </div>`;
    const wpLine=wpOk
     ? `<span class="ok">✓ ${esc(conn.url||'')} · ${esc(conn.user||'')}</span> <button class="act" style="padding:2px 8px;margin:0 0 0 8px;font-size:12px" onclick="wpShowConnect()">Change</button><div id="wp-connect-slot"></div>`
      : conn.configured
       ? `<span class="warn">⚠ Auth failed, re-enter the connection below.</span>${wpForm}`
        : wpForm;

    // ── source status ──
    const src=wpStatus.source||{};
    const srcLine=src.ok
     ? `<span class="ok">✓ ${esc(src.path)} · ${src.file_count} files${src.version?' · v'+esc(src.version):''}</span>`
      : `<span class="warn">No plugin source at ${esc(src.path||'~/mongoose-plugin')}, create the folder or set MONGOOSE_PLUGIN_DIR in .env</span>`;

    // ── installed plugins table ──
    const installed=wpStatus.installed_plugins||[];
    const pluginRows=installed.length
     ? installed.map(p=>`<div class="row">
          <span class="small">${esc(p.name)}</span>
          <span class="muted small">${esc(p.version||'')} · <span class="${p.status==='active'?'ok':'muted'}">${esc(p.status)}</span></span>
          <div style="display:flex;gap:4px">
            ${p.status==='inactive'?`<button class="act go" style="padding:3px 8px;margin:0;font-size:12px" onclick="activatePlugin('${esc(p.plugin)}','active')">Activate</button>`:''}
            ${p.status==='active'?`<button class="act" style="padding:3px 8px;margin:0;font-size:12px" onclick="activatePlugin('${esc(p.plugin)}','inactive')">Deactivate</button>`:''}
            ${p.status==='inactive'?`<button class="act" style="padding:3px 8px;margin:0;font-size:12px;border-color:var(--bad);color:var(--bad)" onclick="deletePlugin('${esc(p.plugin)}')">Delete</button>`:''}
          </div>
        </div>`).join('')
      : (wpOk?'<div class="muted">No plugins installed.</div>':'');

    // ── builds ──
    const builds=wpStatus.builds||[];
    const buildsHtml=builds.length
     ? builds.map(b=>`<div class="row">
          <span class="small">${esc(b.name)}</span>
          <span class="muted small">${(b.size_bytes/1024).toFixed(0)} KB</span>
          <div style="display:flex;gap:4px">
            <button class="act" style="padding:3px 8px;margin:0;font-size:12px" onclick="downloadBuildZip('${esc(b.name)}',this)">Download</button>
            ${wpOk?`<button class="act go" style="padding:3px 8px;margin:0;font-size:12px" onclick="deployZip('${esc(b.path)}')">Deploy to WP</button>`:''}
          </div>
        </div>`).join('')
      : '<div class="muted">No builds yet, click Build below.</div>';

    el.innerHTML=`
      <!-- strip: version + changelog -->
      <div class="card">
        <h2>Mongoose v${esc(status.version)} · build</h2>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <button class="act go" onclick="buildPlugin()">Build plugin zip</button>
          <button class="act go" onclick="deployBeta()">Build + deploy to RTM site</button>
          <button class="act" onclick="loadBuild()">Refresh</button>
        </div>
        <div id="build-out" class="muted"></div>
      </div>

      <!-- changelog -->
      <div class="card">
        <h2>Recent changes</h2>
        ${changeHtml}
      </div>

      <!-- Pi build node -->
      <div class="card">
        <h2>Pi build node
          <span class="muted" style="font-size:12px;font-weight:400;margin-left:8px">offload builds to the always-on Pi</span>
        </h2>
        <div class="muted" style="font-size:13px;margin-bottom:8px">Run Claude Code builds on the Raspberry Pi over SSH instead of the Mac. The Pi is always on and its Claude Code uses your subscription, so it suits overnight and agentic builds. The Mac stays the place that reviews and commits.</div>
        <div id="pibuild-status" class="muted small">Checking the Pi…</div>
        <div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">
          <input id="pibuild-repo" placeholder="local repo path, e.g. ~/mongoose-builds/app" style="width:100%">
          <textarea id="pibuild-task" placeholder="build instruction for claude -p (what to build/change)" style="width:100%;min-height:60px"></textarea>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button class="act go" onclick="runPiBuild()">Build on the Pi</button>
            <label class="muted small" style="display:flex;align-items:center;gap:4px"><input type="checkbox" id="pibuild-allowkey"> allow API-key billing</label>
            <button class="act" onclick="loadPiBuildStatus()">Recheck Pi</button>
          </div>
        </div>
        <div id="pibuild-out" class="muted small" style="margin-top:8px"></div>
      </div>

      <!-- plugin source -->
      <div class="card">
        <h2>Plugin source
          <span class="muted" style="font-size:12px;font-weight:400;margin-left:8px">MONGOOSE_PLUGIN_DIR</span>
        </h2>
        <div>${srcLine}</div>
        ${src.ok?`<div class="muted small" style="margin-top:6px">${esc(src.main_file||'no main PHP file detected')} · ${esc(src.file_count)} file${src.file_count!==1?'s':''}</div>`:`
          <div style="margin-top:10px">
            <button class="act go" onclick="scaffoldPlugin()">Scaffold {{rtm.name}} RTM plugin</button>
            <div class="muted small" style="margin-top:6px">Generates a starter plugin in MONGOOSE_PLUGIN_DIR ready to build and deploy.</div>
          </div>`}
      </div>

      <!-- wordpress connection + installed plugins -->
      <div class="card">
        <h2>WordPress site</h2>
        <div style="margin-bottom:10px">${wpLine}</div>
        ${installed.length?`<h2 style="margin-top:10px">Installed plugins</h2>${pluginRows}`:''}
        <div id="wp-plugin-out" class="muted" style="margin-top:8px"></div>
      </div>

      <!-- promote / swap -->
      ${installed.length>=2?`<div class="card">
        <h2>Promote beta → production</h2>
        <div class="muted" style="margin-bottom:8px;font-size:13px">Activates the beta plugin and deactivates the old production one in a single swap.</div>
        <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
          <div><label class="muted small">Beta slug</label>
            <input id="beta-slug" placeholder="my-plugin-beta/my-plugin-beta.php" style="width:260px"></div>
          <div><label class="muted small">Production slug</label>
            <input id="prod-slug" placeholder="my-plugin/my-plugin.php" style="width:260px"></div>
          <button class="act go" onclick="promotePlugin()">Promote</button>
        </div>
        <div id="promote-out" class="muted" style="margin-top:8px"></div>
      </div>`:``}

      <!-- previous builds -->
      <div class="card">
        <h2>Previous builds</h2>
        ${buildsHtml}
      </div>

      <!-- Mongoose self-deploy: drop a build zip and update, no terminal needed -->
      <div class="card">
        <h2>Update Mongoose</h2>
        <div class="muted" style="margin-bottom:10px;font-size:13px">Drop a Mongoose build <b>.zip</b> here (or pick a file), then stage it safely or update in one step. Your config, data and memory are preserved, and a backup is taken automatically.</div>
        <div style="margin-bottom:12px"><button class="act" onclick="checkForUpdate()">Check for a newer version</button>
          <span id="updcheck-out" class="muted" style="margin-left:8px;font-size:12px"></span></div>
        <div id="updrop" tabindex="0"
             style="border:2px dashed var(--line);border-radius:14px;padding:22px;text-align:center;cursor:pointer;transition:border-color .15s;background:var(--panel2)"
             onclick="document.getElementById('upfile').click()"
             onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();document.getElementById('upfile').click()}">
          <div id="updrop-label" style="font-size:14px;color:var(--text)">Drop a build .zip here, or <span style="color:var(--gold)">choose a file</span></div>
          <div class="muted" id="upchosen" style="font-size:12px;margin-top:6px"></div>
        </div>
        <input id="upfile" type="file" accept=".zip,application/zip" style="display:none" onchange="updatePickFile(this.files)">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
          <button class="act" id="upstage-btn" onclick="updateUploadThen('stage')" disabled>Stage &amp; check (no deploy)</button>
          <button class="act go" id="updeploy-btn" onclick="updateUploadThen('deploy')" disabled>Update now</button>
        </div>
        <div id="up-out" class="muted" style="margin-top:8px"></div>
        <details style="margin-top:10px"><summary class="muted" style="font-size:12px;cursor:pointer">Advanced: stage from a URL or server path instead</summary>
          <input id="stage-src" placeholder="https://... or /path/to/mongoose-x.y.z-sillygoose.zip" style="width:100%;margin:8px 0">
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="act" onclick="stageCandidate()">Stage + check</button>
            <button class="act go" onclick="stageAndDeploy()">Stage + deploy</button>
          </div>
        </details>
        <div id="stage-out" class="muted" style="margin-top:8px"></div>
        <div id="deploy-out" class="muted" style="margin-top:4px"></div>
      </div>`;

    // Moved here from the System tab: Claude Code is the build executor, so its status
    // (where it lives + billing posture) belongs beside the builds it runs.
    let cchtml="";
    // (where it lives + billing posture) belongs beside the builds it runs.
  // Claude Code status (where it lives + billing posture)
  try{
    const cc=await fetch("/cockpit/claude-code/status").then(r=>r.json());
    if(cc && (cc.headline||cc.locations||cc.billing)){
      cchtml+=`<div class="card"><h2>Claude Code</h2>`;
      if(cc.headline) cchtml+=`<div class="muted" style="margin-bottom:8px;font-size:12px">${esc(cc.headline)}</div>`;
      for(const loc of (cc.locations||[])){
        cchtml+=`<div class="row"><span class="k">${esc(loc.where||loc.name||"")}</span><span class="v muted">${esc(loc.status||loc.detail||"")}</span></div>`;
      }
      if(cc.billing && cc.billing.note) cchtml+=`<div class="muted" style="margin-top:6px;font-size:12px">${esc(cc.billing.note)}</div>`;
      cchtml+=`</div>`;
    }
  }catch(e){}
    if(cchtml) el.innerHTML+=cchtml;


  }catch(e){ el.innerHTML=`<div class="card bad">Build tab failed: ${esc(e)}</div>`; }
  // After the tab renders, probe the Pi build node (non-blocking; it SSHes to the Pi).
  loadPiBuildStatus();
}

// ── Pi build node (remote builds over SSH) ───────────────────────────────────
async function loadPiBuildStatus(){
  const el=document.getElementById("pibuild-status"); if(!el) return;
  el.textContent="Checking the Pi…";
  try{
    const d=await j("/cockpit/pi-build/status");
    if(d.ready){
      const pi=d.pi||{};
      const billing = pi.api_key_in_env? '<span class="warn">API-rate billing (key set on Pi)</span>'
                                         : '<span class="ok">subscription billing</span>';
      el.innerHTML=`<span class="ok">✓ Pi ready to build</span> · ${billing} · build dir <code>${esc(d.build_dir)}</code>`;
    } else {
      el.innerHTML=`<span class="warn">⚠ ${esc(d.detail||"Pi not ready")}</span>`;
    }
  }catch(e){ el.innerHTML=`<span class="muted">Couldn't check the Pi: ${esc(e)}</span>`; }
}
async function runPiBuild(){
  const out=document.getElementById("pibuild-out"); if(!out) return;
  const repo=(document.getElementById("pibuild-repo")||{}).value||"";
  const task=(document.getElementById("pibuild-task")||{}).value||"";
  const allowKey=(document.getElementById("pibuild-allowkey")||{}).checked||false;
  if(!repo.trim()||!task.trim()){ out.innerHTML='<span class="warn">Add both a repo path and a build instruction.</span>'; return; }
  out.innerHTML='<span class="muted">Syncing the repo to the Pi and running the build… this can take a while.</span>';
  try{
    const r=await postAction("/cockpit/pi-build/run",{repo:repo.trim(),task:task.trim(),allow_api_key:allowKey});
    const d=await r.json();
    if(d.ok){
      out.innerHTML=`<span class="ok">✓ Built on the Pi in <code>${esc(d.remote_repo||"")}</code></span> · ${d.files_synced||0} files synced${d.rate_limited?' · <span class="warn">rate-limited</span>':''}<div class="muted small" style="margin-top:4px">${esc(d.review_hint||"")}</div>`;
    } else {
      out.innerHTML=`<span class="bad">✗ ${esc(d.detail||d.error||"Pi build did not succeed")}</span>${d.output_tail?`<pre class="small" style="white-space:pre-wrap;margin-top:6px;opacity:.85">${esc((d.output_tail||"").slice(-800))}</pre>`:""}`;
    }
  }catch(e){ out.innerHTML=`<span class="bad">Pi build error: ${esc(e)}</span>`; }
}

// ── plugin build/deploy actions ──────────────────────────────────────────────
async function downloadBuildZip(name,btn){
  const orig=btn?btn.textContent:null; if(btn){ btn.disabled=true; btn.textContent="..."; }
  try{
    const n=await ensureNonce();
    const r=await fetch("/build/plugin/download/"+encodeURIComponent(name),{headers:{"X-Cockpit-Nonce":n||""}});
    if(!r.ok){ alert("Could not download "+name+" (it may have been removed; refresh and try again)."); return; }
    const blob=await r.blob(); const u=URL.createObjectURL(blob);
    const a=document.createElement("a"); a.href=u; a.download=name; a.click();
    setTimeout(()=>URL.revokeObjectURL(u),4000);
  }catch(e){ alert("Could not download "+name+": "+e); }
  finally{ if(btn){ btn.disabled=false; btn.textContent=orig; } }
}
function buildFailMsg(d,fallback){
  // The backend's reason code (e.g. "no_source") is machine-readable, not a sentence for a
  // person. Show its hint/path when present instead of leaving a bare code as the only text.
  const bits=[esc(d.reason||d.error||fallback||"failed")];
  if(d.hint) bits.push(esc(d.hint));
  else if(d.path) bits.push("Expected at "+esc(d.path)+".");
  return bits.join(": ");
}
async function buildPlugin(){
  const out=document.getElementById("build-out"); if(!out) return;
  out.textContent="Building plugin zip…";
  try{
    const r=await postAction("/build/plugin/build",{version_suffix:"beta"});
    const d=await r.json();
    out.innerHTML=d.ok
      ?`<span class="ok">✓ Built: ${esc(d.file_name)} (${((d.size_bytes||0)/1024).toFixed(0)} KB)</span> <button class="act go" style="padding:3px 8px;margin:0;font-size:12px" onclick="downloadBuildZip('${esc(d.file_name)}',this)">Download</button>`
      :`<span class="bad">✗ Build failed: ${buildFailMsg(d)}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 800);
  }catch(e){ out.textContent="Build error: "+e; }
}
async function deployBeta(){
  const out=document.getElementById("build-out"); if(!out) return;
  out.textContent="Building and deploying to RTM site (replaces existing version if installed)…";
  try{
    const r=await postAction("/build/plugin/deploy-beta",{version_suffix:"beta"});
    const d=await r.json();
    out.innerHTML=d.ok
      ?`<span class="ok">✓ ${d.replaced?'Replaced':'Deployed'}: ${esc(d.name)} v${esc(d.version)}${d.status==='inactive'?', click Activate to go live':', active'}</span>`
      :`<span class="bad">✗ ${buildFailMsg(d)}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 800);
  }catch(e){ out.textContent="Deploy error: "+e; }
}
async function deployZip(path){
  const out=document.getElementById("build-out"); if(!out) return;
  out.textContent="Uploading "+path+"…";
  try{
    const r=await postAction("/build/plugin/deploy-beta",{zip_path:path});
    const d=await r.json();
    out.innerHTML=d.ok
      ?`<span class="ok">✓ Uploaded: ${esc(d.name)}, ${esc(d.status)}</span>`
      :`<span class="bad">✗ ${buildFailMsg(d)}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 800);
  }catch(e){ out.textContent="Error: "+e; }
}
async function scaffoldPlugin(){
  const out=document.getElementById("build-out"); if(!out) return;
  out.textContent="Scaffolding {{rtm.name}} RTM plugin…";
  try{
    const r=await postAction("/build/plugin/scaffold",{version:"0.1.0"});
    const d=await r.json();
    out.innerHTML=d.ok
      ?`<span class="ok">✓ Plugin scaffolded at ${esc(d.path)}, ${d.files_created?.length||0} files created.<br>
         <span class="muted small">Edit the PHP file to build out RTM features, then click Build.</span></span>`
      :`<span class="bad">✗ ${esc(d.reason)}: ${esc(d.hint||d.path||"")}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 1000);
  }catch(e){ out.textContent="Scaffold error: "+e; }
}
async function connectWordpress(){
  const out=document.getElementById("wp-connect-out");
  const url=((document.getElementById("wp-url")||{}).value||"").trim();
  const user=((document.getElementById("wp-user")||{}).value||"").trim();
  const pass=((document.getElementById("wp-pass")||{}).value||"").trim();
  if(!url||!user||!pass){ if(out) out.innerHTML='<span class="warn">Fill in all three fields.</span>'; return; }
  if(out) out.textContent="Connecting…";
  try{
    const r=await postAction("/build/wordpress/connect",{url,username:user,app_password:pass});
    const d=await r.json();
    const c=d.connection||{};
    if(d.ok){ if(out) out.innerHTML=`<span class="ok">✓ Connected as ${esc(c.user||user)}</span>`; _reloadSoon(loadBuild, 800); }
    else { if(out) out.innerHTML=`<span class="bad">✗ ${esc(c.reason||d.reason||"Could not connect")}</span>`; }
  }catch(e){ if(out) out.innerHTML=`<span class="bad">✗ ${esc(String(e&&e.message||e))}</span>`; }
}

// Reveal the connect form again for an already-connected site (to change credentials).
function wpShowConnect(){
  const slot=document.getElementById("wp-connect-slot");
  if(!slot||slot.dataset.open) return;
  slot.dataset.open="1";
  slot.innerHTML=`<div style="margin-top:8px">
     <input id="wp-url" placeholder="https://your-site.uk" style="display:block;width:100%;margin:3px 0;padding:6px;border:1px solid var(--line,#444);border-radius:6px;background:transparent;color:inherit" />
     <input id="wp-user" placeholder="WordPress username" style="display:block;width:100%;margin:3px 0;padding:6px;border:1px solid var(--line,#444);border-radius:6px;background:transparent;color:inherit" />
     <input id="wp-pass" type="password" placeholder="Application password" style="display:block;width:100%;margin:3px 0;padding:6px;border:1px solid var(--line,#444);border-radius:6px;background:transparent;color:inherit" />
     <button class="act go" onclick="connectWordpress()" style="margin-top:4px">Save and connect</button>
     <span id="wp-connect-out" class="small" style="margin-left:8px"></span>
    </div>`;
}

async function activatePlugin(slug, status){
  const out=document.getElementById("wp-plugin-out"); if(!out) return;
  out.textContent=(status==="active"?"Activating":"Deactivating")+"…";
  try{
    const r=await postAction("/build/plugin/activate",{plugin:slug,status});
    const d=await r.json();
    out.innerHTML=d.ok?`<span class="ok">✓ ${esc(slug)}: ${esc(d.status)}</span>`
      :`<span class="bad">✗ ${esc(d.reason||d.detail||"failed")}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 600);
  }catch(e){ out.textContent="Error: "+e; }
}
async function deletePlugin(slug){
  if(!confirm("Delete "+slug+" from WordPress? This cannot be undone.")) return;
  const out=document.getElementById("wp-plugin-out"); if(!out) return;
  out.textContent="Deleting…";
  try{
    const r=await requestAction("/build/plugin/"+encodeURIComponent(slug),{},{method:"DELETE"});
    const d=await r.json();
    out.innerHTML=d.ok?`<span class="ok">✓ Deleted ${esc(slug)}</span>`
      :`<span class="bad">✗ ${esc(d.reason||"failed")}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 600);
  }catch(e){ out.textContent="Error: "+e; }
}
async function promotePlugin(){
  const beta=document.getElementById("beta-slug").value.trim();
  const prod=document.getElementById("prod-slug").value.trim();
  const out=document.getElementById("promote-out");
  if(!beta||!prod){ if(out) out.textContent="Enter both slugs."; return; }
  if(out) out.textContent="Promoting…";
  try{
    const r=await postAction("/build/plugin/promote",{beta_slug:beta,production_slug:prod});
    const d=await r.json();
    out.innerHTML=d.ok?`<span class="ok">✓ Swap complete${d.partial?' (partial, check production deactivation manually)':''}</span>`
      :`<span class="bad">✗ Failed at ${esc(d.step||"promote")}: ${esc(d.detail?.reason||d.detail||"error")}</span>`;
    if(d.ok) _reloadSoon(loadBuild, 800);
  }catch(e){ out.textContent="Error: "+e; }
}

// delete plugin uses DELETE method: postAction may only do POST, handle here
const _origPostAction=postAction;


// ── Update Mongoose: drop/pick a build zip, upload it, then stage or deploy. No terminal needed. ──
async function checkForUpdate(){
  var out=document.getElementById("updcheck-out");
  if(out) out.textContent="checking...";
  try{
    var n=await ensureNonce();
    var d=await fetch("/cockpit/update/check",{headers:{"X-Cockpit-Nonce":n}}).then(r=>r.json());
    if(!out) return;
    if(d.update_available){
      out.innerHTML='<span style="color:var(--gold)">Newer version available: '+esc(d.latest_version||"?")+' (you have '+esc(d.current_version||"?")+')</span>';
    } else if(d.current_version){
      out.innerHTML='<span class="ok">You\'re on the latest ('+esc(d.current_version)+')</span>';
    } else {
      out.textContent=d.error||d.note||"could not check";
    }
  }catch(e){ if(out) out.textContent="check failed"; }
}

var _updateFile = null;
function updatePickFile(files){
  _updateFile = (files && files.length)? files[0] : null;
  var chosen=document.getElementById("upchosen");
  var sb=document.getElementById("upstage-btn"), db=document.getElementById("updeploy-btn");
  if(_updateFile){
    if(!/\.zip$/i.test(_updateFile.name)){
      if(chosen) chosen.innerHTML='<span class="bad">That isn\'t a .zip, pick a Mongoose build zip.</span>';
      _updateFile=null; if(sb)sb.disabled=true; if(db)db.disabled=true; return;
    }
    var mb=(_updateFile.size/1048576).toFixed(1);
    if(chosen) chosen.innerHTML='Selected: <b>'+esc(_updateFile.name)+'</b> ('+mb+' MB)';
    if(sb)sb.disabled=false; if(db)db.disabled=false;
  } else {
    if(chosen) chosen.textContent="";
    if(sb)sb.disabled=true; if(db)db.disabled=true;
  }
}
async function updateUploadThen(mode){
  var out=document.getElementById("up-out");
  if(!_updateFile){ if(out) out.innerHTML='<span class="bad">Choose a build .zip first.</span>'; return; }
  if(mode==="deploy" && !confirm("Update Mongoose to "+_updateFile.name+" now? A backup is taken first, and your data/config/memory are preserved.")) return;
  var sb=document.getElementById("upstage-btn"), db=document.getElementById("updeploy-btn");
  if(sb)sb.disabled=true; if(db)db.disabled=true;
  out.textContent="Uploading "+_updateFile.name+"…";
  try{
    // 1) upload the raw bytes -> server returns a temp path (source)
    var n=await ensureNonce();
    var up=await fetch("/build/upload",{method:"POST",
      headers:{"X-Cockpit-Nonce":n||"","X-Filename":_updateFile.name,"Content-Type":"application/zip"},
      body:_updateFile});
    var uj=await up.json();
    if(!uj.ok){ out.innerHTML='<span class="bad">✗ '+esc(uj.error||"upload failed")+'</span>'; if(sb)sb.disabled=false; if(db)db.disabled=false; return; }
    // 2) stage or deploy that uploaded file via the existing endpoints (source = uploaded path)
    if(mode==="stage"){
      out.textContent="Staging + checking "+uj.filename+"… (this runs safety checks, ~30s)";
      var r=await postAction("/build/stage",{source:uj.source},180000);
      var d=await r.json();
      if(d.ok){ out.innerHTML='<span class="ok">✓ Staged v'+esc(d.version||"?")+' ('+esc(d.stage_id||"")+'), checks: '+(d.ready?"✓ passed. Use “Update now” to apply.":"⚠ see Build tab for detail")+'</span>'; preserveScrollAcrossReload(loadBuild); }
      else out.innerHTML='<span class="bad">✗ '+esc(d.error||"staging failed")+'</span>';
    } else {
      out.textContent="Updating Mongoose… do not close this window. (backup → apply → verify)";
      var r2=await postAction("/build/deploy",{source:uj.source},300000);
      var d2=await r2.json();
      if(d2.ok) out.innerHTML='<span class="ok">✓ '+esc(d2.message||"Updated. Mongoose is restarting on the new build.")+'</span>';
      else out.innerHTML='<span class="bad">✗ '+esc(d2.error||"update failed")+'</span>';
    }
  }catch(e){ out.innerHTML='<span class="bad">Error: '+esc(""+e)+'</span>'; }
  finally{ if(sb)sb.disabled=false; if(db)db.disabled=false; }
}
// drag-and-drop onto the dropzone
(function wireUpdateDrop(){
  function bind(){
    var dz=document.getElementById("updrop");
    if(!dz || dz._wired) return;
    dz._wired=true;
    ["dragenter","dragover"].forEach(function(ev){ dz.addEventListener(ev,function(e){ e.preventDefault(); e.stopPropagation(); dz.style.borderColor="var(--gold)"; }); });
    ["dragleave","drop"].forEach(function(ev){ dz.addEventListener(ev,function(e){ e.preventDefault(); e.stopPropagation(); dz.style.borderColor="var(--line)"; }); });
    dz.addEventListener("drop",function(e){ var f=e.dataTransfer&&e.dataTransfer.files; if(f&&f.length) updatePickFile(f); });
  }
  // the dropzone is rendered when the Build tab loads, so (re)bind on an interval-safe hook
  document.addEventListener("click",bind,true);
  setTimeout(bind,500);
})();

// ── Song exchange chat intercepts ────────────────────────────────────────────────────────────
// "recommend X for Y" -> records recommendation on the pending exchange
// "feedback sx_abc123: loved it" -> records feedback

function interceptSongCommand(msg) {
  var rec = msg.trim().match(/^recommend\s+(.+?)\s+(?:to|for)\s+(\S+)$/i);
  if (rec) return {type:"recommend", song:rec[1].trim(), recipient:rec[2].trim()};
  var fb = msg.trim().match(/^feedback\s+(sx_[a-f0-9]+)[:\s]+(.+)$/i);
  if (fb) return {type:"feedback", exchange_id:fb[1], text:fb[2].trim()};
  return null;
}

async function handleSongCommand(cmd, you, bot, sys) {
  var n = await ensureNonce();
  if (cmd.type === "recommend") {
    // resolve the exchange by recipient name
    try {
      var hR = await fetch("/song-exchange/history"); var hD = await hR.json();
      var exchanges = (hD.exchanges||[]).filter(function(e){
        return e.status==="pending" && e.recipient_name.toLowerCase()===cmd.recipient.toLowerCase();
      });
      if (!exchanges.length) {
        bot("no open song exchange with " + esc(cmd.recipient) + " right now: Mongoose will ask again soon.");
        return;
      }
      var exId = exchanges[0].exchange_id;
      var r = await fetch("/song-exchange/"+encodeURIComponent(exId)+"/recommend", {
        method:"POST", headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n},
        body: JSON.stringify({song: cmd.song})});
      var d = await r.json();
      if (d.ok) bot("🎵 recommended \"" + esc(d.song) + "\" to " + esc(d.to) + ". they'll hear about it!");
      else bot(d.error || "could not record recommendation");
    } catch(e) { bot("error: "+e); }
  } else if (cmd.type === "feedback") {
    try {
      var r2 = await fetch("/song-exchange/"+encodeURIComponent(cmd.exchange_id)+"/feedback", {
        method:"POST", headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n},
        body: JSON.stringify({feedback: cmd.text})});
      var d2 = await r2.json();
      if (d2.ok) bot("💬 feedback noted! the person who recommended it will hear about this.");
      else bot(d2.error || "could not record feedback");
    } catch(e) { bot("error: "+e); }
  }
}

async function loadSongExchangePanel() {
  var el = document.getElementById("song-exchange-panel"); if (!el) return;
  try {
    var r = await fetch("/song-exchange/history"); var d = await r.json();
    if (!d.ok || !d.exchanges || !d.exchanges.length) {
      el.innerHTML = '<div class="muted" style="font-size:12px">No exchanges yet: Mongoose will ask on Sunday evenings when there are at least two users.</div>';
      return;
    }
    var html = "";
    for (var ex of d.exchanges.slice(0,5)) {
      var dot = ex.status==="feedback_given"?"ok": ex.status==="recommended"?"warn":"unknown";
      html += `<div class="row" style="margin:6px 0">
        <span class="k"><span class="dot ${dot}"></span>${esc(ex.asker_name)} &#8594; ${esc(ex.recipient_name)}</span>
        <span class="v muted">${esc(ex.song||"awaiting song")}${ex.feedback?" · "+esc(ex.feedback.slice(0,30)):""}</span>
      </div>`;
    }
    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<div class="muted">Could not load.</div>'; }
}

// Users can poke each other via the chat ("poke mic") or the sillygoose hold-button.
// Button: press and hold 1.5s: the goose waddles while charging: release to fire.
// Chat intercept: "poke <name>" resolves before hitting the LLM.

var _pokeTimers = {};

function initPokeBtn(btn, username) {
  var holdMs = 900;
  var prog = btn.querySelector(".poke-progress");
  var timer = null, startAt = 0;

  // Keyboard-operable + screen-reader labelled
  if (!btn.hasAttribute("tabindex")) btn.setAttribute("tabindex", "0");
  btn.setAttribute("role", "button");
  btn.setAttribute("aria-label", "Poke " + username + " (hold, or press Enter)");

  function start(e) {
    if (btn.classList.contains("fired")) return;
    e.preventDefault();
    btn.classList.add("holding");
    startAt = Date.now();
    if (prog) { prog.style.transition = "width " + holdMs + "ms linear"; prog.style.width = "100%"; }
    timer = setTimeout(function() { firePoke(btn, username, prog); }, holdMs);
  }
  function cancel() {
    clearTimeout(timer); timer = null;
    btn.classList.remove("holding");
    if (prog) { prog.style.transition = "none"; prog.style.width = "0%"; }
  }
  btn.addEventListener("mousedown", start);
  btn.addEventListener("touchstart", start, {passive:false});
  btn.addEventListener("mouseup", cancel);
  btn.addEventListener("mouseleave", cancel);
  btn.addEventListener("touchend", cancel);
  btn.addEventListener("touchcancel", cancel);
  // Keyboard: Enter/Space fires immediately: no hold required for keyboard/motor users
  btn.addEventListener("keydown", function(e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!btn.classList.contains("fired")) firePoke(btn, username, prog);
    }
  });
}

async function firePoke(btn, username, prog) {
  btn.classList.remove("holding");
  btn.classList.add("fired");
  if (prog) { prog.style.transition = "none"; prog.style.width = "100%"; }
  var label = btn.querySelector(".poke-label");
  if (label) label.textContent = "poked! 👆🪿";
  try {
    var r = await fetch("/poke/" + encodeURIComponent(username), {method:"POST",
      headers:{"X-Cockpit-Nonce": await ensureNonce()}});
    var d = await r.json();
    if (!d.ok && label) label.textContent = d.error || "couldn't poke";
  } catch(e) { if (label) label.textContent = "error"; }
  setTimeout(function() {
    btn.classList.remove("fired");
    if (prog) { prog.style.width = "0%"; }
    if (label) label.textContent = "hold to poke";
  }, 3000);
}

async function showPokeSetup() {
  var el = document.getElementById("poke-panel"); if (!el) return;
  if (document.getElementById("poke-setup-card")) return;
  var meR = await fetch("/account/me"); var me = await meR.json().catch(()=>({}));
  if (!me.authenticated || !me.account) {
    el.insertAdjacentHTML("beforebegin",
      '<div class="card" id="poke-setup-card" style="border:1px solid var(--gold)">'
      + '<h2>Set your Mongoose name</h2>'
      + '<p class="muted">Sign in with a username to set a display name.</p>'
      + '<button class="act" onclick="document.getElementById(\'poke-setup-card\').remove()">Close</button></div>');
    return;
  }
  var html = `<div class="card" style="border:1px solid var(--gold);margin-bottom:10px" id="poke-setup-card">
    <h2>Your Mongoose identity</h2>
    <div class="muted" style="margin-bottom:10px;font-size:12px">What should others call you?</div>
    <input id="poke-dname" type="text" placeholder="Display name (e.g. Mic, Dev, Toby)" maxlength="32"
      style="width:100%;margin-bottom:10px">
    <label style="display:flex;align-items:center;gap:8px;margin-bottom:10px;font-size:13px">
      <input type="checkbox" id="poke-allow" checked>
      Allow others to poke me (you'll get 👆🪿 notifications)
    </label>
    <button class="act go" onclick="savePokeSetup()">Save</button>
    <button class="act" onclick="document.getElementById('poke-setup-card').remove();loadPokePanel();"
      style="margin-left:8px">Cancel</button>
    <div id="poke-setup-msg" class="muted" style="margin-top:8px"></div>
  </div>`;
  el.insertAdjacentHTML("beforebegin", html);
}

async function savePokeSetup() {
  var dn = (document.getElementById("poke-dname")||{}).value || "";
  var ap = !!(document.getElementById("poke-allow")||{checked:true}).checked;
  var msg = document.getElementById("poke-setup-msg");
  try {
    var r = await fetch("/account/profile", {method:"PUT",
      headers:{"Content-Type":"application/json","X-Cockpit-Nonce": await ensureNonce()},
      body: JSON.stringify({display_name: dn, allow_poke: ap})});
    var d = await r.json();
    if (d.ok) {
      if (msg) msg.innerHTML = '<span class="ok">Saved as <b>' + esc(d.display_name||d.username) + '</b>.</span>';
      setTimeout(function(){ var c=document.getElementById("poke-setup-card"); if(c) c.remove(); loadPokePanel(); }, 1500);
    } else {
      if (msg) msg.textContent = d.error || "could not save";
    }
  } catch(e) { if (msg) msg.textContent = "error: " + e; }
}

async function loadPokePanel() {
  var el = document.getElementById("poke-panel"); if (!el) return;
  el.innerHTML = '<div class="muted">Loading users...</div>';
  try {
    var r = await fetch("/users"); var d = await r.json();
    if (!d.ok) { el.innerHTML = '<div class="muted">Sign in to see users.</div>'; return; }
    var users = d.users || [];
    if (!users.length) { el.innerHTML = '<div class="muted">No other users yet.</div>'; return; }
    var html = "";
    for (var u of users) {
      var canPoke = u.allow_poke;
      html += '<div class="poke-card">';
      html += '<span class="poke-name">' + esc(u.display_name || u.username) + '</span>';
      if (canPoke) {
        html += '<button class="poke-btn" data-user="' + esc(u.username) + '">'
              + '<div class="poke-progress"></div>'
              + '<img class="poke-goose sillygoose-img" src="/static/img/sillygoose-32.png" width="20" height="20" alt="">'
              + '<span class="poke-label">hold to poke</span>'
              + '</button>';
      } else {
        html += '<span class="poke-no">pokes off</span>';
      }
      html += '</div>';
    }
    el.innerHTML = html;
    el.querySelectorAll(".poke-btn").forEach(function(btn) {
      initPokeBtn(btn, btn.dataset.user);
    });
  } catch(e) { el.innerHTML = '<div class="muted">Could not load users.</div>'; }
}

// Chat command intercept: "poke <name>" before hitting the LLM
function interceptPokeCommand(msg) {
  var m = msg.trim().match(/^poke\s+(\S+)$/i);
  if (!m) return null;
  return m[1];
}

async function stageCandidate(){
  const src=document.getElementById("stage-src").value.trim();
  const out=document.getElementById("stage-out");
  if(!src||!out) return;
  out.textContent="Staging…";
  try{
    const payload=src.startsWith("http")?{url:src}:{source:src};
    const r=await postAction("/build/stage",payload);
    const d=await r.json();
    if(d.ok){
      out.innerHTML=`<span class="ok">✓ Staged v${esc(d.version)} (${esc(d.stage_id)}), checks: ${d.ready?"✓ passed":"⚠ see detail"}</span>`;
    } else {
      out.innerHTML=`<span class="bad">✗ ${esc(d.error||"staging failed")}</span>`;
    }
    preserveScrollAcrossReload(loadBuild);
  }catch(e){ out.textContent="Error: "+e; }
}
async function stageAndDeploy(){
  const src=document.getElementById("stage-src").value.trim();
  const out=document.getElementById("deploy-out");
  if(!src||!out) return;
  out.textContent="Staging and deploying… do not close this window.";
  try{
    const payload=src.startsWith("http")?{url:src}:{source:src};
    const r=await postAction("/build/deploy",payload);
    const d=await r.json();
    if(d.ok){
      out.innerHTML=`<span class="ok">✓ ${esc(d.message)}</span>`;
    } else {
      out.innerHTML=`<span class="bad">✗ ${esc(d.error||"deploy failed")}</span>`;
    }
  }catch(e){ out.textContent="Deploy error: "+e; }
}
async function loadScheduler(){
  const el=document.getElementById("scheduler");
  try{
    const data=await j("/cockpit/scheduler"); const jobs=data.jobs||[];
    const running=data.running===true;
    // Overall health first, so the user knows if jobs will actually fire.
    let html=`<div class="card"><h2>Scheduler
      <span class="dot ${running?'ok':'bad'}" style="margin-left:8px"></span>
      <span class="muted" style="font-size:13px">${running?'running':'not running'}</span></h2>`;
    if(!running){
      html+=`<div class="muted" style="margin-bottom:10px">The scheduler isn't running, so these jobs won't fire on their own yet. Install it as a background service so it starts automatically: <span style="font-family:monospace">mongoose service</span>. (The installer normally does this for you.)</div>`;
    } else {
      html+=`<div class="muted" style="margin-bottom:10px">Running. These jobs fire automatically on the schedules below.</div>`;
    }
    html+=`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
      <button class="act" onclick="schedulerControl('free_memory')">Free memory</button>
      <button class="ghost" onclick="schedulerControl('enable','benchmark,reindex_files,sync_email,sync_tasks,delight_topup,self_reflection')">Resume common jobs</button>
    </div>`;
    html+=`</div><div class="card"><h2>Scheduled jobs</h2>`;
    if(!jobs.length) html+=`<div class="muted">No jobs registered.</div>`;
    for(const job of jobs){
      // A grey dot means "never run yet", explain it rather than leaving it ambiguous.
      const jobName = String(job.job||job.name||job.id||"");
      const enabled = job.enabled !== false;
      const status = job.last_status || (job.last_run? "" : "not run yet");
      const dotcls = !enabled ? "muted" : (job.last_status? dotFor(job.last_status) : "muted");
      html+=`<div class="row" style="align-items:flex-start"><span class="k" style="flex:1">
             <span class="dot ${dotcls}"></span><b>${esc(prettyJob(jobName))}</b>${enabled?"":` <span class="pill bad">paused</span>`}
             ${job.description?`<div class="muted" style="font-size:12px;margin:2px 0">${esc(job.description)}</div>`:""}
             <div class="muted" style="font-size:11px">${esc(job.schedule||"")}${job.disabled_reason?` · paused: ${esc(job.disabled_reason)}`:""}</div></span>
             <span class="v muted" style="font-size:12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end">
               <span>last: ${esc(job.last_run||"never")}${status?(", "+esc(status)):""}</span>
               <button class="ghost" onclick="schedulerControl('${enabled?'disable':'enable'}','${esc(jobName)}')">${enabled?'Pause':'Resume'}</button>
             </span></div>`;
    }
    html+=`</div>`;
    el.innerHTML=html;
  }catch(e){ el.innerHTML=retryCard("Scheduler", e, "loadScheduler"); }
}
async function schedulerControl(action,jobs){
  try{
    const n=await ensureNonce();
    const body={action:action,reason:"cockpit scheduler control"};
    if(jobs) body.jobs=jobs;
    const r=await fetch("/cockpit/scheduler/control",{method:"POST",headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n||""},body:JSON.stringify(body)});
    const d=await r.json();
    if(!d.ok){ alert(d.error||"Could not update scheduler."); return; }
    await loadScheduler();
  }catch(e){ alert("Could not update scheduler: "+e); }
}
function prettyJob(name){ return String(name||"").replace(/_/g," ").replace(/\b\w/g,c=>c.toUpperCase()); }
async function reindexNow(btn){
  btn.disabled=true; const old=btn.textContent; btn.textContent="indexing...";
  try{
    const n=await ensureNonce();
    const r=await fetch("/cockpit/reindex-files",{method:"POST",headers:{"X-Cockpit-Nonce":n||""}});
    const d=await r.json();
    if(!d.ok){ btn.textContent=old; btn.disabled=false; alert(d.error||"Couldn't index."); return; }
    btn.textContent=d.started? "indexing started, search again shortly" : `indexed ${d.indexed||0} file(s), search again`;
  }catch(e){ btn.textContent=old; btn.disabled=false; alert("Indexing failed: "+e); }
}

// Sync IMAP mail from the search empty-state, so "no email indexed" has a one-click fix right there.
async function syncMailNow(btn){
  btn.disabled=true; const old=btn.textContent; btn.textContent="syncing mail...";
  const msg=document.getElementById("searchIdxMsg");
  try{
    const r=await postAction("/cockpit/mail/sync",{},10*60*1000); const d=await r.json();
    if(!d.ok){ btn.textContent=old; btn.disabled=false;
      if(msg) msg.textContent=esc(d.error||"mail sync failed (is IMAP set up?)"); return; }
    btn.textContent=`synced ${d.new_messages!=null?d.new_messages:""}, search again`;
    if(msg) msg.textContent=`${d.message_count!=null?d.message_count+" message(s) indexed":""}`;
  }catch(e){ btn.textContent=old; btn.disabled=false; if(msg) msg.textContent="mail sync failed: "+esc(e); }
}
const searchBox = document.getElementById("q");
function _searchDetailEl(i){ return document.getElementById("sr-detail-"+i); }
async function copyResultHandle(v){
  try{ await navigator.clipboard.writeText(v||""); }
  catch(e){ prompt("Copy this result reference", v||""); }
}
function _downloadBlob(blob, filename){
  const url=URL.createObjectURL(blob); const a=document.createElement("a");
  a.href=url; a.download=filename||"download"; document.body.appendChild(a); a.click();
  setTimeout(()=>{ URL.revokeObjectURL(url); a.remove(); }, 1000);
}
function _filenameFromDisposition(cd, fallback){
  const m=/filename="?([^";]+)"?/i.exec(cd||""); return m? m[1] : fallback;
}
async function downloadEmail(mid,fmt,ev){
  if(ev) ev.stopPropagation();
  const n=await ensureNonce();
  const r=await fetch(`/email/message/${encodeURIComponent(mid)}/download?format=${encodeURIComponent(fmt)}`,{headers:{"X-Cockpit-Nonce":n||""}});
  if(!r.ok){ alert("Email download failed: "+(await r.text())); return; }
  const blob=await r.blob();
  _downloadBlob(blob,_filenameFromDisposition(r.headers.get("Content-Disposition"),`email.${fmt}`));
}
async function downloadAttachment(attId,ev){
  if(ev) ev.stopPropagation();
  const n=await ensureNonce();
  const r=await fetch(`/email/attachment/${encodeURIComponent(attId)}/download`,{headers:{"X-Cockpit-Nonce":n||""}});
  if(!r.ok){ alert("Attachment download failed: "+(await r.text())); return; }
  const blob=await r.blob();
  _downloadBlob(blob,_filenameFromDisposition(r.headers.get("Content-Disposition"),"attachment"));
}
async function downloadAllAttachments(mid,ev){
  if(ev) ev.stopPropagation();
  const n=await ensureNonce();
  const r=await fetch(`/email/message/${encodeURIComponent(mid)}/attachments.zip`,{headers:{"X-Cockpit-Nonce":n||""}});
  if(!r.ok){ alert("Attachment download failed: "+(await r.text())); return; }
  const blob=await r.blob();
  _downloadBlob(blob,_filenameFromDisposition(r.headers.get("Content-Disposition"),"attachments.zip"));
}
async function toggleSearchDetail(i, kind, openRef){
  const d=_searchDetailEl(i); if(!d) return;
  if(d.classList.contains("open")){ d.classList.remove("open"); return; }
  d.classList.add("open");
  if(kind!=="email" || !openRef || d.dataset.loaded==="1") return;
  d.innerHTML=`<div class="muted">Loading email...</div>`;
  try{
    const n=await ensureNonce();
    const r=await fetch(`/email/message/${encodeURIComponent(openRef)}`,{headers:{"X-Cockpit-Nonce":n||""}});
    if(!r.ok) throw new Error(await r.text());
    const data=await r.json(); const m=data.message||{};
    const atts=m.attachments||[];
    const downloadable=atts.filter(a=>a.downloadable);
    const attHtml = atts.length? `<div class="result-meta"><b>Attachments</b>: ${atts.map(a=>`${esc(a.filename||"attachment")} ${a.downloadable?`<button class="act" onclick="downloadAttachment('${esc(String(a.id))}',event)">download</button>`:`<span class="muted">metadata only</span>`}`).join(" · ")}</div>` : `<div class="result-meta">No indexed attachments.</div>`;
    const allBtn = downloadable.length>1? `<button class="act" onclick="downloadAllAttachments('${esc(String(m.id))}',event)">Download all attachments</button>` : "";
    d.innerHTML=`<div class="result-meta"><b>From</b>: ${esc(m.sender||"")}</div>
      <div class="result-meta"><b>To</b>: ${esc(m.recipients||"")}</div>
      <div class="result-meta"><b>Date</b>: ${esc(m.sent_at||m.received_at||"")}</div>
      <div class="result-meta"><b>Folder</b>: ${esc(m.mailbox||"")}</div>
      <pre class="email-body">${esc(m.body||"(empty body)")}</pre>
      <div style="margin-top:8px">
        <button class="act" onclick="downloadEmail('${esc(String(m.id))}','txt',event)">Download txt</button>
        <button class="act" onclick="downloadEmail('${esc(String(m.id))}','eml',event)">Download eml</button>
        <button class="act" onclick="downloadEmail('${esc(String(m.id))}','pdf',event)">Download pdf</button>
        ${allBtn}
      </div>${attHtml}`;
    d.dataset.loaded="1";
  }catch(e){ d.innerHTML=`<div class="muted">Could not load email detail: ${esc(String(e))}</div>`; }
}
function renderSearchResult(r,i){
  const kind = String(r.kind||"?");
  const opened = String(r.open||r.path||r.url||"");
  const isEmail = kind==="email";
  const isAttachment = kind==="attachment";
  // An attachment belongs to an email. Clicking it should open the PARENT email (where the body and
  // all its attachments live), so resolve the integer email_id the detail fetch needs. The email
  // result already carries its integer id in `open`.
  const emailRef = isEmail? opened : (isAttachment? String(r.email_id||"") : "");
  const expandable = (isEmail && opened) || (isAttachment && r.email_id);
  const primary = isEmail? (r.title||"(no subject)") : (isAttachment? (r.where||r.title||"attachment") : (r.title||r.where||"result"));
  const secondary = isAttachment? (r.title||"") : (r.where||"");
  const snippet = esc((r.snippet||"").slice(0,500));
  const when = esc(r.sent_at||r.received_at||r.date||"");
  const itemRef = String(r.item_ref||"");
  const reasons = Array.isArray(r.rank_reason)? r.rank_reason.join(" · ") : "";
  const actions = Array.isArray(r.actions)? r.actions.map(a=>a.label||a.action||"").filter(Boolean).join(" · ") : "";
  const corr = Array.isArray(r.correlation_keys)? r.correlation_keys.join(" · ") : "";
  const detailBits = [];
  if(secondary) detailBits.push(`<div class="result-meta">${esc(secondary)}</div>`);
  if(when) detailBits.push(`<div class="result-meta">date: ${when}</div>`);
  if(itemRef) detailBits.push(`<div class="result-meta">reference: <code>${esc(itemRef)}</code>${r.result_set_id?` · set <code>${esc(String(r.result_set_id))}</code>`:""}</div>`);
  if(r.score!=null || reasons) detailBits.push(`<div class="result-meta">rank: ${r.score!=null?esc(String(r.score)):""}${reasons?`: ${esc(reasons)}`:""}</div>`);
  if(actions) detailBits.push(`<div class="result-meta">available actions: ${esc(actions)}</div>`);
  if(corr) detailBits.push(`<div class="result-meta">correlation keys: ${esc(corr)}</div>`);
  const copyValue = itemRef || opened;
  const copyBtn = copyValue? `<button class="act" data-open="${esc(copyValue)}" onclick="event.stopPropagation();copyResultHandle(this.dataset.open)">Copy item ref</button>` : "";
  // Attachment results also offer a direct download of the file itself (in addition to opening the
  // parent email on click). Email results expand to the full body on click.
  const dlBtn = (isAttachment && r.downloadable && r.attachment_id)
   ? `<button class="act" onclick="event.stopPropagation();downloadAttachment('${esc(String(r.attachment_id))}',event)">Download attachment</button>`
    : (isAttachment && !r.downloadable
      ? `<span class="muted" style="font-size:11px">metadata only (re-sync mail to fetch the file)</span>` : "");
  // Click expands the email (its own, or the attachment's parent). Non-expandable results (files,
  // notes) just toggle their metadata.
  const click = expandable
   ? `toggleSearchDetail(${i},'email','${esc(emailRef)}')`
    : `toggleSearchDetail(${i},'${esc(kind)}','${esc(opened)}')`;
  const hint = expandable? (isAttachment? "Click to open the email this attachment came from." : "Click to expand full email.") : "";
  // 0004: a source-type badge per result + a data-src for client-side chip filtering. The badge class
  // maps the kind to a colour (file/mail/note/other). esc() still guards every value.
  const srcKey = isEmail||isAttachment? "mail" : (kind==="note"?"note":(kind==="file"?"file":(kind==="voicemail"?"voicemail":(kind==="notification"?"notification":"other"))));
  const badgeLabel = srcKey==="mail"?"email":(srcKey==="notification"?"notice":kind);
  const badge = `<span class="restype t-${srcKey}">${esc(badgeLabel)}</span>`;
  return `<div class="row result-row" data-src="${srcKey}" tabindex="0" onclick="${click}" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();${click}}">
    ${badge}<span class="k" style="flex:1;max-width:none"><b>${esc(primary)}</b> <span class="src">(${esc(kind)})</span>
      <div class="muted">${snippet||esc(secondary)||"(no preview text)"}</div>
      <div class="search-detail" id="sr-detail-${i}">${detailBits.join("") || '<div class="muted">No extra metadata for this result.</div>'}
        <div style="margin-top:8px">${copyBtn}${dlBtn}${hint?` <span class="muted">${hint}</span>`:""}</div>
      </div>
    </span></div>`;
}
if(searchBox){ searchBox.addEventListener("keydown", async e=>{
  if(e.key!=="Enter"||!searchBox.value.trim())return;
  const term=searchBox.value.trim();
  const res=document.getElementById("results"); res.textContent="searching your files, email and notes...";
  const _t0 = (window.performance && performance.now)? performance.now() : Date.now();
  try{
    const data=await j("/find?q="+encodeURIComponent(term),{},60000);
    const items=data.results||[];
    const sources = data.sources || data.searched || null;
    const covHtml = sources? `<div class="muted" style="font-size:11px;opacity:.7;margin-bottom:6px">searched: ${esc(Array.isArray(sources)?sources.join(", "):String(sources))}</div>` : "";
    if(!items.length){
      const ic = data.indexed_counts || null;
      const files = ic? (ic.files||0) : ((data.indexed_count!=null)?data.indexed_count:null);
      const emails = ic? (ic.email||0) : null;
      const attachments = ic? (ic.attachments||0) : null;
      const voicemails = ic? (ic.voicemail||0) : null;
      const notifications = ic? (ic.notifications||0) : null;
      let why = `<div class="muted">No matches for "<b>${esc(term)}</b>".</div>`;
      if(data.empty_index){
        why += `<div class="muted" style="margin-top:6px">Nothing is indexed yet, so there is nothing to search. Connect a source in Setup, then index it:</div>
                <div style="margin-top:8px"><button class="act go" onclick="reindexNow(this)">Index my files now</button>
                <button class="act" onclick="syncMailNow(this)" style="margin-left:8px">Sync mail now</button>
                <span id="searchIdxMsg" class="muted" style="font-size:11px;margin-left:8px"></span></div>`;
      } else {
        const have = [];
        if(files) have.push(files+" file"+(files==1?"":"s"));
        if(emails) have.push(emails+" email"+(emails==1?"":"s"));
        if(attachments) have.push(attachments+" attachment"+(attachments==1?"":"s"));
        if(voicemails) have.push(voicemails+" voicemail"+(voicemails==1?"":"s"));
        if(notifications) have.push(notifications+" notification"+(notifications==1?"":"s"));
        const haveTxt = have.length? ("Indexed now: "+have.join(", ")+".") : "";
        why += `<div class="muted" style="margin-top:6px">${haveTxt} No match in your indexed files, synced email, voicemails or notifications. Try fewer or different words. `;
        if(!files){ why += `No files are indexed yet. `; }
        if(!emails){ why += `No email is indexed yet, set up IMAP in Setup and sync mail. `; }
        why += `</div>`;
        if(!emails){ why += `<div style="margin-top:8px"><button class="act" onclick="syncMailNow(this)">Sync mail now</button>
                <span id="searchIdxMsg" class="muted" style="font-size:11px;margin-left:8px"></span></div>`; }
      }
      res.innerHTML = covHtml + why;
      const foot=document.getElementById("searchFoot"); if(foot) foot.style.display="none";
      return;
    }
    res.innerHTML = covHtml + items.map(renderSearchResult).join("");
    // 0004: show the keyboard/timing footer, with a real "N results · Mms" line; apply the active chip.
    const ms = Math.max(0, Math.round(((window.performance && performance.now)? performance.now() : Date.now()) - _t0));
    const timing=document.getElementById("searchTiming");
    if(timing) timing.textContent = `${items.length} result${items.length==1?"":"s"} · ${ms}ms`;
    const foot=document.getElementById("searchFoot"); if(foot) foot.style.display="flex";
    applySearchFilter();
    selectSearchResult(0);
  }catch(e){ res.textContent="search failed: "+(e && e.name === "AbortError"? "timed out" : e); }
}); }

// 0004 helpers: client-side source filtering, keyboard navigation, and the ⌘K global open.
var _searchFilter = "all";
function setSearchFilter(chip){
  _searchFilter = chip && chip.dataset? (chip.dataset.filter||"all") : "all";
  var chips=document.querySelectorAll("#searchFilters .chip");
  for(var i=0;i<chips.length;i++){ chips[i].classList.toggle("on", chips[i]===chip); }
  if(_searchFilter==="notification" && searchBox && !searchBox.value.trim()){
    browseRecentNotifications();
    return;
  }
  applySearchFilter();
}
async function browseRecentNotifications(){
  // Search alone required already knowing a matching word; without it, notifications were
  // completely unbrowsable even when Overview could correctly report one exists. This lets the
  // Notifications chip actually show something when clicked with nothing typed.
  const res=document.getElementById("results"); if(!res) return;
  res.textContent="loading recent notifications...";
  try{
    const d=await j("/notifications/recent?hours=720&limit=50",{},20000);
    const rows=d.notifications||[];
    const foot=document.getElementById("searchFoot");
    if(!rows.length){ res.innerHTML='<div class="muted">No notifications in the last 30 days.</div>'; if(foot) foot.style.display="none"; return; }
    const items=rows.map(function(n){ return {kind:"notification", title:n.title||"(no title)",
      where:(n.app||n.source||""), received_at:n.received_at}; });
    res.innerHTML = items.map(renderSearchResult).join("");
    if(foot) foot.style.display="flex";
  }catch(e){ res.innerHTML='<div class="muted">Could not load notifications. <button class="act" onclick="browseRecentNotifications()">Retry</button></div>'; }
}
function applySearchFilter(){
  var rows=document.querySelectorAll("#results .result-row");
  var shown=0;
  for(var i=0;i<rows.length;i++){
    var s=rows[i].getAttribute("data-src")||"other";
    var match = (_searchFilter==="all") || (s===_searchFilter);
    rows[i].style.display = match? "" : "none";
    if(match) shown++;
  }
  return shown;
}
function _visibleResultRows(){
  var out=[]; var rows=document.querySelectorAll("#results .result-row");
  for(var i=0;i<rows.length;i++){ if(rows[i].style.display!=="none") out.push(rows[i]); }
  return out;
}
function selectSearchResult(idx){
  var rows=_visibleResultRows(); if(!rows.length) return;
  if(idx<0) idx=0; if(idx>=rows.length) idx=rows.length-1;
  for(var i=0;i<rows.length;i++){ rows[i].classList.toggle("sel", i===idx); }
  try{ rows[idx].scrollIntoView({block:"nearest"}); }catch(e){}
  window.__searchSel = idx;
}
if(searchBox){ searchBox.addEventListener("keydown", function(e){
  if(e.key!=="ArrowDown" && e.key!=="ArrowUp" && !(e.key==="Enter")) return;
  var rows=_visibleResultRows(); if(!rows.length) return;
  var cur = (typeof window.__searchSel==="number")? window.__searchSel : 0;
  if(e.key==="ArrowDown"){ e.preventDefault(); selectSearchResult(cur+1); }
  else if(e.key==="ArrowUp"){ e.preventDefault(); selectSearchResult(cur-1); }
  else if(e.key==="Enter" && searchBox.value.trim()==="" ){ /* let empty Enter pass */ }
}); }
// ⌘K / Ctrl-K opens search from any tab and focuses the input.
document.addEventListener("keydown", function(e){
  if((e.metaKey||e.ctrlKey) && String(e.key).toLowerCase()==="k"){
    e.preventDefault();
    try{ activateTab("search"); }catch(_e){}
    var q=document.getElementById("q"); if(q){ q.focus(); q.select && q.select(); }
  }
});

// Voice chat: record in the browser, relay to the server for Whisper + owner reply.
let mediaRec=null, chunks=[];
let chatReady=false;
// Files & staging (System tab): upload large files (up to 200MB) and list what's staged.
async function uploadStaged(btn){
  const f=document.getElementById('stagefile'); const out=document.getElementById('stageout');
  if(!f||!f.files||!f.files[0]){ out.innerHTML='<span style="color:var(--warn)">Choose a file first.</span>'; return; }
  const file=f.files[0]; const sizeMB=(file.size/(1024*1024));
  if(sizeMB>200){ out.innerHTML='<span style="color:var(--warn)">'+esc(file.name)+' is '+sizeMB.toFixed(0)+'MB: the staging limit is 200MB.</span>'; return; }
  btn.disabled=true; out.innerHTML='Uploading '+esc(file.name)+' ('+sizeMB.toFixed(1)+'MB)…';
  try{
    const n=await ensureNonce();
    const r=await fetch('/files/stage',{method:'POST',headers:{'X-Cockpit-Nonce':n||'','X-Filename':file.name,'Content-Type':file.type||'application/octet-stream'},body:file});
    const d=await r.json();
    if(d.ok){ out.innerHTML='<span style="color:var(--ok)">✓ Staged '+esc(d.filename||file.name)+'. '+esc(d.next||'')+'</span>'; refreshStagedList(); }
    else { out.innerHTML='<span style="color:var(--warn)">'+esc(d.error||'upload failed')+'</span>'; }
  }catch(e){ out.innerHTML='<span style="color:var(--warn)">Upload failed: '+esc(e.message||e)+'</span>'; }
  finally{ btn.disabled=false; }
}
async function refreshStagedList(){
  const box=document.getElementById('stagelist'); if(!box) return;
  try{
    const n=await ensureNonce();
    const r=await fetch('/files/staged',{headers:{'X-Cockpit-Nonce':n||''}});
    const d=await r.json(); const st=(d&&d.staged)||[];
    if(!st.length){ box.innerHTML='<span class="muted">Nothing staged yet.</span>'; return; }
    box.innerHTML='<div class="muted" style="margin-bottom:6px">Staged files</div>'+st.map(function(a){
      var avail = a.available_for_self_update?'<span style="color:var(--gold)">build · applyable</span>':(a.available_for_rtm?'RTM':'reference');
      return '<div class="row"><span class="v" style="text-align:left;flex:1">'+esc(a.filename||'file')+' <span class="muted">('+(a.size_mb||0)+'MB · '+esc(avail)+')</span></span></div>';
    }).join('');
  }catch(e){ box.innerHTML='<span class="muted">could not load staged files</span>'; }
}
// Attach a file to the browser chat (<= 30MB). Imports body-data CSVs, stores anything else as a
// referenceable asset, and writes the outcome into the chat screen. Files over 30MB are bounced to the
// large-file staging area (System tab).
async function attachChatFile(input){
  const file=input&&input.files&&input.files[0]; if(!file) return;
  input.value="";
  const screen=document.getElementById("ircScreen");
  const writeLine=(html)=>{ if(!screen) return; const el=document.createElement("div"); el.className="irc-line"; el.innerHTML=html; screen.appendChild(el); screen.scrollTop=screen.scrollHeight; };
  const sizeMB=(file.size/(1024*1024));
  if(sizeMB>30){ writeLine(`<span class="irc-sys">* ${esc(file.name)} is ${sizeMB.toFixed(0)}MB: too big for a chat attachment (30MB max). Use <a href="#" onclick="show('system');return false" style="color:var(--gold)">large-file staging</a> (up to 200MB).</span>`); return; }
  writeLine(`<span class="irc-ts">${(new Date()).toTimeString().slice(0,8)}</span> <span class="irc-nick-you">&lt;you&gt;</span> 📎 ${esc(file.name)} (${sizeMB.toFixed(1)}MB)`);
  writeLine(`<span class="irc-sys">* uploading ${esc(file.name)}…</span>`);
  try{
    const n=await ensureNonce();
    const r=await fetch("/cockpit/chat/attach",{method:"POST",headers:{"X-Cockpit-Nonce":n||"","X-Filename":file.name,"Content-Type":file.type||"application/octet-stream"},body:file});
    const d=await r.json();
    if(!d.ok){ writeLine(`<span class="irc-sys">* ${esc(d.error||"upload failed")}</span>`); return; }
    if(d.lane==="body_data"){ writeLine(`<span class="irc-ts">${(new Date()).toTimeString().slice(0,8)}</span> <span class="irc-nick-bot">&lt;mongoose&gt;</span> Imported your ${esc(d.detected||"data")}${d.imported!=null?(" ("+d.imported+" rows)"):""}. Thanks: that makes the body features real.`); }
    else { writeLine(`<span class="irc-ts">${(new Date()).toTimeString().slice(0,8)}</span> <span class="irc-nick-bot">&lt;mongoose&gt;</span> ${esc(d.summary||("Saved "+(d.filename||file.name)+"."))} You can ask me about it.`); }
  }catch(e){ writeLine(`<span class="irc-sys">* upload failed: ${esc(e.message||e)}</span>`); }
}
function chatSetup(){
  const root=document.getElementById("chat");
  if(chatReady){ const f=document.getElementById("ircField"); if(f) f.focus(); return; }
  chatReady=true;
  root.innerHTML=`
    <div class="irc-win">
      <div class="irc-title"><img src="/static/img/mongoose-32.png?v=2" alt="" width="16" height="16" style="vertical-align:-3px;margin-right:6px">#mongoose
        <span style="opacity:.7;font-weight:400">, you &amp; the local brain</span>
        <span class="dots"><b class="mn"></b><b class="mx"></b><b class="cl"></b></span>
      </div>
      <div class="irc-screen" id="ircScreen" role="log" aria-live="polite" aria-relevant="additions text" aria-label="Conversation with Mongoose"></div>
      <div class="irc-input">
        <button type="button" id="ircAttach" title="Attach a file (up to 30MB)" aria-label="Attach a file" onclick="document.getElementById('ircFile').click()" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:15px;padding:0 6px 0 0">📎</button>
        <input type="file" id="ircFile" style="display:none" onchange="attachChatFile(this)">
        <span class="irc-prompt">you&gt;</span>
        <span class="irc-cursorwrap">
          <input class="irc-field" id="ircField" autocomplete="off" spellcheck="false" maxlength="8000" placeholder="type a message and hit enter..." aria-label="Type a message to Mongoose">
          <span class="irc-mirror" id="ircMirror"></span><span class="irc-block" id="ircBlock" style="position:absolute;top:50%;transform:translateY(-50%)"></span>
        </span>
      </div>
      <div class="irc-hint">
        <span class="irc-emoji-bar" id="ircEmoji"></span>
        <span>enter to send &middot; 📎 up to 30MB &middot; <a href="#" onclick="show('system');return false" style="color:var(--dim)">large files</a> &middot; runs on the local brain</span>
      </div>
    </div>`;
  const screen=document.getElementById("ircScreen");
  const field=document.getElementById("ircField");
  // Position the blinking lime block right after the typed text (the mirror measures text width).
  const mirror=document.getElementById("ircMirror"), block=document.getElementById("ircBlock");
  function placeCursor(){
    if(!mirror||!block||!field) return;
    mirror.textContent = field.value || "";
    const w = Math.min(mirror.offsetWidth, field.offsetWidth - 10);
    block.style.left = w + "px";
    // hide the block while the field is unfocused so it reads as inactive
    block.style.display = (document.activeElement===field)? "inline-block" : "none";
  }
  field.addEventListener("input", placeCursor);
  field.addEventListener("keyup", placeCursor);
  field.addEventListener("click", placeCursor);
  field.addEventListener("focus", ()=>{ block.style.display="inline-block"; placeCursor(); });
  field.addEventListener("blur", ()=>{ block.style.display="none"; });
  setTimeout(placeCursor, 50);

  // emoji quick-bar (90s-flavoured set, expanded)
  const EMO=["🙂","😄","😎","🤖","👍","🔥","💾","✨","🎮","☕","💚","🫡","😂","🤔","🙌","🎉","😅","🫠","👀","💡","🐭","🌙","⚡","❤️"];
  const bar=document.getElementById("ircEmoji");
  EMO.forEach(e=>{ const b=document.createElement("button"); b.textContent=e; b.type="button";
    b.onclick=()=>{ field.value+=e; field.focus(); placeCursor(); }; bar.appendChild(b); });

  function ts(){ const d=new Date();
    return "["+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")+"]"; }
  function line(html){ const el=document.createElement("div"); el.className="irc-line"; el.innerHTML=html;
    screen.appendChild(el); screen.scrollTop=screen.scrollHeight; return el; }
  function sys(t){ line(`<span class="irc-sys">* ${esc(t)}</span>`); }
  function mem(events){ // small grey italic notes for memory changes (remembered / forgotten)
    const list = Array.isArray(events)? events : [events];
    for(const e of list){ if(!e) continue;
      if(typeof e === "string"){ line(`<span class="irc-mem">* remembered: ${esc(e)}</span>`); continue; }
      if(e.type === "remembered" && e.fact){
        line(`<span class="irc-mem">* remembered: ${esc(e.fact.replace(/^The user /,""))}</span>`);
      } else if(e.type === "forgotten" && e.fact){
        line(`<span class="irc-mem">* forgotten: ${esc(e.fact.replace(/^The user /,""))}</span>`);
      } else if(e.type === "updated" && e.fact){
        line(`<span class="irc-mem">* updated: ${esc(e.fact.replace(/^The user /,""))}${e.superseded?` (was: ${esc(e.superseded.replace(/^The user /,""))})`:""}</span>`);
      }
      // forget_needs_confirmation / memory_update_failed are conveyed in the reply text itself
    }
  }
  function you(t){ line(`<span class="irc-ts">${ts()}</span> <span class="irc-nick-you">&lt;you&gt;</span> ${esc(t)}`); }
  function bot(t){ line(`<span class="irc-ts">${ts()}</span> <span class="irc-nick-bot">&lt;mongoose&gt;</span> ${esc(t)}`); }

  // the input uses a native blinking block caret (caret-color), so it sits exactly at the text
  // cursor and moves as you type/edit, no manual cursor tracking needed.

  // opening "join" banter, in the IRC idiom
  line(`<span class="irc-join">*** Now talking in #mongoose</span>`);
  line(`<span class="irc-join">*** mongoose (~local@127.0.0.1) has joined</span>`);
  sys("local brain online. say hi, ask anything, or try \"tell me a joke\". 💚");

  // Load recent history so this reads as ONE continuous conversation (voice + text share it).
  async function __loadChatHistory(){ try{
    const n=await ensureNonce();
    const r=await fetch("/cockpit/chat/history",{headers:{"X-Cockpit-Nonce":n||""}});
    if(!r.ok){
      // A failed fetch must NEVER look identical to "you have no history": that is exactly
      // what makes a transient glitch (a stale nonce right after navigating from another page,
      // a network hiccup) read as if the conversation was actually deleted. It was not; the
      // real data lives in the database untouched. Say so, visibly, with a way to try again.
      sys("Couldn't load your earlier messages right now (this is a display problem, not data "
          + "loss, your conversation is safe). ");
      const retryEl=line('<button class="act" onclick="__loadChatHistory()" style="font-size:11px;padding:3px 8px">Retry loading history</button>');
      return;
    }
    const d=await r.json();
    const msgs=(d&&d.messages)||[];
    if(msgs.length){
      sys("(earlier in this conversation)");
      for(const m of msgs){ if(m.role==="user") you(m.content||""); else if(m.role==="assistant") bot(m.content||""); }
      sys("(now)");
    }
  }catch(e){
    sys("Couldn't load your earlier messages right now (this is a display problem, not data "
        + "loss, your conversation is safe). ");
    line('<button class="act" onclick="__loadChatHistory()" style="font-size:11px;padding:3px 8px">Retry loading history</button>');
  } }
  __loadChatHistory();

  // The surpriser, IN the chat. Previously it only lived as a button on Overview, so the chat window
  // "never showed surpriser updates". Here Mongoose proactively surfaces a useful notice as a chat
  // line, once shortly after you open chat, then on a slow interval, but only when there's
  // something genuinely worth saying (the endpoint returns surfaced=false otherwise), and never the
  // same notice twice.
  const __seenSurprise = new Set();
  async function pollSurprise(){
    try{
      const n=await ensureNonce();
      const r=await fetch("/cockpit/suggest",{headers:{"X-Cockpit-Nonce":n||""}});
      if(!r.ok) return;
      const d=await r.json();
      if(d && d.surfaced && d.suggestion){
        const s=d.suggestion;
        const sig=s.signature||s.text||"";
        if(sig && !__seenSurprise.has(sig)){
          __seenSurprise.add(sig);
          line(`<span class="irc-ts">${ts()}</span> <span class="irc-nick-bot">&lt;mongoose&gt;</span> <span class="irc-notice">${esc(s.text)}</span>`
               + (s.why?` <span class="irc-mem">(${esc(s.why)})</span>`:""));
        }
      }
    }catch(e){ /* best-effort */ }
  }
  setTimeout(pollSurprise, 8000);          // a little after you arrive
  setInterval(pollSurprise, 180000);       // then every few minutes, quietly

  let busy=false;
  async function send(){
    const t=field.value.trim(); if(!t||busy) return;
    // Poke command intercept: "poke <name>" fires before hitting the LLM
    const pokeTarget = interceptPokeCommand(t);
    if (pokeTarget) {
      you(t); field.value="";
      try {
        const n = await ensureNonce();
        const r = await fetch("/poke/"+encodeURIComponent(pokeTarget), {method:"POST",
          headers:{"X-Cockpit-Nonce":n||""}});
        const d = await r.json();
        if (d.ok) bot("👆🪿 poked " + esc(d.to||pokeTarget) + "! they'll get a notification.");
        else if (d.available) bot("no user named \"" + esc(pokeTarget) + "\". Users: " + d.available.join(", "));
        else bot(d.error || "could not poke");
      } catch(e) { bot("poke failed: " + e); }
      return;
    }
    // Song exchange intercepts: "recommend X for Y" and "feedback sx_xxx: ..."
    const songCmd = interceptSongCommand(t);
    if (songCmd) {
      you(t); field.value="";
      await handleSongCommand(songCmd, you, bot, sys);
      return;
    }
    you(t); field.value=""; busy=true;
    const typing=line(`<span class="irc-ts">${ts()}</span> <span class="irc-nick-bot">&lt;mongoose&gt;</span> <span class="irc-typing">typing<i>.</i><i>.</i><i>.</i></span>`);
    try{
      const n=await ensureNonce();
      // Guard against an infinite "typing..." if the local model stalls or the body never completes:
      // abort after 120s and tell the user, rather than spinning forever.
      const ctrl=new AbortController();
      const to=setTimeout(()=>ctrl.abort(), 120000);
      let __ep=""; try{ if(__cockpitSession) __ep=__cockpitSession.boot_id+":"+__cockpitSession.epoch; }catch(_e){}
      const r=await fetch("/cockpit/chat",{method:"POST",
        headers:{"X-Cockpit-Nonce":n||"","Content-Type":"application/json","X-Cockpit-Epoch":__ep},
        body:JSON.stringify({text:t}), signal:ctrl.signal});
      clearTimeout(to);
      if(r.status===409){
        typing.remove();
        window.__cockpitStale=true;
        sys("This tab was superseded by a newer Mongoose tab (or a restart) so this message was NOT sent, to protect the conversation. Nothing was lost. Reload the page and send it again.");
        return;
      }
      const d=await r.json();
      typing.remove();
      if(!d.ok){ sys(d.error||"something went wrong"); }
      else { bot(d.reply||"(no reply)"); if(d.memory_events && d.memory_events.length) mem(d.memory_events); else if(d.remembered && d.remembered.length) mem(d.remembered); }
    }catch(e){ typing.remove();
      if(e&&e.name==="AbortError"){ sys("the local model took too long to answer (over 2 minutes). It may be loading or busy, try again, or check Ollama on the Mac."); }
      else { sys("connection failed: "+(e&&e.name?e.name:e)); }
    }
    busy=false; field.focus();
  }
  field.addEventListener("keydown", e=>{ if(e.key==="Enter"){ e.preventDefault(); send(); } });
  field.focus();
}

async function voiceSetup(){
  const rec=document.getElementById("rec"); if(!rec||rec._wired)return; rec._wired=true;
  const out=document.getElementById("vout");
  // Browsers only expose the mic in a secure context (https or localhost). Warn
  // upfront on a plain-http LAN page so it's not a mystery when recording fails.
  const warn=document.getElementById("voicewarn");
  if(warn && !window.isSecureContext){
    warn.innerHTML="<b>Microphone needs a secure connection.</b> Browsers only allow mic access over "
      +"https or on the Mac itself (localhost). On this device over plain http the mic is blocked, "
      +"that's a browser rule, not a Mongoose setting. To use voice: open the cockpit on the Mac "
      +"(<span style=\"font-family:monospace\">http://127.0.0.1:8765</span>), or put Mongoose behind "
      +"https. Typed search and everything else still work here.";
    // Don't offer a button that can only fail, disable it and say why on the button itself.
    const b=document.getElementById("rec");
    if(b){ b.disabled=true; b.style.opacity="0.5"; b.style.cursor="not-allowed";
           b.textContent="Voice needs https or the Mac"; }
    return;  // skip wiring the recorder; it cannot work in this context
  }
  const start=async()=>{
    // If the browser doesn't expose mediaDevices at all (insecure context, or an unusual browser),
    // getUserMedia would throw before any permission prompt, which is exactly "I pressed talk and
    // got an error but was never asked for the mic". Detect that up front and explain it.
    if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
      out.innerHTML=`<div class="muted">This browser won't allow microphone access here. That usually means the page is on plain http (not https or the Mac itself). Open the cockpit on the Mac at <span style="font-family:monospace">http://127.0.0.1:8765</span>, or set up https, then the browser will ask for mic permission.</div>`;
      return;
    }
    try{
      // Plain {audio:true}, no exotic constraints, so we don't trip OverconstrainedError on a
      // device that lacks a specific capability.
      // Prefer speech-friendly capture; fall back to plain {audio:true} so we never trip
      // OverconstrainedError on a device that lacks a constraint.
      let stream;
      try{
        stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,sampleRate:48000,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
      }catch(_e){
        stream=await navigator.mediaDevices.getUserMedia({audio:true});
      }
      const _vm=(window.MediaRecorder&&MediaRecorder.isTypeSupported)?['audio/webm;codecs=opus','audio/webm','audio/mp4','audio/aac','audio/ogg;codecs=opus','audio/ogg'].find(m=>MediaRecorder.isTypeSupported(m)):'';
      mediaRec=_vm?new MediaRecorder(stream,{mimeType:_vm,audioBitsPerSecond:128000}):new MediaRecorder(stream); chunks=[];
      const _vmime=((_vm||mediaRec.mimeType||'audio/webm')).split(';')[0];
      mediaRec.ondataavailable=e=>chunks.push(e.data);
      mediaRec.onstop=async()=>{
        const blob=new Blob(chunks,{type:_vmime});
        out.innerHTML=`<div class="muted">transcribing...</div>`;
        const n=await ensureNonce();
        let __vep=""; try{ if(__cockpitSession) __vep=__cockpitSession.boot_id+":"+__cockpitSession.epoch; }catch(_e){}
        try{
          const r=await fetch("/cockpit/voice",{method:"POST",
            headers:{"X-Cockpit-Nonce":n||"","Content-Type":"application/octet-stream","X-Audio-Mime":_vmime,"X-Cockpit-Epoch":__vep},body:blob});
          if(r.status===409){
            window.__cockpitStale=true;
            out.innerHTML=`<div class="muted">This tab was superseded by a newer Mongoose tab, so this voice turn was NOT sent, to protect the conversation. Nothing was lost. Reload the page and try again.</div>`;
            return;
          }
          const d=await r.json();
          if(!d.ok){ out.innerHTML=`<div class="muted">${esc(d.error||"voice failed")}</div>`; return; }
          out.innerHTML=`<div class="row"><span class="k">you said</span><span class="v">${esc(d.transcript||"")}</span></div>
                         <div class="row"><span class="k">Mongoose</span><span class="v" style="text-align:left;flex:1">${esc(d.reply||"")}</span></div>`;
          if(document.getElementById("speak").checked && d.reply && window.speechSynthesis){
            const u=new SpeechSynthesisUtterance(d.reply);
            const pv=typeof preferredVoice==='function'?preferredVoice():null;
            if(pv) u.voice=pv;
            if(typeof ttsRate==='function') u.rate=ttsRate();
            speechSynthesis.cancel(); speechSynthesis.speak(u);
          }
        }catch(e){ out.innerHTML=`<div class="muted">voice failed: ${esc(e)}</div>`; }
        stream.getTracks().forEach(t=>t.stop());
      };
      mediaRec.start(); rec.textContent="Recording... release to send"; rec.classList.add("no");
    }catch(e){
      const name = e && e.name? e.name : String(e);
      let msg;
      if(name==="NotAllowedError" || name==="SecurityError"){
        msg = "Microphone permission was denied. Allow mic access for this site in your browser (the address bar usually has a camera/mic icon), then try again.";
      } else if(name==="NotFoundError" || name==="OverconstrainedError"){
        msg = "No usable microphone was found. Check that a mic is connected and selected in System Settings, then try again.";
      } else if(name==="NotReadableError"){
        msg = "The microphone is in use by another app. Close anything else using the mic and try again.";
      } else if(!window.isSecureContext){
        const secureBase = "https://" + ((location.hostname==="127.0.0.1"||location.hostname==="::1") ? ("localhost"+(location.port?":"+location.port:"")) : location.host);
        msg = "The mic is blocked because this page is on plain http. Open this page over "+secureBase+", or use trusted HTTPS.";
      } else {
        msg = "Couldn't start the microphone ("+esc(name)+"). Check that a mic is connected and permitted.";
      }
      out.innerHTML=`<div class="muted">${msg}</div>`;
    } };
  const stop=()=>{ if(mediaRec&&mediaRec.state!=="inactive"){ mediaRec.stop(); }
    rec.textContent="Hold to talk"; rec.classList.remove("no"); };
  rec.onmousedown=start; rec.onmouseup=stop; rec.onmouseleave=()=>{ if(mediaRec&&mediaRec.state==="recording")stop(); };
  rec.ontouchstart=e=>{e.preventDefault();start();}; rec.ontouchend=e=>{e.preventDefault();stop();};
}

let _curLog=null;
async function loadLogs(){
  const list=document.getElementById("loglist");
  try{
    const data=await j("/cockpit/logs");
    const logs=data.logs||[];
    if(!logs.length){ list.innerHTML=`<div class="muted">No log files yet. They appear once the scheduler runs (mongoose service install).</div>`; return; }
    list.innerHTML=logs.map(l=>{
      const kb=Math.round((l.bytes||0)/1024);
      const active=(_curLog===l.name)?"go":"";
      return `<button class="act ${active}" onclick="viewLog('${esc(l.name)}')">${esc(l.name)} <span class="muted">(${kb}KB)</span></button>`;
    }).join(" ");
    if(_curLog) viewLog(_curLog);
  }catch(e){ list.innerHTML=`<div class="muted">Logs unavailable (${esc(e)}).</div>`; }
}
async function viewLog(name){
  _curLog=name;
  const view=document.getElementById("logview"); view.textContent="loading...";
  try{
    const d=await j("/cockpit/logs/"+encodeURIComponent(name));
    // textContent (not innerHTML): log lines are attacker-influenceable, never render as HTML
    const head=d.truncated?"(showing last 64KB)\n\n":"";
    view.textContent=head+(d.text||"(empty)");
    view.scrollTop=view.scrollHeight;
    // re-render the list so the active button highlights
    const list=document.getElementById("loglist");
    [...list.querySelectorAll("button")].forEach(b=>{
      b.classList.toggle("go", b.textContent.trim().startsWith(name));
    });
  }catch(e){ view.textContent="could not read log: "+e; }
}

// ── Voicemail tab: detected voicemails with transcripts + audio ───────────────
async function loadVoicemail(targetId){
  const el=document.getElementById(targetId||"voicemail"); if(!el) return;
  el.innerHTML='<div class="card"><h2>Voicemail</h2><div class="muted">Loading voicemails…</div></div>';
  let data;
  try{ data=await j("/voicemails?limit=30"); }
  catch(e){ el.innerHTML=retryCard("Voicemail", e, "loadVoicemail"); return; }
  const vms=data.voicemails||[]; const stats=data.stats||{};
  const total=stats.total!=null?stats.total:vms.length;
  const transcribed=stats.transcribed!=null?stats.transcribed:vms.filter(v=>v.transcript).length;
  let head=`<div class="card"><h2>📞 Voicemail</h2>
    <div class="muted" style="margin-bottom:10px">${total} voicemail${total==1?"":"s"} detected${transcribed?`, ${transcribed} transcribed`:""}. These are phone messages Mongoose spotted in your email (e.g. from your phone provider).</div>
    <div style="display:flex;gap:8px;margin-bottom:6px">
      <input id="vmSearch" aria-label="Search voicemails by caller, transcript, or filename" placeholder="Search caller, transcript, filename…" style="flex:1;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px"/>
      <button class="act" onclick="vmDoSearch()">Search</button>
    </div></div>`;
  if(!vms.length){
    head+=`<div class="card"><div class="muted">No voicemails yet. When your provider emails you a voicemail notification, it'll appear here with its transcript and audio.</div></div>`;
    el.innerHTML=head; return;
  }
  head+=`<div id="vmList">`+vms.map(renderVoicemail).join("")+`</div>`;
  el.innerHTML=head;
  const sb=document.getElementById("vmSearch");
  if(sb) sb.addEventListener("keydown",e=>{ if(e.key==="Enter") vmDoSearch(); });
}
function renderVoicemail(v){
  const caller=esc(v.caller_number||v.subject||"Unknown caller");
  const when=esc(v.voicemail_at||v.email_date||"");
  const status=String(v.transcript_status||"").toLowerCase();
  const hasTx=!!v.transcript;
  const txBadge = hasTx? `<span style="color:var(--ok);font-size:11px">● transcribed</span>`
    : (status.includes("pending")? `<span class="muted" style="font-size:11px">○ transcript pending</span>`
       : `<span class="muted" style="font-size:11px">○ no transcript</span>`);
  const body = hasTx? `<pre class="email-body" style="margin-top:8px">${esc(v.transcript)}</pre>`
    : `<div class="muted" style="font-size:12px;margin-top:6px">Transcript not available yet${status.includes("no_audio")?" (no audio attached)":", voice transcription may not be set up"}.</div>`;
  const audio = v.audio_filename? `<div class="result-meta" style="margin-top:6px">🔊 ${esc(v.audio_filename)}${v.audio_attachment_id?` <button class="act" onclick="downloadAttachment('${esc(String(v.audio_attachment_id))}',event)">download audio</button>`:""}</div>` : "";
  return `<div class="card" style="border-left:3px solid var(--gold)">
    <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">
      <b>${caller}</b> ${txBadge}
    </div>
    <div class="muted" style="font-size:11px">${when}</div>
    ${body}${audio}
  </div>`;
}
async function vmDoSearch(){
  const q=(document.getElementById("vmSearch")||{}).value||"";
  const list=document.getElementById("vmList"); if(!list) return;
  if(!q.trim()){ loadVoicemail(); return; }
  list.innerHTML='<div class="muted">Searching…</div>';
  try{
    const d=await j("/voicemails/search?q="+encodeURIComponent(q.trim()));
    const vms=d.voicemails||[];
    list.innerHTML = vms.length? vms.map(renderVoicemail).join("") : '<div class="muted">No voicemails match that search.</div>';
  }catch(e){ list.innerHTML='<div class="muted">Search failed.</div>'; }
}

// ── WhatsApp tab: the connect → scan → test → monitor journey ─────────────────
let _waPollTimer=null;
let _waSig="";  // last-rendered WhatsApp state signature, so the 3s poll only rebuilds on a real change
async function startWhatsAppContainer(){
  const st=document.getElementById('wa-start-status');
  if(st) st.textContent='Starting WhatsApp...';
  try{
    const r=await api('/cockpit/whatsapp/start-container',{method:'POST'});
    if(st) st.textContent=r&&r.ok ? (r.detail||'Starting...') : ((r&&(r.detail||r.error))||'Could not start it.');
    setTimeout(loadWhatsApp, 4000);
  }catch(e){ if(st) st.textContent='Could not start it: '+e; }
}
async function loadWhatsApp(){
  const el=document.getElementById("whatsapp"); if(!el) return;
  if(_waPollTimer){ clearInterval(_waPollTimer); _waPollTimer=null; }
  el.innerHTML='<div class="card"><h2>WhatsApp</h2><div class="muted">Checking connection…</div></div>';
  await renderWhatsApp();
  // Poll while the tab is open so the QR/connected state updates live.
  _waPollTimer=setInterval(()=>{ if(document.getElementById("whatsapp").classList.contains("active")) renderWhatsApp(); else { clearInterval(_waPollTimer); _waPollTimer=null; } }, 3000);
}
async function renderWhatsApp(){
  // FIELD BUG: this used to read a single top-level st.state/st.detail, but
  // /cockpit/whatsapp/state has always returned {ok, roles:[...]}, two SEPARATE accounts
  // (owner_read + mongoose_send), never a single flat state. st.state was therefore always
  // undefined, always fell into the "error" branch, and st.detail was also always undefined,
  // so the tab always rendered the literal word "error" with nothing else, on every load, for
  // everyone, unconditionally, regardless of whether WhatsApp was actually connected or not.
  const el=document.getElementById("whatsapp"); if(!el) return;
  let st;
  try{ st=await j("/cockpit/whatsapp/state"); }
  catch(e){ el.innerHTML=retryCard("WhatsApp", e, "loadWhatsApp"); return; }
  const roles=(st&&st.roles)||[];
  // Change-detect so the 3s poll stops rebuilding (and flickering) the whole pane every tick. Always
  // rebuild while a QR is live (scan_qr/connecting), since WAHA QRs expire and must refresh; otherwise
  // skip the rebuild when the state signature is unchanged and the pane is already painted.
  const _sig=JSON.stringify([roles.map(r=>[(r.role||((r.account||{}).role)||""),String(r.state||"")]),
    (st&&st.can_start_container)?1:0, roles.length?0:String((st&&st.detail)||"")]);
  const _scanning=roles.some(r=>{const s=String(r.state||"");return s==="scan_qr"||s==="connecting";});
  if(!_scanning && _sig===_waSig && el.querySelector(".card")){ return; }
  _waSig=_sig;
  if(!roles.length){
    const canStart=!!(st&&st.can_start_container);
    el.innerHTML=`<div class="card"><h2>WhatsApp</h2>
      <div class="muted" style="margin-bottom:10px">${esc((st&&st.detail)||"Not set up yet.")}</div>
      <div class="muted" style="font-size:12px;margin-bottom:12px">WhatsApp runs through a local WAHA container so messages stay on your machine. Once it's running, this tab will let you connect, scan a QR, test, and monitor.</div>
      ${canStart?'<button onclick="startWhatsAppContainer()" style="padding:10px 16px;border-radius:10px;border:0;background:var(--gold,#C9A35B);color:#16140F;font-weight:600;">Start WhatsApp</button><div id="wa-start-status" class="muted" style="font-size:12px;margin-top:8px"></div>':''}</div>`;
    return;
  }
  const allConnected = roles.every(r=>r.state==="connected");
  let head=`<div class="card"><h2>WhatsApp ${allConnected?'<span style="color:var(--ok);font-size:14px">\u25CF both linked</span>':''}</h2>
    <div class="muted" style="margin-bottom:12px">Two accounts: <b>your inbox</b> (read) and <b>Mongoose's own number</b> (notifications). Link each with its own QR.</div>`;
  for(const rs of roles){
    const role=rs.role||((rs.account||{}).role)||"owner_read";
    const acc=rs.account||{}; const label=acc.label||(role==="mongoose_send"?"Mongoose's number":"Your inbox");
    const state=String(rs.state||"disconnected");
    head+=`<div style="border-top:1px solid var(--line);padding-top:12px;margin-top:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <b>${esc(label)}</b>
        ${state==="connected"?'<span style="color:var(--ok);font-size:13px">\u25CF connected</span>'
          :(state==="scan_qr"||state==="connecting")?'<span class="muted" style="font-size:13px">linking\u2026</span>'
          :'<span class="bad" style="font-size:13px">not linked</span>'}
      </div>`;
    if(state==="not_configured"||state==="error"){
      head+=`<div class="muted">${esc(rs.detail||"Start the WhatsApp service first.")}</div>`;
    } else if(state==="disconnected"){
      head+=`<button class="act go" onclick="waConnect('${role}',this)">Connect this account</button>`;
    } else if(state==="scan_qr"||state==="connecting"){
      head+=`<div id="wa-qr-${role}"><div class="muted">Loading QR\u2026</div></div>
        <div class="muted" style="font-size:12px;margin-top:6px">Open WhatsApp on your phone \u2192 Settings \u2192 Linked devices \u2192 Link a device, then scan.</div>`;
    } else {
      head+=`<div class="muted">Linked and ready.</div>`;
      if(role==="mongoose_send"){
        head+=`<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
          <input id="waTestNum" placeholder="your number e.g. 447700900000" style="flex:1;min-width:180px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px"/>
          <button class="act" onclick="waTest(this)">Send test to myself</button></div>
          <div id="waTestOut" class="muted" style="font-size:12px;margin-top:6px"></div>`;
      }
    }
    head+=`</div>`;
  }
  head+=`</div><div class="card"><h2>Recent activity</h2><div id="waActivity" class="muted">Loading\u2026</div></div>`;
  el.innerHTML=head;
  for(const rs of roles){
    const role=rs.role||((rs.account||{}).role)||"owner_read";
    if(rs.state==="scan_qr"||rs.state==="connecting") waLoadQrDesktop(role);
  }
  try{
    const a=await j("/cockpit/whatsapp/activity");
    const box=document.getElementById("waActivity");
    const msgs=a.messages||[]; const drafts=a.pending_drafts||[];
    let h="";
    if(drafts.length){ h+=`<div style="margin-bottom:8px"><b>${drafts.length} pending draft(s)</b>, approve in the Approvals tab.</div>`; }
    if(!msgs.length){ h+='<div class="muted">No messages yet. When someone messages you, it shows here.</div>'; }
    else { h+=msgs.map(m=>`<div class="row"><span class="k">${esc(m.contact||m.sender_name||m.chat_id||"?")}</span><span class="v" style="text-align:left;flex:1">${esc((m.body||"").slice(0,120))}</span></div>`).join(""); }
    if(box) box.innerHTML=h;
  }catch(e){ const box=document.getElementById("waActivity"); if(box) box.innerHTML='<div class="muted">Could not load activity.</div>'; }
}
async function waLoadQrDesktop(role){
  const box=document.getElementById("wa-qr-"+role); if(!box) return;
  try{
    const q=await j("/cockpit/whatsapp/qr?role="+encodeURIComponent(role));
    if(q.ok && q.qr_base64){ box.innerHTML=`<img alt="WhatsApp QR" src="data:${esc(q.mime||'image/png')};base64,${esc(q.qr_base64)}" style="width:200px;height:200px;background:#fff;padding:8px;border-radius:10px"/>`; }
    else { box.innerHTML=`<div class="muted">${esc(q.error||"QR not ready yet, give it a moment.")}</div>`; }
  }catch(e){ box.innerHTML='<div class="muted">QR not ready yet.</div>'; }
}
async function waConnect(role,btn){
  if(btn){ btn.disabled=true; btn.textContent="Connecting\u2026"; }
  try{ await postAction("/cockpit/whatsapp/connect",{role:role}); }catch(e){}
  setTimeout(renderWhatsApp, 1200);
}
async function waTest(btn){
  const num=(document.getElementById("waTestNum")||{}).value||"";
  const out=document.getElementById("waTestOut");
  if(btn){ btn.disabled=true; btn.textContent="Sending\u2026"; }
  try{
    const r=await postAction("/cockpit/whatsapp/test",{to:num}); const d=await r.json();
    if(out) out.innerHTML = d.ok? '<span style="color:var(--ok)">Test sent, check WhatsApp on your phone.</span>' : `<span class="bad">${esc(d.error||"failed")}</span>`;
  }catch(e){ if(out) out.innerHTML=`<span class="bad">${esc(String(e))}</span>`; }
  if(btn){ btn.disabled=false; btn.textContent="Send test to myself"; }
}

function load(t){ try{ if(typeof markRefreshed==="function") markRefreshed(); }catch(e){}
  // Models, Benchmarks, Logs render inside System (loadSystem calls them); Voicemail renders
  // inside RTM (loadRTM calls it). So they're not in this top-level dispatch map anymore.
  const loaders={overview:loadOverview,setup:loadSetup,activity:loadActivity,
  approvals:loadApprovals,rtm:loadRTM,build:loadBuild,scheduler:loadScheduler,
  chat:chatSetup,voice:voiceSetup,search:()=>{},devices:loadDevices,system:loadSystem,whatsapp:loadWhatsApp};
  const fn=loaders[t];
  if(typeof fn!=="function"){ return; }
  // Guard each tab load: a throw inside one loader must not blank the tab or break the page. If it
  // fails, the tab shows a visible, retryable error instead of empty content.
  try{
    const r=fn();
    if(r && typeof r.catch==="function"){ r.catch(err=>{
      const el=document.getElementById(t);
      if(el && !el.innerHTML.trim()) el.innerHTML=`<div class="card"><h2>This section hit an error</h2><div class="muted" style="margin:8px 0">${esc(String(err||""))||"It failed to load."}</div><button class="act go" onclick="load('${t}')">Retry</button></div>`;
    }); }
  }catch(err){
    const el=document.getElementById(t);
    if(el && !el.innerHTML.trim()) el.innerHTML=`<div class="card"><h2>This section hit an error</h2><div class="muted" style="margin:8px 0">${esc(String(err||""))||"It failed to load."}</div><button class="act go" onclick="load('${t}')">Retry</button></div>`;
  }
}
// Keep the top-of-page "live" indicator honest regardless of which tab is showing. It reads the same
// doctor health Overview uses, so a user who lands on Setup still sees the true brain state instead of a
// stuck "checking..." or a false "core not ready".
async function refreshLive(){
  try{
    const doc=await j("/doctor");
    const el=document.getElementById("live");
    if(el) el.textContent = doc.ok!==false? "alive" : "needs attention";
    const pulse=document.getElementById("pulse"); if(pulse) pulse.classList.toggle("warn", doc.ok===false);
  }catch(e){ /* leave whatever the watchdog set */ }
}

// Front door: if core setup isn't done, land on the wizard (Setup tab) instead of
// Overview, so a first-run user is guided rather than dropped into a status page.
async function boot(){
  let landing="overview";
  // Explicit deep-link wins: ?tab=setup (or #setup) opens that tab directly. This is how
  // the /welcome "Connect information" button lands the user on Setup rather than dropping
  // them on Overview once core is ready. (QA-audit P0.3)
  const wantTab = (new URLSearchParams(location.search).get("tab")
                   || (location.hash || "").replace(/^#/, "")).toLowerCase();
  if(wantTab && TABS.includes(wantTab)){
    landing = wantTab;
  } else {
    try{
      const wiz=await j("/cockpit/wizard");
      if(!wiz.core_done) landing="setup";
    }catch(e){ /* if the check fails, default to overview */ }
  }
  // activate the landing tab + its nav button (works whether it's primary or under More)
  activateTab(landing);
  // Do NOT hard-write "core not ready" here. The live indicator is owned by the health refresh, which
  // reflects the ACTUAL brain state. A one-time write here got stuck reading "core not ready" forever
  // even while the user was chatting successfully, which was false.
  if(typeof refreshLive==="function"){ try{ refreshLive(); }catch(e){} }
}
// ===== Stale-tab guard (F5) =====
// A tab left open across an update/restart, or a second tab opened later, used to keep writing to
// the same conversation (two clients fighting). On load this tab claims the session (bumping a
// server epoch); it then watches the epoch + the server boot id. If a newer tab claims it, or the
// server restarted (new build), this tab is superseded: it stops auto-refresh and shows a reload
// banner, and writes are blocked (see requestAction/postAction).
let __cockpitSession=null;
(async function sessionGuard(){
  try{ __cockpitSession=await j("/cockpit/session/claim", {}, 8000); }catch(e){ return; }
  setInterval(async()=>{
    if(window.__cockpitStale) return;
    let cur; try{ cur=await j("/cockpit/session/current", {}, 8000); }catch(e){ return; }
    if(!cur || !__cockpitSession) return;
    if(cur.boot_id!==__cockpitSession.boot_id || (cur.epoch||0) > (__cockpitSession.epoch||0)){
      window.__cockpitStale=true;
      if(!document.getElementById("stale-banner")){
        const b=document.createElement("div"); b.id="stale-banner"; b.setAttribute("role","alert");
        b.style.cssText="position:fixed;top:0;left:0;right:0;z-index:9999;background:#3a2a1a;color:#f0c890;"
          +"font-size:14px;text-align:center;padding:10px;border-bottom:1px solid var(--gold-dim)";
        b.innerHTML='Mongoose restarted since this page loaded (or it is open in another tab too). Nothing was lost, your conversation is safe, this tab is just out of sync until you reload. '
          +'<a href="javascript:location.reload()" style="color:var(--gold);font-weight:600">Reload now</a>.';
        document.body.appendChild(b);
      }
    }
  }, 12000);
})();
// ===== Appearance (light/dark): pure token swap, persisted, OS-default-aware =====
// Modular & additive: sets data-theme on <html>; every CSS rule reads the vars, so no
// markup or logic changes. To remove the feature entirely, delete this block, the
// #themeToggle button, and the :root[data-theme="light"] vars in the <style>.
(function initTheme(){
  var KEY="mongoose.cockpit.theme";
  var root = document.documentElement;
  if(!root || !root.dataset){ return; }   // nothing to theme (defensive); never throw at load
  function apply(t){ root.dataset.theme = (t==="light"?"light":"dark");
    // keep the mobile status-bar colour in step with the active skin (designer follow-up)
    try{ var m=document.querySelector('meta[name="theme-color"]');
      if(m) m.setAttribute("content", t==="light"? "#FBF6EC" : "#1a1a1a"); }catch(e){}
  }
  var saved=null; try{ saved=localStorage.getItem(KEY); }catch(e){}
  if(saved==="light"||saved==="dark"){ apply(saved); }
  else if(window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches){ apply("light"); }
  else { apply("dark"); }
  var btn=document.getElementById("themeToggle");
  if(btn) btn.addEventListener("click", function(){
    var next = root.dataset.theme==="light"? "dark" : "light";
    apply(next); try{ localStorage.setItem(KEY, next); }catch(e){}
  });
})();
boot();

// Tabs that must never be auto-refreshed out from under the user (they hold typed input or
// read-and-act content). Defined here, BEFORE the watchdog and the refresh timer that both read it,
// so there's no temporal-dead-zone hazard if either ever runs early.
// RTM is excluded too: it's a heavy, read-and-act tab full of typed inputs (CSV path, allocate,
// record-payment, statement) with its own manual Refresh button. Auto-refreshing it wiped the view
// mid-read (the "flash"), and because it loads slowly it kept tripping the 75s "Reconnecting..."
// watchdog into a reload loop. Same reasoning as setup/devices.
const NO_AUTO_REFRESH = new Set(["search","voice","overview","chat","setup","devices","build","rtm"]);

// ===== UI liveness watchdog =====
// Two failure modes this guards against:
//  (1) the server's EVENT LOOP wedges (a blocking call on an async path): the async /health/live
//      stops answering even though the process is alive. We detect consecutive failures and show a
//      clear "the assistant is stuck" banner with a Restart action, instead of the UI silently
//      freezing with no explanation.
//  (2) this TAB's own refresh loop stalls (throttled in background, JS error, etc.): we track the
//      time of the last successful data refresh; if it goes stale we flag it and, on recovery, force
//      an immediate reload so the user isn't looking at frozen numbers.
// On recovery from either, the banner clears itself and a fresh load() runs.
let __lastGoodRefresh = Date.now();
function markRefreshed(){ __lastGoodRefresh = Date.now(); }
let __liveFails = 0, __wasStuck = false;
function showWatchdog(kind, msg, withRestart){
  const el=document.getElementById("watchdog"); if(!el) return;
  el.className = kind;  // "warn" | "stuck" | ""
  el.style.display = kind? "block" : "none";
  if(!kind){ el.innerHTML=""; return; }
  el.innerHTML = esc(msg) + (withRestart
   ? ` <button onclick="watchdogRestart(this)">Restart the assistant</button>`
    : ` <button onclick="watchdogReload()">Reload now</button>`);
}
function watchdogReload(){ location.reload(); }
async function waitForMongooseThenReload(statusElId){
  // Shared polling pattern (matching the install page's own waitForServerThenReload): poll
  // /health rather than a fixed-delay reload, since too-early lands on a browser error page
  // mid-restart. After the wait, hand over a same-origin retry link instead of stranding
  // the owner, and flag the local-certificate-changed case (a real edge case if the restart
  // regenerated a self-signed cert): only a real navigation, not fetch(), can show that
  // browser prompt.
  const el=()=>document.getElementById(statusElId);
  for(let i=0;i<25;i++){
    await new Promise(r=>setTimeout(r,3000));
    const w=el(); if(w) w.textContent="Waiting for Mongoose to come back... "+((i+1)*3)+"s";
    try{ const r=await j("/health",{cache:"no-store"},2500); if(r){ location.reload(); return; } }catch(_e){}
  }
  const w=el();
  if(w) w.innerHTML='Still waiting. Tap to retry: <a href="'+location.pathname+'" style="color:var(--gold)">reopen this page</a>.'
    +'<div style="margin-top:6px">If your browser shows a certificate warning when you tap, choose Show Details, then visit the site. That happens when the local certificate changed during the restart, and accepting it once fixes it.</div>';
  else location.reload();
}
async function watchdogRestart(btn){
  // Best-effort: ask the server to restart itself. If the loop is wedged this may not be accepted; in
  // that case the honest fallback is the manual command, which we show.
  btn.disabled=true; btn.textContent="restarting...";
  try{
    const r=await postAction("/cockpit/restart",{});
    if(r && r.ok){ setTimeout(()=>location.reload(), 6000); return; }
  }catch(e){}
  btn.textContent="couldn't restart automatically";
  const el=document.getElementById("watchdog");
  if(el) el.insertAdjacentHTML("beforeend",
    `<div style="font-size:11px;margin-top:6px;opacity:.85">Restart from a terminal in the Mongoose folder: <code>./Stop Mongoose.command</code> then <code>./Start Mongoose.command</code> (or <code>.venv/bin/mongoose stop</code> then <code>serve</code>).</div>`);
}
async function watchdogTick(){
  // Probe the event loop with a short timeout. If it doesn't answer in time, the loop is likely
  // wedged (or the server is down). Distinguish via the SYNC /health (threadpool-served).
  let loopOk=false, processOk=false;
  const ctrl=new AbortController(); const to=setTimeout(()=>ctrl.abort(), 4000);
  try{ const r=await fetch("/health/live",{signal:ctrl.signal}); loopOk=r.ok; }catch(e){ loopOk=false; }
  finally{ clearTimeout(to); }
  if(!loopOk){
    try{ const r2=await fetch("/health",{cache:"no-store"}); processOk=r2.ok; }catch(e){ processOk=false; }
  }
  if(loopOk){
    __liveFails=0;
    const staleMs = Date.now() - __lastGoodRefresh;
    const active=document.querySelector(".tab.active");
    const activeId = active? active.id : "";
    const selfRefreshing = !NO_AUTO_REFRESH.has(activeId);
    if(selfRefreshing && staleMs > 75000){
      showWatchdog("warn", "This view hasn't updated in a while. Reconnecting...", false);
      try{ if(active){ preserveScrollAcrossReload(()=>load(activeId)); markRefreshed(); } }catch(e){}
    } else if(__wasStuck){
      showWatchdog("", "", false);
      __wasStuck=false;
      try{ const a=document.querySelector(".tab.active"); if(a) preserveScrollAcrossReload(()=>load(a.id)); }catch(e){}
    } else {
      showWatchdog("", "", false);
    }
  } else {
    __liveFails++;
    if(__liveFails>=3){
      __wasStuck=true;
      if(processOk){
        showWatchdog("stuck", "The assistant is busy or stuck, its task loop isn't responding. The server is up but not processing right now.", true);
      } else {
        showWatchdog("stuck", "Can't reach Mongoose. The server may have stopped or be restarting.", false);
      }
    }
  }
}
setInterval(watchdogTick, 5000);
setTimeout(watchdogTick, 1500);
// Light background refresh for live/operational tabs only. Overview, Search and Voice are EXCLUDED:
// they hold read-and-act content (setup hints, the worked example, voice UI) that must not vanish
// from under the user while they're reading or interacting. Interval widened so refreshing tabs
// aren't twitchy.
// SETUP and DEVICES are excluded too: re-running their loader does el.innerHTML=... which destroys
// any OAuth JSON / IMAP / token text the user has typed but not yet saved. That was the
// "disappears in 30s and deletes everything I typed" bug. Belt-and-braces, we ALSO skip any refresh
// while the user is actively focused in an input/textarea, or has typed into a setup field that
// isn't saved yet, so no timed reload can ever clobber in-progress typing on any tab.
function userIsEditing(){
  const a=document.activeElement;
  if(a && (a.tagName==="INPUT" || a.tagName==="TEXTAREA" || a.isContentEditable)) return true;
  // any setup field that has a value the user entered (not yet saved), don't wipe it
  for(const f of document.querySelectorAll(".wiz-field, .q")){
    if(f.value && f.value.trim() && f.value!==f.defaultValue) return true;
  }
  return false;
}
// (NO_AUTO_REFRESH is defined above, before the watchdog.) Setup and Devices are excluded so
// re-running their loader can't wipe typed input; the userIsEditing() guard is the belt-and-braces.
// Every loader wholesale-replaces its tab's innerHTML, which resets scroll to the top on every
// refresh, pulling a person out of whatever they were reading mid-tab, repeatedly, every 30s.
// A full diff-based re-render is a much bigger change; preserving scroll position across the
// refresh is the direct, bounded fix for "jumpy": remember where the person was, put them back
// there once the new content has painted.
function preserveScrollAcrossReload(reloadFn){
  const active=document.querySelector(".tab.active");
  const scroller=(active && active.scrollHeight>active.clientHeight) ? active : null;
  const savedElTop = scroller ? scroller.scrollTop : null;
  const savedWinTop = window.scrollY;
  // Also remember inner scroll containers (a log list, a table) by id and which <details> were open,
  // so a wholesale innerHTML rebuild does not scroll an inner pane back to the top or collapse an
  // expanded section under the reader. Restore-by-id works because loaders rebuild with stable ids.
  const innerTops={}, openDetails={};
  if(active){
    active.querySelectorAll("[id]").forEach(el=>{ if(el.scrollHeight>el.clientHeight+4 && el.scrollTop>0) innerTops[el.id]=el.scrollTop; });
    active.querySelectorAll("details[id]").forEach(d=>{ if(d.open) openDetails[d.id]=1; });
  }
  const r = reloadFn();
  const restore=()=>{
    if(scroller && savedElTop!=null) scroller.scrollTop=savedElTop;
    window.scrollTo(0, savedWinTop);
    for(const id in openDetails){ const d=document.getElementById(id); if(d && "open" in d) d.open=true; }
    for(const id in innerTops){ const el=document.getElementById(id); if(el) el.scrollTop=innerTops[id]; }
  };
  if(r && typeof r.then==="function") r.then(()=>setTimeout(restore,0)).catch(()=>{});
  else setTimeout(restore,0);
}
// Reload a tab's content AFTER an action without yanking the reader's scroll to the top: the same as a
// bare setTimeout(loadX, ms), but the reload runs through preserveScrollAcrossReload. Used for the
// post-action refreshes (a build step applied, a backup run, a snapshot taken) so the fresh result
// appears in place instead of bouncing the view up.
function _reloadSoon(fn, ms){ setTimeout(function(){ try{ preserveScrollAcrossReload(fn); }catch(_e){ try{ fn(); }catch(__e){} } }, ms||0); }
setInterval(()=>{ const active=document.querySelector(".tab.active");
  if(active && !NO_AUTO_REFRESH.has(active.id) && !userIsEditing()) preserveScrollAcrossReload(()=>load(active.id));
}, 30000);
</script>
</body>
</html>"""


# Serve-time asset externalisation: the cockpit pages inline ~200KB of CSS+JS and are served no-store
# (for freshness after updates), so today the whole payload re-downloads on every visit (the reason
# the cockpit "takes ages to load"). This lifts the single largest <style> and <script> block into
# content-hashed files (/ui-asset/<hash>.css|js) served with immutable caching, so the browser fetches
# the heavy code ONCE and reuses it across loads, while the HTML doc stays small and no-store. The
# templates are the source of truth and are NOT edited: only the SERVED output is rewritten, and the
# JS/CSS content is byte-identical (just relocated), so behaviour cannot change. Version-stable: the
# __MONGOOSE_VERSION__ placeholder lives only in the HTML body, never inside a script/style block.
UI_ASSETS: dict[str, tuple[str, str]] = {}  # content-hash -> (body, content_type). Read by /ui-asset.


def _externalize_assets(html: str) -> str:
    import hashlib as _hl
    import re as _re
    for tag, ctype in (("style", "text/css; charset=utf-8"),
                       ("script", "application/javascript; charset=utf-8")):
        try:
            pat = _re.compile("<" + tag + r"(?![^>]*\bsrc=)[^>]*>(.*?)</" + tag + ">", _re.S)
            blocks = list(pat.finditer(html))
            if not blocks:
                continue
            big = max(blocks, key=lambda m: len(m.group(1)))
            body = big.group(1)
            if len(body) < 4000:  # leave small inline blocks (e.g. the service-worker registration)
                continue
            h = _hl.sha1(body.encode("utf-8")).hexdigest()[:16]
            UI_ASSETS[h] = (body, ctype)
            ref = ('<link rel="stylesheet" href="/ui-asset/' + h + '.css">' if tag == "style"
                   else '<script src="/ui-asset/' + h + '.js"></script>')
            html = html[:big.start()] + ref + html[big.end():]
        except Exception:
            pass  # never break page rendering in order to optimise it
    return html


def ui_asset_urls() -> list[str]:
    """Current externalised asset URLs, for the service worker to precache aggressively."""
    return [f"/ui-asset/{h}.{'css' if 'css' in ctype else 'js'}" for h, (_body, ctype) in UI_ASSETS.items()]


def _js_function_source(src: str, name: str) -> str:
    """Return the full `async function NAME(...){...}` block from a page constant, brace-matched with
    string/template/comment awareness, so another page can REUSE it verbatim (one source of truth, no
    drift). Empty string if not found."""
    import re
    m = re.search(r"\basync function " + re.escape(name) + r"\s*\(", src)
    if not m:
        return ""
    start = m.start()
    i = src.index("{", m.end() - 1)
    depth, j, mode, n = 0, i, None, len(src)
    while j < n:
        c, two = src[j], src[j:j + 2]
        if mode in ("'", '"', "`"):
            if c == "\\":
                j += 2; continue
            if c == mode:
                mode = None
            j += 1; continue
        if mode == "//":
            if c == "\n":
                mode = None
            j += 1; continue
        if mode == "/*":
            if two == "*/":
                mode = None; j += 2; continue
            j += 1; continue
        if two == "//":
            mode = "//"; j += 2; continue
        if two == "/*":
            mode = "/*"; j += 2; continue
        if c in "'\"`":
            mode = c; j += 1; continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    return ""


# Setup-action handlers the cockpit's own Setup-tab buttons call but that were only ever DEFINED in the
# install/launchpad pages, so on /cockpit those buttons (guest access via the Cloudflare tunnel, reboot
# persistence, LAN access, remote-access repair, body import) threw ReferenceError and silently did
# nothing. We reuse the SAME source from those pages (no duplication, no drift) and inject it here.
_COCKPIT_BORROWED_ACTIONS = (
    ("INSTALL_HTML", ["enablePhone", "serviceInstall", "tunnelInstall", "tunnelLogin",
                      "tunnelProvision", "repairRemoteAccess"]),
    ("DEVICE_LAUNCHPAD_HTML", ["uploadBodyData"]),
)
_cockpit_actions_cache: str | None = None


def _cockpit_borrowed_actions_js() -> str:
    global _cockpit_actions_cache
    if _cockpit_actions_cache is None:
        parts = []
        for const_name, fns in _COCKPIT_BORROWED_ACTIONS:
            src = globals().get(const_name, "")
            for fn in fns:
                blk = _js_function_source(src, fn)
                if blk:
                    parts.append(blk)
        _cockpit_actions_cache = "\n".join(parts)
    return _cockpit_actions_cache


def _rtm_page_tokens() -> dict[str, str]:
    """Page tokens the site profile has no scalar for: the flat-id picker list (every profile block,
    flats 1-9) and the published half-year budget in pence that the apportionment reconcile checks against
    (from the RTM property config; empty, meaning 'no published-total check', when there is none).
    The flat list token sits inside a quoted JS array in the template, ['{{RTM_FLAT_IDS_QUOTED}}'], so the
    raw template still parses; it expands to the quoted ids joined by ',' with the leading '' entry."""
    from . import profile as _profile
    flats = [""] + [f"{code}F{n}" for code in _profile.block_codes() for n in range(1, 10)]
    try:
        from decimal import Decimal
        from .services.rtm_example_court import load_property
        pence = str(int(Decimal(str(load_property()["constants"]["half_year_budget"])) * 100))
    except Exception:
        pence = ""
    return {"{{RTM_FLAT_IDS_QUOTED}}": "','".join(flats),
            "{{RTM_PUBLISHED_TOTAL_PENCE}}": pence}


def _profile_page(html: str) -> str:
    """Fill the site-profile placeholders ({{rtm.name}}, {{site.domain}}, {{owner.name}}, ...) in a page
    template at serve time, HTML/JS-safe and cached per profile, BEFORE assets are externalised so the
    content-hashed /ui-asset files carry the rendered text."""
    from .settings import render_profile_copy
    return render_profile_copy(html, html=True, extra=_rtm_page_tokens())


def cockpit_html() -> str:
    # Version truth: the header reads __version__, never a hardcoded string. The external audit
    # found the cockpit proudly displaying 1.2.0 while /health said 1.32.1; for a system whose
    # whole pitch is status honesty, a lying version badge is more than cosmetic.
    from mongoose import __version__
    html = COCKPIT_HTML.replace("__MONGOOSE_VERSION__", __version__)
    # Inject the borrowed Setup-tab action handlers so those buttons are not dead ReferenceError traps.
    html = html.replace("</body>", "<script>\n" + _cockpit_borrowed_actions_js() + "\n</script>\n</body>", 1)
    return _externalize_assets(_profile_page(html))


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mongoose</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<style>
@font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}
@font-face{font-family:"Inter";font-weight:500;font-display:swap;src:url("/static/fonts/inter-latin-500-normal.woff2") format("woff2")}
@font-face{font-family:"Fraunces";font-weight:500;font-display:swap;src:url("/static/fonts/fraunces-latin-500-normal.woff2") format("woff2")}
@font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url("/static/fonts/fraunces-latin-600-normal.woff2") format("woff2")}
@font-face{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}
</style>
<style>
  :root{ --ink:#16140F; --panel:#1F1C16; --panel2:#26221B; --line:#322D24;
         --text:#ECE6DA; --dim:#9C9486; --ok:#7CB893; --gold:#C9A35B; --gold-dim:#7d6736; --bad:#D0785E; }
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.6 "Inter",system-ui,sans-serif;background:var(--ink);color:var(--text);
       display:flex;min-height:100vh;align-items:center;justify-content:center;
       background-image:radial-gradient(1000px 500px at 50% -20%, rgba(201,163,91,0.08), transparent 70%)}
  .box{background:linear-gradient(180deg,var(--panel),#1c1914);border:1px solid var(--line);
       border-radius:16px;padding:30px;width:340px;
       box-shadow:0 20px 60px -30px rgba(0,0,0,0.9)}
  .mark{display:flex;align-items:center;gap:11px;margin-bottom:18px}
  .pulse{width:11px;height:11px;border-radius:50%;background:var(--ok);position:relative}
  .pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--ok);
                animation:breathe 2.6s ease-in-out infinite}
  @keyframes breathe{0%{transform:scale(1);opacity:.55}70%{transform:scale(2.6);opacity:0}100%{opacity:0}}
  h1{font-family:"Fraunces",serif;font-size:22px;margin:0;font-weight:600}
  h1 .sub{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.2em;
          text-transform:uppercase;color:var(--gold);margin-left:6px}
  p{color:var(--dim);font-size:13.5px;margin:0 0 18px}
  input[type=password]{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);
       border-radius:11px;padding:13px;font-size:15px;box-sizing:border-box;font-family:inherit}
  input[type=password]:focus{outline:none;border-color:var(--gold)}
  label{display:flex;gap:9px;align-items:center;color:var(--dim);font-size:13px;margin:14px 0}
  button{width:100%;background:var(--panel2);border:1px solid var(--gold-dim);color:#e9d9b4;
       border-radius:11px;padding:13px;font-size:15px;font-weight:500;cursor:pointer;margin-top:6px;
       font-family:inherit;transition:border-color .15s,background .15s}
  button:hover{border-color:var(--gold);background:#2c2820}
  .err{color:var(--bad);font-size:13px;min-height:18px;margin-top:10px}
  @media (prefers-reduced-motion:reduce){.pulse::after{animation:none}}
</style></head><body>
<div class="box">
  <div class="mark"><img class="logo-emerge" src="/static/img/mongoose-64.png" alt="Mongoose" width="44" height="44" style="margin-right:6px"><h1>Mongoose<span class="sub">cockpit</span></h1></div>
  <div class="sillygoose-wrap" style="margin:6px 0 14px;justify-content:center">
    <img class="sillygoose-img" src="/static/img/sillygoose-256.png" alt="Sillygoose" width="30" height="30">
    <div class="sillygoose-sub">your household goose</div>
  </div>
  <div id="tokpane">
    <p>Enter your access token to use Mongoose on this device.</p>
    <label for="tok" class="sr-only">Access token</label>
    <input id="tok" type="password" placeholder="access token" autocomplete="current-password" aria-label="Access token">
    <label><input id="rem" type="checkbox"> Remember this device for 30 days</label>
    <button onclick="go()">Sign in</button>
    <p style="margin-top:14px;font-size:13px"><a href="#" onclick="showAcct();return false" style="color:var(--gold)">Have a username? Sign in with that instead</a></p>
  </div>
  <div id="acctpane" style="display:none">
    <p id="acctlede">Sign in with your Mongoose username and password.</p>
    <input id="au" type="text" placeholder="username" autocomplete="username" autocapitalize="none" aria-label="Username" style="width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:13px 14px;border-radius:11px;font-size:15px;margin-bottom:10px">
    <input id="ap" type="password" placeholder="password" autocomplete="current-password" aria-label="Password">
    <input id="ap2" type="password" placeholder="confirm password" autocomplete="new-password" aria-label="Confirm password" style="display:none">
    <input id="ad" type="text" placeholder="what should Mongoose call you?" autocomplete="name" autocapitalize="words" maxlength="40" aria-label="The name you want to be known by" style="display:none">
    <label><input id="arem" type="checkbox" checked> Stay signed in on this device for 30 days</label>
    <button id="acctbtn" onclick="acctGo()">Sign in</button>
    <p style="margin-top:12px;font-size:13px">
      <a href="#" id="acctswap" onclick="toggleCreate();return false" style="color:var(--gold)">Create an account</a>
      &nbsp;·&nbsp;
      <a href="#" onclick="showTok();return false" style="color:var(--dim)">Use a token instead</a>
    </p>
  </div>
  <div class="err" id="err" role="alert" aria-live="assertive"></div>
</div>
<script>
function showAcct(){ document.getElementById("tokpane").style.display="none"; document.getElementById("acctpane").style.display=""; document.getElementById("err").textContent=""; }
function showTok(){ document.getElementById("acctpane").style.display="none"; document.getElementById("tokpane").style.display=""; document.getElementById("err").textContent=""; }
var creating=false;
function toggleCreate(){
  creating=!creating;
  document.getElementById("ap2").style.display = creating? "" : "none";
  document.getElementById("ad").style.display = creating? "" : "none";
  document.getElementById("acctbtn").textContent = creating? "Create account & sign in" : "Sign in";
  document.getElementById("acctswap").textContent = creating? "I already have an account" : "Create an account";
  document.getElementById("acctlede").textContent = creating
   ? "Pick a username and password. You can then sign in from any phone, tablet or computer."
    : "Sign in with your Mongoose username and password.";
  document.getElementById("err").textContent="";
}
async function go(){
  const err=document.getElementById("err"); err.textContent="";
  const token=document.getElementById("tok").value;
  const remember=document.getElementById("rem").checked;
  try{
    const r=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({token,remember})});
    if(r.ok){ location.href="/ui"; }
    else { err.textContent="Invalid token. Check MONGOOSE_API_TOKEN."; }
  }catch(e){ err.textContent="Could not reach Mongoose ("+e+")."; }
}
async function acctGo(){
  const err=document.getElementById("err"); err.textContent="";
  const username=document.getElementById("au").value.trim();
  const password=document.getElementById("ap").value;
  const remember=document.getElementById("arem").checked;
  if(!username || !password){ err.textContent="Enter a username and password."; return; }
  try{
    if(creating){
      const c=document.getElementById("ap2").value;
      if(password!==c){ err.textContent="Passwords don't match."; return; }
      const dn=document.getElementById("ad").value.trim();
      const regBody={username,password}; if(dn) regBody.display_name=dn;
      const reg=await fetch("/account/register",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify(regBody)});
      if(!reg.ok){ const j=await reg.json().catch(()=>({})); err.textContent=(j.error||"Could not create account")+(reg.status===401?" (open this from a device that's already signed in, or use a token first).":""); return; }
    }
    const r=await fetch("/account/login",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({username,password,remember})});
    if(r.ok){ location.href="/ui"; }
    else if(r.status===429){ err.textContent="Too many attempts; wait a few minutes."; }
    else { err.textContent="Invalid username or password."; }
  }catch(e){ err.textContent="Could not reach Mongoose ("+e+")."; }
}
document.getElementById("tok").addEventListener("keydown",e=>{ if(e.key==="Enter")go(); });
document.getElementById("ap").addEventListener("keydown",e=>{ if(e.key==="Enter" && document.getElementById("ap2").style.display==="none")acctGo(); });
document.getElementById("ap2").addEventListener("keydown",e=>{ if(e.key==="Enter")acctGo(); });
</script>
</body></html>"""


def login_html() -> str:
    return LOGIN_HTML



# The phone-facing pairing page (PWA). Opened after scanning the QR (which points at
# /pair?code=...&host=...). It reads the code from the URL, asks for a device name, and
# claims it. On success it shows the device token once and offers Add-to-Home-Screen.
# Local-first styling, no external assets.
PAIR_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mongoose</title>
<link rel="manifest" href="/device/manifest.webmanifest">
<meta name="apple-mobile-web-app-title" content="Mongoose">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<style>
  :root{ --ink:#16140F; --panel:#1F1C16; --panel2:#26221B; --line:#322D24;
         --text:#ECE6DA; --dim:#9C9486; --ok:#7CB893; --gold:#C9A35B; --gold-dim:#7d6736; --bad:#D0785E; }
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.6 "Inter",system-ui,sans-serif;background:var(--ink);color:var(--text);
       display:flex;min-height:100vh;align-items:center;justify-content:center;padding:18px;
       background-image:radial-gradient(900px 500px at 50% -20%, rgba(201,163,91,0.08), transparent 70%)}
  @font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}
  @font-face{font-family:"Inter";font-weight:600;font-display:swap;src:url("/static/fonts/inter-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url("/static/fonts/fraunces-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}
  .box{background:linear-gradient(180deg,var(--panel),#1c1914);border:1px solid var(--line);
       border-radius:16px;padding:26px;width:100%;max-width:420px;
       box-shadow:0 20px 60px -30px rgba(0,0,0,0.9)}
  .mark{display:flex;align-items:center;gap:11px;margin-bottom:8px}
  .pulse{width:11px;height:11px;border-radius:50%;background:var(--ok);position:relative}
  .pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--ok);
                animation:breathe 2.6s ease-in-out infinite}
  @keyframes breathe{0%{transform:scale(1);opacity:.55}70%{transform:scale(2.6);opacity:0}100%{opacity:0}}
  h1{font-family:"Fraunces",serif;font-size:22px;margin:0;font-weight:600}
  h1 .sub{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.2em;
          text-transform:uppercase;color:var(--gold);margin-left:6px}
  p{color:var(--dim);font-size:14px;margin:6px 0 16px}
  label{display:block;color:var(--dim);font-size:13px;margin:14px 0 4px}
  input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);
       border-radius:11px;padding:13px;font-size:16px;font-family:inherit}
  input:focus{outline:none;border-color:var(--gold)}
  button{width:100%;background:var(--panel2);border:1px solid var(--gold-dim);color:#e9d9b4;
       border-radius:11px;padding:14px;font-size:16px;font-weight:600;cursor:pointer;margin-top:16px;
       font-family:inherit}
  button:hover{border-color:var(--gold)}
  .row{display:flex;gap:10px;align-items:center;margin:8px 0;font-size:13px;color:var(--dim)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--gold)}
  .msg{margin-top:14px;font-size:14px;min-height:20px}
  .tok{font-family:"IBM Plex Mono",monospace;font-size:12px;word-break:break-all;background:#141109;
       border:1px solid var(--line);border-radius:9px;padding:12px;margin-top:8px;color:#d6cfc0}
  .ok{color:var(--ok)} .bad{color:var(--bad)}
  .caps{margin-top:10px}
  .cap{display:inline-block;font-size:11px;font-family:"IBM Plex Mono",monospace;color:var(--gold);
       border:1px solid var(--gold-dim);border-radius:20px;padding:3px 9px;margin:3px 4px 0 0}

  @media (prefers-reduced-motion:reduce){ *,*::before,*::after{ animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important; } }
</style></head><body>
<div class="box">
  <div class="mark"><img class="logo-emerge" src="/static/img/mongoose-64.png" alt="Mongoose" width="44" height="44" style="margin-right:6px"><h1>Pair with Mongoose<span class="sub">device</span></h1><img src="/static/img/sillygoose-256.png" alt="" width="30" height="30" style="margin-left:auto"></div>
  <p id="intro">Connecting this device to Mongoose on your Mac.</p>
  <div id="form">
    <label>What should Mongoose call you?</label>
    <input id="person" type="text" placeholder="e.g. {{owner.name}}" autocomplete="name">
    <label>This device's name (optional)</label>
    <input id="dname" type="text" placeholder="e.g. {{owner.name}}'s phone" autocomplete="off">
    <button onclick="pair()" id="btn">Pair this device</button>
  </div>
  <div class="msg" id="msg" role="status" aria-live="polite"></div>
</div>
<script>
const params = new URLSearchParams(location.search);
const code = params.get("code") || "";
if(!code){
  document.getElementById("intro").textContent="No pairing code in the link. On the Mac cockpit, open Devices and add a device to get a fresh QR code.";
  document.getElementById("form").style.display="none";
}
function esc(s){return String(s==null?"":s).replace(/[&<>\"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));}
function _loginText(){
  const u=(document.getElementById('id-user')||{}).value||'';
  const p=(document.getElementById('id-pass')||{}).value||'';
  return 'Mongoose login\nUsername: '+u+'\nPassword: '+p+'\n';
}
function copyLogin(){
  const m=document.getElementById('id-msg');
  try{ navigator.clipboard.writeText(_loginText()); if(m)m.textContent='Copied. Paste it somewhere safe now.'; }
  catch(e){ if(m)m.textContent='Copy is not available here; please write it down.'; }
}
function downloadLogin(){
  const m=document.getElementById('id-msg');
  try{
    const blob=new Blob([_loginText()],{type:'text/plain'});
    const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='mongoose-login.txt';
    document.body.appendChild(a); a.click(); a.remove();
    if(m)m.textContent='Saved mongoose-login.txt to your downloads.';
  }catch(e){ if(m)m.textContent='Could not save the file; please copy it instead.'; }
}
async function saveIdentity(btn){
  const u=(document.getElementById('id-user')||{}).value.trim();
  const p=(document.getElementById('id-pass')||{}).value;
  const dn=(((document.getElementById('id-display')||{}).value)||window._pairPerson||u||'').trim();
  const allowDup=!!((document.getElementById('id-allow-dup')||{}).checked);
  const m=document.getElementById('id-msg');
  if(!u||!p){ if(m)m.textContent='Enter a username and password, or keep the suggested ones.'; return; }
  if(btn) btn.disabled=true;
  try{
    const tok=localStorage.getItem('mongoose_device_token')||'';
    const r=await fetch('/account/register',{method:'POST',headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':tok},
      body:JSON.stringify({username:u,password:p,display_name:dn,allow_poke:true,allow_duplicate_display_name:allowDup})});
    const d=await r.json();
    if(d&&d.ok){ if(m)m.innerHTML='<span style="color:var(--ok)">Saved. That is your login for any browser. You can open Mongoose now.</span>'; if(btn) btn.disabled=true; return; }
    const err=((d&&d.error)||'').toLowerCase();
    if(err.indexOf('display_name')>=0){
      // Display-name collision: what OTHERS see clashes. Reveal the "different person" override.
      const w=document.getElementById('id-allow-wrap'); if(w) w.style.display='flex';
      if(m)m.innerHTML='<span style="color:var(--warn,#d8a200)">Someone here is already shown as <b>'+esc(dn)+'</b>. Change the display name above so people can tell you apart. If this is genuinely a different person who shares the name, tick the box and save again. If it is your OTHER device, an owner can merge you from the Devices tab.</span>';
    } else if(err.indexOf('taken')>=0){
      if(m)m.innerHTML='<span style="color:var(--warn,#d8a200)">That login name (<b>'+esc(u)+'</b>) is already taken. Change the username above to something else.</span>';
    } else {
      if(m)m.textContent=((d&&d.error)||'Could not save; try different details.');
    }
    if(btn) btn.disabled=false;
  }catch(e){ if(m)m.textContent='Could not save right now; you can set it later in Settings.'; if(btn) btn.disabled=false; }
}
async function pair(){
  const msg=document.getElementById("msg"); const btn=document.getElementById("btn");
  const person=document.getElementById("person").value.trim();
  let name=document.getElementById("dname").value.trim();
  if(!person){ msg.innerHTML='<span class="bad">Please say what Mongoose should call you.</span>'; return; }
  if(!name){ name=person+"'s device"; }
  window._pairPerson=person;
  btn.disabled=true; msg.textContent="Pairing...";
  try{
    const r=await fetch("/pair/claim",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code,device_name:name,display_name:person,device_type:(function(){var u=navigator.userAgent||'';if(/iPhone|iPad|iPod/i.test(u))return 'ios';if(/Android/i.test(u))return 'android';if(/Mac/i.test(u))return 'mac';if(/Windows/i.test(u))return 'windows';return 'phone';})()})});
    const d=await r.json();
    if(!d.ok){ msg.innerHTML='<span class="bad">'+esc(d.error||"pairing failed")+'</span>'; btn.disabled=false; return; }
    document.getElementById("form").style.display="none";
    localStorage.setItem('mongoose_device_token', d.device_token);
    let caps=(d.capabilities||[]).map(c=>'<span class="cap">'+esc(c)+'</span>').join("");
    const ua=navigator.userAgent||'';
    const isiOS=/iPhone|iPad|iPod/i.test(ua);
    // The home-screen entry must be the APP (launchpad), captured from the launchpad screen, not this
    // one-time /pair page. On iOS "Add to Home Screen" saves the CURRENT url, and on Android the
    // manifest start_url resolves against the current origin, so the user must be on the launchpad when
    // they add it. So: open Mongoose first, then add THAT screen.
    //
    // CROSS-ORIGIN HANDOFF: /pair may load on a different origin than launchpad_url (LAN
    // bootstrap page handing off to the DDNS canonical app address, or vice versa). The token
    // was just stored via localStorage on THIS page's origin, which is invisible on any other
    // origin. Carry it in the URL fragment (never sent to the server or a request log, unlike
    // a query string) so the destination origin can import it before the user installs the
    // PWA; importPairingHandoff() on the launchpad reads and immediately strips it.
    function handoffUrl(url){
      if(!url) return '';
      try{
        const dest=new URL(url, location.href);
        if(dest.origin===location.origin) return url;
      }catch(_e){ return url; }
      const hash='mongoose_device_token='+encodeURIComponent(d.device_token||'')+
                 '&mongoose_device_profile='+encodeURIComponent(d.profile||'primary_phone');
      return url+(url.indexOf('#')>=0?'&':'#')+hash;
    }
    const launchpad = handoffUrl(d.launchpad_url || '/device/launchpad');
    const stepTwo = isiOS
      ? 'tap the <b>Share</b> button (the square with an up-arrow), then <b>Add to Home Screen</b>.'
      : 'open the browser menu (<b>\u22EE</b>), then <b>Install app</b> or <b>Add to Home screen</b>.';
    // CORRECTED: whatever gets Added to Home Screen becomes a PERMANENT bookmark for that
    // icon, and a LAN address is meaningless the moment the phone leaves this network. The
    // primary link must be the durable one (DDNS, when configured), not whichever one is
    // guaranteed to load in this exact moment. What actually needs solving is WHY the durable
    // link might not load from here right now, not routing around it by bookmarking the
    // wrong thing. The most common cause is a router that can't loop back to its own public
    // address for a device on the same home network specifically, a router setting, not
    // something Mongoose can fix by itself, so this is now named honestly rather than hidden.
    const diag = d.ddns_reachability || {};
    const ddnsSuspect = d.launchpad_is_remote && diag.reason === "hairpin_or_no_portforward";
    const ddnsStale = d.launchpad_is_remote && diag.reason === "dns_stale";
    let fallbackBlock = '';
    if (ddnsSuspect || ddnsStale) {
      const why = ddnsStale
        ? 'Your Dynamic DNS address currently points at an out-of-date IP, so it will not connect from anywhere right now until it catches up.'
        : 'Your home Wi-Fi router may not be able to loop back to its own public address for a device on the same network, a common router limitation. This link should still work fine once you are away from home.';
      const lanLink = handoffUrl(d.launchpad_lan_fallback_url||'');
      fallbackBlock = '<div style="margin-top:10px;padding:10px 12px;border:1px solid var(--warn,#d8a200);border-radius:10px;background:rgba(255,176,0,.06);font-size:13px;color:var(--dim)">'
        +'<b style="color:var(--text)">Heads up:</b> '+esc(why)+'<br><br>'
        +'Try <b>Open Mongoose</b> above first; if it does not load, use the link below to open it on THIS Wi-Fi right now. '
        +'Adding that version to your home screen will only work while you are on this Wi-Fi; come back and redo Add to Home Screen with the link above once this is sorted, to get an icon that works everywhere.<br>'
        +'<div class="tok" style="margin-top:6px;word-break:break-all">'+esc(lanLink)+'</div></div>';
    }
    const uname=esc(d.suggested_username||'');
    const pword=esc(d.suggested_password||'');
    const dname=esc(d.display_name||window._pairPerson||'');
    const identityBlock=
      '<div style="margin-bottom:14px;padding:14px;border:1px solid var(--gold);border-radius:12px;background:rgba(201,163,91,.08)">'+
      '<div style="font-weight:700;margin-bottom:4px">Save your Mongoose login</div>'+
      '<div style="font-size:13px;color:var(--dim);margin-bottom:10px">This is your personal login for signing in from any other browser. Write it down or save the file. You can change any of these before you continue.</div>'+
      '<label style="font-size:12px">Display name (what others see)</label>'+
      '<input id="id-display" value="'+dname+'" autocomplete="name" style="width:100%;margin:2px 0 8px;box-sizing:border-box">'+
      '<label style="font-size:12px">Username</label>'+
      '<input id="id-user" value="'+uname+'" autocomplete="username" style="width:100%;margin:2px 0 8px;box-sizing:border-box">'+
      '<label style="font-size:12px">Password</label>'+
      '<input id="id-pass" value="'+pword+'" autocomplete="new-password" style="width:100%;margin:2px 0 10px;box-sizing:border-box">'+
      '<label id="id-allow-wrap" style="display:none;align-items:center;gap:7px;font-size:12px;margin-bottom:8px"><input type="checkbox" id="id-allow-dup"> It is genuinely a different person who shares this name</label>'+
      '<button class="button-link" onclick="copyLogin()" style="background:var(--panel2,#26221b);color:var(--text);border:1px solid var(--line);padding:8px 12px;border-radius:9px;font-weight:600;margin-right:6px">Copy</button>'+
      '<button class="button-link" onclick="downloadLogin()" style="background:var(--panel2,#26221b);color:var(--text);border:1px solid var(--line);padding:8px 12px;border-radius:9px;font-weight:600;margin-right:6px">Download file</button>'+
      '<button class="button-link" onclick="saveIdentity(this)" style="background:var(--gold);color:#1a1a1a;padding:8px 12px;border-radius:9px;font-weight:700">Save my login</button>'+
      '<div id="id-msg" style="font-size:12px;color:var(--dim);margin-top:8px"></div>'+
      '</div>';
    msg.innerHTML=identityBlock+'<span class="ok">\u2713 Paired as '+esc(person)+'.</span>'+
      '<div class="caps">'+caps+'</div>'+
      '<p style="margin-top:14px">This device is now a Mongoose surface. The Mac stays the brain.</p>'+
      '<a class="button-link" href="'+esc(launchpad)+'" style="display:block;text-align:center;background:var(--gold);color:#1a1a1a;padding:13px 18px;border-radius:11px;text-decoration:none;font-weight:600;margin-top:14px">Open Mongoose</a>'+
      fallbackBlock+
      '<div style="margin-top:12px;padding:12px 14px;border:1px solid var(--gold-dim);border-radius:11px;background:rgba(201,163,91,.06)">'+
        '<div style="font-weight:600;margin-bottom:4px;color:var(--text)">Add Mongoose to your home screen</div>'+
        '<div style="font-size:13px;color:var(--dim);line-height:1.5"><b>1.</b> Tap <b>Open Mongoose</b> above.<br><b>2.</b> On the Mongoose screen, '+stepTwo+'<br><span style="color:var(--gold-dim)">Add it from the Mongoose screen, not this pairing page (this link is one-time). It opens over your home address, so the icon keeps working when you are away from home too.</span></div></div>'+
      '<details style="margin-top:12px"><summary class="muted">Show token once</summary><div class="tok">'+esc(d.device_token)+'</div></details>';
  }catch(e){ msg.innerHTML='<span class="bad">Could not reach Mongoose ('+esc(e)+').</span>'; btn.disabled=false; }
}
</script>
</body></html>"""


def pair_html() -> str:
    return _profile_page(PAIR_HTML)


# Guest invite landing: local-first. Rajdhani is provisioned into the owner's data-dir font
# cache and served by /static/fonts/rajdhani.css; when it has not been fetched yet, that CSS
# falls back to any system-installed Rajdhani and then Inter. No third-party font request is
# made from the guest browser.
def guest_invite_html(token: str = "", status: dict | None = None) -> str:
    import json as _json
    safe_token = _json.dumps(str(token or ""))
    safe_status = _json.dumps(status or {"ok": False, "error": "unknown_invite"})
    from .settings import _html_safe
    from . import profile as _profile
    owner_html = _html_safe(_profile.owner_name())  # sits inside a JS string literal: entity-escaped
    return f'''<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Mongoose Sillygoose invite</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<link rel="preload" href="/static/fonts/rajdhani.css" as="style">
<link rel="stylesheet" href="/static/fonts/rajdhani.css">
<style>
@font-face{{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}}
@font-face{{font-family:"Inter";font-weight:600;font-display:swap;src:url("/static/fonts/inter-latin-600-normal.woff2") format("woff2")}}
@font-face{{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}}
:root{{--ink:#16140F;--panel:#1F1C16;--panel2:#26221B;--line:#3A3328;--text:#ECE6DA;--dim:#A69D8E;--ok:#7CB893;--gold:#C9A35B;--gold2:#F0D99B;--bad:#D0785E}}
*{{box-sizing:border-box}}
html,body{{margin:0;min-height:100%;background:var(--ink);color:var(--text);font:16px/1.55 "Inter",system-ui,sans-serif}}
body{{display:flex;align-items:center;justify-content:center;padding:18px;background-image:radial-gradient(900px 460px at 50% -12%,rgba(201,163,91,.18),transparent 70%),radial-gradient(700px 500px at 120% 110%,rgba(124,184,147,.10),transparent 60%)}}
.shell{{width:min(520px,100%);min-height:620px;display:flex;align-items:center;justify-content:center}}
.card{{width:100%;background:linear-gradient(180deg,rgba(31,28,22,.96),rgba(22,20,15,.98));border:1px solid var(--line);border-radius:24px;padding:28px;box-shadow:0 30px 90px -45px #000;overflow:hidden;position:relative}}
.card:before{{content:"";position:absolute;inset:-120px -80px auto; height:240px;background:radial-gradient(circle,rgba(201,163,91,.16),transparent 60%);pointer-events:none}}
.stage{{position:relative;text-align:center}}
.logo{{width:132px;height:132px;object-fit:contain;filter:drop-shadow(0 20px 35px rgba(0,0,0,.35));animation:pulse 1.7s ease-in-out infinite}}
.goose{{width:92px;height:92px;object-fit:contain;margin-top:4px;opacity:0;transform:translateY(8px) scale(.92);transition:opacity .5s ease, transform .5s ease;animation:pulse 1.35s ease-in-out infinite}}
.goose.show{{opacity:1;transform:translateY(0) scale(1)}}
@keyframes pulse{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.055)}}}}
.loading{{width:210px;height:7px;border-radius:999px;background:#141109;border:1px solid var(--line);margin:22px auto 0;overflow:hidden;opacity:0;transition:opacity .25s ease}}
.loading.show{{opacity:1}}
.loading span{{display:block;width:0;height:100%;background:linear-gradient(90deg,var(--gold),var(--gold2));animation:loadbar 2s ease-in-out forwards}}
@keyframes loadbar{{from{{width:0}}to{{width:100%}}}}
.welcome{{display:none;animation:rise .36s ease-out both}}
.welcome.show{{display:block}}
h1{{font-family:"Rajdhani","Inter",system-ui,sans-serif;font-size:clamp(28px,7vw,42px);line-height:1.05;font-weight:700;letter-spacing:.035em;text-transform:uppercase;margin:18px 0 10px;color:#F4E4B8;text-wrap:balance}}
.sub{{color:var(--dim);font-size:14px;margin:0 auto 18px;max-width:36em}}
@keyframes rise{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:none}}}}
button,.button{{appearance:none;border:1px solid var(--gold);background:linear-gradient(180deg,#E4C573,#B88D3F);color:#18140e;border-radius:14px;padding:13px 18px;font:700 15px/1 "Rajdhani","Inter",system-ui,sans-serif;letter-spacing:.035em;text-transform:uppercase;cursor:pointer;box-shadow:0 12px 28px -18px var(--gold);text-decoration:none;display:inline-block}}
button.secondary{{background:transparent;color:var(--gold2);box-shadow:none}}
button.delete{{display:block;margin:14px auto 0;background:#221E17;border:1px solid var(--line);color:var(--dim);box-shadow:none;font-size:12px;padding:9px 14px;border-radius:11px}}
button.delete:hover{{color:var(--bad);border-color:var(--bad)}}
.form{{display:none;text-align:left;animation:rise .28s ease-out both}}
.form.show{{display:block}}
label{{display:block;color:var(--gold2);font-weight:650;margin:14px 0 6px}}
.hint{{display:block;color:var(--dim);font-weight:400;font-size:12px;margin-top:2px}}
input,textarea{{width:100%;background:#141109;border:1px solid var(--line);border-radius:13px;color:var(--text);padding:13px;font:16px/1.45 "Inter",system-ui,sans-serif}}
textarea{{min-height:90px;resize:vertical}}
input:focus,textarea:focus{{outline:2px solid rgba(201,163,91,.45);border-color:var(--gold)}}
.check{{display:flex;gap:11px;align-items:flex-start;margin:14px 0;color:var(--dim);font-size:14px}}
.check input{{width:auto;margin-top:3px;accent-color:var(--gold)}}
.msg{{min-height:24px;margin-top:14px;color:var(--dim)}}
.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
.small{{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--gold);margin-bottom:12px}}
.invalid{{text-align:center}}
@media (prefers-reduced-motion:reduce){{*,*:before,*:after{{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important}}}}
</style></head><body>
<main class="shell"><section class="card" aria-live="polite">
  <div id="invalid" class="invalid" style="display:none">
    <img src="/static/img/mongoose-128.png" alt="Mongoose" width="84" height="84">
    <h1>Invite needs a refresh</h1><p class="sub" id="invalidMsg">This invite is not available.</p>
  </div>
  <div id="intro" class="stage">
    <div class="small">private guest invite</div>
    <img id="mongooseHero" class="logo" src="/static/img/mongoose-512.png" alt="Pulsating Mongoose">
    <div><img id="gooseHero" class="goose" src="/static/img/sillygoose-256.png" alt="Pulsating Sillygoose"></div>
    <div id="bar" class="loading" aria-label="Loading your Mongoose invite"><span></span></div>
    <div id="welcome" class="welcome">
      <h1>You’re invited to join Mongoose</h1>
      <p class="sub">Are you in?</p>
      <button id="readyBtn" type="button">Continue</button>
      <button id="deleteBtn" type="button" class="delete">Delete this invitation</button>
    </div>
  </div>
  <form id="guestForm" class="form">
    <div class="small">one-time guest setup</div>
    <label for="mongooseName">How do you want Mongoose to refer to you?<span class="hint">This is your cosy assistant name.</span></label>
    <input id="mongooseName" name="mongooseName" autocomplete="nickname" maxlength="80" required>
    <label for="publicName">How do you want human guests to refer to you?<span class="hint">This is what other guests may see if you allow it.</span></label>
    <input id="publicName" name="publicName" autocomplete="name" maxlength="80" required>
    <label for="funFact">A fun fact about yourself<span class="hint">Small, harmless, and preferably a bit you.</span></label>
    <textarea id="funFact" name="funFact" maxlength="280" required></textarea>
    <label class="check"><input id="announceJoin" type="checkbox"> <span>Can we tell other guests that you joined?</span></label>
    <label class="check"><input id="shareFact" type="checkbox"> <span>Can we tell other guests your fun fact?</span></label>
    <button id="finishBtn" type="submit">Finish guest setup</button>
    <div id="formMsg" class="msg" role="status" aria-live="polite"></div>
  </form>
</section></main>
<script>
const INVITE_TOKEN={safe_token};
const INVITE_STATUS={safe_status};
const CLIENT_VERSION={_json.dumps((status or {}).get("server_version") or "1.1")};
// Strip the invite token out of the address bar + history. The claim POST uses the INVITE_TOKEN
// constant, not the URL, so the bearer token no longer needs to sit in browser history or be shared
// if the visitor copies the address.
try{{ if(window.history&&history.replaceState){{ history.replaceState({{}}, "", "/guest/invite"); }} }}catch(_e){{}}
function esc(s){{return String(s==null?'':s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function showInvalid(message){{document.getElementById('intro').style.display='none';document.getElementById('guestForm').style.display='none';document.getElementById('invalid').style.display='block';document.getElementById('invalidMsg').textContent=message||'This invite is not available.';}}
function _revealNormal(){{
  setTimeout(()=>document.getElementById('gooseHero').classList.add('show'),1000);
  setTimeout(()=>document.getElementById('bar').classList.add('show'),1100);
  setTimeout(()=>document.getElementById('welcome').classList.add('show'),3100);
}}
async function revealIntro(){{
  if(!INVITE_STATUS.ok){{ showInvalid(INVITE_STATUS.message||'This invite is not available.'); return; }}
  let tok='';
  try{{ tok=localStorage.getItem('mongoose_device_token')||''; }}catch(e){{}}
  if(tok){{
    // A token in localStorage only means "already in" if it still resolves to a LIVE device.
    // After the owner revokes or removes a guest, the browser keeps a DEAD token, and blocking
    // on mere presence trapped the person on "You are already in" forever. Validate it; if it is
    // definitively not a live device (4xx), clear it and let them claim this invite fresh.
    let jok=false, status=0, prof='';
    try{{
      const r=await fetch('/device/whoami',{{headers:{{'X-Mongoose-Device-Token':tok}}}});
      status=r.status;
      const jd=await r.json().catch(()=>({{}}));
      jok = r.ok && jd.ok===true;
      prof = (jd.device && jd.device.profile) || '';
    }}catch(e){{ status=-1; }}
    if(jok){{
      // An OWNER device must never turn itself into a guest: claiming a guest invite on the
      // owner's own device is exactly what mixes the two identities and confuses the whole UI.
      // Refuse plainly and point them to open it on the guest's phone instead.
      const isOwner = prof && prof!=='trusted_guest';
      const inner = isOwner
        ? '<h1>This is the owner device.</h1><p class="sub">You are set up here as the owner of this Mongoose, so this device cannot also join as a guest. Send this invite link to the other person and open it on THEIR phone.</p>'
        : '<h1>You are already in.</h1><p class="sub">This browser already has a Mongoose guest token.</p>';
      document.getElementById('intro').innerHTML='<img class="logo" src="/static/img/mongoose-512.png" alt="Mongoose">'+inner+'<a class="button" href="/device/launchpad">Open Mongoose</a>'; return;
    }}
    if(status>=400 && status<500){{
      try{{ localStorage.removeItem('mongoose_device_token'); localStorage.removeItem('mongoose_device_profile'); localStorage.removeItem('mongoose_guest_public_name'); }}catch(e){{}}
    }}
  }}
  _revealNormal();
}}
document.getElementById('readyBtn').addEventListener('click',()=>{{
  document.getElementById('intro').style.display='none';document.getElementById('guestForm').classList.add('show');
  // Prefill both name fields with the name the owner suggested; the guest can change either freely.
  var sugg=(INVITE_STATUS.suggested_name||'').trim();
  if(sugg){{ var mn=document.getElementById('mongooseName'); var pn=document.getElementById('publicName');
    if(mn&&!mn.value) mn.value=sugg; if(pn&&!pn.value) pn.value=sugg; }}
  setTimeout(()=>document.getElementById('mongooseName').focus(),30);
}});
document.getElementById('deleteBtn').addEventListener('click',async ()=>{{
  if(!confirm('Delete this invitation? The link will stop working for good.')) return;
  var btn=document.getElementById('deleteBtn'); btn.disabled=true;
  try{{
    const r=await fetch('/guest/invite/'+encodeURIComponent(INVITE_TOKEN)+'/delete',{{method:'POST'}});
    const d=await r.json();
    if(d&&d.ok){{ document.getElementById('intro').innerHTML='<img class="logo" src="/static/img/mongoose-512.png" alt="Mongoose"><h1>Invitation deleted</h1><p class="sub">This link no longer works. Nothing was set up, nothing was stored about you.</p>'; }}
    else {{ btn.disabled=false; alert('Could not delete this invitation.'); }}
  }}catch(_e){{ btn.disabled=false; alert('Could not reach Mongoose to delete the invitation.'); }}
}});
function storageAvailable(){{ try{{ const k='__mg_probe__'; localStorage.setItem(k,'1'); localStorage.removeItem(k); return true; }}catch(_e){{ return false; }} }}
document.getElementById('guestForm').addEventListener('submit',async (e)=>{{
  e.preventDefault();
  const msg=document.getElementById('formMsg'); const btn=document.getElementById('finishBtn');
  const body={{mongoose_name:document.getElementById('mongooseName').value.trim(),public_name:document.getElementById('publicName').value.trim(),fun_fact:document.getElementById('funFact').value.trim(),announce_join:document.getElementById('announceJoin').checked,share_fun_fact:document.getElementById('shareFact').checked,client_version:CLIENT_VERSION}};
  if(!body.mongoose_name||!body.public_name||!body.fun_fact){{msg.innerHTML='<span class="bad">Names and fun fact are needed before Sillygoose opens the gate.</span>';return;}}
  if(!storageAvailable()){{msg.innerHTML='<span class="bad">This browser is blocking local storage, so Mongoose cannot save your guest pass here. Open this link in a normal Safari or Chrome window (not private mode), or ask {owner_html} for a fresh invite.</span>';btn.disabled=false;return;}}
  btn.disabled=true; msg.textContent='Opening your guest space...';
  try{{
    const r=await fetch('/guest/invite/'+encodeURIComponent(INVITE_TOKEN)+'/claim',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
    const d=await r.json();
    if(!d.ok){{msg.innerHTML='<span class="bad">'+esc(d.message||d.error||'Invite claim failed')+'</span>';btn.disabled=false;return;}}
    localStorage.setItem('mongoose_device_token',d.device_token);
    localStorage.setItem('mongoose_device_profile','trusted_guest');
    localStorage.setItem('mongoose_guest_public_name',d.public_name||body.public_name);
    showGuestIdentity(d, body.public_name);
  }}catch(err){{msg.innerHTML='<span class="bad">Could not reach Mongoose ('+esc(err)+').</span>';btn.disabled=false;}}
}});
function showGuestIdentity(d, publicName){{
  const u=esc(d.suggested_username||''); const p=esc(d.suggested_password||''); const dn=esc(d.public_name||publicName||'');
  const html='<div style="text-align:center;margin-bottom:8px"><img class="logo" src="/static/img/sillygoose-256.png" alt="Sillygoose"><h1>You are in!</h1>'
    +'<p class="sub">Here is your personal Mongoose login. This is your identity. Save it to come in: it is how you are recognised, and how people can poke you by name. You can change it any time.</p></div>'
    +'<label style="font-size:13px;display:block">Display name<input id="gi-display" value="'+dn+'" style="width:100%;margin:3px 0 8px;box-sizing:border-box"></label>'
    +'<label style="font-size:13px;display:block">Username<input id="gi-user" value="'+u+'" autocomplete="username" style="width:100%;margin:3px 0 8px;box-sizing:border-box"></label>'
    +'<label style="font-size:13px;display:block">Password<input id="gi-pass" value="'+p+'" autocomplete="new-password" style="width:100%;margin:3px 0 8px;box-sizing:border-box"></label>'
    +'<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">'
    +'<button type="button" class="button" onclick="copyGuestIdentity(this)">Copy login</button>'
    +'<button type="button" class="button" onclick="saveGuestIdentity(this)">Create my identity</button></div>'
    +'<div id="gi-msg" style="font-size:12px;color:#999;margin-top:6px"></div>';
  document.getElementById('guestForm').innerHTML=html;
}}
function copyGuestIdentity(btn){{
  const u=(document.getElementById('gi-user')||{{}}).value||''; const p=(document.getElementById('gi-pass')||{{}}).value||'';
  const txt='Mongoose login\\nUsername: '+u+'\\nPassword: '+p;
  try{{ navigator.clipboard.writeText(txt); btn.textContent='Copied'; setTimeout(function(){{ btn.textContent='Copy login'; }},1500); }}
  catch(_e){{ const m=document.getElementById('gi-msg'); if(m) m.textContent='Copy did not work; write it down: '+u+' / '+p; }}
}}
async function saveGuestIdentity(btn){{
  const dn=((document.getElementById('gi-display')||{{}}).value||'').trim();
  const u=((document.getElementById('gi-user')||{{}}).value||'').trim();
  const p=((document.getElementById('gi-pass')||{{}}).value||'').trim();
  const m=document.getElementById('gi-msg');
  if(!u||!p){{ if(m) m.textContent='Pick a username and password to come in.'; return; }}
  btn.disabled=true; if(m) m.textContent='Saving your login...';
  try{{
    const tok=localStorage.getItem('mongoose_device_token')||'';
    const r=await fetch('/account/register',{{method:'POST',headers:{{'Content-Type':'application/json','X-Mongoose-Device-Token':tok}},body:JSON.stringify({{username:u,password:p,display_name:dn||u,allow_duplicate_display_name:false}})}});
    const jd=await r.json().catch(function(){{ return {{}}; }});
    if(r.ok && jd && jd.ok!==false){{
      // Sign in right away so the launchpad already recognises this identity (poke pane, who is
      // who) with no second login; the device token authorises the sign-in over the tunnel.
      try{{ await fetch('/account/login',{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json','X-Mongoose-Device-Token':tok}},body:JSON.stringify({{username:u,password:p,remember:true}})}}); }}catch(_e){{}}
      if(m) m.innerHTML='<span style="color:#7CB893">Saved and signed in. Opening Mongoose...</span>';
      setTimeout(function(){{ location.href='/device/launchpad'; }},800); return;
    }}
    btn.disabled=false;
    const err=String((jd&&(jd.error||jd.message))||'could not save').toLowerCase();
    if(err.indexOf('display')>=0){{ if(m) m.textContent='That display name is already used here. Change it so people can tell you apart (this also stops impersonation).'; }}
    else if(err.indexOf('taken')>=0||err.indexOf('exist')>=0){{ if(m) m.textContent='That username is taken. Try another one.'; }}
    else {{ if(m) m.textContent='Could not save the login. Adjust the details and try again.'; }}
  }}catch(e){{ btn.disabled=false; if(m) m.textContent='Could not reach Mongoose. Check your connection and try again.'; }}
}}
revealIntro();
</script></body></html>'''



DEVICE_LAUNCHPAD_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<script>
/* Keep this phone surface mobile even when the browser has "Request Desktop Site" on for this origin.
   Android propagates that per-site toggle into an installed PWA. Desktop mode forces a wide layout
   viewport and ignores width=device-width, so everything renders tiny. When the layout comes out far
   wider than the physical screen we re-pin the viewport to the real screen width so it stays phone-sized. */
(function(){
  function pin(){
    try{
      var m=document.querySelector('meta[name="viewport"]');
      if(!m){return;}
      var sw=(window.screen&&window.screen.width)||0;
      var lw=document.documentElement.clientWidth||window.innerWidth||0;
      if(sw&&lw&&lw>sw*1.3){
        m.setAttribute('content','width='+sw+', initial-scale=1, viewport-fit=cover');
      }
    }catch(e){}
  }
  pin();
  window.addEventListener('DOMContentLoaded',pin);
  window.addEventListener('resize',pin);
})();
</script>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Mongoose">
<meta name="theme-color" content="#1a1a1a">
<title>Mongoose</title>
<link rel="manifest" href="/device/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<script>
/* Fetch concurrency gate (same rationale as the desktop cockpit): cap in-flight fetches so the phone
   never fires a herd that starves the single-threaded server. Transparent when few requests are active. */
(function(){
  if(window.__mgFetchGate)return; window.__mgFetchGate=true;
  var MAX=4, active=0, q=[], _f=window.fetch.bind(window);
  function pump(){ while(active<MAX && q.length){ var t=q.shift(); active++;
    _f(t.a,t.b).then(function(r){active--;pump();t.res(r);},function(e){active--;pump();t.rej(e);}); } }
  window.fetch=function(a,b){ return new Promise(function(res,rej){ q.push({a:a,b:b,res:res,rej:rej}); pump(); }); };
})();
</script>
<script>if('serviceWorker' in navigator){window.addEventListener('load',function(){navigator.serviceWorker.register('/sw.js').then(function(r){try{r.update();}catch(_e){}}).catch(function(){});});}</script>
<style>
:root{--ink:#16140F;--panel:#1F1C16;--panel2:#26221B;--line:#322D24;--text:#ECE6DA;--dim:#9C9486;--ok:#7CB893;--gold:#C9A35B;--bad:#D0785E;--warn:#d1a85a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
html,body{margin:0;height:100%;width:100%;max-width:100vw;background:var(--ink);color:var(--text);font:17px/1.45 system-ui,sans-serif;overflow:hidden;-webkit-text-size-adjust:100%}
/* Respect the iPhone Dynamic Island / notch and the home indicator so nothing (the notifications
   banner especially) renders under the island where it cannot be tapped. */
body{display:flex;flex-direction:column;box-sizing:border-box;max-width:100vw;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}
/* Flexbox overflow guard: a wide flex child must not stretch the column past the viewport (which makes
   a standalone PWA scale the whole page down to fit ("desktop version, everything tiny"). */
body>*{min-width:0;max-width:100vw}
img,pre,table{max-width:100%}
#top{padding:10px 14px 0;display:flex;align-items:center;gap:8px;flex-shrink:0}
#top img{width:28px;height:28px;opacity:.9}
#top h1{font-size:17px;font-weight:700;margin:0;flex:1}
/* Tabs styled as subtle raised keys: rounded tops, a soft top highlight and drop shadow for depth,
   the active one lifted and tinted with its own quiet identity colour (olive / warm orange /
   neutral). No loud pulsing fills. */
#tabs{display:flex;gap:6px;padding:7px 8px 0;flex-shrink:0;border-bottom:1px solid var(--line)}
.tab{flex:1;padding:11px 4px;text-align:center;font-size:15px;font-weight:600;color:var(--dim);cursor:pointer;
  border:1px solid var(--line);border-bottom:none;border-radius:11px 11px 0 0;
  background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(0,0,0,.14));
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05);opacity:.72;
  transition:transform .18s,background .18s,box-shadow .18s,color .18s,opacity .18s;
  -webkit-tap-highlight-color:transparent}
.tab.on{color:var(--text);opacity:1;transform:translateY(-2px);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.12),0 5px 12px rgba(0,0,0,.38)}
#tab-talk.on{background:linear-gradient(180deg,rgba(150,150,72,.30),rgba(120,120,52,.10));border-color:rgba(172,172,96,.55)}
#tab-irc.on{background:linear-gradient(180deg,rgba(192,112,60,.32),rgba(150,80,40,.10));border-color:rgba(202,124,72,.55)}
#tab-home.on,#tab-cockpit.on{background:linear-gradient(180deg,rgba(142,152,162,.24),rgba(92,102,112,.08));border-color:rgba(152,162,172,.5)}
#panes{flex:1;overflow:hidden;position:relative}
.pane{position:absolute;inset:0;overflow-y:auto;padding:12px 14px;display:none;flex-direction:column}
.pane.on{display:flex}

/* ── TALK PANE ── */
/* ── TALK PANE: the desktop chat's terminal window, EXACT values, in the phone layout. Dark in both
   themes, like the desktop #chat. All colours copied from the desktop .irc-* rules, not invented. ── */
#talk-pane{align-items:stretch;gap:0}
/* window title bar (desktop .irc-title) */
.term-title{display:flex;align-items:center;gap:6px;background:linear-gradient(90deg,#0c5c0c,#073f07);
  color:#cfffcf;font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;font-weight:600;letter-spacing:.05em;
  padding:6px 10px;border:2px solid #000;border-bottom:none;border-radius:4px 4px 0 0;text-shadow:0 0 6px rgba(57,255,20,.5)}
.term-title img{width:16px;height:16px;animation:none;filter:none;vertical-align:-3px}
.term-title .sub{opacity:.7;font-weight:400}
.term-title .dots{display:flex;gap:5px;margin-left:auto}
.term-title .dots b{width:11px;height:11px;border-radius:50%;border:1px solid #000;box-shadow:inset 1px 1px 0 rgba(255,255,255,.35)}
.term-title .dots .mn{background:#ffd23a}.term-title .dots .mx{background:#46d160}.term-title .dots .cl{background:#ec5a5a}
/* CRT phosphor screen (desktop .irc-screen values), faint scanline layer, inset vignette */
#thread,#irc-log{position:relative;color:#2fd40f;
  font-family:"IBM Plex Mono",ui-monospace,monospace;text-shadow:0 0 8px rgba(57,255,20,.9),0 0 17px rgba(57,255,20,.5);
  border:2px solid #000;background-color:#020a02;
  background-image:repeating-linear-gradient(0deg,rgba(0,0,0,.2) 0,rgba(0,0,0,.2) 1px,transparent 2px,transparent 3px);
  box-shadow:inset 0 0 60px rgba(0,0,0,.5),0 0 24px rgba(57,255,20,.08)}
#thread{flex:1;display:flex;flex-direction:column;gap:1px;overflow-y:auto;padding:11px 12px 8px;font-size:11.5px;line-height:1.5;min-height:80px}
.tline{position:relative;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;margin:2px 0;animation:tIn 0.12s ease both;text-shadow:0 0 8px rgba(57,255,20,.85),0 0 16px rgba(57,255,20,.45)}
@keyframes tIn{from{opacity:0;transform:translateY(2px)}to{opacity:1;transform:none}}
.tts{color:#1f7a12}
.tnick-you{color:#ffb000;text-shadow:0 0 5px rgba(255,176,0,.5);font-weight:600}
.tnick-bot{color:#8bff6a;text-shadow:0 0 7px rgba(139,255,106,.7);font-weight:700}
.tsys{color:#1f9e0c;font-style:italic}
.tline a{color:#8affc0;word-break:break-all;overflow-wrap:anywhere}
/* surpriser + memory lines, exactly like the desktop chat: soft gold notice, grey italic aside */
.tnotice{color:#e9d39a}
.tmem{color:#6b7a6b;font-style:italic;font-size:12px;opacity:.85}
/* hint + emoji bar (desktop .irc-hint / .irc-emoji-bar) */
.term-hint{color:#1f7a12;font-size:11px;padding:6px 8px;background:#020a02;border:2px solid #000;border-top:1px solid #0c2a0c;
  font-family:"IBM Plex Mono",ui-monospace,monospace;display:flex;flex-direction:column;gap:5px}
.term-emoji{display:flex;gap:4px;flex-wrap:wrap}
.term-emoji button{background:#0a1a0a;border:1px solid #144014;border-radius:3px;cursor:pointer;font-size:16px;padding:2px 6px;line-height:1.3}
/* header heartbeat: warm gold glow on the mongoose + sillygoose IMAGES only. The "Mongoose" title text
   keeps its own colour (a gold-tinted title read as wrong). */
#top img{animation:goosePulse 1.9s ease-in-out infinite}
@keyframes goosePulse{0%,100%{filter:drop-shadow(0 0 2px rgba(255,190,90,.4))}50%{filter:drop-shadow(0 0 9px rgba(255,190,90,.95))}}
/* Backstop: nothing in the chat may be wider than the screen. A single unbreakable string (a long guest
   URL, a token) used to push the page wider than the viewport, so the phone browser shrank the WHOLE PWA
   to fit ("everything too fine"). Clamp horizontal overflow so the layout always matches the screen. */
html,body{max-width:100%;overflow-x:hidden}
.say-btn{background:none;border:none;cursor:pointer;padding:0;opacity:.55;font-size:13px;line-height:1;transition:opacity .12s;-webkit-tap-highlight-color:transparent}
.say-btn:hover,.say-btn:active{opacity:1}
.irc-say{margin-right:5px;vertical-align:baseline}
.notif-item.open .notif-body{display:block !important}
/* margin-top clears the absolutely-positioned identity label (top:-18px) from the PTT hint above,
   so the identity label (guest threads) no longer overlaps "Hold to speak". */
#input-row{display:flex;gap:6px;align-items:flex-end;flex-shrink:0;margin-top:14px;
  background:#041004;border:2px solid #000;border-radius:4px;padding:6px 8px;box-shadow:inset 1px 1px 0 #143a14}
.term-prompt{color:#4dff2a;text-shadow:0 0 6px rgba(57,255,20,.65);align-self:center;white-space:nowrap;
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;padding:0 2px 8px}
#msg{flex:1;background:transparent;border:none;color:#4dff2a;caret-color:#4dff2a;
  font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:12px;text-shadow:0 0 5px rgba(57,255,20,.55);
  padding:8px 4px;resize:none;max-height:120px;min-height:38px;overflow-y:hidden}
#msg::placeholder{color:#1f7a12}
/* Hide the textarea scrollbar entirely: a single-line composer with overflow shows a glitchy grey
   vertical scrollbar track right next to the send button. autosize() turns scrolling on only when the
   message actually grows past max-height. */
#msg::-webkit-scrollbar{width:0;height:0}
#msg{scrollbar-width:none}
#msg:focus{outline:none}
#send-btn{width:44px;height:44px;border-radius:6px;background:#0c5c0c;border:1px solid #39ff14;
  display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0;padding:0;box-shadow:0 0 8px rgba(57,255,20,.25)}
#send-btn svg{width:20px;height:20px;fill:#cfffcf}
#send-btn:disabled{opacity:.4;cursor:default}

/* ── PTT BUTTON: compact, label BESIDE the mic (not below) to save vertical space ── */
#ptt-wrap{display:flex;flex-direction:row;align-items:center;gap:9px;flex-shrink:0;padding:2px 0 6px}
#ptt{width:38px;height:38px;border-radius:50%;border:none;background:var(--panel);
  border:2px solid var(--line);cursor:pointer;position:relative;transition:.12s;display:flex;
  align-items:center;justify-content:center;padding:0;flex-shrink:0}
#ptt svg{width:17px;height:17px;fill:var(--dim);transition:.12s}
#ptt.listening{background:#1e2e1e;border-color:var(--ok);box-shadow:0 0 0 6px rgba(124,184,147,.18)}
#ptt.listening svg{fill:var(--ok)}
#ptt.processing{border-color:var(--gold);animation:pulse .9s ease-in-out infinite}
#ptt.processing svg{fill:var(--gold)}
#ptt.error{border-color:var(--bad)}
#ptt.error svg{fill:var(--bad)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(201,163,91,.3)}50%{box-shadow:0 0 0 8px rgba(201,163,91,0)}}
#ptt-hint{font-size:11px;color:var(--dim);text-align:left;min-height:0}
#mic-warn{background:#2e2210;border:1px solid var(--warn);border-radius:10px;padding:10px 12px;
  font-size:13px;color:var(--warn);display:none;text-align:center;line-height:1.4}

/* ── STATUS / HOME PANE ── */
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:13px 14px;margin-bottom:10px}
.card h2{font-size:13px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin:0 0 7px}
.card .body{font-size:15.5px}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.pill{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:3px 9px;margin:2px;font-size:12px;color:var(--dim)}

  /* Poke card: hold-to-poke with a waddling sillygoose that launches when it fires. Ported from the
     browser cockpit so the phone gets the same delight. Touch-first (the launchpad is a phone PWA). */
  .poke-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--line)}
  .poke-row:last-child{border-bottom:none}
  .poke-who{flex:1;font-size:14px;color:var(--fg)}
  .poke-no{font-size:11px;color:var(--dim);font-style:italic}
  .pk-btn{position:relative;overflow:hidden;-webkit-tap-highlight-color:transparent;
    background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--dim);
    font-size:13px;padding:8px 12px;display:flex;align-items:center;gap:8px;cursor:pointer;
    user-select:none;-webkit-user-select:none;touch-action:none;min-width:120px;justify-content:center}
  .pk-btn.holding{border-color:var(--gold);color:var(--gold)}
  .pk-btn.fired{border-color:#39ff14;color:#39ff14}
  .pk-prog{position:absolute;left:0;top:0;bottom:0;width:0%;background:rgba(212,175,55,.18);z-index:0}
  .pk-lbl,.pk-goose{position:relative;z-index:1}
  .pk-goose{display:inline-block;font-size:16px;transition:transform .1s}
  .pk-btn.holding .pk-goose{animation:pkWaddle .35s ease-in-out infinite}
  .pk-btn.fired .pk-goose{animation:pkLaunch .5s ease-out forwards}
  @keyframes pkWaddle{0%,100%{transform:rotate(-8deg) scale(1)}50%{transform:rotate(8deg) scale(1.15)}}
  @keyframes pkLaunch{0%{transform:scale(1) rotate(0);opacity:1}60%{transform:scale(1.5) rotate(-15deg);opacity:1}100%{transform:scale(.8) rotate(10deg) translateX(28px);opacity:0}}
  .pk-recv{font-size:12px;color:var(--dim);padding:3px 0}

  /* Tap responsiveness: every button gives an instant tactile press, and a full-page navigation shows
     an immediate overlay so it never feels dead while the next page loads. */
  @keyframes navspin{to{transform:rotate(360deg)}}
  button:active,.act:active,.ghost:active,.subtab:active,.pk-btn:active,.tile:active{transform:translateY(1px);filter:brightness(.9)}
  #nav-overlay{position:fixed;inset:0;z-index:9999;display:none;flex-direction:column;align-items:center;
    justify-content:center;gap:12px;background:rgba(22,20,15,.94);color:var(--text);font:15px/1.4 system-ui,sans-serif}
  #nav-overlay .sp{width:34px;height:34px;border:3px solid var(--line);border-top-color:var(--gold);border-radius:50%;animation:navspin .8s linear infinite}

  @media (prefers-reduced-motion:reduce){ *,*::before,*::after{ animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important; } }
</style></head><body>
<div id="nav-overlay"><div class="sp"></div><div id="nav-overlay-label">Opening&hellip;</div></div>
<div id="top">
  <img src="/static/img/mongoose-32.png?v=2" alt="">
  <h1>Mongoose</h1>
  <img src="/static/img/sillygoose-256.png" alt="" width="22" height="22" style="opacity:.9">
</div>
<div id="tabs">
  <button class="tab on" onclick="showTab('talk')" id="tab-talk" role="tab" aria-selected="true">Private Chat 🤫</button>
  <button class="tab" onclick="showTab('irc')" id="tab-irc" role="tab" aria-selected="false">IRC 🐦 (public)</button>
  <button class="tab" onclick="showTab('home')" id="tab-home" role="tab" aria-selected="false">Status</button>
  <button class="tab" onclick="showTab('cockpit')" id="tab-cockpit" role="tab" aria-selected="false">Cockpit</button>
</div>
<div id="panes">
  <!-- TALK PANE -->
  <div class="pane on" id="talk-pane">
    <div class="term-title"><img src="/static/img/mongoose-32.png?v=2" alt="">#mongoose<span class="sub">, you &amp; the local brain</span><span class="dots"><b class="mn"></b><b class="mx"></b><b class="cl"></b></span></div>
    <div id="thread" role="log" aria-live="polite" aria-relevant="additions text" aria-label="Conversation"></div>
    <div class="term-hint"><span class="term-emoji" id="term-emoji"></span><span>enter to send &middot; 📎 up to 30MB &middot; runs on the local brain</span></div>
    <div id="mic-warn"></div>
    <div id="ptt-wrap">
      <button id="ptt" aria-label="Hold to speak" title="Hold to speak">
        <svg viewBox="0 0 24 24"><path d="M12 1a4 4 0 0 1 4 4v7a4 4 0 0 1-8 0V5a4 4 0 0 1 4-4zm6.5 10.5A6.5 6.5 0 0 1 5.5 11.5M12 19v4M8 23h8"/></svg>
      </button>
      <div id="ptt-hint">Hold to speak · tap to type</div>
    </div>
    <div id="input-row">
      <div id="chat-identity" aria-live="polite" style="position:absolute;top:-18px;left:12px;font-size:10.5px;color:var(--dim);letter-spacing:.02em"></div>
      <button id="attach-btn" type="button" onclick="document.getElementById('phoneChatFile').click()" aria-label="Attach a file (up to 30MB)" style="background:none;border:none;color:var(--dim);cursor:pointer;padding:0 3px;font-size:12px;align-self:center">📎</button>
      <input type="file" id="phoneChatFile" style="display:none" onchange="attachPhoneChatFile(this)">
      <span class="term-prompt" aria-hidden="true">you&gt;</span>
      <textarea id="msg" rows="1" placeholder="message..." aria-label="Type a message" oninput="autosize(this)"></textarea>
      <button id="send-btn" onclick="sendText()" aria-label="Send" disabled>
        <svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>
      </button>
    </div>
  </div>
  <!-- STATUS PANE -->
  <div class="pane" id="irc-pane">
    <div style="display:flex;flex-direction:column;height:100%">
      <div style="padding:8px 12px 4px;font-size:11px;color:#1f7a12;font-family:'IBM Plex Mono',ui-monospace,monospace">#lounge · everyone here (you, guests, mongoose) shares this one room · mongoose replies when addressed or asked</div>
      <div id="irc-log" style="flex:1;overflow-y:auto;padding:10px 12px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11.5px;line-height:1.5"></div>
      <span class="term-emoji" id="irc-emoji" style="padding:5px 10px 0;background:#041004"></span>
      <div style="display:flex;gap:6px;align-items:center;padding:6px 10px;background:#041004;border-top:2px solid #000">
        <span aria-hidden="true" style="color:#4dff2a;text-shadow:0 0 6px rgba(57,255,20,.65);font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;white-space:nowrap">you&gt;</span>
        <input id="irc-input" placeholder="say something to the room..." aria-label="Say something to the room" autocomplete="off"
               style="flex:1;background:transparent;border:none;outline:none;color:#4dff2a;caret-color:#4dff2a;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;text-shadow:0 0 5px rgba(57,255,20,.55);padding:8px 4px"
               onkeydown="if(event.key==='Enter')ircSend()">
        <button onclick="ircSend()" id="irc-send" aria-label="Send to the room"
                style="background:#0c5c0c;color:#cfffcf;border:1px solid #39ff14;border-radius:6px;padding:9px 16px;font-weight:600;cursor:pointer;box-shadow:0 0 8px rgba(57,255,20,.25)">Send</button>
      </div>
    </div>
  </div>
  <div class="pane" id="home-pane">
    <!-- Find box: jump to any cockpit screen or start a chat action from the owner's daily surface.
         Screen results deep-link to /device/cockpit; chat actions prefill this phone's own chat. -->
    <div class="card" style="margin-bottom:10px">
      <input id="lp-find" type="search" inputmode="search" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false"
             placeholder="Find a screen or action (e.g. reset password, arrears)&hellip;"
             style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:10px;color:var(--text);padding:10px 12px;font:inherit;font-size:15px">
      <div id="lp-find-res" style="margin-top:8px;display:none"></div>
    </div>
    <div id="status-root"><div class="card"><div class="body" style="color:var(--dim)">Loading…</div></div></div>
    <div class="card" style="margin-top:0">
      <h2 style="font-size:13px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin:0 0 7px">Reply voice</h2>
      <div class="body" style="font-size:13px;color:var(--dim);margin-bottom:8px">Choose how Mongoose speaks its replies on this phone.</div>
      <select id="voicePickerPhone" style="width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:10px;color:var(--text);padding:9px;font:inherit">
        <option value="">Loading voices…</option></select>
      <div id="voiceSavedFlashPhone" style="font-size:12px;color:var(--ok,#7BAA6E);opacity:0;transition:opacity .3s;margin-top:6px"></div>
      <button onclick="speechSynthesis.cancel();speak('Hi, I am Mongoose')" style="margin-top:10px;width:100%;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px;color:var(--text);font:inherit;cursor:pointer">Test voice</button>
    </div>
  </div>
  <!-- SYSTEM PANE (owner only) -->
  <div class="pane" id="cockpit-pane">
    <div class="card"><div class="body" style="color:var(--dim);font-size:14px">Every Mongoose control. Tap an area to open it.</div></div>
    <div id="cockpit-tiles" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px"></div>
  </div>
</div>
<script>
// Activate the tab named by the URL hash BEFORE the big (deferred/externalised) bundle loads, so a
// navigation to #home/#cockpit/#irc never flashes the default Chat pane first. This block is small so
// _externalize_assets leaves it inline (only the biggest script is externalised), and it runs
// synchronously during parse. Idempotent with applyTabHash(true) in boot.
(function(){try{var h=(location.hash||'').replace(/^#/,'').toLowerCase();
if(h==='home'||h==='cockpit'||h==='irc'){
  var ps=document.querySelectorAll('.pane'); for(var i=0;i<ps.length;i++)ps[i].classList.remove('on');
  var ts=document.querySelectorAll('.tab'); for(var j=0;j<ts.length;j++)ts[j].classList.remove('on');
  var p=document.getElementById(h+'-pane'); if(p)p.classList.add('on');
  var t=document.getElementById('tab-'+h); if(t)t.classList.add('on');
}}catch(_e){}})();
</script>
<script>
// ── utils ──
(function importPairingHandoff(){
  // A LAN-vs-DDNS cross-origin handoff: /pair may load on a different origin than the
  // launchpad it hands off to (LAN bootstrap -> DDNS canonical app address). localStorage is
  // origin-scoped, so a token stored while pairing on the LAN origin is invisible here on the
  // DDNS origin without this. The fragment is never sent to the server or any request log
  // (unlike a query string), and is stripped immediately after import so it never lingers in
  // history/bookmarks.
  try{
    if(!location.hash || location.hash.indexOf('mongoose_device_token=')<0) return;
    var hp=new URLSearchParams(location.hash.slice(1));
    var tok=hp.get('mongoose_device_token')||'';
    var profile=hp.get('mongoose_device_profile')||'';
    if(tok && tok.length>20){
      localStorage.setItem('mongoose_device_token', tok);
      if(profile) localStorage.setItem('mongoose_device_profile', profile);
      history.replaceState(null, document.title, location.pathname+location.search);
    }
  }catch(_e){}
})();
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
// Full-page navigation with INSTANT feedback: mark the button busy and show an "Opening…" overlay right
// away, so a slow page load never feels like a dead tap. Use for any cross-page button.
function navTo(btn,url,label){
  try{ if(btn){ btn.disabled=true; if(btn.textContent && btn.textContent.indexOf('…')<0){ btn.textContent=(btn.textContent.replace(/→\s*$/,'').trim())+' …'; } } }catch(_e){}
  try{ var ov=document.getElementById('nav-overlay'); if(ov){ var lb=document.getElementById('nav-overlay-label'); if(lb&&label) lb.textContent=label; ov.style.display='flex'; } }catch(_e){}
  try{ location.href=url; }catch(_e){ window.location=url; }
}
function token(){return localStorage.getItem('mongoose_device_token')||''}
async function api(path,opts={}){
  const h=opts.headers||{}
  h['X-Mongoose-Device-Token']=token()
  if(opts.body&&!h['Content-Type'])h['Content-Type']='application/json'
  // Network resilience: every call has a timeout (so a glitch never hangs the UI forever) and
  // one automatic retry for idempotent GETs. A dropped wifi packet shouldn't freeze the app.
  const timeoutMs = opts.timeoutMs || 12000
  const isGet = !opts.method || opts.method.toUpperCase()==='GET'
  const attempts = opts.noRetry ? 1 : (isGet ? 2 : 1)
  let lastErr
  for(let i=0;i<attempts;i++){
    const ctrl = new AbortController()
    const timer = setTimeout(()=>ctrl.abort(), timeoutMs)
    try{
      const r=await fetch(path,{...opts,headers:h,signal:ctrl.signal})
      clearTimeout(timer)
      if(!r.ok){
        let bodyTxt=''; try{ bodyTxt=await r.text(); }catch(_){}
        let msg='';
        try{ const j=JSON.parse(bodyTxt); msg=(j&&(j.message||j.error||j.detail))||''; }catch(_){ msg=bodyTxt; }
        // never surface a raw payload or an owner/localhost-only notice to a phone or guest
        if(!msg || /restricted to localhost|forbidden|MONGOOSE_ALLOW_REMOTE/i.test(msg)) msg="That isn't available on this device.";
        else if(r.status>=500) msg='Mongoose hit a problem. Try again in a moment.';
        else if(msg.length>140) msg=msg.slice(0,140);
        const er=new Error(msg); er.status=r.status; throw er;
      }
      return r.json()
    }catch(e){
      clearTimeout(timer)
      lastErr = (e&&e.name==='AbortError') ? new Error('timed out') : e
      // brief backoff before a retry (only GETs retry)
      if(i<attempts-1) await new Promise(res=>setTimeout(res, 600))
    }
  }
  throw lastErr
}
// ── tabs ──
function loadCockpitTab(){
  var el=document.getElementById('cockpit-tiles'); if(!el||el.dataset.filled)return;
  // KEEP IN STEP with AREAS (the /device/cockpit grid) and `screens` (the launcher search index).
  // Three hardcoded copies of the same list is why the Tasks screen shipped invisible: it was added to
  // the other two and missed here, which is the list the owner actually taps. test_phone_surfaces_agree
  // now fails if they diverge again.
  var T=[['🏢','RTM','rtm'],['✅','Approvals','approvals'],['🗒️','Tasks','tasks'],['📜','Activity','activity'],['🔎','Search','search'],['⏰','Scheduler','scheduler'],['📱','Devices','devices'],['🟢','WhatsApp','whatsapp'],['🛠️','Build','build'],['⚙️','Setup','setup'],['🖥️','System','system'],['📊','Benchmarks','benchmarks']];
  var st='display:flex;flex-direction:column;align-items:center;gap:6px;text-decoration:none;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:16px 8px;min-height:86px;justify-content:center';
  el.innerHTML=T.map(function(x){return '<a href="/device/cockpit#view='+x[2]+'" style="'+st+'"><span style="font-size:26px">'+x[0]+'</span><span style="font-size:13px">'+x[1]+'</span></a>';}).join('');
  el.dataset.filled='1';
}
function showTab(name){
  document.querySelectorAll('.tab').forEach(t=>{ t.classList.remove('on'); t.setAttribute('aria-selected','false'); })
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'))
  const tb=document.getElementById('tab-'+name); if(tb){ tb.classList.add('on'); tb.setAttribute('aria-selected','true'); }
  document.getElementById(name+'-pane').classList.add('on')
  if(name==='home')loadStatus()
  if(name==='cockpit')loadCockpitTab()
  if(name==='irc')ircOpen(); else ircClose();
  // Keep the URL hash in step with the visible tab. Without this the hash kept its old value (e.g.
  // #talk) after the owner tapped Cockpit/Status, so a PWA reload or a foreground bfcache restore ran
  // applyTabHash against the STALE #talk and yanked them back to chat. replaceState does not fire
  // hashchange, so there is no loop.
  try{ if(window.history&&history.replaceState) history.replaceState(null,'',location.pathname+location.search+'#'+name); }catch(_e){}
}
// Deep-link: a notification (e.g. a poke) opens /device/launchpad#irc to land straight in the
// lounge, whether the app was closed (read on load) or already open (hashchange).
// Foreground timestamp. A real notification TAP brings the PWA to the foreground right before it
// navigates, so we can tell an intentional deep-link from a background or programmatic hash flip
// (a stacked chat notification, a service-worker navigate) that would otherwise YANK the owner off
// the tab they are actively reading. Updated on every return to visible.
let _fgAt=Date.now();
document.addEventListener('visibilitychange',()=>{ if(!document.hidden)_fgAt=Date.now(); });
window.addEventListener('pageshow',()=>{ _fgAt=Date.now(); });
function applyTabHash(fromBoot){
  try{
    const h=(location.hash||'').replace(/^#/,'').toLowerCase();
    if(h!=='irc'&&h!=='talk'&&h!=='home'&&h!=='cockpit') return;
    // Never let a hash change pull the view to a chat tab while the owner is actively on another
    // tab in the foreground. Honor it on first load, or within a moment of the app coming to the
    // foreground (a genuine notification tap). Otherwise stay where the owner put themselves.
    if(fromBoot!==true && (h==='talk'||h==='irc')){
      const cur=document.querySelector('.tab.on');
      const curId=cur?cur.id.replace('tab-',''):'';
      if(curId && curId!==h && (Date.now()-_fgAt)>2500) return;
    }
    showTab(h);
  }catch(e){}
}
window.addEventListener('hashchange',()=>applyTabHash(false));
// ── the lounge (IRC tab): one room for the owner, every guest, and mongoose itself ──
let _ircSince=0,_ircTimer=null,_ircMyName='',_ircBusy=false,_ircSending=false,_ircThinking=null,_ircThinkTimer=null;
function _ircClearThinking(){
  if(_ircThinkTimer){ clearTimeout(_ircThinkTimer); _ircThinkTimer=null; }
  if(_ircThinking&&_ircThinking.remove) _ircThinking.remove();
  _ircThinking=null;
}
function _ircShowThinking(){
  // mongoose is composing OFF the request path; show a gentle placeholder that the next poll
  // clears the instant mongoose's line lands. A safety timer removes it if a reply never comes,
  // so a silent model can never leave a '…' stuck in the room forever.
  _ircClearThinking();
  _ircThinking=ircLine({mongoose:true, text:'…', created_at:new Date().toISOString()});
  _ircThinkTimer=setTimeout(_ircClearThinking, 150000);
}
function _ircTime(ts){ try{ const d=new Date(ts.includes('T')?ts:ts.replace(' ','T')+'Z'); return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0'); }catch(e){ return '--:--'; } }
function ircLine(m){
  const log=document.getElementById('irc-log'); if(!log) return;
  const el=document.createElement('div'); el.className='tline';
  const who=m.mongoose?'mongoose':(m.sender||'someone');
  // Phosphor palette, matching the desktop chat: mongoose bright green, you amber, everyone else lime.
  const color=m.mongoose?'#54ff3a':((m.mine!==undefined?m.mine:who===_ircMyName)?'#ffb000':'#63d5ff');
  const t=document.createElement('span'); t.textContent='['+_ircTime(m.created_at||'')+'] '; t.style.color='#1f7a12'; t.style.fontSize='11px';
  const n=document.createElement('span'); n.textContent='<'+who+'> '; n.style.color=color; n.style.fontWeight='600';
  // The local model sometimes echoes its own name into the reply, which would render as the name
  // twice ("<mongoose> mongoose: hi"). Strip a leading self-label on mongoose lines so the room
  // shows the speaker exactly once. This also cleans lines already stored with the old prefix.
  let _body=m.text||'';
  if(m.mongoose) _body=_body.replace(/^\s*(?:mongoose|sillygoose)\s*:\s*/i,'');
  const x=document.createElement('span'); x.innerHTML=lpLinkify(_body);  // links (fortune cards etc) clickable
  // Mongoose lines get a tap-to-play speaker to the LEFT of the timestamp (a tap is the user gesture
  // phones need to allow speech). Uses the voice chosen in the Status tab.
  if(m.mongoose && window.speechSynthesis){
    const say=document.createElement('button'); say.className='say-btn irc-say'; say.type='button';
    say.setAttribute('aria-label','Play aloud'); say.title='Play aloud'; say.textContent='🔊';
    say.onclick=function(e){ e.stopPropagation(); try{ speak(_body); }catch(_e){} };
    el.appendChild(say);
  }
  el.appendChild(t); el.appendChild(n); el.appendChild(x);
  const nearBottom=(log.scrollHeight-log.scrollTop-log.clientHeight)<80;
  log.appendChild(el);
  if(nearBottom) log.scrollTop=log.scrollHeight;
  return el;
}
async function ircPoll(){
  if(_ircBusy || _ircSending) return;  // never poll mid-send: it would re-fetch our optimistic echo
  _ircBusy=true;
  try{
    // Catch up fully: keep pulling while a page comes back full, so returning from background (or a
    // burst while away) lands on "now" in a couple of round-trips instead of 60-per-4s. Advance the
    // cursor ONLY over lines actually rendered (ircLine returns the node), never blindly, so a
    // missing log element can't mark ids seen-but-undrawn and skip them.
    for(let guard=0; guard<25; guard++){
      const r=await api('/device/irc/history?since_id='+_ircSince+'&limit=60',{timeoutMs:10000});
      if(!(r&&r.ok)) break;
      const msgs=r.messages||[]; const before=_ircSince; let mongooseSpoke=false;
      for(const m of msgs){ const node=ircLine(m); if(node){ if(m.id>_ircSince)_ircSince=m.id; if(m.mongoose)mongooseSpoke=true; } }
      if(mongooseSpoke) _ircClearThinking();
      if(msgs.length<60 || _ircSince===before) break;  // reached the tail (or could not advance)
    }
  }catch(e){}
  finally{ _ircBusy=false; }
}
// A surpriser IN the lounge, styled like the private chat but SAFE ONLY (delight/general). The server
// filters to harmless kinds (safe=1), so an owner-private nudge can never reach the guest-visible room.
function ircNotice(text,why){
  const log=document.getElementById('irc-log'); if(!log) return;
  const el=document.createElement('div'); el.className='tline';
  const t=document.createElement('span'); t.textContent='['+_ircTime(new Date().toISOString())+'] '; t.style.color='#1f7a12'; t.style.fontSize='11px';
  const n=document.createElement('span'); n.textContent='<mongoose> '; n.style.color='#8bff6a'; n.style.fontWeight='700'; n.style.textShadow='0 0 7px rgba(139,255,106,.7)';
  const s=document.createElement('span'); s.className='tnotice'; s.innerHTML=lpLinkify(text);
  el.appendChild(t); el.appendChild(n); el.appendChild(s);
  if(why){ const w=document.createElement('span'); w.className='tmem'; w.textContent=' ('+why+')'; el.appendChild(w); }
  const nearBottom=(log.scrollHeight-log.scrollTop-log.clientHeight)<80;
  log.appendChild(el); if(nearBottom) log.scrollTop=log.scrollHeight;
}
const _seenLoungeSurprise=new Set();
async function pollLoungeSurprise(){
  try{ const q=await api('/cockpit/last-surprise?safe=1');
    if(q && q.surfaced && q.suggestion){ const s=q.suggestion, sig=s.signature||s.text||'';
      if(sig && !_seenLoungeSurprise.has(sig)){ _seenLoungeSurprise.add(sig); ircNotice(s.text, s.why||''); } }
  }catch(e){}
}
async function ircOpen(){
  const log=document.getElementById('irc-log');
  if(log && !log.childElementCount){
    try{ const w=await api('/device/whoami',{timeoutMs:8000}); const d=(w&&w.device)||{};
         _ircMyName=(d.display_name||d.name||'').replace(/:/g,'').slice(0,40); }catch(e){}
    _ircSince=0; log.innerHTML='';
    await ircPoll();
    if(!log.childElementCount){ const hint=document.createElement('div'); hint.style.color='var(--dim)';
      hint.textContent='* the room is quiet. say hi, everyone paired with this Mongoose can see it.'; log.appendChild(hint); }
    log.scrollTop=log.scrollHeight;
  }
  try{ pollLoungeSurprise(); }catch(_e){}   // a safe (general) surpriser in the lounge, once, deduped
  if(!_ircTimer) _ircTimer=setInterval(()=>{
    const pane=document.getElementById('irc-pane');
    if(pane && pane.classList.contains('on') && !document.hidden) ircPoll();
  }, 4000);
  // Poll the instant the app comes back to the foreground, so returning to a live room does not
  // wait up to 4s (and then catches up in one go via ircPoll's loop).
  if(!window.__ircVis){ window.__ircVis=true; document.addEventListener('visibilitychange',()=>{
    const pane=document.getElementById('irc-pane');
    if(!document.hidden && pane && pane.classList.contains('on')) ircPoll();
  }); }
}
function ircClose(){ if(_ircTimer){ clearInterval(_ircTimer); _ircTimer=null; } _ircClearThinking(); }
// ── Lounge send: durable outbox so a message is NEVER lost on a bad connection ──
// The message goes into a localStorage queue the instant you hit Send, so it survives signal loss and
// even a page reload. Each carries a stable client_msg_id, so a retry is deduped server-side and can
// never double-post. On a network drop the line stays queued (with a clear "queued" note, not a
// "disappeared" one) and auto-sends the moment you are back online.
const IRC_OUTBOX_KEY='pc_irc_outbox';
function ircGetOutbox(){ try{ return JSON.parse(localStorage.getItem(IRC_OUTBOX_KEY)||'[]'); }catch(e){ return []; } }
function ircSetOutbox(q){ try{ localStorage.setItem(IRC_OUTBOX_KEY, JSON.stringify(q)); }catch(e){} }
function ircNewId(){ return 'irc-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8); }

async function ircSend(){
  const inp=document.getElementById('irc-input');
  const text=(inp&&inp.value||'').trim(); if(!text) return;
  const q=ircGetOutbox(); q.push({id:ircNewId(), text:text, ts:Date.now()}); ircSetOutbox(q);
  if(inp){ inp.value=''; }               // safe to clear: the message now lives in the durable outbox
  await ircFlushOutbox();
  if(inp) inp.focus();
}

async function ircFlushOutbox(){
  if(_ircSending) return;
  if(typeof token==='function' && !token()) return;
  const btn=document.getElementById('irc-send');
  let q=ircGetOutbox();
  // Drop stale queued lines (>15 min): almost always already delivered, replaying them resurrects old text.
  q=q.filter(function(it){ return !it.ts || (Date.now()-it.ts)<15*60*1000; }); ircSetOutbox(q);
  while(q.length){
    const item=q[0];
    _ircSending=true; if(btn) btn.disabled=true;
    let myLine=null;
    try{
      myLine=ircLine({sender:_ircMyName||'you', text:item.text, mine:true, created_at:new Date().toISOString()});
      const r=await api('/device/irc/send',{method:'POST',body:JSON.stringify({text:item.text, client_msg_id:item.id}),timeoutMs:30000,noRetry:true});
      if(myLine&&myLine.remove) myLine.remove();
      _ircSending=false;
      if(r&&r.ok===false){
        // Server rejected the CONTENT (e.g. too long): drop it from the queue and hand the text back so
        // it can be edited, rather than retrying a message that will always be refused.
        q.shift(); ircSetOutbox(q);
        ircLine({sender:'*',text:r.error||'Message not sent.',created_at:new Date().toISOString()});
        if(inp && !inp.value) inp.value=item.text;
        break;
      }
      q.shift(); ircSetOutbox(q);            // delivered (or deduped): remove from the queue
      await ircPoll();
      if(r&&r.pending) _ircShowThinking();
    }catch(e){
      // Network drop: leave it in the queue and stop. It sends automatically on reconnect (or next send).
      if(myLine&&myLine.remove) myLine.remove();
      _ircSending=false;
      ircLine({sender:'*',text:'No signal right now, your message is queued and will send itself when you are back online.',created_at:new Date().toISOString()});
      break;
    }
    q=ircGetOutbox();
  }
  _ircSending=false;
  if(btn) btn.disabled=false;
}
// Auto-send the queue the moment connectivity returns, and once on load in case a message was left
// queued from a previous poor-signal session.
window.addEventListener('online', function(){ try{ ircFlushOutbox(); }catch(e){} });
try{ setTimeout(function(){ try{ ircFlushOutbox(); }catch(e){} }, 1500); }catch(e){}
// ── thread rendering ──
const thread=document.getElementById('thread')
// Escape (XSS-safe) THEN turn full URLs and root-relative download links (/rtm/doc, /dl) into
// tappable anchors, so a link Mongoose hands back in chat (a fortune card, a document) is clickable
// instead of dead text.
function lpLinkify(t){
  var s=esc(t);
  s=s.replace(/(https?:\/\/[^\s<]+[^\s<.,;:)])/g,'<a href="$1" target="_blank" rel="noopener" style="color:var(--gold);text-decoration:underline">$1</a>');
  s=s.replace(/(^|[\s(\n])(\/(?:rtm\/doc|dl)\/[A-Za-z0-9_\-]+)/g,'$1<a href="$2" target="_blank" rel="noopener" style="color:var(--gold);text-decoration:underline">Open link</a>');
  return s;
}
function _ts(iso){ var d; try{ d=iso?new Date(iso):new Date(); if(isNaN(d.getTime()))d=new Date(); }catch(_e){ d=new Date(); }
  return '['+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+'] '; }
function addBubble(role,text,iso){
  // Terminal line, matching the desktop chat EXACTLY: "[HH:MM] <you>" (amber) / "[HH:MM] <mongoose>"
  // (green) / a dim italic "* system" line. lpLinkify escapes + linkifies, so the body is safe HTML.
  const d=document.createElement('div')
  d.className='tline t-'+(role==='user'?'you':role==='assistant'?'bot':'sys')
  // Mongoose's own replies get a tap-to-play speaker at the START of the line (a TAP is the user
  // gesture phones need to allow speech; auto-speak after an async reply is blocked by the browser).
  if(role==='assistant' && window.speechSynthesis){
    const pb=document.createElement('button'); pb.className='say-btn irc-say'; pb.type='button';
    pb.setAttribute('aria-label','Play aloud'); pb.title='Play aloud'; pb.textContent='🔊';
    pb.onclick=function(e){ e.stopPropagation(); try{ speak(text); }catch(_e){} };
    d.appendChild(pb);
  }
  if(role==='sys'){
    const s=document.createElement('span'); s.className='tsys'; s.innerHTML='* '+lpLinkify(text); d.appendChild(s);
  }else{
    const t=document.createElement('span'); t.className='tts'; t.textContent=_ts(iso); d.appendChild(t);
    const n=document.createElement('span'); n.className=(role==='user')?'tnick-you':'tnick-bot';
    n.textContent=(role==='user')?'<you> ':'<mongoose> '; d.appendChild(n);
    const b=document.createElement('span'); b.innerHTML=lpLinkify(text); d.appendChild(b);
    d._body=b;  // so a later reply update can set the BODY, not the whole bubble (which would wipe the nick)
  }
  thread.appendChild(d)
  thread.scrollTop=thread.scrollHeight
  return d
}
// Update a bubble's text WITHOUT destroying its nick/timestamp/speaker spans. Setting .textContent on
// the whole bubble div wiped all of them, so a reply that arrived via the slow job/poll path rendered
// as bare text with no "<mongoose>" label (the second-reply-lost-its-nick bug). Write the body span.
function setBubbleText(bubble,text){
  if(bubble && bubble._body){ bubble._body.innerHTML=lpLinkify(text); }
  else if(bubble){ bubble.textContent=text; }
  thread.scrollTop=thread.scrollHeight;
}
function addNotice(text,why){
  // A proactive surpriser line, formatted exactly like the desktop chat: "mongoose>" then the notice
  // in soft gold, then an optional grey-italic aside (the "why").
  const d=document.createElement('div'); d.className='tline'
  const t=document.createElement('span'); t.className='tts'; t.textContent=_ts(); d.appendChild(t)
  const n=document.createElement('span'); n.className='tnick-bot'; n.textContent='<mongoose> '; d.appendChild(n)
  const s=document.createElement('span'); s.className='tnotice'; s.innerHTML=lpLinkify(text); d.appendChild(s)
  if(why){ const w=document.createElement('span'); w.className='tmem'; w.textContent=' ('+why+')'; d.appendChild(w) }
  thread.appendChild(d); thread.scrollTop=thread.scrollHeight; return d
}
// Emoji quick-bar: a small curated set (taps insert into the given input). Shared by BOTH chats.
var _EMO=["🙂","😄","👍","🔥","🙌","🤔","💚","🐭"];
function _fillEmoji(barId, inputId, after){
  var bar=document.getElementById(barId); if(!bar||bar.dataset.filled) return; bar.dataset.filled='1';
  _EMO.forEach(function(e){ var b=document.createElement('button'); b.type='button'; b.textContent=e;
    b.onclick=function(){ var m=document.getElementById(inputId); if(!m) return; m.value+=e; m.focus(); if(after){ try{ after(m); }catch(_e){} } };
    bar.appendChild(b); }); }
_fillEmoji('term-emoji','msg',function(m){ try{ autosize(m); }catch(_e){} var sb=document.getElementById('send-btn'); if(sb) sb.disabled=!m.value.trim(); });
_fillEmoji('irc-emoji','irc-input',null);
const _seenSurprise=new Set()
let _localNotices=[]  // proactive surpriser lines are client-only (never in server history), so track them
                      // to re-append on the 4s rebuild, else they vanish. Expire after 10 min.
let _surpriseSeeded=false
function _showSurprise(s){ const sig=(s&&(s.signature||s.text))||''; if(sig && !_seenSurprise.has(sig)){ _seenSurprise.add(sig); addNotice(s.text, s.why||''); _localNotices.push({text:s.text, why:s.why||'', ts:Date.now()}); _localNotices=_localNotices.filter(n=>Date.now()-n.ts<600000); } }
async function pollSurprise(){
  // The surpriser IN the phone chat, matching the desktop: proactively surface a genuinely useful notice
  // (the endpoint returns surfaced=false when there is nothing worth saying), never the same one twice.
  try{
    const r=await api('/cockpit/suggest')  // open endpoint; api's device token is harmless here
    if(r && r.surfaced && r.suggestion){ _showSurprise(r.suggestion); return }
    // Nothing live (the shared shape-cooldown). On the FIRST poll, show the most recent REAL surpriser
    // once, so its formatted style is actually visible instead of never appearing.
    if(!_surpriseSeeded){ _surpriseSeeded=true;
      const q=await api('/cockpit/last-surprise'); if(q && q.surfaced && q.suggestion) _showSurprise(q.suggestion);
    }
  }catch(e){}
}
let _histSig=''
let _histErrShown=false  // guards the one-off "could not load history" notice so a freeze cannot stack it
let _pendingJobs=0
// Messages THIS device has sent but not yet seen echoed back in the server's history. A background
// history rebuild must NEVER drop a message you already sent and can see: a vanishing sent message is
// the single most alarming chat bug (it happened when a turn was deduped or routed without persisting a
// chat turn, so the next poll rebuilt the thread without it). We re-pin these on every rebuild until the
// server confirms them, then expire after a bounded window so a genuinely lost send does not pin forever.
let _localSent=[]  // {id, text, ts}
function _pinLocalSent(id,text){ try{ if(!text)return; _localSent.push({id:id||'',text:text,ts:Date.now()}); if(_localSent.length>50)_localSent.shift(); }catch(_e){} }
function _localSentFresh(){ const now=Date.now(); _localSent=_localSent.filter(m=>(now-m.ts)<OUTBOX_MAX_AGE_MS); return _localSent }
var _histAt=0;
async function loadHistory(){
  // Coalesce a rapid double-load: on boot the chat loads once, then the immediate connProbe() heartbeat
  // succeeds and _refreshActiveTab() reloads it, so it flashed. A second call within 1.2s is dropped; the
  // 4s live poll and the foreground-return refresh are both well outside that window, so they still run.
  var _now=Date.now();
  if(_now-_histAt<1200) return;
  _histAt=_now;
  try{
    const d=await api('/device/chat/history?limit=30')
    let msgs=[...(d.messages||[])]
    // Authoritative render order + dedupe: the monotonic server `id`. Order STRICTLY by id ascending
    // (never by created_at, which ties across concurrent turns and made bubbles shuffle between polls),
    // and drop any duplicate id so a message can never render twice. The atomic per-conversation turn
    // lock on the server guarantees ids reflect true turn order; this makes the DISPLAY honour it.
    msgs.sort((a,b)=>((a.id||0)-(b.id||0)))
    const _seenIds=new Set()
    msgs=msgs.filter(m=>{ const k=(m.id!=null)?('#'+m.id):('t:'+m.role+':'+((m.content||m.text||''))); if(_seenIds.has(k))return false; _seenIds.add(k); return true })
    // Change-detect so the 4s poll never flickers a thread that hasn't moved, and never
    // rebuilds while a pending-job bubble is live (rebuilding would destroy it). Keyed on id so a
    // genuine reorder is always redrawn.
    const sig=JSON.stringify(msgs.map(m=>[m.id||0,m.role,(m.sender||''),(m.content||m.text||'').slice(0,80)]))
    if(sig===_histSig || _pendingJobs>0){ _histSig=sig; return }
    // Never wipe the visible thread to render an EMPTY history. Right after sending, the client
    // bubbles (your message + the reply) can be ahead of the server's persisted copy, and a poll
    // that briefly returned nothing once flashed the whole thread empty before it repopulated a
    // few seconds later. If the server has nothing yet but something is on screen, keep it.
    if(!msgs.length && thread.children.length>0){ return }
    _histSig=sig
    _histErrShown=false  // history loaded fine: allow one fresh notice if it later fails again
    // Only re-pin to the bottom if the owner was ALREADY at the bottom. If they scrolled up to read
    // earlier messages, a poll that lands a new bubble must not yank them back down mid-read.
    const _atBottom=(thread.scrollHeight-thread.scrollTop-thread.clientHeight)<80
    thread.innerHTML=''
    // Sender labels (register B2/privacy): the history API marks messages another device
    // wrote; render the label so someone else's words are never indistinguishable from yours.
    msgs.forEach(m=>addBubble(m.role,(m.sender?('['+m.sender+'] '):'')+(m.content||m.text||''),m.created_at))
    // Re-pin any of THIS device's recently-sent messages the server has not echoed yet, so a rebuild
    // never erases a message you already sent. Drop the ones the server now includes (confirmed).
    const _serverUserTexts=new Set(msgs.filter(m=>m.role==='user').map(m=>(m.content||m.text||'').trim()))
    _localSent=_localSentFresh().filter(m=>!_serverUserTexts.has(m.text.trim()))
    _localSent.forEach(m=>addBubble('user',m.text))
    // Re-append recent surpriser notices: they are client-only (never in server history), so a rebuild
    // would otherwise wipe the proactive line and break the chat-room feel. Expire after 10 min.
    _localNotices=_localNotices.filter(n=>Date.now()-n.ts<600000)
    _localNotices.forEach(n=>addNotice(n.text,n.why))
    if(_atBottom) thread.scrollTop=thread.scrollHeight
  }catch(e){
    // Show the notice at most ONCE per outage, not once per 4s poll: a sustained freeze used to
    // stack up an identical "(could not load history)" bubble every cycle. The reconnecting banner
    // and the send-outbox already signal the outage; this is just a one-off thread hint.
    if(!_histSig && !_histErrShown){ addBubble('sys','(could not load history)'); _histErrShown=true }
  }
}
// Talk live-refresh (register C8, reimplemented here so the release carries it): poll every
// 4 seconds while the tab is visible and Talk is the active pane, and refresh immediately
// when the app regains focus, so the pane never sits on stale state like the PWA once did.
setInterval(()=>{ try{
  if(document.hidden) return
  const t=document.getElementById('talk-pane')
  if(t && t.classList.contains('on')) loadHistory()
}catch(e){} },4000)
document.addEventListener('visibilitychange',()=>{ if(!document.hidden){ try{ loadHistory() }catch(e){} } })
async function ensureNamed(){
  // Register B2: a re-paired device with no saved name renders its person as 'someone' in
  // IRC. Prompt exactly the unnamed non-guest devices, once, with a one-field bar.
  try{
    const d=await api('/device/self/name',{timeoutMs:8000})
    if(!d||!d.ok) return
    if((d.display_name||'').trim()) return
    if(String(d.profile||'').indexOf('guest')>=0) return
    const bar=document.createElement('div')
    bar.id='name-bar'
    bar.style.cssText='display:flex;gap:6px;align-items:center;padding:8px;background:rgba(128,128,0,.15);border-radius:10px;margin:6px 0'
    bar.innerHTML='<span style="font-size:13px">What should I call you?</span>'+
      '<input id="name-input" type="text" maxlength="40" style="flex:1;min-width:90px" placeholder="e.g. {{owner.name}}">'+
      '<button class="act" onclick="saveMyName(this)">Save</button>'
    thread.parentNode.insertBefore(bar,thread)
  }catch(e){}
}
async function saveMyName(btn){
  const v=(document.getElementById('name-input')||{}).value||''
  if(!v.trim())return
  if(btn)btn.disabled=true
  try{
    const d=await api('/device/self/name',{method:'POST',body:JSON.stringify({name:v.trim()})})
    if(d&&d.ok){ const b=document.getElementById('name-bar'); if(b)b.remove() }
    else if(btn)btn.disabled=false
  }catch(e){ if(btn)btn.disabled=false }
}
ensureNamed();
// Deep link: another surface (e.g. the RTM tab's "Build / update code" button) can send the owner
// here with the chat pre-filled via /device/launchpad#say=<text>. Fill it in and focus, do NOT
// auto-send (the plugin build is a deliberate, confirm-gated action).
// LEADING SEMICOLON IS LOAD-BEARING: without it, `ensureNamed()` + this `(function(){...})()` parse as
// `ensureNamed()(...)` (ASI does not insert a semicolon before `(`), a TypeError at load that halted
// the whole chat init so the send button stopped responding. Never start a line with `(` after a call.
;(function(){ try{
  if(!location.hash || location.hash.indexOf('say=')<0) return;
  var say=new URLSearchParams(location.hash.slice(1)).get('say');
  if(!say) return;
  try{ showTab('talk'); }catch(_e){}
  var t=document.getElementById('msg');
  if(t){ t.value=say; try{ autosize(t); }catch(_e2){} t.focus();
    try{ var sb=document.getElementById('send-btn'); if(sb) sb.disabled=!t.value.trim(); }catch(_e3){} }
}catch(_e){} })();
// ── text chat ──
const msgEl=document.getElementById('msg')
const sendBtn=document.getElementById('send-btn')
msgEl.addEventListener('input',()=>sendBtn.disabled=!msgEl.value.trim())
msgEl.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendText()}})
function autosize(el){el.style.height='auto';var sh=el.scrollHeight;el.style.height=Math.min(sh,120)+'px';
  el.style.overflowY=sh>120?'auto':'hidden';
  // Belt-and-braces: keep the send button in sync on every input, even if the addEventListener above
  // did not attach (script-ordering), so a typed message can always be sent.
  try{var sb=document.getElementById('send-btn');if(sb)sb.disabled=!el.value.trim();}catch(_e){}}
// Attach a file to the phone chat (<= 30MB). Body-data CSVs are imported; other files are stored and
// referenceable. Files over 30MB are bounced to large-file staging (the Mac cockpit System tab).
async function attachPhoneChatFile(input){
  const file=input&&input.files&&input.files[0]; if(!file) return;
  input.value='';
  const sizeMB=(file.size/(1024*1024));
  if(sizeMB>30){ addBubble('sys', file.name+' is '+sizeMB.toFixed(0)+'MB: too big for a chat attachment (30MB max). Use large-file staging on the Mac cockpit (up to 200MB).'); return; }
  addBubble('user','📎 '+file.name+' ('+sizeMB.toFixed(1)+'MB)');
  const pending=addBubble('sys','uploading '+file.name+'…');
  try{
    const r=await fetch('/device/chat/attach',{method:'POST',headers:{'X-Mongoose-Device-Token':token(),'X-Filename':file.name,'Content-Type':file.type||'application/octet-stream'},body:file});
    let d={}; try{ d=await r.json(); }catch(_){ d={ok:false,error:'no response'}; }
    if(pending&&pending.remove) pending.remove();
    if(!d.ok){ addBubble('sys', d.error||'upload failed'); return; }
    if(d.lane==='body_data'){ addBubble('assistant','Imported your '+(d.detected||'data')+(d.imported!=null?(' ('+d.imported+' rows)'):'')+'. That makes the body features real.'); }
    else { addBubble('assistant', d.summary||('Saved '+(d.filename||file.name)+'.')); }
  }catch(e){ if(pending&&pending.remove) pending.remove(); addBubble('sys','upload failed: '+(e.message||e)); }
}
// ── Offline outbox: queue text when offline / on timeout, flush on reconnect ──────────────────
const OUTBOX_PREFIX='mongoose_device_outbox_v2:'
function outboxKey(){
  // Scope queued messages to the paired device token. Otherwise an offline owner message can be
  // flushed later after the browser is re-paired as a guest/new device. The token already lives in
  // localStorage; using a short suffix in the key avoids cross-account leakage without adding a secret.
  const t=token(); return OUTBOX_PREFIX+(t?t.slice(-12):'anon')
}
function legacyOutboxKey(){return 'mongoose_device_outbox_v1'}
function getOutbox(){
  try{
    let q=JSON.parse(localStorage.getItem(outboxKey())||'[]')
    if(!q.length){
      const legacy=JSON.parse(localStorage.getItem(legacyOutboxKey())||'[]')
      if(legacy.length && token()){ q=legacy.map(x=>Object.assign({displayed:false},x)); setOutbox(q); localStorage.removeItem(legacyOutboxKey()) }
    }
    return Array.isArray(q)?q:[]
  }catch(_){return[]}
}
function setOutbox(items){try{localStorage.setItem(outboxKey(),JSON.stringify((items||[]).slice(-20)))}catch(_){}}
function _newMsgId(){ return Date.now().toString(36)+Math.random().toString(36).slice(2,8); }
function queueText(text, displayed, id){const q=getOutbox();q.push({text,ts:Date.now(),displayed:!!displayed,id:id||_newMsgId()});setOutbox(q)}
function clearAllOutboxes(){
  try{
    const keys=[]; for(let i=0;i<localStorage.length;i++){ const k=localStorage.key(i); if(k && (k===legacyOutboxKey() || k.indexOf(OUTBOX_PREFIX)===0)) keys.push(k); }
    keys.forEach(k=>localStorage.removeItem(k));
  }catch(_){}
}
async function pollChatJob(jobId,bubble){
  _pendingJobs++
  try{ return await _pollChatJobInner(jobId,bubble) } finally { _pendingJobs=Math.max(0,_pendingJobs-1); _histSig='' }
}
async function _pollChatJobInner(jobId,bubble){
  // The server never cancels a slow turn; it hands back a job id and the reply lands here.
  // Poll every 2 seconds (a live incident showed a 15-25s "ask again" dead end losing both
  // the reply and, via cancellation, the message itself). The bubble is a live status with
  // a visible elapsed count, never a fake reply.
  const started=Date.now()
  for(let i=0;i<150;i++){
    await new Promise(r=>setTimeout(r,2000))
    try{
      const s=await api('/device/chat/poll?job_id='+encodeURIComponent(jobId),{timeoutMs:8000,noRetry:true})
      if(s&&s.done){
        setBubbleText(bubble, s.reply||s.error||'(no reply)')
        return
      }
      const secs=Math.round((Date.now()-started)/1000)
      setBubbleText(bubble, 'Still thinking… '+secs+'s (the reply will appear here)')
    }catch(e){ /* transient poll failure: keep polling; history has the reply as backstop */ }
  }
  bubble.className='bubble sys'
  setBubbleText(bubble, 'This is taking unusually long. Pull down to refresh: if the brain finished, the reply is in the history.')
}

const OUTBOX_MAX_AGE_MS=15*60*1000; // 15 min: after this a queued send is stale, do not replay it
async function flushOutbox(){
  if(!token()||!navigator.onLine)return
  let q=getOutbox(); if(!q.length)return
  // Drop STALE queued messages first. A send that never completed within 15 minutes is almost always a
  // long-running turn the brain ALREADY handled: the plugin build holds the request open for minutes,
  // so the phone times out and queues it, and replaying it hours later resurrects an old request as if
  // it were new (the server dedups it with "already on that one", pointing at something from hours ago,
  // and the stale replay clobbers what is on screen). Never replay something that old.
  const _fresh=q.filter(it=>!it.ts||(Date.now()-it.ts)<OUTBOX_MAX_AGE_MS);
  if(_fresh.length!==q.length){ q=_fresh; setOutbox(q); if(!q.length)return; }
  // send oldest-first; stop on the first failure so order is preserved. If the message was already
  // shown when queued offline, don't draw the user bubble a second time on reconnect.
  while(q.length){
    const item=q[0]
    try{
      const d=await api('/device/chat',{method:'POST',body:JSON.stringify({message:item.text,client_msg_id:item.id||''}),timeoutMs:90000,noRetry:true})
      if(!item.displayed) addBubble('user',item.text)
      _pinLocalSent(item.id,item.text)  // re-pin on (re)send so a rebuild can't wipe a reconnected message
      // DUPLICATE: the server already handled this exact message (same client_msg_id) and is only
      // re-reporting its answer, which is ALREADY on screen and already persisted. Drawing it again
      // is what printed mongoose's reply twice, a minute apart, from one stored row. Drop the queued
      // item and let the history poll render the canonical copy; never add a second bubble.
      if(d&&d.duplicate){ q.shift(); setOutbox(q); _histSig=''; try{ loadHistory() }catch(_e){} continue }
      if(d&&d.pending&&d.job_id){ const b=addBubble('assistant','…'); q.shift(); setOutbox(q); await pollChatJob(d.job_id,b); continue }
      addBubble('assistant',d.reply||'(no reply)')
      q.shift(); setOutbox(q)
    }catch(e){ break }
  }
  thread.scrollTop=thread.scrollHeight
}

async function sendText(){
  const txt=msgEl.value.trim(); if(!txt)return
  msgEl.value=''; msgEl.style.height='auto'; sendBtn.disabled=true
  // ONE idempotency id per composed message, reused on every retry, so the server dedups a re-send
  // whose reply was merely lost, WITHOUT ever blocking a genuinely re-typed identical message
  // (that gets a fresh id).
  const mid=_newMsgId();
  // If the phone is offline, queue immediately rather than spinning.
  if(!navigator.onLine){
    queueText(txt,true,mid)
    addBubble('user',txt); _pinLocalSent(mid,txt)
    addBubble('sys','Offline. Queued, it will send when you reconnect.')
    msgEl.disabled=false; thread.scrollTop=thread.scrollHeight; return
  }
  addBubble('user',txt); _pinLocalSent(mid,txt)
  const thinking=addBubble('assistant','…')
  // Protect the live bubbles from the 4s history-rebuild for the WHOLE send, not just during
  // pollChatJob. /device/chat itself holds for up to 25s (the guest chat budget) before it hands
  // back a job id, and during that await the poll would rebuild history and wipe this thinking
  // bubble (it appeared, vanished at ~5s, and the reply only surfaced on a later poll ~2min in).
  // Holding _pendingJobs>0 across the whole send keeps the bubble in situ and updated in place.
  _pendingJobs++
  try{
    // chat can legitimately take a while (the model is thinking), so allow a generous timeout;
    // still bounded so a truly dead connection doesn't hang the bubble forever.
    const d=await api('/device/chat',{method:'POST',body:JSON.stringify({message:txt,client_msg_id:mid}),timeoutMs:90000,noRetry:true})
    if(d&&d.pending&&d.job_id){ await pollChatJob(d.job_id,thinking) }
    else{ setBubbleText(thinking, d.reply||'(no reply)') }
  }catch(e){
    // Timed out or unreachable: queue it (SAME id) so a retry can't create a duplicate turn.
    queueText(txt,true,mid)
    thinking.className='bubble sys'
    thinking.textContent = (e&&e.message==='timed out')
      ? 'That took too long. Queued, it will retry when the connection is back.'
      : 'Could not reach Mongoose. Queued, it will retry when you reconnect.';
  } finally {
    _pendingJobs=Math.max(0,_pendingJobs-1); _histSig='';
    msgEl.disabled=false; // never leave the field stuck
  }
  thread.scrollTop=thread.scrollHeight
}
// ── push-to-talk ──
const ptt=document.getElementById('ptt')
const pttHint=document.getElementById('ptt-hint')
const micWarn=document.getElementById('mic-warn')
let mediaRec=null, chunks=[], pttActive=false
let _recMime='audio/webm', _recStartedAt=0
function setPttState(s){
  ptt.className=s
  const hints={'':"Hold to speak · tap to type",listening:"Recording… release to send",processing:"Thinking…",error:"Tap to try again"}
  pttHint.textContent=hints[s]||''
}
function dismissMicWarn(){
  const w=document.getElementById('mic-warn');
  if(w) w.style.display='none';
  window._micWarnDismissed=true;
}
async function startRecording(){
  micWarn.style.display='none'
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
    if(window._micWarnDismissed){ setPttState('error'); pttHint.textContent='Type a message instead, mic needs HTTPS.'; return; }
    const lanIp = window.location.hostname;
    const port  = window.location.port || '8765';
    micWarn.innerHTML=`<button onclick="dismissMicWarn()" aria-label="Dismiss" style="position:absolute;top:6px;right:8px;background:none;border:none;color:var(--gold);font-size:20px;line-height:1;cursor:pointer;padding:2px 6px">×</button>
      <b>Microphone needs HTTPS.</b><br>
      To enable it:<br>
      1. On your Mac, open <code style="color:var(--gold)">https://127.0.0.1:${port}/ui#setup</code><br>
      2. Go to the <b>Setup</b> tab → <b>Voice on phones (HTTPS)</b> → <b>Set up phone HTTPS</b><br>
      3. Follow the one-time certificate install step shown there<br>
      Then come back to this page, it will reload automatically. <span class="muted" style="font-size:12px">(You can dismiss this and just type for now.)</span>`;
    micWarn.style.position='relative';
    micWarn.style.display='block'; return
  }
  try{
    let stream
    try{
      stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,sampleRate:48000,echoCancellation:true,noiseSuppression:true,autoGainControl:true}})
    }catch(_e){
      stream=await navigator.mediaDevices.getUserMedia({audio:true})
    }
    // Pick a MIME the browser actually supports (iOS Safari gives mp4/aac, Chrome webm/opus).
    const mime=pickAudioMime()
    chunks=[]; mediaRec=new MediaRecorder(stream, mime?{mimeType:mime,audioBitsPerSecond:128000}:undefined)
    _recMime = mime || (mediaRec.mimeType||'').split(';')[0] || 'audio/webm'
    _recStartedAt = Date.now()
    mediaRec.ondataavailable=e=>{if(e.data.size>0)chunks.push(e.data)}
    mediaRec.onstop=()=>sendVoice(stream)
    mediaRec.start(); setPttState('listening')
  }catch(e){
    if(e.name==='NotAllowedError'){micWarn.textContent='Microphone permission denied. Allow it in your browser settings, then try again.'}
    else if(e.name==='NotFoundError'){micWarn.textContent='No microphone found on this device.'}
    else{micWarn.textContent='Could not access microphone: '+e.message}
    micWarn.style.display='block'; setPttState('error')
  }
}
function pickAudioMime(){
  if(!window.MediaRecorder||!MediaRecorder.isTypeSupported)return ''
  const candidates=['audio/webm;codecs=opus','audio/webm','audio/mp4','audio/aac','audio/ogg;codecs=opus','audio/ogg']
  return candidates.find(m=>MediaRecorder.isTypeSupported(m))||''
}
function stopRecording(){if(mediaRec&&mediaRec.state==='recording'){mediaRec.stop()}}
async function sendVoice(stream){
  stream.getTracks().forEach(t=>t.stop())
  // Short-recording guard: an accidental tap shouldn't send junk audio.
  if(!chunks.length || (Date.now()-(_recStartedAt||0))<350){
    setPttState(''); pttHint.textContent='Hold to speak · tap to type'; return
  }
  setPttState('processing')
  const mime=(_recMime||'audio/webm').split(';')[0]
  const blob=new Blob(chunks,{type:mime})
  const me=addBubble('user','🎤 …')
  const rep=addBubble('assistant','…')
  try{
    const h={'X-Mongoose-Device-Token':token(),'X-Audio-Mime':mime}
    // bounded: a stuck upload or cold model shouldn't hang the bubble forever
    const ctrl=new AbortController(); const to=setTimeout(()=>ctrl.abort(),90000)
    let r;
    try{ r=await fetch('/device/voice',{method:'POST',headers:h,body:blob,signal:ctrl.signal}) }
    finally{ clearTimeout(to) }
    const d=await r.json().catch(()=>({ok:false,error:'bad voice response'}))
    if(d.whisper_missing){micWarn.innerHTML='Voice transcription not set up on the Mac yet. <a href="/" target="_blank" style="color:var(--gold)">Open Mongoose on the Mac</a> → the Setup tab → Voice transcription → Install.';micWarn.style.display='block';me.remove();rep.remove();setPttState('');return}
    if(d.transient){me.remove();rep.remove();setPttState('error');pttHint.textContent=d.error||'Voice is slow right now, type for now';setTimeout(()=>{setPttState('');pttHint.textContent='Hold to speak · tap to type'},2500);return}
    if(!d.ok){me.textContent='🎤 (error)';rep.textContent='Error: '+(d.error||'unknown');setPttState('error');setTimeout(()=>setPttState(''),2000);return}
    if(!d.transcript){me.remove();rep.remove();setPttState('');pttHint.textContent="Didn't catch that, try again";setTimeout(()=>setPttState(''),1500);return}
    setBubbleText(me, '🎤 '+d.transcript);
    if(d.pending && d.job_id){ setBubbleText(rep, '…'); setPttState(''); thread.scrollTop=thread.scrollHeight; pollChatJob(d.job_id, rep); return; }
    setBubbleText(rep, d.reply||'(no reply)')
    if(d.slow && rep){ rep.className='bubble sys' }
  }catch(e){
    me.textContent='🎤 (error)'
    rep.textContent = (e&&e.name==='AbortError') ? 'Voice took too long, try again or type.' : ('Could not send: '+(e.message||e))
    rep.className='bubble sys'
  }
  setPttState(''); thread.scrollTop=thread.scrollHeight
}
// speak reply with device TTS using the user's preferred voice
const VOICE_PREF_KEY='mongoose_tts_voice'
function loadVoicesPhone(){
  const sel=document.getElementById('voicePickerPhone'); if(!sel||!window.speechSynthesis) return;
  let voices=speechSynthesis.getVoices(); if(!voices.length) return;
  // English voices only (Mongoose replies in English; a French voice reads English in a French
  // accent). If the phone has no English MALE voice, that is an OS thing: add one in the phone's
  // Text-to-Speech / Spoken Content settings. The app can only offer voices the phone has installed.
  const _en=voices.filter(v=>/^en(-|_|$)/i.test(v.lang||'')); if(_en.length) voices=_en;
  let saved=localStorage.getItem(VOICE_PREF_KEY)||'';
  // If the saved voice is not in the offered list (e.g. after filtering to English, or a stale
  // name-based value), the menu would SHOW one voice while speak() fell back to the default. Pin
  // saved to a real, offered voice and persist it so the menu, storage, and playback all agree.
  if(!saved || !voices.some(v=>v.voiceURI===saved)){ saved=(voices[0]||{}).voiceURI||''; try{ if(saved) localStorage.setItem(VOICE_PREF_KEY,saved); }catch(_e){} }
  // Key options by the UNIQUE voiceURI, not the name: many OS voices share a name and differ only
  // by language, so a name lookup always resolved to the FIRST match, which is why every pick
  // played the same voice.
  sel.innerHTML=voices.map(v=>`<option value="${esc(v.voiceURI)}"${v.voiceURI===saved?' selected':''}>${esc(v.name)} (${esc(v.lang)})</option>`).join('');
  if(saved) sel.value=saved;
  if(!sel.dataset.wired){ sel.dataset.wired='1'; sel.addEventListener('change',()=>{
    localStorage.setItem(VOICE_PREF_KEY,sel.value);
    const c=document.getElementById('voiceSavedFlashPhone');
    if(c){ c.textContent='Saved \u2713'; c.style.opacity='1';
      setTimeout(()=>{ c.style.opacity='0'; }, 1800); }
  }); }
}
function speak(text){
  if(!text||!window.speechSynthesis)return
  const u=new SpeechSynthesisUtterance(text)
  const saved=localStorage.getItem(VOICE_PREF_KEY)||''
  if(saved){ const v=speechSynthesis.getVoices().find(x=>x.voiceURI===saved); if(v){ u.voice=v; u.lang=v.lang; } }
  u.rate=1.05; speechSynthesis.cancel(); speechSynthesis.speak(u)
}
// touch events for PTT
// Pointer events unify mouse, touch, and pen into one path; keyboard adds Space/Enter for
// accessibility. A single source of truth (pttActive) prevents double-starts.
ptt.setAttribute('tabindex','0')
ptt.setAttribute('role','button')
function _pttDown(e){ if(e&&e.preventDefault)e.preventDefault(); if(!pttActive){ pttActive=true; startRecording(); } }
function _pttUp(e){ if(e&&e.preventDefault)e.preventDefault(); if(pttActive){ pttActive=false; stopRecording(); } }
ptt.addEventListener('pointerdown',_pttDown)
ptt.addEventListener('pointerup',_pttUp)
ptt.addEventListener('pointerleave',e=>{ if(pttActive) _pttUp(e); })
ptt.addEventListener('pointercancel',e=>{ if(pttActive) _pttUp(e); })
// Keyboard: hold Space/Enter to talk (keydown repeats, so guard with pttActive)
ptt.addEventListener('keydown',e=>{ if((e.key===' '||e.key==='Enter')&&!pttActive){ e.preventDefault(); _pttDown(e); } })
ptt.addEventListener('keyup',e=>{ if((e.key===' '||e.key==='Enter')&&pttActive){ e.preventDefault(); _pttUp(e); } })

// ── status pane ──
// ── Launchpad find box (Status pane) ──────────────────────────────────────────────────────────
// Two kinds of result: a SCREEN deep-links to /device/cockpit#view=X (opened there); a CHAT action
// prefills this phone's own chat with a draft that the intent resolver routes to a real handler. The
// screen views and the chat drafts are mirrored by tests/test_find_box_coverage.py so this list can
// never drift out from under a real destination the way 'repair' silently went missing before.
function lpFindIndex(){
  var screens=[['Approvals','approvals','approve cancel pending whatsapp draft'],
    ['Scheduler','scheduler','scheduler jobs cron pause resume memory'],
    ['Devices','devices','device revoke unpair paired guest'],
    ['Activity','activity','logs log activity recent history'],
    ['Search','search','search find documents files email voicemail attachments'],
    ['Setup','setup','setup connectors sync install dependencies integrations'],
    ['WhatsApp','whatsapp','whatsapp messages drafts'],
    ['RTM','rtm','rtm {{RTM_NAME_LOWER}} arrears demands flats management'],
    ['Build','build','build deploy update release rtm plugin bug workflow'],
    ['Tasks','tasks','task tasks todo to-do deadline due reminder priority reprioritise outstanding microsoft'],
    ['Benchmarks','benchmarks','benchmark benchmarks model models speed latency tool calls scoring'],
    ['System','system','system health benchmark models resources memory doctor notifications push']];
  var chats=[['Reset a guest password','reset password repair pair forgotten locked out login credentials','reset a guest’s password'],
    ['Generate a guest invite link','guest invite link trusted onboarding qr claim pair repair','Give me a guest invite link for '],
    ['How are the arrears looking','arrears rtm owing service charge leaseholder','How are the arrears looking?'],
    ['Build the next RTM plugin','rtm build wordpress plugin management iteration','Build the next RTM plugin iteration'],
    ['Play some music','music play songs playlist','Play some music'],
    ['Prepare me for something','prepare brief meeting trustee readiness','Prepare me for '],
    ['Surprise me','surprise delight suggestion nudge','Surprise me']];
  var idx=[];
  screens.forEach(function(s){ idx.push({ic:'🧭',label:s[0],kind:'Screen',kw:(s[0]+' '+s[2]).toLowerCase(),view:s[1]}); });
  chats.forEach(function(c){ idx.push({ic:'💬',label:c[0],kind:'Chat',kw:(c[0]+' '+c[1]).toLowerCase(),draft:c[2]}); });
  return idx;
}
var _lpIndex=[], _lpMatches=[];
function lpFindRender(q){
  var res=document.getElementById('lp-find-res'); if(!res)return; q=(q||'').trim().toLowerCase();
  if(!q){ res.style.display='none'; res.innerHTML=''; return; }
  var terms=q.split(/\s+/);
  _lpMatches=_lpIndex.filter(function(it){ return terms.every(function(t){ return it.kw.indexOf(t)>=0; }); }).slice(0,12);
  if(!_lpMatches.length){ res.innerHTML='<div class="body" style="color:var(--dim);padding:4px 2px">No screen or action matches.</div>'; res.style.display='block'; return; }
  res.innerHTML=_lpMatches.map(function(it,i){
    return '<div class="lp-opt" data-i="'+i+'" style="display:flex;align-items:center;gap:10px;padding:10px 6px;border-top:1px solid var(--line);cursor:pointer">'
      +'<span style="font-size:17px;width:22px;text-align:center">'+it.ic+'</span>'
      +'<span style="flex:1;font-size:14px">'+esc(it.label)+'</span>'
      +'<span style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);border-radius:99px;padding:1px 7px">'+it.kind+'</span></div>';
  }).join('');
  res.style.display='block';
  var opts=res.querySelectorAll('.lp-opt');
  for(var k=0;k<opts.length;k++){ (function(el){ el.onclick=function(){ lpFindPick(parseInt(el.getAttribute('data-i'),10)); }; })(opts[k]); }
}
function lpFindPick(i){
  var m=_lpMatches[i]; if(!m)return;
  var inp=document.getElementById('lp-find'), res=document.getElementById('lp-find-res');
  if(inp) inp.value=''; if(res){ res.style.display='none'; res.innerHTML=''; }
  if(m.view){ location.href='/device/cockpit#view='+m.view; return; }
  if(m.draft){ try{ showTab('talk'); }catch(_e){}
    var t=document.getElementById('msg'); if(t){ t.value=m.draft; try{ autosize(t); }catch(_e2){} t.focus();
      try{ var sb=document.getElementById('send-btn'); if(sb) sb.disabled=!t.value.trim(); }catch(_e3){} } }
}
function lpFindInit(){
  var inp=document.getElementById('lp-find'); if(!inp||inp.dataset.init==='1')return;  // idempotent: loadStatus re-runs
  inp.dataset.init='1';
  // Owner-only: guests reach chat/IRC, not the cockpit navigation. Hide rather than dead-end them.
  if(localStorage.getItem('mg_is_owner')==='0'){ var c=inp.closest('.card'); if(c) c.style.display='none'; return; }
  _lpIndex=lpFindIndex();
  inp.addEventListener('input',function(){ lpFindRender(inp.value); });
  inp.addEventListener('keydown',function(e){ if(e.key==='Enter'){ e.preventDefault(); if(_lpMatches.length) lpFindPick(0); }
    else if(e.key==='Escape'){ inp.value=''; lpFindRender(''); inp.blur(); } });
}
async function loadStatus(){
  try{ lpFindInit(); }catch(_e){}
  const root=document.getElementById('status-root')
  // Render the STATIC shell first so navigation (Open cockpit) is instant and never waits on
  // /device/summary, which aggregates a cross-domain picture and can be slow. Dynamic values fill
  // into placeholder ids once the summary returns. Owner status is remembered so the cockpit link
  // renders immediately on repeat loads (corrected after the fetch if the cached guess was wrong).
  const knownOwner = localStorage.getItem('mg_is_owner')!=='0'
  const cockpitCard = `<div class="card" style="border-color:var(--gold)"><h2>Full cockpit</h2>
        <div class="body" style="color:var(--dim);font-size:13px;margin-bottom:8px">Every Mongoose control, phone-sized.</div>
        <button class="act" onclick="navTo(this,'/device/cockpit')">Open cockpit →</button></div>`
  // Render the shell ONCE. loadStatus is re-called on tab re-open AND after a background job finishes;
  // re-rendering here wiped the filled cards back to skeletons and made "What changed" vanish while you
  // were reading it. On a refresh the elements already exist, so we skip the rebuild and just update
  // values in place below (fill-by-id works either way).
  if(!document.getElementById('what-body')){
    root.innerHTML=`
      <div class="card"><h2>Mongoose</h2><div class="body ok" id="mg-ver">Loading&hellip;</div>
        <div class="body" id="mg-dev" style="color:var(--dim);font-size:13px;margin-top:4px"></div></div>
      <div id="cockpit-slot">${knownOwner?cockpitCard:''}</div>
      <div class="card" id="delight-card" style="display:none"></div>
      <div class="card"><h2>What changed</h2><div class="body" id="what-body">Checking&hellip;</div><div id="notif-slot"></div></div>
      <div class="card" id="messages-card"><h2>Messages</h2><div class="body" id="messages-body" style="color:var(--dim);font-size:13px">Checking&hellip;</div></div>
      <div class="card" id="poke-card"><h2>Poke 👆🪿</h2><div id="poke-body" style="font-size:13px;color:var(--dim)">Checking&hellip;</div></div>
      <div id="suggest-slot"></div>
      <div class="card"><h2>This device</h2><div id="thisdev-body"><span class="body" style="color:var(--dim)">&hellip;</span></div></div>
      <div id="notif-card-slot"></div>`
    // Cards that do NOT depend on /device/summary load immediately (first render only).
    try{ loadDelightCard() }catch(e){}
    try{ loadMessagesCard() }catch(e){}
    try{ loadPoke() }catch(e){}
  }
  // Now fetch the summary and fill the dynamic parts. The cockpit link is already usable above.
  let d
  try{ d=await api('/device/summary') }
  catch(e){
    if(e&&(e.status===403||e.status===401)){
      const inp='width:100%;margin-bottom:8px;box-sizing:border-box;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:9px;padding:10px;font-size:15px';
      root.innerHTML=`<div class="card" style="border-color:var(--gold)"><h2 style="color:var(--gold)">Sign back in</h2>`
        +`<div class="body" style="color:var(--dim);font-size:13px;margin-bottom:10px">This device is signed out (its pass was cleared or expired). Sign in with your Mongoose username and password to restore access on this phone.</div>`
        +`<input id="dl-user" type="text" placeholder="Username" autocomplete="username" style="${inp}">`
        +`<input id="dl-pass" type="password" placeholder="Password" autocomplete="current-password" style="${inp}">`
        +`<button class="act go" onclick="deviceLogin(this)">Sign in</button>`
        +`<div id="dl-msg" style="font-size:12px;color:var(--dim);margin-top:8px"></div>`
        +`<div style="font-size:12px;margin-top:12px;color:var(--dim)">Forgot your password? <a onclick="askOwnerRepair(this);return false" style="color:var(--gold);cursor:pointer">Ask {{owner.name}} for a repair link</a></div>`
        +`<div style="margin-top:12px"><a onclick="clearDevice();return false" style="font-size:11px;color:var(--dim);cursor:pointer">Clear this device and start over</a></div></div>`;
    } else {
      // Do NOT wipe the shell: the cockpit link must stay reachable even when the status data fails.
      const v=document.getElementById('mg-ver'); if(v){ v.className='body'; v.textContent='status unavailable ('+esc((e&&e.message)||'network')+')'; }
    }
    return
  }
  // ── fill the dynamic parts from the summary (the shell is already on screen) ──
  const dev=d.device||{}; const what=d.what_changed||{}; const sug=d.suggestions||[]
  const isOwner=(dev.profile && dev.profile!=='trusted_guest')
  localStorage.setItem('mg_is_owner', isOwner?'1':'0')
  const sysTab=document.getElementById('tab-cockpit'); if(sysTab) sysTab.style.display = isOwner ? '' : 'none'
  const slot=document.getElementById('cockpit-slot'); if(slot) slot.innerHTML = isOwner ? cockpitCard : ''
  const v=document.getElementById('mg-ver'); if(v){ v.className='body ok'; v.textContent = (d.health&&d.health.version)?('v'+d.health.version):'reachable'; }
  const dl=document.getElementById('mg-dev'); if(dl) dl.textContent = (dev.name||'this device')+' · '+(dev.profile||'paired')
  const wb=document.getElementById('what-body'); if(wb) wb.textContent = what.summary||'Nothing new.'
  const notifCount=(what.detail && what.detail.notifications) ? parseInt(what.detail.notifications) : 0
  // Always SET these (clear when empty), never only-set-when-present: otherwise a stale item (e.g. an
  // old suggestion the server no longer sends) persists on screen forever after the refactor stopped
  // wiping the shell each refresh.
  const ns=document.getElementById('notif-slot')
  if(ns) ns.innerHTML = (notifCount && isOwner) ? `<button class="act" onclick="showNotifications(this)" style="margin-top:8px;font-size:12px">View ${notifCount} notification${notifCount===1?'':'s'} →</button><div id="notif-list" style="margin-top:8px"></div>` : ''
  const ss=document.getElementById('suggest-slot')
  if(ss) ss.innerHTML = sug.length ? `<div class="card"><h2>Suggestions</h2>${sug.map(s=>`<div style="margin-bottom:8px;font-size:14px">${esc(s.text||s.title)}</div>`).join('')}</div>` : ''
  const td=document.getElementById('thisdev-body'); if(td) td.innerHTML = (dev.capability_status||[]).map(c=>`<span class="pill">${esc(c.label||c.capability)}</span>`).join('')||'<span class="body" style="color:var(--dim)">No capabilities reported yet.</span>'
  if(isOwner){
    const nc=document.getElementById('notif-card-slot')
    if(nc) nc.innerHTML=`<div class="card" id="notif-card"><h2>Notifications</h2><div class="body" id="notif-status" style="color:var(--dim);font-size:13px">Checking&hellip;</div><button id="notif-btn" onclick="enablePush()" style="display:none;margin-top:10px">Enable notifications</button></div>`
    updateNotifCard()
  }
}
// Show the actual notifications behind "Notifications: N new" (owner device only).
async function showNotifications(btn){
  const box=document.getElementById('notif-list'); if(!box) return;
  if(btn) btn.disabled=true; box.innerHTML='<span style="color:var(--dim);font-size:13px">Loading…</span>'
  try{
    const d=await api('/device/notifications')
    const items=(d&&d.items)||[]
    if(!items.length){ box.innerHTML='<span style="color:var(--dim);font-size:13px">Nothing recent.</span>'; return }
    // Tap a notification to expand its full text (they used to be unreadable, title-only labels).
    box.innerHTML=items.map(n=>`<div class="notif-item" style="padding:7px 0;border-top:1px solid var(--line);cursor:pointer" onclick="this.classList.toggle('open')">
        <div style="font-size:13px">${esc(n.title||'Notification')}</div>
        ${n.body?`<div class="notif-body" style="font-size:12px;color:var(--text);margin-top:3px;display:none">${esc(n.body)}</div>`:''}
        <div style="font-size:11px;color:var(--dim)">${esc(n.app||n.source||'')}${n.received_at?(' · '+esc(_relTime(n.received_at))):''}</div></div>`).join('')
  }catch(e){ box.innerHTML='<span style="color:var(--warn);font-size:13px">'+esc(e.message||'could not load')+'</span>' }
  finally{ if(btn) btn.style.display='none' }
}
// Messages card: unread count + the most recent few, with a tap-through to Talk to reply.
async function loadMessagesCard(){
  const body=document.getElementById('messages-body'); if(!body) return;
  try{
    const d=await api('/device/messages')
    const msgs=(d&&d.messages)||[]; const unread=(d&&d.unread)||0
    if(!msgs.length){ body.innerHTML='<span style="color:var(--dim)">No messages.</span>'; return }
    const recent=msgs.slice(-4)
    body.innerHTML=(unread?`<div style="font-size:13px;color:var(--gold);margin-bottom:6px">${unread} unread</div>`:'')
      + recent.map(m=>`<div style="padding:7px 0;border-top:1px solid var(--line)">
          <div style="font-size:11px;color:var(--dim)">${esc(m.from_display||(m.sender==='owner'?'You':String(m.sender||'').replace(/^guest:/,'Guest ')))}${m.created_at?(' · '+esc(_relTime(m.created_at))):''}</div>
          <div style="font-size:13px">${esc((m.body||'').slice(0,160))}</div></div>`).join('')
      + `<button class="act" onclick="showTab('talk')" style="margin-top:8px;font-size:12px">Open conversation →</button>`
  }catch(e){ body.innerHTML='<span style="color:var(--dim)">Messages unavailable.</span>' }
}

// --- Poke 👆🪿 -------------------------------------------------------------------------------------
// Poking is account-scoped (a poke comes from a person, not a device), so this surface signs in with a
// username and pokes over the session cookie. The launchpad's api() helper adds the DEVICE token, which
// is the wrong identity for poke, so poke calls use plain fetch with credentials:'same-origin' to carry
// the account session cookie set by /account/login.
async function _acctMe(){
  try{ const r=await fetch('/account/me',{credentials:'same-origin',headers:{'X-Mongoose-Device-Token':token()}}); return await r.json(); }
  catch(e){ return {authenticated:false}; }
}
function pkToggleAuth(which){
  const c=document.getElementById('pk-create-box'), s=document.getElementById('pk-signin-box');
  if(!c||!s) return;
  const showSignin = which==='signin';
  c.style.display = showSignin ? 'none' : 'block';
  s.style.display = showSignin ? 'block' : 'none';
  const m=document.getElementById('pk-auth-msg'); if(m) m.textContent='';
}
async function _ownerPokeLogin(){
  // The OWNER is already pre-authenticated by their device, so they should never face a poke
  // sign-in wall. One server call mints (or re-fetches) a poke identity bound to this device and
  // logs it in by setting a session cookie. Idempotent and server-authoritative, so it cannot get
  // stuck the way the old client-side password dance could (a failed register left the owner with
  // no identity and no dropdown). Guests do NOT use this: they got their identity at the invite
  // handoff, and the server refuses this route for guest devices.
  try{
    const r=await fetch('/account/owner-identity',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':token()}});
    const d=await r.json().catch(()=>({}));
    return (r.ok && d && d.ok===true);
  }catch(e){ return false; }
}
async function loadPoke(){
  const body=document.getElementById('poke-body'); if(!body) return;
  const me=await _acctMe();
  if(!me||!me.authenticated||!me.account){
    // Owner? Give them a poke identity silently instead of a sign-in wall (once per load).
    if(!window.__ownerPokeTried){ window.__ownerPokeTried=true; if(await _ownerPokeLogin()){ return loadPoke(); } }
    // Not signed in. Progressive disclosure: show ONE form (create by default, since a new
    // person needs an identity), with a link to switch to sign-in. Dumping both full forms at
    // once read as overwhelming. Both sets of inputs stay in the DOM so the handlers still work.
    const inp='width:100%;margin-bottom:6px;box-sizing:border-box';
    const link='display:inline-block;margin-top:8px;font-size:12px;color:var(--gold);cursor:pointer';
    body.innerHTML=`<div style="color:var(--dim);margin-bottom:10px">A personal identity lets people poke you and see who is who. Create one, or sign in if you already have it.</div>
      <div id="pk-create-box">
        <input id="pk-new-display" type="text" placeholder="Display name (e.g. Toby)" autocomplete="name" style="${inp}">
        <input id="pk-new-user" type="text" placeholder="Choose a username" autocomplete="username" style="${inp}">
        <input id="pk-new-pass" type="password" placeholder="Choose a password" autocomplete="new-password" style="${inp}">
        <button class="act go" onclick="pokeCreateAccount(this)">Create my identity</button>
        <a onclick="pkToggleAuth('signin');return false" style="${link}">Already have a login? Sign in</a>
      </div>
      <div id="pk-signin-box" style="display:none">
        <input id="pk-user" type="text" placeholder="Username" autocomplete="username" style="${inp}">
        <input id="pk-pass" type="password" placeholder="Password" autocomplete="current-password" style="${inp}">
        <label style="display:flex;align-items:center;gap:7px;font-size:12px;margin-bottom:8px"><input type="checkbox" id="pk-remember" checked> Keep me signed in on this device</label>
        <button class="act" onclick="pokeSignIn(this)">Sign in</button>
        <a onclick="pkToggleAuth('create');return false" style="${link}">New here? Create an identity</a>
      </div>
      <span id="pk-auth-msg" style="display:block;margin-top:8px;font-size:12px;color:var(--dim)"></span>`;
    return;
  }
  body.innerHTML='<span style="color:var(--dim)">Loading…</span>';
  const myUsername=me.username||'';
  let users=[], recv=[];
  try{ const r=await fetch('/users',{credentials:'same-origin',headers:{'X-Mongoose-Device-Token':token()}}); const d=await r.json(); if(d.ok) users=d.users||[]; }catch(e){}
  try{ const r=await fetch('/poke/history',{credentials:'same-origin',headers:{'X-Mongoose-Device-Token':token()}}); const d=await r.json(); if(d&&d.ok) recv=(d.received||[]).slice(0,5); }catch(e){}
  const mine=users.find(u=>u.username===myUsername)||{};
  const who=esc(mine.display_name||myUsername||'you');
  let html=`<div style="font-size:12px;color:var(--dim);margin-bottom:8px">Signed in as <b style="color:var(--fg)">${who}</b> · <a href="#" onclick="pokeSignOut();return false" style="color:var(--gold)">sign out</a></div>`;
  // Owner: offer a sign-in password so a cleared/lost device token can be recovered by signing in
  // (the owner identity is auto-created with a random password nobody has seen).
  try{ if((localStorage.getItem('mongoose_device_profile')||'')==='primary_phone'){
    const spi='width:100%;margin:4px 0;box-sizing:border-box;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px;font-size:14px';
    html+=`<details style="margin-bottom:8px"><summary style="font-size:12px;color:var(--gold);cursor:pointer">Set a sign-in password (for getting back in)</summary>`
      +`<div style="font-size:11px;color:var(--dim);margin:6px 0">If this phone ever loses its pass, sign back in with your username and this password.</div>`
      +`<input id="sp-pass" type="password" placeholder="New sign-in password" autocomplete="new-password" style="${spi}">`
      +`<button class="act" onclick="setSigninPassword(this)" style="font-size:13px">Save password</button>`
      +`<div id="sp-msg" style="font-size:12px;color:var(--dim);margin-top:6px"></div></details>`;
  } }catch(_e){}
  const others=users.filter(u=>u.username!==myUsername);
  const pokeable=others.filter(u=>u.allow_poke);
  if(!others.length){
    html+='<div style="color:var(--dim)">No one else to poke yet. When another person signs in to Mongoose, they show up here.</div>';
  }else if(!pokeable.length){
    html+='<div style="color:var(--dim)">No one is accepting pokes right now.</div>';
  }else{
    html+='<div style="font-size:12px;color:var(--dim);margin-bottom:6px">Poke someone</div>'
      +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
      +'<select id="poke-who" style="flex:1;min-width:140px;padding:8px;background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:9px">'
      + pokeable.map(u=>`<option value="${esc(u.username)}">${esc(u.display_name||u.username)}</option>`).join('')
      +'</select>'
      +'<div class="pk-btn" id="poke-do"><div class="pk-prog"></div><span class="pk-goose">🪿</span><span class="pk-lbl">hold to poke</span></div>'
      +'</div>';
    const off=others.filter(u=>!u.allow_poke);
    if(off.length) html+='<div style="font-size:11px;color:var(--dim);margin-top:6px">Not accepting pokes: '+off.map(u=>esc(u.display_name||u.username)).join(', ')+'</div>';
  }
  if(recv.length){
    html+=`<div style="margin-top:10px;font-size:12px;color:var(--dim)">Recent pokes you got:</div>`;
    html+=recv.map(p=>`<div class="pk-recv">👆🪿 from ${esc(p.from_name||p.from_display||p.from||'someone')}${p.poked_at?(' · '+esc(_relTime(p.poked_at))):''}</div>`).join('');
  }
  body.innerHTML=html;
  // wire the hold-to-poke buttons
  const _pdo=document.getElementById('poke-do');
  if(_pdo) initPokeButton(_pdo, function(){ const s=document.getElementById('poke-who'); return s?s.value:''; });
}
async function pokeCreateIdentity(btn){
  // Register B3: poke gated on an identity that setup never offered to create. The paired
  // device's token IS the authority (guests are refused server-side); on success we sign
  // straight in with the same fields, so create-then-use is one motion.
  const u=(document.getElementById('pk-user')||{}).value||'';
  const p=(document.getElementById('pk-pass')||{}).value||'';
  const msg=document.getElementById('pk-auth-msg');
  if(!u||!p){ if(msg)msg.textContent='Type the username and password you want first.'; return; }
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/account/register',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':token()},
      body:JSON.stringify({username:u,password:p,display_name:u})});
    const d=await r.json();
    if(d.ok){ if(msg)msg.textContent='Identity created. Signing you in…'.replace('…','...'); await pokeSignIn(null); }
    else { if(msg)msg.textContent=d.error||'Could not create the identity.'; }
  }catch(e){ if(msg)msg.textContent='Could not reach Mongoose.'; }
  finally{ if(btn)btn.disabled=false; }
}
async function pokeSignIn(btn){
  const u=(document.getElementById('pk-user')||{}).value||'';
  const p=(document.getElementById('pk-pass')||{}).value||'';
  const rem=!!((document.getElementById('pk-remember')||{checked:true}).checked);
  const msg=document.getElementById('pk-auth-msg');
  if(!u||!p){ if(msg)msg.textContent='Enter a username and password.'; return; }
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/account/login',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':token()},body:JSON.stringify({username:u,password:p,remember:rem})});
    const d=await r.json();
    if(d.ok){ loadPoke(); }
    else { if(msg)msg.textContent=d.error||'Sign in failed.'; if(btn)btn.disabled=false; }
  }catch(e){ if(msg)msg.textContent='Error signing in.'; if(btn)btn.disabled=false; }
}
async function pokeCreateAccount(btn){
  const display=(document.getElementById('pk-new-display')||{}).value||'';
  const u=(document.getElementById('pk-new-user')||{}).value||'';
  const p=(document.getElementById('pk-new-pass')||{}).value||'';
  const msg=document.getElementById('pk-auth-msg');
  if(!display.trim()||!u.trim()||!p){ if(msg)msg.textContent='Enter a display name, username and password.'; return; }
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/account/register',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':token()},
      body:JSON.stringify({username:u,password:p,display_name:display,allow_poke:true})});
    const d=await r.json();
    if(!d.ok){ if(msg)msg.textContent=d.error||'Could not create identity.'; if(btn)btn.disabled=false; return; }
    const login=await fetch('/account/login',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':token()},body:JSON.stringify({username:u,password:p,remember:true})});
    const ld=await login.json();
    if(ld.ok){ loadPoke(); }
    else { if(msg)msg.textContent=ld.error||'Created, but sign in failed.'; if(btn)btn.disabled=false; }
  }catch(e){ if(msg)msg.textContent='Error creating identity.'; if(btn)btn.disabled=false; }
}
async function pokeSignOut(){
  try{ await fetch('/account/logout',{method:'POST',credentials:'same-origin',headers:{'X-Mongoose-Device-Token':token()}}); }catch(e){}
  loadPoke();
}
function initPokeButton(btn, username){
  const holdMs=900;
  const prog=btn.querySelector('.pk-prog');
  let timer=null;
  // username may be a plain string OR a getter, so one hold-to-poke button can poke whoever is
  // currently chosen in the dropdown.
  const _uname=()=>((typeof username==='function')?username():username)||'';
  if(!btn.hasAttribute('tabindex')) btn.setAttribute('tabindex','0');
  btn.setAttribute('role','button');
  btn.setAttribute('aria-label','Poke (hold, or press Enter)');
  function start(e){
    if(btn.classList.contains('fired')) return;
    if(e&&e.cancelable) e.preventDefault();
    if(!_uname()) return;
    btn.classList.add('holding');
    if(prog){ prog.style.transition='width '+holdMs+'ms linear'; prog.style.width='100%'; }
    timer=setTimeout(()=>firePoke(btn,_uname()),holdMs);
  }
  function cancel(){
    clearTimeout(timer); timer=null;
    btn.classList.remove('holding');
    if(prog){ prog.style.transition='none'; prog.style.width='0%'; }
  }
  btn.addEventListener('touchstart',start,{passive:false});
  btn.addEventListener('mousedown',start);
  btn.addEventListener('touchend',cancel);
  btn.addEventListener('touchcancel',cancel);
  btn.addEventListener('mouseup',cancel);
  btn.addEventListener('mouseleave',cancel);
  btn.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); if(!btn.classList.contains('fired')&&_uname()) firePoke(btn,_uname()); }});
}
async function firePoke(btn, username){
  btn.classList.remove('holding'); btn.classList.add('fired');
  const prog=btn.querySelector('.pk-prog'); if(prog){ prog.style.transition='none'; prog.style.width='100%'; }
  const lbl=btn.querySelector('.pk-lbl'); if(lbl) lbl.textContent='poked! 👆🪿';
  try{
    const r=await fetch('/poke/'+encodeURIComponent(username),{method:'POST',credentials:'same-origin',headers:{'X-Mongoose-Device-Token':token()}});
    const d=await r.json();
    if(!d.ok && lbl) lbl.textContent=d.error||"couldn't poke";
  }catch(e){ if(lbl) lbl.textContent='error'; }
  setTimeout(()=>{ btn.classList.remove('fired'); if(prog)prog.style.width='0%'; if(lbl)lbl.textContent='hold to poke'; },3000);
}
function _relTime(iso){
  try{ const t=new Date((iso||'').replace(' ','T')+(/[zZ]|[+-]\d\d:?\d\d$/.test(iso||'')?'':'Z'));
    const s=Math.max(0,(Date.now()-t.getTime())/1000);
    if(s<60)return 'just now'; if(s<3600)return Math.floor(s/60)+'m ago';
    if(s<86400)return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago';
  }catch(_){ return '' }
}
async function renderSystemManagePanels(root){
  if(!root) return;
  // Idempotent: render into ONE #sys-manage container we REPLACE, so overlapping/repeat calls can never
  // stack duplicate copies of the manage cluster (the "why is this so repetitive" bug).
  let box=root.querySelector('#sys-manage');
  if(!box){ box=document.createElement('div'); box.id='sys-manage'; root.appendChild(box); }
  let ver=''; try{ const d=await api('/device/summary'); ver=(d.health&&d.health.version)?('v'+d.health.version):''; }catch(e){}
  box.innerHTML=`
    <div class="card"><h2>Update Mongoose</h2>
      <div class="body" style="font-size:13px;color:var(--dim);margin-bottom:10px">Upload a Mongoose package (.zip) to update the Mac${ver?(' (currently '+esc(ver)+')'):''}. Mongoose verifies the file is a real package, runs its safety checks, snapshots the brain first, and can roll back. This restarts the app, so it may go quiet for a moment.</div>
      <input type="file" id="updzip" accept=".zip,application/zip" style="width:100%;font:inherit;color:var(--text);margin-bottom:10px">
      <button class="act go" id="updbtn" onclick="doDeviceUpdate(this)" style="width:100%">Check and apply update</button>
      <div id="updout" class="body" style="font-size:13px;margin-top:10px"></div></div>
    <div class="card"><h2>Staged self-update</h2>
      <div class="muted" style="margin-bottom:8px">Fetch the next version from your configured source and stage it. Safety checks run before anything applies; applying stays a deliberate step (mongoose update apply).</div>
      <button class="act go" onclick="suFetch(this)">Check for an update</button>
      <div id="su-msg" class="muted" style="margin-top:8px"></div>
      <div id="su-stages" class="muted" style="margin-top:8px">Loading&hellip;</div></div>
    <div class="card"><h2>Data export</h2>
      <div class="muted" style="margin-bottom:8px">Download a scoped, owner-only export of your data. Secrets are never included; the broad export redacts private journal wording, so use the journal export if you want that.</div>
      <div style="display:flex;gap:8px;align-items:center"><select id="dumpKind" style="background:#141109;border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text)"></select><button class="act go" onclick="dumpCreate(this)">Create export</button></div>
      <div id="dump-out" class="muted" style="margin-top:8px"></div>
      <div id="dump-list" class="muted" style="margin-top:8px"></div></div>
    <div class="card"><h2>What Mongoose suggested</h2>
      <div class="muted" style="margin-bottom:8px">Recent proactive nudges and how you rated them. A dismiss backs that nudge off for a good while.</div>
      <div id="sugg-list" class="muted">Loading&hellip;</div></div>`;
  suLoadStages(); dumpLoadList(); suggLoad();
  if(typeof loadKnowledge==='function'){
    box.insertAdjacentHTML('beforeend','<details class="card" open><summary>Knowledge and insights</summary><div id="sys-knowledge" class="muted">Loading knowledge, care and research&hellip;</div></details>');
    loadKnowledge();
  }
}
async function loadSystem(){
  const root=document.getElementById('system-root'); if(!root) return;
  root.innerHTML='';
  renderSystemManagePanels(root);
}
async function dumpOpen(id){
  try{
    const d=await api('/cockpit/dump/redeem',{method:'POST',body:JSON.stringify({dump_id:id})});
    if(d && d.ok && d.download_url){ window.open(d.download_url,'_blank'); }
    else { alert((d&&d.error)||'Could not fetch a download link.'); }
  }catch(e){ alert('Download failed.'); }
}
async function suFetch(btn){ btn.disabled=true; const m=document.getElementById('su-msg'); if(m)m.textContent='Checking your update source\u2026';
  try{ const d=await api('/cockpit/update/fetch',{method:'POST',body:JSON.stringify({}),timeoutMs:60000}); if(m)m.textContent=(d&&d.ok)?('Staged '+(d.version||d.stage_id||'a candidate')+'.'):((d&&(d.error||d.detail))||'No update found.'); }
  catch(e){ if(m)m.textContent='Could not reach the update source.'; } finally{ btn.disabled=false; suLoadStages(); } }
async function suLoadStages(){ const el=document.getElementById('su-stages'); if(!el) return;
  try{ const d=await api('/cockpit/update-stage/list',{method:'POST',body:JSON.stringify({})}); const st=(d&&d.stages)||[];
    if(!st.length){ el.innerHTML='<div class="muted">No staged updates.</div>'; return; }
    let h=''; st.forEach(function(s){ const id=s.stage_id||s.id||''; const v=s.version||s.target_version||''; const ap=s.approved?'approved':(s.checks_passed?'checked':'staged');
      h+='<div class="row"><span class="k"><b>'+esc(v||id)+'</b><div class="muted" style="font-size:11px">'+esc(id)+' &middot; '+esc(ap)+'</div></span><span class="v"><button class="act" onclick="suCheck(\''+esc(id)+'\',this)">Check</button> <button class="act" onclick="suDrill(\''+esc(id)+'\',this)">Rollback drill</button> <button class="act go" onclick="suApprove(\''+esc(id)+'\',this)">Approve</button></span></div><div id="su-r-'+esc(id)+'" class="muted" style="font-size:11px;margin:-4px 0 8px"></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load staged updates.'; } }
async function suCheck(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Running checks\u2026';
  try{ const d=await api('/cockpit/update-stage/check',{method:'POST',body:JSON.stringify({stage_id:id}),timeoutMs:120000}); if(r)r.textContent=(d&&d.ok)?'Checks passed.':('Checks: '+JSON.stringify((d&&d.checks)||d).slice(0,150)); }
  catch(e){ if(r)r.textContent='Check failed.'; } finally{ if(btn)btn.disabled=false; } }
async function suDrill(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Dry-running rollback\u2026';
  try{ const d=await api('/cockpit/update-stage/rollback-drill',{method:'POST',body:JSON.stringify({stage_id:id})}); if(r)r.textContent=(d&&d.ok)?'Rollback path verified. You could roll back.':((d&&d.error)||'Rollback drill failed.'); }
  catch(e){ if(r)r.textContent='Rollback drill failed.'; } finally{ if(btn)btn.disabled=false; } }
async function suApprove(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Approving\u2026';
  try{ const d=await api('/cockpit/update-stage/approve',{method:'POST',body:JSON.stringify({stage_id:id})}); if(r)r.textContent=(d&&d.ok)?('Approved. Apply from a terminal with: mongoose update apply '+id):((d&&d.error)||'Approve failed.'); }
  catch(e){ if(r)r.textContent='Approve failed.'; } finally{ if(btn)btn.disabled=false; suLoadStages(); } }
async function dumpLoadList(){ const el=document.getElementById('dump-list'); const sel=document.getElementById('dumpKind');
  try{ const d=await api('/cockpit/dump/list',{method:'POST',body:JSON.stringify({})});
    if(sel && !sel.options.length){ ((d&&d.kinds)||['everything']).forEach(function(k){ const o=document.createElement('option'); o.value=k; o.textContent=k; if(k==='everything')o.selected=true; sel.appendChild(o); }); }
    if(!el) return; const dumps=(d&&d.dumps)||[];
    if(!dumps.length){ el.innerHTML='<div class="muted">No exports yet.</div>'; return; }
    let h='<div class="muted" style="margin-bottom:4px">Recent exports:</div>'; dumps.forEach(function(x){ h+='<div class="row"><span class="k">'+esc(x.kind||'')+'<div class="muted" style="font-size:11px">'+esc((x.created_at||'').slice(0,16))+' &middot; '+Math.round((x.size_bytes||0)/1024)+' KB</div></span><span class="v"><button class="act" onclick="dumpOpen(\''+esc(x.dump_id||'')+'\')">Download</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ if(el)el.textContent='Could not load exports.'; } }
async function dumpCreate(btn){ const sel=document.getElementById('dumpKind'); const kind=(sel&&sel.value)||'everything'; const out=document.getElementById('dump-out'); btn.disabled=true; if(out)out.textContent='Building your export\u2026';
  try{ const d=await api('/cockpit/dump/create',{method:'POST',body:JSON.stringify({kind:kind}),timeoutMs:60000});
    if(d&&d.ok){ const rc=Object.keys(d.row_counts||{}).map(function(k){return k+':'+d.row_counts[k];}).join(', '); if(out)out.innerHTML='Ready. <a href="'+esc(d.download_url||('/dump/'+d.dump_id+'/download'))+'" target="_blank" rel="noopener" style="color:var(--accent)">Download '+esc(d.filename||'export')+'</a><div class="muted" style="font-size:11px;margin-top:4px">'+esc(rc)+'</div>'; }
    else { if(out)out.textContent='Export failed: '+((d&&d.error)||'unknown'); }
  }catch(e){ if(out)out.textContent='Export failed.'; } finally{ btn.disabled=false; dumpLoadList(); } }
async function suggLoad(){ const el=document.getElementById('sugg-list'); if(!el) return;
  try{ const d=await api('/cockpit/suggestions'); const ss=(d&&d.suggestions)||[];
    if(!ss.length){ el.innerHTML='<div class="muted">Nothing surfaced yet.</div>'; return; }
    let h=''; ss.forEach(function(s){ const fb=s.feedback?(' &middot; you said: '+esc(s.feedback)):''; h+='<div class="row"><span class="k">'+esc((s.text||'').slice(0,100))+'<div class="muted" style="font-size:11px">'+esc(s.kind||'')+fb+'</div></span><span class="v"><button class="act" onclick="suggFb(\''+esc(s.signature||'')+'\',\'useful\',this)">Useful</button> <button class="act" onclick="suggFb(\''+esc(s.signature||'')+'\',\'dismissed\',this)">Dismiss</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load suggestions.'; } }
async function suggFb(sig,fb,btn){ if(!sig) return; if(btn)btn.disabled=true;
  try{ await api('/cockpit/suggestions/feedback',{method:'POST',body:JSON.stringify({signature:sig,feedback:fb})}); }catch(e){} finally{ suggLoad(); } }
async function doDeviceUpdate(btn){
  const f=document.getElementById('updzip'); const out=document.getElementById('updout');
  if(!f||!f.files||!f.files[0]){ out.innerHTML='<span style="color:var(--warn)">Choose a .zip first.</span>'; return; }
  const file=f.files[0];
  if(!/\.zip$/i.test(file.name)){ out.innerHTML='<span style="color:var(--warn)">That isn\'t a .zip file.</span>'; return; }
  btn.disabled=true; out.innerHTML='Uploading and checking the package… this can take a minute. Keep this tab open.';
  try{
    const fd=new FormData(); fd.append('file', file, file.name);
    const r=await fetch('/device/update/upload',{method:'POST',headers:{'X-Mongoose-Device-Token':token()},body:fd});
    let d={}; try{ d=await r.json(); }catch(_){ d={ok:false,error:'no response from Mongoose'}; }
    if(d.ok){ out.innerHTML='<span style="color:var(--ok)">✓ Updated'+(d.target_version?(' to '+esc(d.target_version)):'')+'. '+esc(d.detail||'')+'</span>'; }
    else { out.innerHTML='<span style="color:var(--warn)">'+esc(d.error||('Update failed at the '+(d.stage||'?')+' step.'))+'</span>'; }
  }catch(e){ out.innerHTML='<span style="color:var(--warn)">Upload failed: '+esc(e.message||e)+'</span>'; }
  finally{ btn.disabled=false; }
}
// Import one or more body-data CSVs (Alpha Progression sessions/sets/body-comp and/or MyFitnessPal).
// Each file is sent to /body/data/ingest, which auto-detects the kind. Reports per-file results.
async function uploadBodyData(btn){
  const f=document.getElementById('bdfile'); const out=document.getElementById('bdout');
  if(!f||!f.files||!f.files.length){ out.innerHTML='<span style="color:var(--warn)">Choose at least one CSV first.</span>'; return; }
  btn.disabled=true; out.innerHTML='Importing…';
  const lines=[];
  for(const file of f.files){
    try{
      const text=await file.text();
      const d=await api('/body/data/ingest',{method:'POST',body:JSON.stringify({filename:file.name,text:text}),timeoutMs:30000});
      if(d.ok){ lines.push('<span style="color:var(--ok)">✓ '+esc(file.name)+': '+esc(d.detected||'imported')+(d.imported!=null?(' ('+d.imported+')'):'')+'</span>'); }
      else { lines.push('<span style="color:var(--warn)">• '+esc(file.name)+': '+esc(d.error||'failed')+'</span>'); }
    }catch(e){ lines.push('<span style="color:var(--warn)">• '+esc(file.name)+': '+esc(e.message||e)+'</span>'); }
  }
  out.innerHTML=lines.join('<br>');
  btn.disabled=false;
  // refresh the card so the ✓/,  and any remaining ask update
  setTimeout(()=>{ if(typeof loadActivity==='function') loadActivity(); }, 1200);
}
// A warm, rotating fun-fact card on the guest's home screen. Styled by category so animals look
// different from the cosmos. Tap to get another. Pulls from /device/delight (guest-safe, curated).
let _lastDelightText='';
const _DELIGHT_THEME={tech:['💡','#C9A35B'],nature:['🦫','#7CB893'],science:['⚗️','#8AB4D0'],
  cosmos:['🌌','#9A8AD0'],motor:['🏎️','#D0785E'],culture:['🎭','#D0A85A'],retro:['🎵','#C98AB4'],
  joy:['🌍','#7CB893'],ai:['🤖','#8AC9C0'],self:['🦡','#C9A35B'],host:['🙂','#D0C08A']};
async function loadDelightCard(force){
  const card=document.getElementById('delight-card'); if(!card) return;
  // Do NOT swap the fact out from under the reader: the Status tab auto-refreshes every few seconds
  // and was fetching a fresh fact each time, so a long one changed mid-read. Only fetch a new fact on
  // the first load or when they explicitly tap "Another".
  if(!force && card.dataset.loaded==='1') return;
  try{
    const f=await api('/device/delight'+(_lastDelightText?('?exclude='+encodeURIComponent(_lastDelightText)):''));
    if(!f||!f.text) return;
    _lastDelightText=f.text;
    card.dataset.loaded='1';
    const theme=_DELIGHT_THEME[f.style]||['✨','#C9A35B'];
    card.style.display='block';
    card.style.borderLeft='3px solid '+theme[1];
    card.innerHTML=`<h2 style="color:${theme[1]}">${theme[0]} Did you know?</h2>
      <div class="body" style="font-size:14px">${esc(f.text)}</div>
      <div style="margin-top:10px"><button class="act" onclick="loadDelightCard(true)" style="font-size:12px">Another →</button>
      <button class="act" onclick="showTab('talk');setTimeout(()=>{const i=document.getElementById('msg');if(i){i.value='What can I do with you?';if(typeof autosize==='function')autosize(i);if(typeof sendText==='function')sendText();}},150)" style="font-size:12px;margin-left:6px">What else can we do?</button></div>`;
  }catch(e){ /* delight is optional sugar; never block the screen */ }
}
// ── Web Push subscription (real notifications, no extra app) ──────────────────────────────────
// Requires https (secure context) + service worker. Updates the Notifications card to reflect
// state, and lets the user opt in with one tap.
function _urlB64ToUint8(base64String){
  const padding='='.repeat((4-base64String.length%4)%4);
  const base64=(base64String+padding).replace(/-/g,'+').replace(/_/g,'/');
  const raw=atob(base64); const out=new Uint8Array(raw.length);
  for(let i=0;i<raw.length;i++) out[i]=raw.charCodeAt(i);
  return out;
}
function clearDevice(){ try{ clearAllOutboxes(); localStorage.removeItem('mongoose_device_token'); localStorage.removeItem('mongoose_device_profile'); localStorage.removeItem('mongoose_guest_public_name'); }catch(_e){} location.href='/device/launchpad'; }
async function deviceLogin(btn){
  const u=(document.getElementById('dl-user')||{}).value||'';
  const p=(document.getElementById('dl-pass')||{}).value||'';
  const msg=document.getElementById('dl-msg');
  if(!u.trim()||!p){ if(msg){msg.style.color='var(--dim)';msg.textContent='Enter your username and password.';} return; }
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/device/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
    const d=await r.json().catch(()=>({}));
    if(r.ok && d && d.ok && d.device_token){
      try{ localStorage.setItem('mongoose_device_token', d.device_token); if(d.profile) localStorage.setItem('mongoose_device_profile', d.profile); }catch(_e){}
      if(msg){ msg.style.color='var(--ok)'; msg.textContent='Signed in. Loading Mongoose...'; }
      setTimeout(function(){ location.reload(); }, 500);
    } else {
      if(msg){ msg.style.color='var(--dim)'; msg.textContent=(d&&d.error)||'Sign in failed.'; }
      if(btn) btn.disabled=false;
    }
  }catch(e){ if(msg){msg.style.color='var(--dim)';msg.textContent='Could not reach Mongoose.';} if(btn) btn.disabled=false; }
}
function askOwnerRepair(a){
  const msg=document.getElementById('dl-msg');
  if(msg){ msg.style.color='var(--dim)'; msg.textContent='Ask {{owner.name}} to open Mongoose on their phone (Status tab) and send you a repair link. The automatic request-and-approve flow is coming next.'; }
}
async function setSigninPassword(btn){
  const p=(document.getElementById('sp-pass')||{}).value||'';
  const msg=document.getElementById('sp-msg');
  if(!p || p.length<8){ if(msg){msg.style.color='var(--dim)';msg.textContent='Pick a password of at least 8 characters.';} return; }
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/account/set-password',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Mongoose-Device-Token':token()},body:JSON.stringify({password:p})});
    const d=await r.json().catch(()=>({}));
    if(r.ok && d && d.ok){ if(msg){msg.style.color='var(--ok)';msg.textContent='Saved. If this phone ever signs out, sign back in with "'+esc(d.username||'your username')+'" and this password.';} const i=document.getElementById('sp-pass'); if(i) i.value=''; }
    else { if(msg){msg.style.color='var(--dim)';msg.textContent=(d&&d.error)||'Could not set the password.';} }
  }catch(e){ if(msg){msg.style.color='var(--dim)';msg.textContent='Could not reach Mongoose.';} }
  finally{ if(btn) btn.disabled=false; }
}
async function updateNotifCard(){
  const st=document.getElementById('notif-status'); const btn=document.getElementById('notif-btn');
  if(!st) return;
  if(!('serviceWorker' in navigator) || !('PushManager' in window)){
    st.textContent='This browser does not support push notifications.'; return;
  }
  if(!window.isSecureContext){
    st.innerHTML='Notifications need a secure (https) connection. Once Mongoose is on https, you can enable them here.';
    return;
  }
  try{
    const reg=await navigator.serviceWorker.ready;
    const sub=await reg.pushManager.getSubscription();
    if(sub){
      // The browser can hold a subscription the SERVER never stored (data cleared, re-paired to a
      // new device id, or a DB reset), then this card says "on" while pokes/nudges have nowhere to
      // land. Re-register the existing subscription every time we confirm it (idempotent upsert), so
      // "on" here always means the server can actually deliver, bound to THIS device's identity.
      try{ await api('/push/subscribe',{method:'POST',body:JSON.stringify({subscription:sub.toJSON(),device_label:(navigator.userAgent.match(/iPhone|iPad|Android|Mac|Windows/)||['device'])[0]})}); }catch(_e){}
      st.innerHTML='<span style="color:var(--ok)">✓ Notifications are on for this device.</span>'
        +'<div class="muted" style="font-size:12px;margin-top:6px">For the most reliable delivery, add Mongoose to your home screen. When the app is fully closed your phone may delay or batch alerts to save battery, that\'s the operating system, not Mongoose.</div>';
      if(btn){ btn.textContent='Turn off notifications'; btn.style.display='inline-block';
        btn.onclick=disablePush; } }
    else if(Notification.permission==='denied'){
      st.innerHTML='Notifications are blocked in your browser settings for this site. Enable them there to receive alerts.';
    } else {
      st.textContent='Get notified when something needs you, even when the app is closed.';
      if(btn){ btn.textContent='Enable notifications'; btn.style.display='inline-block'; btn.onclick=enablePush; }
    }
  }catch(e){ st.textContent='Could not check notification status.'; }
}
async function enablePush(){
  const st=document.getElementById('notif-status');
  try{
    const perm=await Notification.requestPermission();
    if(perm!=='granted'){ if(st) st.textContent='Notification permission was not granted.'; return; }
    const keyResp=await api('/push/vapid-key');
    if(!keyResp.ok){ if(st) st.textContent='Server is not ready for push yet.'; return; }
    const reg=await navigator.serviceWorker.ready;
    const sub=await reg.pushManager.subscribe({userVisibleOnly:true,
      applicationServerKey:_urlB64ToUint8(keyResp.public_key)});
    await api('/push/subscribe',{method:'POST',
      body:JSON.stringify({subscription:sub.toJSON(),device_label:(navigator.userAgent.match(/iPhone|iPad|Android|Mac|Windows/)||['device'])[0]})});
    if(st) st.innerHTML='<span style="color:var(--ok)">✓ Notifications enabled!</span>';
    updateNotifCard();
  }catch(e){ if(st) st.textContent='Could not enable notifications: '+(e.message||e); }
}
async function disablePush(){
  const st=document.getElementById('notif-status');
  try{
    const reg=await navigator.serviceWorker.ready;
    const sub=await reg.pushManager.getSubscription();
    let serverOk=true;
    if(sub){
      // Do not swallow a failed server-side removal (external audit, additional findings
      // #7): the endpoint used to keep receiving pushes from Mongoose's side even after this
      // said "turned off", because a 403/network failure here was silently caught and
      // ignored. Now the local unsubscribe still happens either way (this browser stops
      // asking), but the message tells the truth about the server side too.
      try{
        const r=await api('/push/unsubscribe',{method:'POST',body:JSON.stringify({endpoint:sub.endpoint})});
        serverOk=!!(r&&r.ok);
      }catch(e){ serverOk=false; }
      await sub.unsubscribe();
    }
    if(st) st.textContent=serverOk
      ? 'Notifications turned off for this device.'
      : 'Turned off locally, but Mongoose could not confirm it server-side; it may still try to notify this device until that clears up.';
    updateNotifCard();
  }catch(e){ if(st) st.textContent='Could not turn off notifications.'; }
}
// ── boot ──
if(window.speechSynthesis){loadVoicesPhone();if(speechSynthesis.onvoiceschanged!==undefined)speechSynthesis.onvoiceschanged=loadVoicesPhone;}
// Connection awareness: show a small banner when the Mac is unreachable, clear it the moment
// we reconnect, and re-pull state automatically. A network blip should self-heal, not strand
// the user on a dead screen.
let _wasOffline=false;
function setConnBanner(offline){
  let b=document.getElementById('conn-banner');
  if(offline){
    if(!b){ b=document.createElement('div'); b.id='conn-banner';
      b.setAttribute('role','status');
      b.style.cssText='position:fixed;top:0;left:0;right:0;z-index:200;background:#3a2a1a;color:#f0c890;'+
        'font-size:13px;text-align:center;padding:7px;border-bottom:1px solid var(--gold-dim)';
      b.textContent='Reconnecting to Mongoose…';
      document.body.appendChild(b); }
  } else if(b){ b.remove(); }
}
function _refreshActiveTab(){
  // Refresh ONLY the tab the owner is actually on. The old code force-called loadStatus() on every
  // reconnect regardless of tab, which re-hammered the slow /device/summary and, on a busy single-thread
  // server, stalled the next navigation, the exact loop that yanked the owner back to Status.
  try{
    var cur=(document.querySelector('.tab.on')||{}).id||''; cur=cur.replace('tab-','');
    if(cur==='home') loadStatus();
    else if(cur==='talk') loadHistory();
    // cockpit + irc self-manage on their own timers; never force Status or switch the owner's tab
  }catch(e){}
}
async function connProbe(){
  try{
    await api('/device/heartbeat',{method:'POST',timeoutMs:8000,noRetry:true});
    if(_wasOffline){ _wasOffline=false; setConnBanner(false);
      try{ flushOutbox(); }catch(e){} _refreshActiveTab(); }
  }catch(e){ _wasOffline=true; setConnBanner(true); }
}
// The browser 'offline'/'online' events fire SPURIOUSLY on flaky mobile/PWA links, a brief stall during
// a slow load reads as 'offline', which flashed the banner and, on the 'online' bounce, re-triggered the
// reload loop. So do NOT trust the event: on 'offline', wait a moment and CONFIRM with a real heartbeat
// before declaring offline. A genuinely slow (but reachable) server no longer flashes the banner.
let _offlineTimer=null;
window.addEventListener('online', ()=>{ if(_offlineTimer){clearTimeout(_offlineTimer);_offlineTimer=null;} connProbe(); });
window.addEventListener('offline', ()=>{
  if(_offlineTimer) return;
  _offlineTimer=setTimeout(()=>{ _offlineTimer=null; connProbe(); }, 1500);
});
// First-run identity: ask the paired person, once, the name they want to be known by, and store it
// via /device/profile so the brain (and messages, notifications) always has a real human name. For an
// owner-class device this also sets the single owner identity used across every owner surface.
async function maybeAskName(){
  let dev={};
  try{ const w=await api('/device/whoami',{timeoutMs:8000}); dev=(w&&w.device)||{}; }
  catch(e){ return; } // can't check right now: never block the app on this
  // WHICH THREAD AM I WRITING INTO? Make it visible at the moment of chatting. A guest-profiled
  // device correctly and silently writes into its own isolated thread; without this line that is
  // indistinguishable in the moment from chatting into the shared brain, and the messages "vanish"
  // from the Mac's history view (they are safe, just in the guest thread).
  try{
    const row=document.getElementById('input-row'); const idl=document.getElementById('chat-identity');
    if(row && idl){
      row.style.position='relative';
      const isGuest = String(dev.profile||'')==='trusted_guest';
      idl.textContent = isGuest
        ? ('guest chat: your own private thread'+(dev.display_name?(' ('+dev.display_name+')'):''))
        : '';
    }
  }catch(_e){}
  if(dev.display_name && String(dev.display_name).trim()) return; // already named
  if(document.getElementById('name-overlay')) return;
  const ov=document.createElement('div'); ov.id='name-overlay';
  ov.setAttribute('role','dialog'); ov.setAttribute('aria-modal','true');
  ov.setAttribute('aria-label','What should Mongoose call you?');
  ov.style.cssText='position:fixed;inset:0;z-index:300;background:rgba(10,9,6,.93);display:flex;align-items:center;justify-content:center;padding:24px';
  ov.innerHTML='<div style="max-width:340px;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px">'
    +'<h2 style="margin:0 0 6px;font-size:19px">What should Mongoose call you?</h2>'
    +'<p style="margin:0 0 14px;color:var(--dim);font-size:13px">The name you want to be known by. You can change it later in your profile.</p>'
    +'<input id="name-input" type="text" autocomplete="name" autocapitalize="words" placeholder="e.g. {{owner.name}}" maxlength="40" style="width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--text);padding:12px 14px;border-radius:11px;font-size:16px;margin-bottom:12px">'
    +'<button id="name-save" style="width:100%;background:var(--gold);color:#211a0d;border:none;border-radius:11px;padding:12px;font-size:15px;font-weight:600;cursor:pointer">Save</button>'
    +'<div id="name-err" role="alert" style="color:var(--bad);font-size:12px;margin-top:8px;min-height:14px"></div></div>';
  document.body.appendChild(ov);
  const inp=document.getElementById('name-input'), btn=document.getElementById('name-save'), nerr=document.getElementById('name-err');
  try{ inp.focus(); }catch(e){}
  async function saveName(){
    const nm=inp.value.trim();
    if(!nm){ nerr.textContent='Please enter a name.'; return; }
    btn.disabled=true; btn.textContent='Saving…'; nerr.textContent='';
    try{
      const r=await api('/device/profile',{method:'POST',body:JSON.stringify({display_name:nm}),noRetry:true});
      if(r && r.ok!==false){ ov.remove(); try{ loadStatus(); }catch(e){} }
      else{ nerr.textContent=(r&&r.error)||'Could not save.'; btn.disabled=false; btn.textContent='Save'; }
    }catch(e){ nerr.textContent='Could not reach Mongoose.'; btn.disabled=false; btn.textContent='Save'; }
  }
  btn.addEventListener('click',saveName);
  inp.addEventListener('keydown',e=>{ if(e.key==='Enter')saveName(); });
}
if(!token()){
  thread.innerHTML=''; addBubble('sys','Not paired yet. Scan the QR code from the Mac cockpit.')
}else{
  maybeAskName()
  applyTabHash(true)
  loadHistory()
  setTimeout(pollSurprise, 8000)      // a little after you arrive
  setInterval(pollSurprise, 180000)   // then quietly every few minutes, like the desktop chat
  // heartbeat doubles as a connection probe: it updates the banner and auto-recovers
  setInterval(connProbe, 15000)
  connProbe()
  // flush any messages queued in a previous offline session, and keep retrying periodically
  flushOutbox()
  setInterval(flushOutbox, 12000)
}
(function(){var _p=document.getElementById('ping'); if(_p) _p.style.background=token()?'var(--ok)':'var(--bad)';})()
</script><script>
/* Notification permission driver: the end-to-end chain (permission -> pushManager.subscribe
   -> /push/subscribe) only starts if the person is ASKED. If permission is still 'default',
   nudge with a card; if 'granted' but this browser has no live subscription (cleared data,
   new phone), quietly resubscribe so notifications keep working without anyone noticing. */
(function(){
  var KEY='mongoose-notify-nudge-dismissed';
  function dismissed(){ try{ return localStorage.getItem(KEY)==='1'; }catch(_e){ return false; } }
  if(!('Notification' in window) || !('serviceWorker' in navigator)) return;
  window.addEventListener('load', function(){ setTimeout(async function(){
    try{
      if(Notification.permission==='granted'){
        var reg=await navigator.serviceWorker.ready;
        var sub=await reg.pushManager.getSubscription();
        if(!sub && typeof enablePush==='function'){ enablePush(); }
        return;
      }
      if(Notification.permission!=='default' || dismissed()) return;
      var host=document.querySelector('main')||document.body;
      var el=document.createElement('div');
      el.style.cssText='margin:10px 12px;padding:12px 14px;background:var(--panel2,#26221B);border:1px solid var(--line,#322D24);border-radius:12px;display:flex;gap:10px;align-items:center;';
      var s=document.createElement('span'); s.style.cssText='flex:1;color:var(--text,#ECE6DA);font-size:14px;';
      s.textContent='Turn on notifications so Mongoose can actually reach you: pokes, replies, and things that need you.';
      var b=document.createElement('button'); b.textContent='Turn on';
      b.style.cssText='padding:8px 14px;border-radius:10px;border:0;background:var(--gold,#C9A35B);color:#16140F;font-weight:600;';
      var x=document.createElement('button'); x.textContent='\u00d7'; x.setAttribute('aria-label','dismiss');
      x.style.cssText='background:none;border:0;color:var(--dim,#9C9486);font-size:18px;padding:4px 8px;';
      x.onclick=function(){ try{ localStorage.setItem(KEY,'1'); }catch(_e){} el.remove(); };
      b.onclick=function(){ el.remove(); if(typeof enablePush==='function'){ enablePush(); } else { Notification.requestPermission(); } };
      el.appendChild(s); el.appendChild(b); el.appendChild(x);
      // Sit the banner just under the tab bar (in normal flow) rather than at the very top of the
      // page, so its button is never tucked under the Dynamic Island / status bar.
      var tabsEl=document.getElementById('tabs');
      if(tabsEl && tabsEl.parentNode){ tabsEl.parentNode.insertBefore(el, tabsEl.nextSibling); }
      else { host.insertBefore(el, host.firstChild); }
    }catch(_e){}
  }, 800); });
})();
</script>
<script>
/* Install-as-app prompt: the standard Android/Chrome wiring commercial sites use.
   Chrome fires beforeinstallprompt when installability criteria pass (manifest + service
   worker + trusted HTTPS); we stash the event and show our own card, because the browser's
   own mini-infobar is unreliable. iOS never fires it, so iPhones/iPads get an Add-to-Home-
   Screen hint instead. Nothing shows when already installed (standalone display mode), after
   a dismissal (per-browser, localStorage), or on plain http / self-signed certs where Chrome
   will not offer install anyway. */
(function(){
  var KEY='mongoose-install-dismissed';
  function standalone(){ return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone===true; }
  function dismissed(){ try{ return localStorage.getItem(KEY)==='1'; }catch(_e){ return false; } }
  function card(text, btnLabel, onTap){
    var host=document.querySelector('main')||document.body;
    var el=document.createElement('div');
    el.id='install-card';
    el.style.cssText='margin:10px 12px;padding:12px 14px;background:var(--panel2,#26221B);border:1px solid var(--line,#322D24);border-radius:12px;display:flex;gap:10px;align-items:center;';
    var span=document.createElement('span'); span.style.cssText='flex:1;color:var(--text,#ECE6DA);font-size:14px;'; span.textContent=text;
    var b=document.createElement('button'); b.textContent=btnLabel;
    b.style.cssText='padding:8px 14px;border-radius:10px;border:0;background:var(--gold,#C9A35B);color:#16140F;font-weight:600;';
    var x=document.createElement('button'); x.textContent='\u00d7'; x.setAttribute('aria-label','dismiss');
    x.style.cssText='background:none;border:0;color:var(--dim,#9C9486);font-size:18px;padding:4px 8px;';
    x.onclick=function(){ try{ localStorage.setItem(KEY,'1'); }catch(_e){} el.remove(); };
    b.onclick=onTap;
    el.appendChild(span); el.appendChild(b); el.appendChild(x);
    host.insertBefore(el, host.firstChild);
    return el;
  }
  if(standalone()||dismissed()) return;
  window.addEventListener('beforeinstallprompt', function(e){
    e.preventDefault();
    var deferred=e;
    var el=card('Add Mongoose to your home screen: full screen, its own icon, one tap away.','Install', function(){
      el.remove();
      deferred.prompt();
      deferred.userChoice.then(function(){ deferred=null; });
    });
  });
  var ua=navigator.userAgent||'';
  if(/iphone|ipad|ipod/i.test(ua) && !window.MSStream){
    window.addEventListener('load', function(){
      setTimeout(function(){
        if(standalone()||dismissed()||document.getElementById('install-card')) return;
        card('Add Mongoose to your home screen: tap Share, then "Add to Home Screen".','How', function(){
          alert('In Safari: tap the Share button (square with an arrow), scroll down, tap "Add to Home Screen", then "Add".');
        });
      }, 1200);
    });
  }
})();
</script>
</body></html>"""


def device_launchpad_html() -> str:
    return _externalize_assets(_profile_page(DEVICE_LAUNCHPAD_HTML))


# ── Owner phone: full-cockpit, phone-native ───────────────────────────────────────────────────
# A redone, app-grid navigation for the OWNER's phone (profile primary_phone). Every Cockpit area
# is a tile; the high-traffic ones (Approvals, Scheduler, Devices) are self-contained phone screens
# that fit a phone with minimal scrolling, and the rest open the full Cockpit at /ui#<area> (which
# the owner phone can now reach, since an owner device token authenticates the whole surface).
# Pure static HTML/JS: calls existing /cockpit/* endpoints with the device token, plus the cockpit
# nonce on writes. A non-owner (guest) device sees a redirect note, the /cockpit/* calls are refused
# for guests by the request guard regardless.
PHONE_COCKPIT_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<script>
/* Keep this phone surface mobile even when the browser has "Request Desktop Site" on for this origin.
   Android propagates that per-site toggle into an installed PWA. Desktop mode forces a wide layout
   viewport and ignores width=device-width, so everything renders tiny. When the layout comes out far
   wider than the physical screen we re-pin the viewport to the real screen width so it stays phone-sized. */
(function(){
  function pin(){
    try{
      var m=document.querySelector('meta[name="viewport"]');
      if(!m){return;}
      var sw=(window.screen&&window.screen.width)||0;
      var lw=document.documentElement.clientWidth||window.innerWidth||0;
      if(sw&&lw&&lw>sw*1.3){
        m.setAttribute('content','width='+sw+',initial-scale=1,viewport-fit=cover');
      }
    }catch(e){}
  }
  pin();
  window.addEventListener('DOMContentLoaded',pin);
  window.addEventListener('resize',pin);
})();
</script>
<title>Mongoose Cockpit</title>
<link rel="manifest" href="/device/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<style>
:root{--bg:#0f0d08;--card:#17130c;--line:#2a2418;--gold:#d4af37;--dim:#9a8f78;--ok:#5fb878;--bad:#d9544e;--fg:#ece3d0}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
html,body{margin:0;height:100%;width:100%;max-width:100vw;overflow-x:hidden}
body{background:var(--bg);color:var(--fg);font-family:-apple-system,system-ui,Segoe UI,sans-serif;font-size:16px;line-height:1.42;display:flex;flex-direction:column;height:100dvh;max-width:100vw;overflow:hidden;-webkit-text-size-adjust:100%;text-size-adjust:100%}
/* Flexbox overflow guard: a flex child defaults to min-width:auto and can stretch the whole column
   PAST the viewport, which makes a standalone PWA scale the page down ("desktop version, tiny").
   Clamp every flex child to zero min-width and 100% max-width so nothing widens the surface. */
body>*{min-width:0;max-width:100vw}
img,pre,table,.docgrid{max-width:100%}
/* iOS zooms the page when a focused input is under 16px. Floor every field at 16px so the
   phone PWA never shrinks the whole surface on tap. Inline font-size still wins where set,
   so the RTM inputs below are also lifted to 16px individually. */
input,select,textarea{font-size:16px}
header{display:flex;flex-direction:column;gap:8px;padding:12px 14px 10px;border-bottom:1px solid var(--line);flex:0 0 auto;position:relative;z-index:5;background:var(--bg)}
.hrow{display:flex;align-items:center;gap:8px}
#ribbon-search{width:100%;background:var(--card);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:11px 12px;font-size:16px;-webkit-appearance:none}
#ribbon-search:focus{outline:none;border-color:var(--gold)}
#ribbon-results{position:absolute;left:0;right:0;top:100%;z-index:30;background:var(--bg);border-bottom:1px solid var(--line);max-height:72vh;overflow-y:auto;-webkit-overflow-scrolling:touch;display:none}
.rib-opt{padding:11px 16px;border-top:1px solid var(--line);cursor:pointer;display:flex;align-items:center;gap:10px}
.rib-opt:first-child{border-top:none}
.rib-opt .ic{font-size:18px;width:22px;text-align:center;flex:0 0 auto}
.rib-opt .tx{flex:1;min-width:0}
.rib-opt .lb{font-size:15px}
.rib-opt.sel{background:rgba(212,175,55,.12)}
.rib-kind{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.04em;border:1px solid var(--line);border-radius:99px;padding:1px 7px;flex:0 0 auto}
header .ttl{font-weight:800;letter-spacing:.04em}
header button.back{background:none;border:none;color:var(--gold);font-size:26px;line-height:1;padding:2px 6px;display:none;cursor:pointer}
main{flex:1 1 auto;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:14px 16px 30px}
.view{display:none}.view.on{display:block}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 6px;text-align:center;color:var(--fg);cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:7px;min-height:84px;justify-content:center;box-sizing:border-box}
.tile .ic{font-size:24px;line-height:1}
.tile .lb{font-size:13px;font-weight:600}
.tile:active{border-color:var(--gold)}
/* Instant tactile press feedback on every button, so taps never feel dead while a view loads. */
button:active,.act:active,.ghost:active,.subtab:active{transform:translateY(1px);filter:brightness(.9)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.card.bad{background:var(--card);border-color:var(--bad);box-shadow:inset 0 0 0 1px rgba(208,120,94,.15)}
.card.bad::before{content:"";display:block;height:3px;margin:-14px -14px 12px;background:var(--bad);border-radius:14px 14px 0 0}
.card.warn{background:var(--card);border-color:var(--warn)}
.card h3{margin:0 0 6px;font-size:16px}
.muted{color:var(--dim);font-size:14px}
.row{display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid var(--line);font-size:15px}
.row:first-child{border-top:none}
button.act{background:var(--gold);color:#1a1408;border:none;border-radius:10px;padding:10px 15px;font-size:15px;font-weight:700;min-height:40px;cursor:pointer}
button.ghost{background:none;border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:9px 13px;font-size:15px;min-height:40px;cursor:pointer}
button.danger{background:none;border:1px solid var(--bad);color:var(--bad);border-radius:10px;padding:9px 12px;font-size:14px;min-height:40px;cursor:pointer}
.pill{display:inline-block;font-size:12px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.pill.ok{color:var(--ok);border-color:var(--ok)}
.pill.bad{color:var(--bad);border-color:var(--bad)}
.empty{text-align:center;color:var(--dim);padding:34px 10px}
a.lnk{color:var(--gold);text-decoration:none}
.subtabs{display:flex;flex-wrap:wrap;gap:6px;padding-bottom:8px;margin-bottom:4px}
.subtab{flex:0 0 auto;display:flex;align-items:center;min-height:34px;background:var(--card);border:1px solid var(--line);color:var(--dim);border-radius:99px;padding:6px 12px;font-size:13px;cursor:pointer}
.subtab.on{color:var(--gold);border-color:var(--gold)}
.bad{color:var(--bad)}
.docgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.doccard{background:var(--bg);border:1px solid var(--line);border-radius:14px;overflow:hidden;display:flex;flex-direction:column}
.docthumb{width:100%;aspect-ratio:4/3;background:#0b0906;display:flex;align-items:center;justify-content:center;overflow:hidden}
.docthumb img{width:100%;height:100%;object-fit:cover;display:block}
.docthumb .ph{font-size:36px;line-height:1;opacity:.85}
.docbody{padding:10px 11px;display:flex;flex-direction:column;gap:5px}
.docname{font-size:14px;font-weight:600;line-height:1.3;word-break:break-word}
.docmeta{font-size:12px;color:var(--dim)}
.docsum{font-size:13px;line-height:1.38;color:var(--fg);opacity:.92}
.docsum.analysing{color:var(--dim);font-style:italic;opacity:1}
.docfacts{font-size:13px;color:var(--fg);margin:5px 0 2px;padding:5px 8px;background:var(--card);border:1px solid var(--line);border-radius:8px}
.docchips{display:flex;flex-wrap:wrap;gap:5px;margin-top:2px}
.docchip{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--dim);background:var(--card)}
</style></head>
<body>
<header>
  <div class="hrow">
    <button class="back" id="back" onclick="go('home')">&lsaquo;</button>
    <img src="/static/img/mongoose-64.png" alt="" width="24" height="24" style="border-radius:6px">
    <div class="ttl" id="title">Mongoose</div>
  </div>
  <input id="ribbon-search" type="search" inputmode="search" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false" placeholder="Find a menu, section or function&hellip;">
  <div id="ribbon-results" role="listbox"></div>
</header>
<main id="main">
  <section class="view on" id="v-home">
    <div class="muted" id="who" style="margin-bottom:13px">Loading&hellip;</div>
    <!-- Server-rendered tile grid so the cockpit paints INSTANTLY from the cached shell, before the
         JS bundle downloads and parses. These mirror the AREAS list; renderGrid() re-emits identical
         markup on boot and reconciles any drift, so this is a fast preview, not a second source of
         truth. Taps become live the moment the bundle loads (onclick references AREAS/tileClick). -->
    <div class="grid" id="grid"><div class="tile" onclick="tileClick(AREAS[0])"><div class="ic">🏠</div><div class="lb">Overview</div></div><div class="tile" onclick="tileClick(AREAS[1])"><div class="ic">💬</div><div class="lb">Chat</div></div><div class="tile" onclick="tileClick(AREAS[2])"><div class="ic">🎙️</div><div class="lb">Voice</div></div><div class="tile" onclick="tileClick(AREAS[3])"><div class="ic">✅</div><div class="lb">Approvals</div></div><div class="tile" onclick="tileClick(AREAS[4])"><div class="ic">⏰</div><div class="lb">Scheduler</div></div><div class="tile" onclick="tileClick(AREAS[5])"><div class="ic">📱</div><div class="lb">Devices</div></div><div class="tile" onclick="tileClick(AREAS[6])"><div class="ic">📜</div><div class="lb">Activity</div></div><div class="tile" onclick="tileClick(AREAS[7])"><div class="ic">🔎</div><div class="lb">Search</div></div><div class="tile" onclick="tileClick(AREAS[8])"><div class="ic">⚙️</div><div class="lb">Setup</div></div><div class="tile" onclick="tileClick(AREAS[9])"><div class="ic">🟢</div><div class="lb">WhatsApp</div></div><div class="tile" onclick="tileClick(AREAS[10])"><div class="ic">🏢</div><div class="lb">RTM</div></div><div class="tile" onclick="tileClick(AREAS[11])"><div class="ic">🛠️</div><div class="lb">Build</div></div><div class="tile" onclick="tileClick(AREAS[12])"><div class="ic">🖥️</div><div class="lb">System</div></div><div class="tile" onclick="tileClick(AREAS[13])"><div class="ic">📊</div><div class="lb">Benchmarks</div></div></div>
  </section>
  <section class="view" id="v-approvals"><div id="approvals-root"></div></section>
  <section class="view" id="v-scheduler"><div id="scheduler-root"></div></section>
  <section class="view" id="v-devices"><div id="devices-root"></div></section>
  <section class="view" id="v-system"><div id="system-root"></div></section>
  <section class="view" id="v-activity"><div id="activity-root"></div></section>
  <section class="view" id="v-whatsapp"><div id="whatsapp-root"></div></section>
  <section class="view" id="v-setup"><div id="setup-root"></div></section>
  <section class="view" id="v-rtm"><div id="rtm-root"></div></section>
  <section class="view" id="v-build"><div id="build-root"></div></section>
  <section class="view" id="v-search"><div id="search-root"></div></section>
  <section class="view" id="v-benchmarks"><div id="benchmarks-root"></div></section>
  <section class="view" id="v-tasks"><div id="tasks-root"></div></section>
</main>
<script>
(function importPairingHandoff(){
  try{
    if(!location.hash || location.hash.indexOf('mongoose_device_token=')<0) return;
    var hp=new URLSearchParams(location.hash.slice(1));
    var tok=hp.get('mongoose_device_token')||'';
    var profile=hp.get('mongoose_device_profile')||'';
    if(tok && tok.length>20){
      localStorage.setItem('mongoose_device_token', tok);
      if(profile) localStorage.setItem('mongoose_device_profile', profile);
      history.replaceState(null, document.title, location.pathname+location.search);
    }
  }catch(_e){}
})();
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function token(){return localStorage.getItem('mongoose_device_token')||''}
let _nonce='';
async function nonce(){ if(_nonce) return _nonce; try{ const r=await fetch('/cockpit/nonce',{headers:{'X-Mongoose-Device-Token':token()}}); const j=await r.json(); _nonce=j.cockpit_nonce||''; }catch(e){} return _nonce; }
async function api(path,opts){
  opts=opts||{}; const h=opts.headers||{}; h['X-Mongoose-Device-Token']=token();
  // Some Cockpit GETs are nonce-gated too (e.g. WhatsApp state/activity), so always attach the
  // cockpit nonce. Endpoints that don't check it simply ignore the header.
  h['X-Cockpit-Nonce']=await nonce();
  const isWrite = opts.method && opts.method.toUpperCase()!=='GET';
  if(isWrite && opts.body && !h['Content-Type']) h['Content-Type']='application/json';
  const timeoutMs = opts.timeoutMs || 15000;
  const ctrl=new AbortController(); const t=setTimeout(()=>ctrl.abort(), timeoutMs);
  try{
    const r=await fetch(path,Object.assign({},opts,{headers:h,signal:ctrl.signal})); clearTimeout(t);
    if(!r.ok){
      // A 403 on a nonce-gated endpoint (RTM Docs/Receipts) almost always means the cached cockpit nonce
      // went stale: the server mints a fresh _COCKPIT_NONCE on every restart, and this device cached the
      // old one. Drop the cache, fetch a fresh nonce, and retry ONCE. A 403 fails before the handler runs,
      // so re-issuing (even a write) has no side effect. Without this, a redeploy left the phone stuck on
      // "Could not load RTM (owner only)" until a full manual reload.
      if(r.status===403 && !opts._retried){ _nonce=''; return api(path, Object.assign({},opts,{_retried:true})); }
      let m=''; try{ m=(await r.json()).message||''; }catch(_){} const e=new Error(m||('HTTP '+r.status)); e.status=r.status; throw e;
    }
    const ct=r.headers.get('content-type')||''; return ct.indexOf('json')>=0?r.json():r.text();
  }catch(e){ clearTimeout(t); throw e; }
}
// Every Cockpit area as a tile. screen = self-contained phone screen; ui = open full Cockpit area;
// open = jump elsewhere on the phone (chat/voice live in the launchpad).
var AREAS=[
  {ic:'\uD83C\uDFE0',lb:'Overview',open:function(){location.href='/device/launchpad#home'}},
  {ic:'\uD83D\uDCAC',lb:'Chat',open:function(){location.href='/device/launchpad#talk'}},
  {ic:'\uD83C\uDF99\uFE0F',lb:'Voice',open:function(){location.href='/device/launchpad#talk'}},
  {ic:'\u2705',lb:'Approvals',screen:'approvals'},
  {ic:'\u23F0',lb:'Scheduler',screen:'scheduler'},
  {ic:'\uD83D\uDCF1',lb:'Devices',screen:'devices'},
  {ic:'\uD83D\uDCDC',lb:'Activity',screen:'activity'},
  {ic:'\uD83D\uDD0E',lb:'Search',screen:'search'},
  {ic:'\u2699\uFE0F',lb:'Setup',screen:'setup'},
  {ic:'\uD83D\uDFE2',lb:'WhatsApp',screen:'whatsapp'},
  {ic:'\uD83C\uDFE2',lb:'RTM',screen:'rtm'},
  {ic:'\uD83D\uDEE0\uFE0F',lb:'Build',screen:'build'},
  {ic:'\uD83D\uDDA5\uFE0F',lb:'System',screen:'system'},
  {ic:'\uD83D\uDCCA',lb:'Benchmarks',screen:'benchmarks'},
  {ic:'\u2705',lb:'Tasks',screen:'tasks'}
];
var LOADERS={approvals:loadApprovals,scheduler:loadScheduler,devices:loadDevices,system:loadSystem,activity:loadActivity,whatsapp:loadWhatsapp,setup:loadSetup,rtm:loadRtm,build:loadBuild,search:loadSearch,benchmarks:loadBenchmarks,tasks:loadTasksScreen};
var TITLES={home:'Mongoose',approvals:'Approvals',scheduler:'Scheduler',devices:'Devices',system:'System',activity:'Activity',whatsapp:'WhatsApp',setup:'Setup',rtm:'RTM',build:'Build',search:'Search',benchmarks:'Benchmarks',tasks:'Tasks'};
function go(view){
  var vs=document.querySelectorAll('.view'); for(var i=0;i<vs.length;i++) vs[i].classList.remove('on');
  document.getElementById('v-'+view).classList.add('on');
  document.getElementById('back').style.display = view==='home'?'none':'block';
  document.getElementById('title').textContent=TITLES[view]||'Mongoose';
  document.getElementById('main').scrollTop=0;
  if(LOADERS[view])LOADERS[view]();
}
function tileClick(a){ if(a.screen)return go(a.screen); if(a.ui)return go(a.ui); if(a.open)return a.open(); }
function renderGrid(){
  document.getElementById('grid').innerHTML=AREAS.map(function(a,i){
    return '<div class="tile" onclick="tileClick(AREAS['+i+'])"><div class="ic">'+a.ic+'</div><div class="lb">'+esc(a.lb)+'</div></div>';
  }).join('');
}
function failCard(e,what){ return '<div class="card bad">Could not load '+what+(e&&e.status===403?' (owner only).':'.')+'</div>'; }
function macOnly(feature){ alert((feature||'That area')+' is still finished on the Mac cockpit. This phone screen keeps the common actions here so you do not hit a forbidden /ui page.'); }
// ---- Search ----
var _phoneSearchResults=[];
async function loadSearch(){
  var root=document.getElementById('search-root');
  root.innerHTML='<div class="card"><h3>Search everything</h3><div class="muted" style="margin-bottom:8px">Files, email, attachments, voicemails and notifications. Results get item refs, actions and a short-lived result set so chat can understand “the first one”.</div><input id="phone-search-q" type="search" placeholder="e.g. service charge email, watchdog bug" style="width:100%;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:10px;padding:11px;font-size:16px"><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"><button class="act" onclick="runPhoneSearch()">Search</button><button class="ghost" onclick="phoneSearchKind(\'file,email,attachment\')">Docs + mail</button><button class="ghost" onclick="phoneSearchKind(\'voicemail\')">Voicemails</button><button class="ghost" onclick="phoneSearchKind(\'notification\')">Notifications</button></div><div id="phone-search-hint" class="muted" style="margin-top:8px"></div></div><div id="phone-search-results"></div>';
  setTimeout(function(){ var q=document.getElementById('phone-search-q'); if(q){ q.focus(); q.addEventListener('keydown',function(e){ if(e.key==='Enter')runPhoneSearch(); }); }},50);
}
async function phoneSearchKind(kinds){ var q=document.getElementById('phone-search-q'); if(!q||!q.value.trim()){ if(q) q.focus(); return; } return runPhoneSearch(kinds); }
function _phoneActionLabels(actions){ return (actions||[]).slice(0,4).map(function(a){ return esc(a.label||a.action||'action'); }).join(' · '); }
async function runPhoneSearch(kinds){
  var q=(document.getElementById('phone-search-q')||{}).value||''; q=q.trim(); if(!q)return;
  var out=document.getElementById('phone-search-results'); var hint=document.getElementById('phone-search-hint');
  out.innerHTML='<div class="muted">Searching…</div>'; if(hint)hint.textContent='';
  try{
    var url='/find?q='+encodeURIComponent(q)+'&limit=15'+(kinds?('&kinds='+encodeURIComponent(kinds)):'');
    var d=await api(url,{timeoutMs:45000}); var rs=d.result_set_id||''; var items=d.results||[]; _phoneSearchResults=items;
    if(hint) hint.textContent=items.length? (items.length+' result(s)'+(rs?(' · result set '+rs):'')) : (d.empty_index?'Nothing indexed yet. Connect or sync a source in Setup.':'No matching items.');
    if(!items.length){ out.innerHTML='<div class="empty">No results.</div>'; return; }
    out.innerHTML=items.map(function(r,i){ var acts=_phoneActionLabels(r.actions); var rr=(r.rank_reason||[]).join(' · '); var ref=r.item_ref||''; return '<div class="card"><div class="row" style="border:none"><span style="max-width:70%"><b>'+esc(r.title||'(untitled)')+'</b><br><span class="muted">'+esc(r.kind||'item')+(r.connector?(' · '+esc(r.connector)):'')+'</span></span><span class="pill">#'+(i+1)+'</span></div>'+(r.where?('<div class="muted">'+esc(r.where)+'</div>'):'')+(r.snippet?('<div style="font-size:13px;margin-top:7px">'+esc(r.snippet)+'</div>'):'')+(ref?('<div class="muted" style="margin-top:7px">item '+esc(ref)+(rs?(' · set '+esc(rs)):'')+'</div>'):'')+(rr?('<div class="muted" style="margin-top:5px">why: '+esc(rr)+'</div>'):'')+(acts?('<div class="muted" style="margin-top:5px">actions: '+acts+'</div>'):'')+'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:9px">'+(ref?('<button class="ghost" onclick="copyItemRef(\''+esc(ref)+'\')">Copy ref</button><button class="ghost" onclick="showItemActions(\''+esc(ref)+'\',this)">Actions</button>'):'')+'</div><div class="muted" id="phone-actions-'+i+'" style="margin-top:7px"></div></div>'; }).join('');
  }catch(e){ out.innerHTML='<div class="card bad">Search failed: '+esc(e.message||e)+'</div>'; }
}
function copyItemRef(ref){
  try{ navigator.clipboard.writeText(ref); }
  catch(_e){ try{ prompt('Copy this item ref', ref); }catch(__){} }
}
function _actionBoxForButton(btn){
  const card=btn && btn.closest ? btn.closest('.card') : null;
  let box=card?card.querySelector('.phone-actions-box'):null;
  if(!box && card){ box=document.createElement('div'); box.className='phone-actions-box muted'; box.style.marginTop='8px'; card.appendChild(box); }
  return box || document.getElementById('phone-search-results');
}
async function phoneDownload(url, filename){
  const h={'X-Mongoose-Device-Token':token(),'X-Cockpit-Nonce':await nonce()};
  const r=await fetch(url,{headers:h});
  if(!r.ok) throw new Error('download failed');
  const blob=await r.blob(); const a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=filename||'mongoose-download'; document.body.appendChild(a); a.click();
  setTimeout(()=>{try{URL.revokeObjectURL(a.href);a.remove();}catch(_e){}},1000);
}
function relatedQuery(keys,title){
  const terms=(keys||[]).map(function(k){return String(k).replace(/^(term|email|phone):/,'');}).filter(Boolean).slice(0,4);
  if(!terms.length && title) terms.push(String(title).split(/\s+/).slice(0,4).join(' '));
  return terms.join(' ');
}
async function runItemAction(action, item, box){
  const a=action.action||action; const id=item.native_id || item.open || ''; const snap=item.snapshot||{};
  try{
    if(a==='search_nearby'){ const q=relatedQuery(item.correlation_keys,item.title); const inp=document.getElementById('phone-search-q'); if(inp) inp.value=q||item.title||''; await runPhoneSearch(); return; }
    if(a==='show_location'){ box.innerHTML='<div>Location:</div><code style="word-break:break-all;color:var(--fg)">'+esc(id||item.where||'unknown')+'</code>'; return; }
    if(a==='open_email' || a==='open_parent_email'){ const mid=(a==='open_parent_email'?(snap.email_id||snap.open):id); const d=await api('/email/message/'+encodeURIComponent(mid),{timeoutMs:20000}); const m=d.message||{}; box.innerHTML='<div style="color:var(--fg);font-weight:700">'+esc(m.subject||item.title||'Email')+'</div><div style="margin-top:4px">'+esc(m.sender||m.from||item.where||'')+'</div><div style="margin-top:8px;white-space:pre-wrap;color:var(--fg)">'+esc(String(m.body||m.text||m.snippet||'').slice(0,1200))+'</div>'; return; }
    if(a==='download_email_txt'){ await phoneDownload('/email/message/'+encodeURIComponent(id)+'/download?format=txt','email-'+id+'.txt'); box.textContent='Download started.'; return; }
    if(a==='download_email_eml'){ await phoneDownload('/email/message/'+encodeURIComponent(id)+'/download?format=eml','email-'+id+'.eml'); box.textContent='Download started.'; return; }
    if(a==='download_attachment'){ const aid=snap.attachment_id||id; await phoneDownload('/email/attachment/'+encodeURIComponent(aid)+'/download', snap.filename||('attachment-'+aid)); box.textContent='Download started.'; return; }
    if(a==='open_voicemail'){ const d=await api('/voicemail/'+encodeURIComponent(id),{timeoutMs:20000}); const v=d.voicemail||{}; box.innerHTML='<div style="color:var(--fg);font-weight:700">'+esc(v.subject||item.title||'Voicemail')+'</div><div style="margin-top:4px">'+esc(v.caller_number||item.where||'')+'</div><div style="margin-top:8px;white-space:pre-wrap;color:var(--fg)">'+esc(String(v.transcript||v.notice_text||'No transcript yet.').slice(0,1200))+'</div>'; return; }
    if(a==='download_audio'){ await phoneDownload('/voicemail/'+encodeURIComponent(id)+'/audio','voicemail-'+id+'.wav'); box.textContent='Download started.'; return; }
    if(a==='view_notification'){ box.innerHTML='<div style="color:var(--fg)">'+esc(item.title||'Notification')+'</div><div style="margin-top:6px">'+esc((item.snapshot&&item.snapshot.snippet)||item.where||'')+'</div>'; return; }
    box.textContent='This action is listed but not yet executable on the phone.';
  }catch(e){ box.textContent='Action failed: '+(e.message||e); }
}
function renderActionButtons(d,box){
  const actions=d.actions||[];
  if(!actions.length){ box.textContent='No actions available.'; return; }
  box.innerHTML='<div style="margin-bottom:6px">Actions for <b style="color:var(--fg)">'+esc(d.title||d.item_ref||'item')+'</b></div>'+actions.map(function(a,i){ return '<button class="ghost" style="margin:3px 5px 3px 0;font-size:12px" data-ai="'+i+'">'+esc(a.label||a.action||'action')+'</button>'; }).join('')+'<div class="muted" style="margin-top:6px">'+esc((d.correlation_keys||[]).slice(0,4).join(' · '))+'</div>';
  const btns=box.querySelectorAll('button[data-ai]'); for(let i=0;i<btns.length;i++){ btns[i].onclick=function(){ runItemAction(actions[parseInt(this.getAttribute('data-ai'),10)], d, box); }; }
}
async function showItemActions(ref,btn){
  if(btn){ btn.disabled=true; btn.textContent='Loading…'; }
  const box=_actionBoxForButton(btn); if(box) box.textContent='Loading actions…';
  try{
    var d=await api('/item/actions?item_ref='+encodeURIComponent(ref),{timeoutMs:15000});
    if(!d.ok){ if(box) box.textContent=d.error||'No actions available.'; return; }
    renderActionButtons(d,box);
  }catch(e){ if(box) box.textContent='Could not load actions.'; }
  finally{ if(btn){ btn.disabled=false; btn.textContent='Actions'; } }
}
// ---- Approvals ----
async function loadApprovals(){
  var root=document.getElementById('approvals-root'); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/cockpit/approvals'); var items=d.approvals||[]; window.__ap=items;
    if(!items.length){ root.innerHTML='<div class="empty">Nothing waiting for approval.</div>'; return; }
    // Normalise: backend sends actions:[] (generic shape). Legacy approve/cancel fields are also
    // accepted so an older server still works; in the current backend they are absent.
    window.__phoneApActions=[];
    root.innerHTML=items.map(function(it,i){
      var actions=it.actions||[];
      if(!actions.length){ if(it.approve) actions.push({label:'Approve',style:'go',url:it.approve}); if(it.cancel) actions.push({label:'Cancel',style:'no',url:it.cancel}); }
      var btns=actions.map(function(ac,ai){
        var idx=window.__phoneApActions.length; window.__phoneApActions.push(ac);
        return '<button class="act '+(ac.style==='no'?'ghost':'')+(ac.style==='go'?'go':'')+'" onclick="runPhoneApproval('+idx+')">'+esc(ac.label)+'</button>';
      }).join(' ');
      return '<div class="card" id="ap-'+i+'"><h3>'+esc(it.label||it.contact||it.kind||'Pending')+'</h3>'+
        '<div class="muted" style="margin-bottom:10px">'+esc(it.body||'')+'</div>'+
        '<div style="display:flex;gap:8px;flex-wrap:wrap">'+btns+'</div></div>';
    }).join('');
  }catch(e){ root.innerHTML=failCard(e,'approvals'); }
}
async function runPhoneApproval(idx){
  var ac=(window.__phoneApActions||[])[idx]; if(!ac) return;
  var card=null;
  // find the card that contains this button
  try{ card=document.querySelector('[onclick="runPhoneApproval('+idx+')"]').closest('.card'); }catch(_){}
  if(card) card.style.opacity=.5;
  try{
    var r=await api(ac.url,{method:'POST',body:JSON.stringify(ac.body||{})});
    if(!r||!r.ok){ if(card) card.style.opacity=1; alert(r&&r.error||'Action failed, nothing was changed.'); return; }
    if(card){ var host=card.parentNode; card.parentNode.removeChild(card);
      // The acted card is gone; do NOT rebuild the whole list (that reset the scroll). Just show the
      // empty state if nothing is left.
      if(host && !host.querySelector('.card')){ try{ var rt=document.getElementById('approvals-root'); if(rt) rt.innerHTML='<div class="empty">No pending approvals.</div>'; }catch(_e){} } }
  }catch(e){ if(card) card.style.opacity=1; alert('Could not complete this action. Nothing was changed.'); }
}
// ---- Scheduler ----
async function loadScheduler(){
  var root=document.getElementById('scheduler-root'); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/cockpit/scheduler'); var jobs=d.jobs||[];
    var head='<div class="card"><h3>Scheduler <span class="pill '+(d.running?'ok':'bad')+'">'+(d.running?'running':'stopped')+'</span></h3>'+      '<div class="muted">'+jobs.length+' job'+(jobs.length===1?'':'s')+'.</div>'+      '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><button class="act" onclick="schedulerControlMobile(\'free_memory\')">Free memory</button><button class="ghost" onclick="schedulerControlMobile(\'enable\',\'benchmark,reindex_files,sync_email,sync_tasks,delight_topup,self_reflection\')">Resume common jobs</button></div></div>';
    var list = jobs.length ? jobs.map(function(j){ var nm=String(j.name||j.job||j.id||'job'); var en=j.enabled!==false; return '<div class="card"><div class="row" style="border:none">'+      '<span>'+esc(nm)+(en?'':' <span class="pill bad">paused</span>')+'</span><span class="pill">'+esc(j.schedule||j.cron||j.next||'')+'</span></div>'+      (j.last?('<div class="muted">last: '+esc(j.last)+'</div>'):'')+      (j.disabled_reason?('<div class="muted">paused: '+esc(j.disabled_reason)+'</div>'):'')+      '<button class="ghost" onclick="schedulerControlMobile(\''+(en?'disable':'enable')+'\',\''+esc(nm)+'\')">'+(en?'Pause':'Resume')+'</button></div>'; }).join('') : '<div class="empty">No jobs scheduled.</div>';
    root.innerHTML=head+list;
  }catch(e){ root.innerHTML=failCard(e,'scheduler'); }
}
async function schedulerControlMobile(action,jobs){
  var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0;
  try{
    var body={action:action,reason:'phone scheduler control'}; if(jobs) body.jobs=jobs;
    await api('/cockpit/scheduler/control',{method:'POST',body:JSON.stringify(body)});
    await loadScheduler();
    if(mn) mn.scrollTop=y;  // keep the job list where it was instead of snapping to the top
  }catch(e){ alert('Could not update scheduler.'); }
}
// ---- Devices ----
async function loadDevices(){
  var root=document.getElementById('devices-root'); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/cockpit/devices'); var ds=d.devices||[];
    if(!ds.length){ root.innerHTML='<div class="empty">No paired devices.</div>'; return; }
    root.innerHTML=ds.map(function(dev){ return '<div class="card"><div class="row" style="border:none">'+
      '<span>'+esc(dev.name||dev.device_id)+'<br><span class="muted">'+esc(dev.profile||'')+(dev.last_seen?(' \u00b7 '+esc(dev.last_seen)):'')+'</span></span>'+
      '<button class="danger" onclick="revoke(\''+esc(dev.device_id)+'\',this)">Revoke</button></div></div>'; }).join('');
  }catch(e){ root.innerHTML=failCard(e,'devices'); }
}
async function revoke(id,btn){ if(!confirm('Revoke this device? It loses access immediately.'))return; btn.disabled=true;
  var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0;
  try{ await api('/cockpit/devices/revoke',{method:'POST',body:JSON.stringify({device_id:id})}); await loadDevices(); if(mn) mn.scrollTop=y; }catch(e){ btn.disabled=false; alert('Could not revoke.'); } }
// ---- System ----

// ---- System manage panels (phone copy; desktop keeps its own handler set) ----
async function renderSystemManagePanels(root){
  if(!root) return;
  // Idempotent: render the manage cluster into ONE dedicated #sys-manage container we REPLACE, never
  // append to root. Previously this used insertAdjacentHTML('beforeend',...) three times, so when
  // loadSystem ran twice with overlap (tab re-activate, a refresh race, or the error path also calling
  // us) the async appends stacked and the whole cluster rendered twice. The "why is this so repetitive"
  // bug. A replaced single container can never duplicate, however many times we are called.
  let box=root.querySelector('#sys-manage');
  if(!box){ box=document.createElement('div'); box.id='sys-manage'; root.appendChild(box); }
  let ver=''; try{ const d=await api('/device/summary'); ver=(d.health&&d.health.version)?('v'+d.health.version):''; }catch(e){}
  box.innerHTML=`
    <div class="card"><h2>Update Mongoose</h2>
      <div class="body" style="font-size:13px;color:var(--dim);margin-bottom:10px">Upload a Mongoose package (.zip) to update the Mac${ver?(' (currently '+esc(ver)+')'):''}. Mongoose verifies the file is a real package, runs its safety checks, snapshots the brain first, and can roll back. This restarts the app, so it may go quiet for a moment.</div>
      <input type="file" id="updzip" accept=".zip,application/zip" style="width:100%;font:inherit;color:var(--text);margin-bottom:10px">
      <button class="act go" id="updbtn" onclick="doDeviceUpdate(this)" style="width:100%">Check and apply update</button>
      <div id="updout" class="body" style="font-size:13px;margin-top:10px"></div></div>
    <div class="card"><h2>Staged self-update</h2>
      <div class="muted" style="margin-bottom:8px">Fetch the next version from your configured source and stage it. Safety checks run before anything applies; applying stays a deliberate step (mongoose update apply).</div>
      <button class="act go" onclick="suFetch(this)">Check for an update</button>
      <div id="su-msg" class="muted" style="margin-top:8px"></div>
      <div id="su-stages" class="muted" style="margin-top:8px">Loading&hellip;</div></div>
    <div class="card"><h2>Data export</h2>
      <div class="muted" style="margin-bottom:8px">Download a scoped, owner-only export of your data. Secrets are never included; the broad export redacts private journal wording, so use the journal export if you want that.</div>
      <div style="display:flex;gap:8px;align-items:center"><select id="dumpKind" style="background:#141109;border:1px solid var(--line);border-radius:8px;padding:8px;color:var(--text)"></select><button class="act go" onclick="dumpCreate(this)">Create export</button></div>
      <div id="dump-out" class="muted" style="margin-top:8px"></div>
      <div id="dump-list" class="muted" style="margin-top:8px"></div></div>
    <div class="card"><h2>What Mongoose suggested</h2>
      <div class="muted" style="margin-bottom:8px">Recent proactive nudges and how you rated them. A dismiss backs that nudge off for a good while.</div>
      <div id="sugg-list" class="muted">Loading&hellip;</div></div>
    <div class="card"><h2>Brain snapshots</h2>
      <div class="muted" style="margin-bottom:8px">Daily safety copies of your brain. They add up (roughly 800 MB each); prune to the newest 14 to reclaim disk. Read-only otherwise.</div>
      <div id="snap-box" class="muted">Loading&hellip;</div></div>`;
  suLoadStages(); dumpLoadList(); suggLoad(); loadPhoneSnapshots();
  if(typeof loadKnowledge==='function'){
    box.insertAdjacentHTML('beforeend','<details class="card" open><summary>Knowledge and insights</summary><div id="sys-knowledge" class="muted">Loading knowledge, care and research&hellip;</div></details>');
    loadKnowledge();
  }
}
// Brain snapshots on the phone: count + total size + a prune-to-14 action, mirroring the desktop
// card so this owner journey is not terminal-only. POST /cockpit/snapshots/prune then reload.
function fmtSnapSize(b){ b=b||0; if(b>=1073741824) return (b/1073741824).toFixed(2)+' GB'; return fmtBytes(b); }
async function loadPhoneSnapshots(){
  var box=document.getElementById('snap-box'); if(!box) return;
  try{
    var s=await api('/cockpit/snapshots');
    var snaps=s.snapshots||[]; var n=(s.count!=null?s.count:snaps.length);
    var rows='';
    snaps.slice(0,8).forEach(function(sn){ rows+='<div class="row"><span>'+esc(sn.name||'?')+'</span><span class="muted">'+fmtSnapSize(sn.db_bytes||0)+'</span></div>'; });
    if(snaps.length>8) rows+='<div class="muted" style="font-size:12px;margin-top:4px">…and '+(snaps.length-8)+' older</div>';
    box.classList.remove('muted');
    box.innerHTML='<div class="row" style="border:none"><span>'+n+' snapshot'+(n===1?'':'s')+'</span><span class="muted">'+fmtSnapSize(s.total_bytes||0)+' total</span></div>'+rows+
      '<div style="margin-top:10px"><button class="ghost" onclick="prunePhoneSnapshots(this)">Prune to newest 14</button></div>'+
      '<div id="snap-result" class="muted" style="margin-top:8px"></div>';
  }catch(e){ box.classList.remove('muted'); box.innerHTML='<div class="muted">Snapshots unavailable. <button class="ghost" onclick="loadPhoneSnapshots()">Retry</button></div>'; }
}
async function prunePhoneSnapshots(btn){
  if(!confirm('Delete all but the newest 14 brain snapshots?')) return;
  var out=document.getElementById('snap-result'); if(btn){btn.disabled=true;btn.textContent='Pruning…';}
  if(out)out.textContent='Pruning snapshots…';
  try{
    var d=await api('/cockpit/snapshots/prune',{method:'POST',body:JSON.stringify({keep:14})});
    var pr=(d&&d.pruned)||{};
    if(out)out.textContent='Removed '+(pr.deleted||0)+', kept '+(pr.kept||0)+'.';
    setTimeout(loadPhoneSnapshots,600);
  }catch(e){ if(out)out.textContent='Prune failed.'; if(btn){btn.disabled=false;btn.textContent='Prune to newest 14';} }
}
async function dumpOpen(id){
  try{
    const d=await api('/cockpit/dump/redeem',{method:'POST',body:JSON.stringify({dump_id:id})});
    if(d && d.ok && d.download_url){ window.open(d.download_url,'_blank'); }
    else { alert((d&&d.error)||'Could not fetch a download link.'); }
  }catch(e){ alert('Download failed.'); }
}
async function suFetch(btn){ btn.disabled=true; const m=document.getElementById('su-msg'); if(m)m.textContent='Checking your update source\u2026';
  try{ const d=await api('/cockpit/update/fetch',{method:'POST',body:JSON.stringify({}),timeoutMs:60000}); if(m)m.textContent=(d&&d.ok)?('Staged '+(d.version||d.stage_id||'a candidate')+'.'):((d&&(d.error||d.detail))||'No update found.'); }
  catch(e){ if(m)m.textContent='Could not reach the update source.'; } finally{ btn.disabled=false; suLoadStages(); } }
async function suLoadStages(){ const el=document.getElementById('su-stages'); if(!el) return;
  try{ const d=await api('/cockpit/update-stage/list',{method:'POST',body:JSON.stringify({})}); const st=(d&&d.stages)||[];
    if(!st.length){ el.innerHTML='<div class="muted">No staged updates.</div>'; return; }
    let h=''; st.forEach(function(s){ const id=s.stage_id||s.id||''; const v=s.version||s.target_version||''; const ap=s.approved?'approved':(s.checks_passed?'checked':'staged');
      h+='<div class="row"><span class="k"><b>'+esc(v||id)+'</b><div class="muted" style="font-size:11px">'+esc(id)+' &middot; '+esc(ap)+'</div></span><span class="v"><button class="act" onclick="suCheck(\''+esc(id)+'\',this)">Check</button> <button class="act" onclick="suDrill(\''+esc(id)+'\',this)">Rollback drill</button> <button class="act go" onclick="suApprove(\''+esc(id)+'\',this)">Approve</button></span></div><div id="su-r-'+esc(id)+'" class="muted" style="font-size:11px;margin:-4px 0 8px"></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load staged updates.'; } }
async function suCheck(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Running checks\u2026';
  try{ const d=await api('/cockpit/update-stage/check',{method:'POST',body:JSON.stringify({stage_id:id}),timeoutMs:120000}); if(r)r.textContent=(d&&d.ok)?'Checks passed.':('Checks: '+JSON.stringify((d&&d.checks)||d).slice(0,150)); }
  catch(e){ if(r)r.textContent='Check failed.'; } finally{ if(btn)btn.disabled=false; } }
async function suDrill(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Dry-running rollback\u2026';
  try{ const d=await api('/cockpit/update-stage/rollback-drill',{method:'POST',body:JSON.stringify({stage_id:id})}); if(r)r.textContent=(d&&d.ok)?'Rollback path verified. You could roll back.':((d&&d.error)||'Rollback drill failed.'); }
  catch(e){ if(r)r.textContent='Rollback drill failed.'; } finally{ if(btn)btn.disabled=false; } }
async function suApprove(id,btn){ if(btn)btn.disabled=true; const r=document.getElementById('su-r-'+id); if(r)r.textContent='Approving\u2026';
  try{ const d=await api('/cockpit/update-stage/approve',{method:'POST',body:JSON.stringify({stage_id:id})}); if(r)r.textContent=(d&&d.ok)?('Approved. Apply from a terminal with: mongoose update apply '+id):((d&&d.error)||'Approve failed.'); }
  catch(e){ if(r)r.textContent='Approve failed.'; } finally{ if(btn)btn.disabled=false; suLoadStages(); } }
async function dumpLoadList(){ const el=document.getElementById('dump-list'); const sel=document.getElementById('dumpKind');
  try{ const d=await api('/cockpit/dump/list',{method:'POST',body:JSON.stringify({})});
    if(sel && !sel.options.length){ ((d&&d.kinds)||['everything']).forEach(function(k){ const o=document.createElement('option'); o.value=k; o.textContent=k; if(k==='everything')o.selected=true; sel.appendChild(o); }); }
    if(!el) return; const dumps=(d&&d.dumps)||[];
    if(!dumps.length){ el.innerHTML='<div class="muted">No exports yet.</div>'; return; }
    let h='<div class="muted" style="margin-bottom:4px">Recent exports:</div>'; dumps.forEach(function(x){ h+='<div class="row"><span class="k">'+esc(x.kind||'')+'<div class="muted" style="font-size:11px">'+esc((x.created_at||'').slice(0,16))+' &middot; '+Math.round((x.size_bytes||0)/1024)+' KB</div></span><span class="v"><button class="act" onclick="dumpOpen(\''+esc(x.dump_id||'')+'\')">Download</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ if(el)el.textContent='Could not load exports.'; } }
async function dumpCreate(btn){ const sel=document.getElementById('dumpKind'); const kind=(sel&&sel.value)||'everything'; const out=document.getElementById('dump-out'); btn.disabled=true; if(out)out.textContent='Building your export\u2026';
  try{ const d=await api('/cockpit/dump/create',{method:'POST',body:JSON.stringify({kind:kind}),timeoutMs:60000});
    if(d&&d.ok){ const rc=Object.keys(d.row_counts||{}).map(function(k){return k+':'+d.row_counts[k];}).join(', '); if(out)out.innerHTML='Ready. <a href="'+esc(d.download_url||('/dump/'+d.dump_id+'/download'))+'" target="_blank" rel="noopener" style="color:var(--accent)">Download '+esc(d.filename||'export')+'</a><div class="muted" style="font-size:11px;margin-top:4px">'+esc(rc)+'</div>'; }
    else { if(out)out.textContent='Export failed: '+((d&&d.error)||'unknown'); }
  }catch(e){ if(out)out.textContent='Export failed.'; } finally{ btn.disabled=false; dumpLoadList(); } }
async function suggLoad(){ const el=document.getElementById('sugg-list'); if(!el) return;
  try{ const d=await api('/cockpit/suggestions'); const ss=(d&&d.suggestions)||[];
    if(!ss.length){ el.innerHTML='<div class="muted">Nothing surfaced yet.</div>'; return; }
    let h=''; ss.forEach(function(s){ const fb=s.feedback?(' &middot; you said: '+esc(s.feedback)):''; h+='<div class="row"><span class="k">'+esc((s.text||'').slice(0,100))+'<div class="muted" style="font-size:11px">'+esc(s.kind||'')+fb+'</div></span><span class="v"><button class="act" onclick="suggFb(\''+esc(s.signature||'')+'\',\'useful\',this)">Useful</button> <button class="act" onclick="suggFb(\''+esc(s.signature||'')+'\',\'dismissed\',this)">Dismiss</button></span></div>'; });
    el.innerHTML=h;
  }catch(e){ el.textContent='Could not load suggestions.'; } }
async function suggFb(sig,fb,btn){ if(!sig) return; if(btn)btn.disabled=true;
  try{ await api('/cockpit/suggestions/feedback',{method:'POST',body:JSON.stringify({signature:sig,feedback:fb})}); }catch(e){} finally{ suggLoad(); } }

// Benchmark history on the phone: the desktop cockpit had a full history card but M drives the phone
// cockpit, so surface every recorded overnight run here. Renders the /cockpit/benchmarks `history`
// array: each dated run with its model(s), then per task the name, a truncated prompt, the output
// summary and the outcome. Read-only.
// ---- Tasks: view, reprioritise, set deadlines, and label RTM vs personal ----
// The task store is LOCAL. Microsoft To Do is imported INTO Mongoose and nothing is ever written
// back (the connector holds Tasks.Read only), so an RTM task cannot travel out to Microsoft. The
// screen states that plainly rather than leaving the owner to assume it.
var _tScope='all';
function tSetScope(s){ _tScope=s||'all'; loadTasksScreen(); }
async function loadTasksScreen(){
  var root=document.getElementById('tasks-root'); if(!root) return;
  root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/cockpit/tasks?limit=300&scope='+encodeURIComponent(_tScope));
    var ts=d.tasks||[], c=d.counts||{};
    var chip=function(lbl,key,n){ return '<button class="subtab'+(_tScope===key?' on':'')+'" onclick="tSetScope(\''+key+'\')">'+lbl+(n!=null?(' '+n):'')+'</button>'; };
    var head='<div class="card"><h3 style="margin:0 0 6px">Tasks</h3>'
      +'<div class="muted small">'+(c.total||0)+' open · '+(c.undated||0)+' with no deadline</div>'
      +'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">'
      +chip('All','all',c.total)+chip('RTM','rtm',(c.rtm_effective!=null?c.rtm_effective:c.rtm))+chip('Personal','personal',c.personal)+chip('Unclassified','unclassified',c.unclassified)
      +'</div>'
      +'<div class="muted small" style="margin-top:8px">Microsoft To Do syncs one way, INTO Mongoose. Nothing here is written back, so RTM tasks never reach Microsoft.</div></div>';
    var rows=ts.map(function(t){
      // A hard deadline and a self-set nudge are different facts, so the pill says which it is.
      var due=(t.due_at||''), nudge=(!due && t.reminder_at)?t.reminder_at:'';
      var when=due||nudge;
      var overdue=when && (new Date(when) < new Date());
      var pri=String(t.priority||'').toLowerCase();
      // Show the EFFECTIVE verdict: ticked by the owner, or suggested by the guess. An always-empty
      // box across every task told him nothing, which is how chat and this screen ended up disagreeing.
      var rtm=(t.is_rtm_effective===true);
      var suggested=(t.rtm_basis!=='owner');
      return '<div class="card" id="task-'+t.id+'"><div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">'
        +'<span style="font-weight:600;flex:1">'+esc(t.title||'(untitled)')+'</span>'
        +'<span class="pill'+(overdue?' bad':'')+'" style="flex:0 0 auto">'
        +(when?((due?'due ':'nudge ')+esc(String(when).slice(0,10))):'no date')+'</span></div>'
        +'<div class="muted small" style="margin-top:4px">'+esc(t.list_name||'')+(t.source?(' · '+esc(t.source)):'')+(pri?(' · '+esc(pri)):'')+'</div>'
        +'<div class="t-acts" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;align-items:center">'
        +'<input type="date" value="'+esc(String(due).slice(0,10))+'" onchange="tSetDue('+t.id+',this.value)" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:5px;color:var(--text)">'
        +'<select onchange="tSetPriority('+t.id+',this.value)" style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:5px;color:var(--text)">'
        +['','high','normal','low'].map(function(o){return '<option value="'+o+'"'+(pri===o?' selected':'')+'>'+(o||'priority')+'</option>';}).join('')
        +'</select>'
        +'<label class="muted small" title="'+(suggested?'Suggested by Mongoose. Tick or untick to decide it yourself.':'You set this.')+'" style="display:flex;align-items:center;gap:4px'+(suggested?';color:var(--gold)':'')+'">'
        +'<input type="checkbox"'+(rtm?' checked':'')+' onchange="tSetRtm('+t.id+',this.checked)"> RTM'+(suggested?' (guess)':'')+'</label>'
        +'<button class="ghost" style="padding:4px 9px;font-size:12px" onclick="tDone('+t.id+',this)">Done</button>'
        +'</div></div>';
    }).join('');
    root.innerHTML=head+(ts.length?rows:'<div class="empty">Nothing here.</div>');
  }catch(e){ root.innerHTML=failCard(e,'tasks'); }
}
// Edits update the ROW, not the screen. Every change used to refetch all 65 tasks and re-render the
// whole list, so setting a date meant a multi-second wait during which the task you were editing
// vanished and then reappeared somewhere else. The write is small and its result is known; only a
// reorder needs the list back, and that can wait until the next natural refresh.
async function _tPatch(id,body){
  try{ return await api('/cockpit/tasks/'+id,{method:'POST',body:JSON.stringify(body)}); }
  catch(e){ return {ok:false,error:(e&&e.message)||'failed'}; }
}
function _tFlash(id,msg,bad){
  var row=document.getElementById('task-'+id); if(!row) return;
  var n=row.querySelector('.t-note'); if(!n){ n=document.createElement('span'); n.className='t-note muted'; n.style.marginLeft='8px'; row.querySelector('.t-acts').appendChild(n); }
  n.textContent=msg; n.style.color=bad?'var(--bad,#b3403a)':'var(--dim)';
  clearTimeout(n._t); n._t=setTimeout(function(){ n.textContent=''; }, 2500);
}
async function _tEdit(id,body,label){
  _tFlash(id,'saving...');
  var r=await _tPatch(id,body);
  _tFlash(id, r&&r.ok===false ? (r.error||'could not save') : label, r&&r.ok===false);
}
async function tSetDue(id,v){ await _tEdit(id, v?{due_at:v}:{clear:['due_at']}, v?'date set':'date cleared'); }
async function tSetPriority(id,v){ await _tEdit(id, v?{priority:v}:{clear:['priority']}, v?'priority set':'priority cleared'); }
async function tSetRtm(id,on){ await _tEdit(id,{is_rtm:!!on}, on?'marked RTM':'marked personal'); }
async function tDone(id,btn){
  // Remove the row locally rather than refetching everything. A completed task is simply gone; making
  // the owner wait several seconds and watch the whole list rebuild to learn that is worse than useless.
  btn.disabled=true;
  var r=await _tPatch(id,{status:'completed'});
  if(r&&r.ok===false){ btn.disabled=false; _tFlash(id, r.error||'could not complete', true); return; }
  var row=document.getElementById('task-'+id); if(row){ row.style.opacity='.4'; setTimeout(function(){ row.remove(); }, 250); }
}
async function loadBenchmarks(){
  var root=document.getElementById('benchmarks-root'); if(!root) return;
  root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var m=await api('/cockpit/benchmarks');
    var hist=(m&&m.history)||[];
    var h='';
    if(m&&m.last_run){ h+='<div class="card"><h3>Latest run</h3><div class="row" style="border:none"><span>Last run</span><span class="muted">'+esc(m.last_run)+'</span></div>'+
      ((m.last_revelation_body)?('<div class="muted" style="margin-top:6px">'+esc(m.last_revelation_body)+'</div>'):'')+'</div>'; }
    if(!hist.length){ h+='<div class="empty">No benchmark runs recorded yet. Mongoose benchmarks your models overnight; check back after a run.</div>'; root.innerHTML=h; return; }
    h+='<div class="card"><h3>History</h3><div class="muted">Every recorded overnight run, newest first. Read-only.</div></div>';
    hist.forEach(function(run){
      var models=(run.models||[]).join(', ');
      var c='<div class="card"><h3>'+esc(run.date||'?')+'</h3><div class="muted" style="margin-bottom:6px">'+esc(models||'unknown model')+
        (run.task_count!=null?(' · '+run.task_count+' task'+(run.task_count===1?'':'s')):'')+'</div>';
      var tasks=run.tasks||[];
      if(!tasks.length){ c+='<div class="muted" style="font-size:12px">No per-task diagnoses recorded for this run.</div>'; }
      tasks.forEach(function(t){
        c+='<div style="margin:6px 0;padding-left:8px;border-left:2px solid var(--line)">'+
           '<div style="font-size:13px"><b>'+esc(t.task||'?')+'</b> <span class="muted">'+esc(t.model||'')+'</span></div>';
        if(t.prompt) c+='<div class="muted" style="font-size:12px;margin-top:2px">Prompt: '+esc(String(t.prompt).slice(0,160))+(String(t.prompt).length>160?'…':'')+'</div>';
        if(t.summary) c+='<div class="muted" style="font-size:12px;margin-top:2px">Output: '+esc(t.summary)+'</div>';
        if(t.outcome) c+='<div class="muted" style="font-size:12px;margin-top:2px">'+esc(t.outcome)+'</div>';
        c+='</div>';
      });
      c+='</div>';
      h+=c;
    });
    root.innerHTML=h;
  }catch(e){ root.innerHTML=failCard(e,'benchmarks'); }
}
async function loadSystem(){
  var root=document.getElementById('system-root'); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/system/status'); var m=d.mongoose||{}; var warn=m.warnings||[]; var home=d.home||{};
    var conn=(d.connectors&&d.connectors.summary)?d.connectors.summary:(d.connectors||{});
    var sup=null; try{ sup=await api('/cockpit/supervisor'); }catch(_){}
    var supWarn=(sup&&sup.running===false)?('<div class="card" style="border-color:var(--gold)"><h3 style="color:var(--gold)">No supervisor running</h3><div class="muted" style="margin:6px 0 10px">If Mongoose crashes or a restart fails it won\'t come back on its own. Start the supervisor so it and the server keep each other alive.</div><button class="ghost" style="border-color:var(--gold);color:var(--gold)" onclick="startSupervisorPhone(this)">Start supervisor</button></div>'):'';
    var h=supWarn+'<div class="card"><h3>Mongoose <span class="pill '+(m.ok?'ok':'bad')+'">'+(m.ok?'healthy':'attention')+'</span></h3>'+
      (warn.length?('<div class="muted" style="margin-top:6px">'+warn.length+' warning'+(warn.length===1?'':'s')+'</div>'+
        warn.slice(0,5).map(function(w){return '<div class="muted" style="margin-top:4px">&bull; '+esc(typeof w==='string'?w:(w.message||w.title||''))+'</div>';}).join('')):'<div class="muted" style="margin-top:6px">No warnings.</div>')+'</div>';
    h+='<div class="card"><div class="row" style="border:none"><span>Home Assistant</span><span class="pill '+(home.home_assistant_configured?'ok':'')+'">'+(home.home_assistant_configured?'configured':'off')+'</span></div>'+
       '<div class="row"><span>Alexa bridge</span><span class="pill '+(home.alexa_bridge_configured?'ok':'')+'">'+(home.alexa_bridge_configured?'configured':'off')+'</span></div></div>';
    if(conn && conn.total!=null){ h+='<div class="card"><div class="row" style="border:none"><span>Connectors</span><span class="muted">'+(conn.usable||0)+'/'+(conn.total||0)+' usable'+(conn.broken?(' \u00b7 '+conn.broken+' broken'):'')+'</span></div></div>'; }
    var res=null; try{ res=await api('/system/resources',{timeoutMs:12000}); }catch(_e){}
    if(res){ var r=res.resources||res; var cur=(r.current_process||{}); var mem=(r.system_memory||{}); h+='<div class="card"><h3>Resources <span class="pill '+((r.level==='red')?'bad':(r.level==='amber'?'':'ok'))+'">'+esc(r.level||'unknown')+'</span></h3>'+
      '<div class="row"><span>Python RSS</span><span class="muted">'+esc(cur.rss_mb!=null?(cur.rss_mb+' MB'):'unknown')+'</span></div>'+
      '<div class="row"><span>System memory</span><span class="muted">'+esc(mem.available_mb!=null?(mem.available_mb+' MB free'):'unknown')+'</span></div>'+
      ((r.warnings&&r.warnings.length)?r.warnings.slice(0,3).map(function(w){return '<div class="muted" style="margin-top:4px">• '+esc(w)+'</div>';}).join(''):'<div class="muted" style="margin-top:6px">No resource warnings.</div>')+'</div>'; }
    // Benchmark conclusions were desktop-only, so on the phone they were unreachable. Surface the
    // current verdict (or the still-gathering status) here, with apply/dismiss when a setup is vested.
    try{
      var tn=await api('/cockpit/tuning'); var bm=await api('/cockpit/benchmarks').catch(function(){return {};});
      var verdict = (tn&&tn.vested) ? (((tn.recommendation&&tn.recommendation.summary))||'A better model setup is ready to apply.')
                                    : ((tn&&tn.reason)||'Still gathering benchmark evidence.');
      var acts = (tn&&tn.vested) ? '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap"><button class="act go" onclick="phoneTune(\'apply\',this)">Switch to it</button><button class="ghost" onclick="phoneTune(\'dismiss\',this)">Not now</button></div>' : '';
      h+='<div class="card"><h3>Benchmarks and tuning</h3><div class="muted" style="margin-top:6px">'+esc(verdict)+'</div>'+
         ((bm&&bm.last_run)?('<div class="row"><span>Last run</span><span class="muted">'+esc(bm.last_run)+'</span></div>'):'')+
         ((bm&&bm.last_revelation_body)?('<div class="muted" style="margin-top:6px">'+esc(bm.last_revelation_body)+'</div>'):'')+acts+'</div>';
    }catch(_e){}
    h+='<div class="card"><button class="ghost" onclick="runDoctor(this)">Run doctor</button><div id="doctor-out" class="muted" style="margin-top:8px"></div></div>';
    h+='<div class="card" id="fortunes-card"><h3>🥠 Fortune cards</h3><div class="muted small" style="margin-bottom:6px">Fortune PDFs baked from chat. They are otherwise only reachable via a one-time chat link; this is the browse view. Read-only.</div><div id="fortunes-list" class="muted small">Loading&hellip;</div></div>';
    root.innerHTML=h;
    renderSystemManagePanels(root);
    loadFortunes();
  }catch(e){ root.innerHTML=failCard(e,'system'); renderSystemManagePanels(root); }
}
async function loadFortunes(){
  var el=document.getElementById('fortunes-list'); if(!el)return;
  try{
    var d=await api('/cockpit/fortunes'); var rows=d.fortunes||[];
    if(!rows.length){ el.innerHTML='<div class="muted small">No fortune cards yet. Ask in chat: “make me a fortune cookie”.</div>'; return; }
    el.innerHTML=rows.map(function(f){
      var kb=(f.size!=null)?(Math.max(1,Math.round(f.size/1024))+' KB'):'';
      var when=f.mtime?String(f.mtime).slice(0,10):'';
      var det=[kb,when].filter(function(x){return x;}).join(' · ');
      var link=f.download_url?('<a class="act" style="padding:3px 9px;font-size:12px" href="'+esc(f.download_url)+'" target="_blank" rel="noopener">⬇️ Open</a>'):'<span class="muted small">link unavailable</span>';
      return '<div class="row" style="align-items:center;gap:8px"><span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(f.name||'fortune.pdf')+(det?(' <span class="muted small">· '+esc(det)+'</span>'):'')+'</span>'+link+'</div>';
    }).join('');
  }catch(e){ el.innerHTML='<div class="muted small">Could not load fortunes: '+esc(String((e&&e.message)||e))+'</div>'; }
}
// The phone cockpit's System pane renders an "Update Mongoose" button but this function lived only
// in the launchpad shell, so on /device/cockpit the button threw ReferenceError and did nothing.
// Same implementation as the launchpad copy so both surfaces behave identically.
async function doDeviceUpdate(btn){
  const f=document.getElementById('updzip'); const out=document.getElementById('updout');
  if(!f||!f.files||!f.files[0]){ out.innerHTML='<span style="color:var(--warn)">Choose a .zip first.</span>'; return; }
  const file=f.files[0];
  if(!/\.zip$/i.test(file.name)){ out.innerHTML='<span style="color:var(--warn)">That isn\'t a .zip file.</span>'; return; }
  btn.disabled=true; out.innerHTML='Uploading and checking the package\u2026 this can take a minute. Keep this tab open.';
  try{
    const fd=new FormData(); fd.append('file', file, file.name);
    const r=await fetch('/device/update/upload',{method:'POST',headers:{'X-Mongoose-Device-Token':token()},body:fd});
    let d={}; try{ d=await r.json(); }catch(_){ d={ok:false,error:'no response from Mongoose'}; }
    if(d.ok){ out.innerHTML='<span style="color:var(--ok)">\u2713 Updated'+(d.target_version?(' to '+esc(d.target_version)):'')+'. '+esc(d.detail||'')+'</span>'; }
    else { out.innerHTML='<span style="color:var(--warn)">'+esc(d.error||('Update failed at the '+(d.stage||'?')+' step.'))+'</span>'; }
  }catch(e){ out.innerHTML='<span style="color:var(--warn)">Upload failed: '+esc(e.message||e)+'</span>'; }
  finally{ btn.disabled=false; }
}
// Act on a benchmark tuning verdict from the phone (apply the recommended setup, or dismiss it),
// then reload the System pane so the card reflects the new state.
async function phoneTune(action,btn){ if(btn)btn.disabled=true;
  var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0;
  try{ await api('/cockpit/tuning/'+encodeURIComponent(action),{method:'POST'}); }catch(_e){}
  await loadSystem(); if(mn) mn.scrollTop=y; }  // keep position instead of snapping to the top
async function startSupervisorPhone(btn){ if(btn){btn.disabled=true;btn.textContent='Starting\u2026';}
  try{ var d=await api('/cockpit/supervisor/start',{method:'POST'}); if(d&&d.ok!==false){ if(btn)btn.textContent='Started'; setTimeout(loadSystem,1400);} else { if(btn){btn.disabled=false;btn.textContent='Start supervisor';} } }
  catch(e){ if(btn){btn.disabled=false;btn.textContent='Start supervisor';} } }
async function runDoctor(btn){ btn.disabled=true; var out=document.getElementById('doctor-out'); out.textContent='Running\u2026';
  try{ var d=await api('/doctor'); out.textContent=(d.ok?'All checks passed.':((d.warnings&&d.warnings.length)?(d.warnings.length+' issue(s) found.'):'Completed.')); }
  catch(e){ out.textContent='Could not run doctor.'; } finally{ btn.disabled=false; } }
// ---- Activity (logs) ----
function fmtBytes(b){ b=b||0; if(b<1024)return b+' B'; if(b<1048576)return (b/1024).toFixed(0)+' KB'; return (b/1048576).toFixed(1)+' MB'; }
async function loadActivity(){
  var root=document.getElementById('activity-root'); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/cockpit/logs'); var logs=d.logs||[];
    if(!logs.length){ root.innerHTML='<div class="empty">No logs yet.</div>'; return; }
    root.innerHTML='<div class="card"><h3>Logs</h3><div class="muted">Tap a log for the latest lines.</div></div>'+
      logs.map(function(l){ return '<div class="card" style="cursor:pointer" onclick="viewLog(\''+esc(l.name)+'\',this)"><div class="row" style="border:none"><span>'+esc(l.name)+'</span><span class="muted">'+fmtBytes(l.bytes)+'</span></div></div>'; }).join('');
  }catch(e){ root.innerHTML=failCard(e,'activity'); }
}
async function viewLog(name,card){
  try{ var d=await api('/cockpit/logs/'+encodeURIComponent(name)); var txt=(typeof d==='string')?d:(d.text||d.content||(d.lines?d.lines.join('\n'):JSON.stringify(d)));
    var tail=String(txt).split('\n').slice(-40).join('\n');
    card.innerHTML='<div class="row" style="border:none"><span>'+esc(name)+'</span><span class="muted">latest</span></div><pre style="white-space:pre-wrap;font-size:11px;color:var(--dim);max-height:48vh;overflow:auto;margin:8px 0 0">'+esc(tail)+'</pre>';
    card.onclick=null; card.style.cursor='default';
  }catch(e){ alert('Could not open log.'); }
}
// ---- WhatsApp ----
var __waPoll=null;
function waStopPoll(){ if(__waPoll){ clearTimeout(__waPoll); __waPoll=null; } }
function waMsg(t){ var m=document.getElementById('wa-msg'); if(m) m.textContent=t; }
function waRoleCard(rs){
  var role=rs.role||((rs.account||{}).role)||'owner_read';
  var acc=rs.account||{}; var label=acc.label||role; var num=acc.number||'';
  var state=String(rs.state||'disconnected');
  var pill = state==='connected'?'<span class="pill ok">connected</span>'
           : (state==='scan_qr'||state==='connecting')?'<span class="pill">linking</span>'
           : (state==='not_configured'||state==='error')?'<span class="pill bad">service down</span>'
           : '<span class="pill bad">not linked</span>';
  var sub = role==='owner_read' ? 'Read/index your inbox so Mongoose can answer questions about it.'
                                : 'Mongoose sends notifications and approved messages from its own number.';
  var body='';
  if(state==='not_configured'||state==='error'){
    body='<div class="muted">'+esc(rs.detail||'Start the WhatsApp service first.')+'</div>';
  } else if(state==='disconnected'){
    body='<button class="ghost" onclick="waConnect(\''+role+'\',this)">Connect this account</button>';
  } else if(state==='scan_qr'||state==='connecting'){
    body='<div id="wa-qr-'+role+'"><div class="muted">Loading QR&hellip;</div></div>'+
         '<div class="muted" style="margin-top:6px">Scan on the phone for this account: WhatsApp &rarr; Settings &rarr; Linked devices &rarr; Link a device.</div>'+
         '<div style="margin-top:6px;color:var(--accent,#25D366)">Waiting for scan&hellip;</div>';
  } else {
    body='<div class="muted">Linked and ready.</div>';
    if(role==='mongoose_send'){
      body+='<div style="margin-top:8px"><input id="wa-test-to" placeholder="'+(num?esc(num):'owner number, e.g. +447...')+'" style="padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--panel2,#111);color:inherit;width:210px"/> '+
            '<button class="ghost" onclick="waSendTest(this)">Send test</button></div>';
    }
  }
  return '<div class="card"><h3>'+esc(label)+' '+pill+'</h3><div class="muted" style="margin:4px 0 8px">'+esc(sub)+'</div>'+body+'</div>';
}
async function loadWhatsapp(){
  var root=document.getElementById('whatsapp-root'); if(!root) return;
  waStopPoll(); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var st={}; try{ st=await api('/cockpit/whatsapp/state'); }catch(_){ st={roles:[]}; }
    var roles=st.roles||[];
    var anyDown = roles.some(function(r){ return r.state==='not_configured'||r.state==='error'; });
    var anyLinking = roles.some(function(r){ return r.state==='scan_qr'||r.state==='connecting'; });
    var allConnected = roles.length>0 && roles.every(function(r){ return r.state==='connected'; });
    var h='<div class="card"><h3>WhatsApp '+(allConnected?'<span class="pill ok">both linked</span>':'')+'</h3>'+
      '<div class="muted">Two accounts: <b>your inbox</b> (read) and <b>Mongoose\'s own number</b> (notifications). Link each with its own QR.</div>';
    if(anyDown){ h+='<div style="margin-top:8px"><button class="act go" onclick="waStart(this)">Start WhatsApp service</button></div>'; }
    h+='<div id="wa-msg" class="muted" style="margin-top:8px"></div></div>';
    if(!roles.length){ h+='<div class="empty">No WhatsApp accounts configured.</div>'; }
    roles.forEach(function(rs){ h+=waRoleCard(rs); });
    root.innerHTML=h;
    roles.forEach(function(rs){ var role=rs.role||((rs.account||{}).role); if(rs.state==='scan_qr'||rs.state==='connecting'){ waLoadQr(role); } });
    if(anyLinking){ window.__waSig=roles.map(function(r){return (r.role||'')+':'+r.state;}).join(','); __waPoll=setTimeout(waPoll,2500); }
  }catch(e){ root.innerHTML=failCard(e,'whatsapp'); }
}
async function waPoll(){
  // While a QR is up we used to rebuild the ENTIRE WhatsApp pane every 2.5s, which flickered and reset
  // scroll. Instead poll state, refresh only the QR image(s) in place, and rebuild ONCE if a role's
  // state actually changes (e.g. it links). No change -> no rebuild.
  var st={}; try{ st=await api('/cockpit/whatsapp/state'); }catch(_){ __waPoll=setTimeout(waPoll,2500); return; }
  var roles=st.roles||[];
  var sig=roles.map(function(r){return (r.role||'')+':'+r.state;}).join(',');
  if(sig!==window.__waSig){ loadWhatsapp(); return; }
  var linking=roles.filter(function(r){ return r.state==='scan_qr'||r.state==='connecting'; });
  linking.forEach(function(r){ waLoadQr(r.role||((r.account||{}).role)); });
  if(linking.length){ __waPoll=setTimeout(waPoll,2500); }
}
async function waLoadQr(role){
  try{ var q=await api('/cockpit/whatsapp/qr?role='+encodeURIComponent(role));
    var box=document.getElementById('wa-qr-'+role); if(!box) return;
    if(q && q.ok && q.qr_base64){ box.innerHTML='<img alt="QR" src="data:'+esc(q.mime||'image/png')+';base64,'+esc(q.qr_base64)+'" style="width:224px;height:224px;background:#fff;padding:10px;border-radius:12px"/>'; }
    else { box.innerHTML='<div class="muted">QR not ready yet, retrying&hellip;</div>'; }
  }catch(_){}
}
async function waStart(btn){ btn.disabled=true; waMsg('Starting the WhatsApp service (a few seconds)\u2026');
  try{ var j=await api('/cockpit/whatsapp/start',{method:'POST',timeoutMs:90000}); waMsg((j&&j.detail)||''); setTimeout(loadWhatsapp,3500); }
  catch(e){ waMsg('Could not start the service. Is Docker running?'); btn.disabled=false; } }
async function waConnect(role,btn){ btn.disabled=true; waMsg('Connecting\u2026');
  try{ await api('/cockpit/whatsapp/connect',{method:'POST',body:JSON.stringify({role:role})}); setTimeout(loadWhatsapp,1500); }
  catch(e){ waMsg('Connect failed. Try again in a moment.'); btn.disabled=false; } }
async function waSendTest(btn){ var el=document.getElementById('wa-test-to'); var to=(el&&el.value)||''; btn.disabled=true; waMsg('Sending a test from Mongoose\u2026');
  try{ var j=await api('/cockpit/whatsapp/test',{method:'POST',body:JSON.stringify({to:to})}); waMsg((j&&j.ok)?'Sent - check WhatsApp on your phone.':('Test could not send: '+((j&&(j.error||j.reason))||'unknown'))); }
  catch(e){ waMsg('Test failed to send.'); } finally{ btn.disabled=false; } }
// ---- Setup (connectors: verify / sync / sync-all / install-missing) ----
async function loadSetup(){
  var root=document.getElementById('setup-root'); if(!root) return; root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    // Connector probing hits live remotes and can be slow under load; a short timeout + tolerating
    // failure means Setup ALWAYS renders (the System section + Mac note) instead of hanging on
    // "Loading" for the full 15s api() timeout when a probe stalls. Pull to refresh retries.
    var d=null; try{ d=await api('/connectors/status',{timeoutMs:6000}); }catch(_cs){}
    var cs=(d&&d.connectors)||[]; var sm=(d&&d.summary)||{};
    var journey={journeys:[]}; try{ journey=await api('/connectors/journey',{timeoutMs:6000}); }catch(_e){}
    var byName={}; (journey.journeys||[]).forEach(function(j){ byName[String(j.name||'')]=j; });
    var connLine = d? ((sm.usable||0)+' usable \u00b7 '+(sm.configured||0)+' configured'+(sm.broken?(' \u00b7 '+sm.broken+' broken'):'')+' of '+(sm.total||cs.length)+'.')
                    : 'Connector status is taking a moment. Pull to refresh.';
    var h='<div class="card"><h3>Connectors</h3><div class="muted">'+connLine+'</div>'+
      '<div class="muted" style="margin-top:6px">Each card now shows the next real-life step: connect, verify, re-authenticate, or sync.</div>'+
      '<div style="display:flex;gap:8px;margin-top:8px"><button class="ghost" onclick="syncAll(this)">Sync all</button><button class="ghost" onclick="installMissing(this)">Install missing</button></div>'+
      '<div id="setup-msg" class="muted" style="margin-top:8px"></div></div>';
    h+= cs.length? cs.map(function(c){ var nm=String(c.name||c.label||'connector'); var st=(c.usable===false&&c.configured)?'bad':(c.usable?'ok':''); var lbl=c.usable?'usable':(c.configured?'broken':'off'); var jn=byName[nm]||{}; var nxt=jn.next_action||c.detail||'';
      // A not-configured connector needs CONNECTING, not verify/sync. Google Drive (backups) has a
      // phone sign-in; the others still complete on the Mac (loopback OAuth), so be honest about that.
      var connectBtn = (!c.configured)? (nm==='google_drive'
          ? '<button class="act" style="padding:6px 10px;font-size:12px" onclick="connectDrivePhone(this)">Connect</button>'
          : '<button class="ghost" style="padding:6px 10px;font-size:12px" onclick="macOnly(\'Connecting '+esc(nm)+'\')">Connect</button>') : '';
      return '<div class="card"><div class="row" style="border:none"><span>'+esc(nm)+' <span class="pill '+st+'">'+lbl+'</span><div class="muted" style="margin-top:5px">'+esc(nxt)+'</div></span>'+
        '<span style="display:flex;gap:6px">'+connectBtn+'<button class="ghost" style="padding:6px 10px;font-size:12px" onclick="verifyConn(\''+esc(nm)+'\',this)">Verify</button>'+
        '<button class="ghost" style="padding:6px 10px;font-size:12px" onclick="syncConn(\''+esc(nm)+'\',this)">Sync</button></span></div></div>'; }).join('') : '';
    // System status + safe controls, phone-visible (no secrets, no destructive buttons)
    try{
      var mg={},tr={},wh={};
      try{ mg=await api('/cockpit/memory/guard'); }catch(_a){}
      try{ tr=await api('/cockpit/voice/transcriber'); }catch(_b){}
      try{ wh=await api('/webhook/setup'); }catch(_c){}
      var trReady=!!(tr&&tr.status&&tr.status.ready);
      var hogs=(((mg&&mg.external_hogs)||[]).map(function(x){return esc(x.name)+' '+Math.round(x.mb||0)+'MB';})).join(', ');
      h+='<div class="card"><h3>System</h3>'+
        '<div class="row"><span class="muted">Chat model</span><span>'+esc((mg&&mg.protected_model)||'–')+'</span></div>'+
        '<div class="row"><span class="muted">Memory</span><span>'+esc(String((mg&&mg.level)||'–'))+(hogs?(' · '+hogs):'')+'</span></div>'+
        '<div class="row"><span class="muted">Transcriber</span><span>'+(trReady?'ready':'not installed')+'</span></div>'+
        '<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">'+
          '<button class="ghost" onclick="relieveMem(this)">Relieve memory</button>'+
          (trReady?'':'<button class="ghost" onclick="installTranscriber(this)">Install transcriber</button>')+
        '</div><div id="sys-msg" class="muted" style="margin-top:8px"></div></div>';
      h+='<div class="card"><h3>Webhook (RTM / HA → Mongoose)</h3>'+
        '<div class="muted">'+((wh&&wh.configured)?'Secret is configured.':'No secret set yet.')+'</div>'+
        ((wh&&wh.ingest_url)?('<div class="muted small" style="margin-top:4px;word-break:break-all">'+esc(wh.ingest_url)+'</div>'):'')+
        '<div class="muted small" style="margin-top:6px">The secret value is shown only on the Mac (Setup → Security &amp; HTTPS). Use that same value in WordPress and the HA add-on.</div></div>';
    }catch(_sys){}
    h+='<div class="card"><div class="muted">Google Drive backups connect right here on your phone (Connect above). Gmail, Calendar and Microsoft still complete in a browser on the Mac, as does editing config.</div>'+
       '<button class="ghost" style="margin-top:8px" onclick="macOnly(\'Full setup\')">What needs the Mac? &rarr;</button></div>';
    root.innerHTML=h;
  }catch(e){ root.innerHTML=failCard(e,'setup'); }
}
async function relieveMem(btn){ btn.disabled=true; var m=document.getElementById('sys-msg'); if(m)m.textContent='Relieving…';
  try{ var d=await api('/cockpit/memory/guard/relieve',{method:'POST'}); var ev=(d.evicted_secondary_models||[]); var rec=(d.recommendations||[]).map(function(x){return esc(x.name)+': '+esc(x.reclaim);});
    if(m)m.innerHTML=(ev.length?('Freed '+ev.map(esc).join(', ')+'. '):'Main model kept (nothing wasteful loaded). ')+(rec.length?('<br>To reclaim more: '+rec.join('<br>')):''); }
  catch(e){ if(m)m.textContent='Could not relieve memory.'; } finally{ btn.disabled=false; } }
async function installTranscriber(btn){ btn.disabled=true; var m=document.getElementById('sys-msg'); if(m)m.textContent='Installing transcriber, ~150MB download…';
  try{ await api('/cockpit/voice/transcriber/install',{method:'POST',timeoutMs:120000}); if(m)m.textContent='Install started, it will be ready shortly. Pull to refresh.'; }
  catch(e){ if(m)m.textContent='Install failed.'; } finally{ btn.disabled=false; } }
function setupMsg(t){ var m=document.getElementById('setup-msg'); if(m)m.textContent=t; }
async function syncAll(btn){ btn.disabled=true; setupMsg('Syncing all connectors\u2026');
  try{ await api('/connectors/sync-all',{method:'POST',timeoutMs:60000}); setupMsg('Sync complete.'); loadSetup(); }catch(e){ setupMsg('Sync failed.'); btn.disabled=false; } }
async function installMissing(btn){ btn.disabled=true; setupMsg('Installing missing dependencies\u2026');
  try{ var d=await api('/setup/install-missing',{method:'POST',timeoutMs:120000}); setupMsg((d&&d.detail)?d.detail:'Done.'); }catch(e){ setupMsg('Install failed.'); } finally{ btn.disabled=false; } }
async function verifyConn(name,btn){ btn.disabled=true; var o=btn.textContent; btn.textContent='\u2026';
  try{ var d=await api('/connectors/verify/'+encodeURIComponent(name),{method:'POST',timeoutMs:30000}); btn.textContent=(d&&(d.usable||d.ok))?'OK':'Failed'; setTimeout(function(){btn.textContent=o;btn.disabled=false;},2500); }catch(e){ btn.textContent=o; btn.disabled=false; } }
async function syncConn(name,btn){ btn.disabled=true; var o=btn.textContent; btn.textContent='\u2026';
  try{ await api('/connectors/sync/'+encodeURIComponent(name),{method:'POST',timeoutMs:45000}); btn.textContent='Synced'; setTimeout(function(){btn.textContent=o;btn.disabled=false;},2500); }catch(e){ btn.textContent=o; btn.disabled=false; } }
// Connect Google Drive for backups from the PHONE: ask the server for the consent URL (nonce-gated,
// mints a one-time state), then navigate there. Consent finishes at the public /oauth/drive/callback.
async function connectDrivePhone(btn){
  btn.disabled=true; var o=btn.textContent; btn.textContent='opening Google\u2026';
  var m=document.getElementById('setup-msg');
  try{
    var r=await api('/cockpit/drive-connect-url',{method:'POST',timeoutMs:15000});
    if(r&&r.ok&&r.url){ if(m)m.textContent='Sending you to Google. Approve access and you will come back here.'; location.href=r.url; return; }
    if(m)m.textContent=(r&&r.error)||'Could not start Google sign-in.'; btn.textContent=o; btn.disabled=false;
  }catch(e){ if(m)m.textContent='Could not start Google sign-in.'; btn.textContent=o; btn.disabled=false; }
}
// ---- Music ----
// ---- RTM (Example Court): sub-tabbed drill-downs ----
var _rtmTab='dashboard';
var _rtmRenderTok=0;
// Selected charging period for the RTM demands view. undefined = not yet resolved (first load picks the
// CURRENT period); null = the owner deliberately chose "All periods"; a string = that period.
var _rtmPeriod;
function rtmSetPeriod(p){ _rtmPeriod = p ? String(p) : null; rtmRender(); }
var _mailBox='inbox';
var _mailLimit=12;
var _mailBodies={};
var _docLimit=24;
var _rtmDocFlat='';   // per-flat document filter ('' = all flats)
function rtmDocFilterFlat(v){ _rtmDocFlat=String(v||''); _docLimit=24; rtmRender(); }
function rtmMailBox(b){ _mailBox=(b==='sent')?'sent':'inbox'; _mailLimit=12; rtmRender(); }
async function mailLoadMore(){
  var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0;
  _mailLimit+=20; await rtmRender(); if(mn) mn.scrollTop=y;  // grow the list, stay where you were
}
async function mailRefresh(btn){
  var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0;
  btn.disabled=true; var o=btn.innerHTML; btn.textContent='Syncing…';
  try{ await api('/cockpit/mail/refresh',{method:'POST',timeoutMs:45000}); }catch(e){}
  btn.disabled=false; btn.innerHTML=o; await rtmRender(); if(mn) mn.scrollTop=y;
}
// Progressive reveal: show the body a chunk at a time behind "Show more" buttons rather than
// unrolling the whole email. textContent (not innerHTML) keeps it safe and needs no escaping.
function mailRenderBody(id, text){ _mailBodies[id]={text:String(text==null?'':text), shown:0}; mailReveal(id); }
function mailReveal(id){
  var st=_mailBodies[id]; if(!st) return;
  var host=document.getElementById('mailread-'+id); if(!host) return;
  var CH=1400, end=st.shown+CH;
  if(end<st.text.length){ var nl=st.text.indexOf('\n', end); if(nl>-1 && nl-end<400) end=nl+1; }
  if(end>st.text.length) end=st.text.length;
  var pre=host.querySelector('pre.mail-read-pre');
  if(!pre){ pre=document.createElement('pre'); pre.className='email-body mail-read-pre'; pre.style.maxHeight='none'; host.appendChild(pre); }
  pre.textContent=st.text.slice(0, end); st.shown=end;
  var btn=host.querySelector('.mail-more-btn');
  if(end<st.text.length){
    if(!btn){ btn=document.createElement('button'); btn.className='act mail-more-btn'; btn.style.marginTop='8px';
      btn.onclick=function(ev){ ev.stopPropagation(); mailReveal(id); }; }
    var lbl='Show more ('+(st.text.length-end)+' more characters)'; btn.textContent=lbl; host.appendChild(btn);
  } else if(btn){ btn.remove(); }
}
function pick(o,ks){ for(var i=0;i<ks.length;i++){ var v=o[ks[i]]; if(v!=null&&v!=='') return v; } return ''; }
function moneyOf(o){
  var s=pick(o,['amount_outstanding','amount','total','balance','demanded']);
  if(s!=='') return (typeof s==='number')?('\u00a3'+s.toFixed(2)):String(s);
  var p=pick(o,['amount_outstanding_pence','amount_pence','total_pence','balance_pence','demanded_pence']);
  if(p!=='') return '\u00a3'+(Number(p)/100).toFixed(2);
  return '';
}
function rtmCard(primary,parts){
  var det=parts.filter(function(x){return x!==''&&x!=null;}).map(function(x){return esc(String(x));}).join(' \u00b7 ');
  return '<div class="card"><div style="font-size:14px">'+esc(String(primary||'\u2014'))+'</div>'+(det?('<div class="muted" style="margin-top:3px">'+det+'</div>'):'')+'</div>';
}
function rtmVoicemailWhen(v){ var s=v.voicemail_at||''; if(s){ return String(s).replace(/^\w+day,\s*/,''); } return rtmMailDate(v.email_date||v.indexed_at||''); }
function rtmVoicemailCard(v){
  var caller=esc(v.caller_number||'Unknown caller');
  var when=esc(rtmVoicemailWhen(v));
  var tr=String(v.transcript||'').trim();
  var body=tr?esc(tr):'<span class="muted">No transcript. Download the recording to listen.</span>';
  var mb=v.audio_size_bytes?(' ('+(Math.round(v.audio_size_bytes/1024/1024*10)/10)+' MB)'):'';
  return '<div class="card"><div class="row" style="border:none;padding:0">'+
    '<span style="font-weight:600">\ud83d\udcde '+caller+'</span><span class="muted small">'+when+'</span></div>'+
    '<div class="muted small" style="margin-top:6px;white-space:pre-wrap">'+body+'</div>'+
    '<button class="act" style="margin-top:8px;padding:4px 9px;font-size:12px" onclick="rtmVoicemailPlay('+(v.id)+',this)">\u2b07\ufe0f Download recording'+mb+'</button></div>';
}
async function rtmVoicemailPlay(id,btn){
  var o=btn.innerHTML; btn.disabled=true; btn.textContent='Downloading\u2026';
  try{ await phoneDownload('/voicemail/'+id+'/audio','voicemail-'+id+'.wav'); btn.innerHTML=o; btn.disabled=false; }
  catch(e){ btn.textContent='Download failed'; setTimeout(function(){btn.innerHTML=o;btn.disabled=false;},2000); }
}
var _rtmLoadedAt=0;
async function loadRtm(){
  // Coalesce a double-open: the RTM view was being loaded twice in quick succession (tile click + a hash/
  // nonce-driven re-nav), rebuilding the whole subtab bar and flashing. If we just built it moments ago,
  // skip the rebuild , the first load's content stands.
  var _now=Date.now();
  if(_now-_rtmLoadedAt<900 && document.getElementById('rtm-content')) return;
  _rtmLoadedAt=_now;
  var root=document.getElementById('rtm-root');
  var tabs=[['dashboard','Overview'],['mail','Mail'],['voicemail','Voicemail'],['arrears','Arrears'],['demands','Demands'],['payables','Payables'],['flats','Flats'],['docs','Docs'],['receipts','Receipts'],['ops','Ops']];
  root.innerHTML='<div id="rtm-sync-strip"></div><div class="subtabs">'+tabs.map(function(t){return '<button class="subtab'+(t[0]===_rtmTab?' on':'')+'" data-tab="'+t[0]+'" onclick="rtmTab(\''+t[0]+'\')">'+t[1]+'</button>';}).join('')+'</div><div id="rtm-content"><div class="muted">Loading&hellip;</div></div>';
  loadRtmSyncStrip();
  rtmRender();
}
async function loadRtmSyncStrip(){
  var el=document.getElementById('rtm-sync-strip'); if(!el)return;
  var s=null; try{ s=await api('/rtm/deploy/sync-status'); }catch(_e){ el.innerHTML=''; return; }
  if(!s){ el.innerHTML=''; return; }
  var colours={green:'#3ecf8e',amber:'#e0b341',red:'#e06a6a',unknown:'#7a7a7a'};
  var c=colours[s.level]||colours.unknown;
  var age=s.live_confirmation_age_seconds;
  var ago=function(iso){ if(!iso)return ''; var t=Date.parse(iso); if(isNaN(t))return ''; var d=(Date.now()-t)/1000; return d<90?'just now':(d<5400?Math.round(d/60)+'m ago':(d<172800?Math.round(d/3600)+'h ago':Math.round(d/86400)+'d ago')); };
  var when = age==null ? '' : (age<90?'just now':(age<5400?Math.round(age/60)+'m ago':(age<172800?Math.round(age/3600)+'h ago':Math.round(age/86400)+'d ago')));
  var sub = s.friendly_line || ((s.detail||'') + (when?(' · '+when):''));
  var h8=function(x){ return x? String(x).slice(0,8) : '-'; };
  var badge = s.build_mismatch ? '<span style="margin-left:6px;font-size:10px;background:#4a2a2a;color:#e0b341;border-radius:6px;padding:1px 5px">build mismatch</span>' : '';
  var builds='<div style="margin-top:5px;font-size:10px;color:var(--dim);font-family:monospace">'
    +'local '+esc(h8(s.local_hash))+' · deployed '+esc(h8(s.deployed_hash))+' · live '+esc(h8(s.live_hash))+badge+'</div>';
  var hb = s.heartbeat_last_ok_at ? ('<div style="margin-top:2px;font-size:10px;color:var(--dim)">last site check: '+esc(ago(s.heartbeat_last_ok_at))+'</div>')
        : (s.heartbeat_error ? '<div style="margin-top:2px;font-size:10px;color:#e0b341">site check: '+esc(s.heartbeat_error)+'</div>' : '');
  var pend = s.pending_plan ? '<div style="margin-top:4px;font-size:11px;color:var(--dim)">Pending: '+esc(s.pending_plan.name||'a plan')+'</div>' : '';
  var latest = s.local_ahead ? '<div style="margin-top:2px;font-size:10px;color:#e0b341">a newer local build is ready to deploy</div>' : '';
  // When WordPress is not connected on Mongoose's side, offer the connect form right here (the phone
  // Build view is Mongoose self-update, so this RTM deploy strip is the coherent home for it).
  var wpUnconfigured = /wordpress not configured/i.test(String(s.heartbeat_error||'')) || s.wp_configured===false;
  // Only show this deploy strip when there is something to ACT on (a build to deploy, a mismatch, a
  // pending plan, or WordPress not connected). In the steady state (deployed + in sync) it just showed
  // "deployed - / live -" noise and re-rendered/flashed on every RTM view, so hide it entirely then.
  var _actionable = !!(s.build_mismatch || s.local_ahead || s.pending_plan || wpUnconfigured);
  if(!_actionable){ el.innerHTML=''; return; }
  var inp='display:block;width:100%;margin:3px 0;padding:9px;border:1px solid var(--line);border-radius:7px;background:var(--panel2);color:var(--text);font-size:16px';
  var wpConnect = wpUnconfigured ? (
    '<div style="margin-top:8px;border-top:1px solid var(--line);padding-top:8px">'
    +'<div style="font-size:11px;color:var(--dim);margin-bottom:5px">Connect Mongoose to WordPress. In WordPress open Users, then your Profile, then Application Passwords, and create one for Mongoose.</div>'
    +'<input id="wp-url" placeholder="https://{{site.domain}}" value="https://{{site.domain}}" style="'+inp+'">'
    +'<input id="wp-user" placeholder="WordPress username" autocapitalize="none" style="'+inp+'">'
    +'<input id="wp-pass" type="password" placeholder="Application password" style="'+inp+'">'
    +'<button class="act go" style="margin-top:4px;padding:5px 10px;font-size:12px" onclick="connectWordpress()">Save and connect</button>'
    +'<span id="wp-connect-out" style="font-size:11px;margin-left:8px"></span></div>'
  ) : '';
  // The plugin lifecycle, made visible: BUILD its code (chat / Claude Code), DEPLOY the current source
  // to WordPress (one tap, beta, not activated), VERIFY the live site. This strip is the coherent home
  // for it (the Build view is Mongoose's OWN self-update, not the plugin).
  var wpOk = !wpUnconfigured;
  var actions='<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:6px">'
    +'<button class="act" style="padding:4px 9px;font-size:11px" onclick="rtmBuildInChat()">🛠️ Build / update code</button>'
    +(wpOk?'<button class="act go" style="padding:4px 9px;font-size:11px" onclick="rtmDeployPlugin(this)">🚀 Deploy to WordPress</button>':'')
    +'<button class="act" style="padding:4px 9px;font-size:11px" onclick="rtmVerifyNow(this)">Verify live site</button>'
    +'</div><div id="rtm-deploy-out" class="small" style="margin-top:6px"></div>';
  el.innerHTML='<div style="display:flex;align-items:flex-start;gap:10px;background:var(--panel2);border:1px solid var(--line);border-left:3px solid '+c+';border-radius:12px;padding:10px 12px;margin-bottom:12px">'
    +'<div style="width:8px;height:8px;border-radius:50%;background:'+c+';margin-top:5px;flex:0 0 auto"></div>'
    +'<div style="min-width:0;flex:1"><div style="font-weight:600;font-size:13px;color:var(--text)">'+esc(s.headline||'{{rtm.name}} plugin')+'</div>'
    +(sub?'<div style="margin-top:2px;font-size:11px;color:var(--dim)">'+esc(sub)+'</div>':'')
    +builds+hb+latest+pend+wpConnect+actions+'</div></div>';
}
function rtmBuildInChat(){
  // The plugin's CODE is authored by Claude Code from chat (billed, confirm-gated), so send the owner to
  // Private Chat with the command pre-filled rather than pretend a button can safely run it silently.
  location.href='/device/launchpad#say='+encodeURIComponent('build the RTM plugin');
}
async function rtmDeployPlugin(btn){
  var out=document.getElementById('rtm-deploy-out');
  if(!confirm('Package the current plugin source and deploy it to WordPress as a BETA? It will be installed but NOT activated (you activate it as a separate step).'))return;
  if(btn){ btn.disabled=true; btn.textContent='Deploying…'; }
  try{
    var d=await api('/rtm/deploy/deploy-latest',{method:'POST',timeoutMs:120000});
    var dep=(d&&d.deploy)||{};
    if(dep.ok){ if(out) out.innerHTML='<span class="ok">✓ Deployed '+esc(dep.version||'beta')+' to WordPress (beta, not yet active). Activate it: WordPress → Plugins → activate “{{rtm.name}} RTM”, or say “activate the plugin” in chat.</span>'; }
    else {
      // Auto-upload can fail (stock WordPress won't install a custom zip over REST). Show the plain
      // manual steps + a link to download the built zip so it can be uploaded by hand once.
      var hint=dep.hint||dep.detail||dep.reason||'deploy failed';
      var dl=(d&&d.zip_download_url)?('<div style="margin-top:6px"><a class="act go" href="'+esc(d.zip_download_url)+'" download style="display:inline-block;font-size:12px">⬇️ Download the plugin zip</a></div>'):'';
      if(out) out.innerHTML='<span class="warn">'+esc(hint)+'</span>'+dl;
    }
  }catch(e){ if(out) out.innerHTML='<span class="bad">✗ '+esc(String((e&&e.message)||e))+' (Mongoose may still be finishing; press Verify shortly).</span>'; }
  if(btn){ btn.disabled=false; btn.textContent='🚀 Deploy to WordPress'; }
  // Do NOT re-render the whole strip here: loadRtmSyncStrip rebuilds it via innerHTML and would wipe
  // this result (the download link / activate steps) the user needs to read and act on. Press Verify
  // to refresh the sync status when ready.
}
async function rtmVerifyNow(btn){
  if(btn){ btn.disabled=true; btn.textContent='Checking…'; }
  try{ await api('/rtm/deploy/verify',{method:'POST',timeoutMs:15000}); }catch(_e){}
  loadRtmSyncStrip();
}
async function connectWordpress(){
  var out=document.getElementById('wp-connect-out');
  var url=((document.getElementById('wp-url')||{}).value||'').trim();
  var user=((document.getElementById('wp-user')||{}).value||'').trim();
  var pass=((document.getElementById('wp-pass')||{}).value||'').trim();
  if(!url||!user||!pass){ if(out) out.innerHTML='<span style="color:#e0b341">Fill in all three fields.</span>'; return; }
  if(out) out.textContent='Connecting…';
  try{
    var d=await api('/build/wordpress/connect',{method:'POST',body:JSON.stringify({url:url,username:user,app_password:pass}),timeoutMs:20000});
    var c=(d&&d.connection)||{};
    if(d&&d.ok){ if(out) out.innerHTML='<span style="color:#3ecf8e">✓ Connected as '+esc(c.user||user)+'</span>'; setTimeout(loadRtmSyncStrip,900); }
    else { if(out) out.innerHTML='<span style="color:#e06a6a">✗ '+esc((c&&c.reason)||(d&&d.reason)||'Could not connect')+'</span>'; }
  }catch(e){ if(out) out.innerHTML='<span style="color:#e06a6a">✗ '+esc(String(e&&e.message||e))+'</span>'; }
}
// ---- RTM Ops: the service-charge engine's write surface (records only; never moves money) ----
async function rtmOpsRender(c){
  c.innerHTML=''+
    '<div class="card"><h3>Money in</h3>'+
    '<div class="muted" style="margin-bottom:8px">Two ways a payment is confirmed: importing the Metro bank CSV (bank-truth), or a WordPress/manual "mark paid". The credits list below is tagged by source, so a WordPress confirmation is never mistaken for a bank-matched one. Nothing here moves money.</div>'+
    '<div style="display:flex;gap:8px"><input id="op-csv" placeholder="Metro CSV path on the Mac" style="flex:1"><button class="act" onclick="rtmMetroImport(this)">Import Metro CSV</button></div>'+
    '<div id="op-import-out" class="muted" style="margin-top:6px"></div>'+
    '<div id="op-bank" class="muted" style="margin-top:8px">Loading credits&hellip;</div>'+
    '<div style="display:flex;gap:6px;margin-top:10px"><input id="op-al-tx" placeholder="Bank tx id" style="flex:2"><input id="op-al-doc" placeholder="Demand doc no." style="flex:2"><input id="op-al-amt" placeholder="Amount" inputmode="decimal" style="flex:1"></div>'+
    '<button class="act" onclick="rtmAllocate(this)" style="width:100%;margin-top:6px">Allocate credit to demand</button>'+
    '<div id="op-al-out" class="muted" style="margin-top:6px"></div>'+
    '<div style="display:flex;gap:6px;margin-top:12px"><input id="op-flat" placeholder="Flat (e.g. {{RTM_BLOCK_A}}F1)" style="flex:1"><input id="op-doc" placeholder="Demand doc no." style="flex:1"></div>'+
    '<div style="display:flex;gap:6px;margin-top:6px"><input id="op-ref" placeholder="Payment reference" style="flex:2"><input id="op-amt" placeholder="Amount" inputmode="decimal" style="flex:1"><input id="op-date" type="date" style="flex:1"></div>'+
    '<button class="act go" onclick="rtmRecordPayment(this)" style="width:100%;margin-top:6px">Issue receipt (demand at zero)</button>'+
    '<div id="op-pay-out" class="muted" style="margin-top:8px"></div>'+
    '<div style="display:flex;gap:8px;margin-top:10px"><input id="op-st-flat" placeholder="Flat id" style="flex:1"><button class="act" onclick="rtmStatement(this)">Statement</button></div>'+
    '<div id="op-st-out" class="muted" style="margin-top:8px"></div></div>'+
    '<div class="card"><h3>Books</h3><div id="op-books" class="muted">Loading&hellip;</div>'+
    '<div style="display:flex;gap:6px;margin-top:8px"><select id="op-tx-fund" style="flex:1"><option value="service">service charge trust</option><option value="reserve">reserve fund trust</option></select>'+
    '<select id="op-tx-dir" style="flex:1"><option value="in">in</option><option value="out">out</option></select></div>'+
    '<input id="op-tx-amt" placeholder="Amount" inputmode="decimal" style="width:100%;margin-top:6px">'+
    '<input id="op-tx-desc" placeholder="Description" style="width:100%;margin-top:6px">'+
    '<button class="act" onclick="rtmBooksTx(this)" style="width:100%;margin-top:8px">Record transaction</button>'+
    '<button class="act go" onclick="rtmWeeklyReconcile(this)" style="width:100%;margin-top:8px">Run weekly reconciliation</button>'+
    '<div id="op-books-out" class="muted" style="margin-top:8px"></div></div>'+
    '<div class="card"><h3>Transfers</h3><div id="op-transfers" class="muted">Loading&hellip;</div></div>'+
    '<div class="card"><h3>Setup once</h3><div id="op-setup" class="muted">Loading&hellip;</div>'+
    '<div style="display:flex;gap:8px;margin-top:8px"><button class="act" onclick="rtmSecDiscover(this)">Discover secretary</button><button class="act" onclick="rtmPortalKeypair(this)">Generate portal keypair</button></div>'+
    '<div style="display:flex;gap:8px;margin-top:8px"><button class="act go" onclick="rtmPortalPublish(this)">Publish portal snapshot</button><button class="act" onclick="rtmOpsLoad()">Refresh</button></div>'+
    '<div class="muted" style="margin-top:8px;font-size:12px">One-time imports run from the Mac: mongoose rtm import-flats / import-demands with a CSV path.</div>'+
    '<div id="op-setup-out" class="muted" style="margin-top:6px"></div></div>';
  rtmOpsLoad();
}
async function rtmOpsLoad(){
  rtmBankLoad();
  try{ var b=await api('/rtm/books/summary'); var lb=b.ledger_balances||{}; var rec=(b.reconciliation||{}).overall||b.reconciliation||{};
    var el=document.getElementById('op-books'); if(el) el.innerHTML=Object.keys(lb).map(function(k){return '<div class="row"><span>'+esc(k)+'</span><span class="muted">'+esc(String(lb[k]))+'</span></div>';}).join('')+'<div class="row"><span>Reconciliation</span><span class="pill '+((rec.status==='reconciled'||rec.ok)?'ok':'')+'">'+esc(String(rec.status||rec.verdict||'unknown'))+'</span></div>';
  }catch(e){ var el=document.getElementById('op-books'); if(el) el.textContent='Could not load books.'; }
  try{ var ts=await api('/rtm/transfers/status'); var obs=ts.obligations||[]; var flags=ts.flags||{};
    var byState={}; obs.forEach(function(o){ var s=o.state||o.status||'unknown'; byState[s]=(byState[s]||0)+1; });
    var fl=Object.keys(flags).filter(function(k){return flags[k];});
    var el2=document.getElementById('op-transfers'); if(el2) el2.innerHTML=(obs.length?Object.keys(byState).map(function(s){return '<div class="row"><span>'+esc(s)+'</span><span class="muted">'+byState[s]+'</span></div>';}).join(''):'<div class="muted">No obligations recorded.</div>')+(fl.length?('<div class="row"><span>Flags</span><span class="pill bad">'+esc(fl.join(', '))+'</span></div>'):'<div class="row"><span>Flags</span><span class="pill ok">clear</span></div>');
  }catch(e){ var el2=document.getElementById('op-transfers'); if(el2) el2.textContent='Could not load transfers.'; }
  try{ var sec=await api('/rtm/secretary/status'); var por=null; try{ por=await api('/rtm/portal/readiness'); }catch(_e){}
    var sitems=(sec&&sec.items)||[]; var scnt=(sec&&sec.counts)||{};
    var summ=[]; ['overdue','due_soon','upcoming'].forEach(function(k){ if(scnt[k]) summ.push(scnt[k]+' '+k.replace('_',' ')); });
    var lines='<div class="row"><span>Company secretary</span><span class="muted">'+esc(summ.length?summ.join(', '):'no deadlines computed yet')+'</span></div>';
    // The statutory deadlines themselves (Companies House / HMRC), so a due-soon filing is visible
    // here instead of buried. Undated "ongoing" watchlist rows are skipped to keep this glanceable.
    sitems.filter(function(x){return x.due_date;}).slice(0,6).forEach(function(x){
      var st=String(x.status||''); var pill=(st==='overdue')?'bad':(st==='done'?'ok':'');
      var col=(st==='due_soon')?'color:var(--gold);font-weight:600':'';
      lines+='<div class="row"><span style="max-width:58%">'+esc(x.title||'')+'<div class="muted" style="font-size:11px">'+esc(x.authority||'')+'</div></span><span class="v"><span class="muted" style="'+col+'">'+esc(x.due_date||'')+'</span> <span class="pill '+pill+'">'+esc(st.replace('_',' ')||'-')+'</span></span></div>';
    });
    if(por){ var checks=por.checks||por.items||por; if(Array.isArray(checks)){ lines+=checks.slice(0,8).map(function(x){var ok=x.ok||x.ready; var name=x.label||x.name||x.check||x.title||''; var det=x.detail||x.hint||''; var pill=ok?'ok':((x.severity==='error'||x.severity==='critical')?'bad':''); return '<div class="row"><span style="max-width:64%">'+esc(name)+(det?('<div class="muted" style="font-size:11px">'+esc(det)+'</div>'):'')+'</span><span class="pill '+pill+'">'+(ok?'ready':'todo')+'</span></div>';}).join(''); } else { lines+='<div class="row"><span>Portal</span><span class="muted">'+esc(String(por.ready!=null?(por.ready?'ready':'not ready'):'see report'))+'</span></div>'; } }
    var el3=document.getElementById('op-setup'); if(el3) el3.innerHTML=lines;
  }catch(e){ var el3=document.getElementById('op-setup'); if(el3) el3.textContent='Could not load setup status.'; }
}
async function rtmBankLoad(){
  try{ var d=await api('/rtm/manage/bank-transactions?limit=20'); var rows=(d&&d.transactions)||[];
    var el=document.getElementById('op-bank'); if(!el) return;
    if(!rows.length){ el.innerHTML='<div class="muted">No bank credits imported yet.</div>'; return; }
    el.innerHTML='<div class="muted" style="margin-bottom:4px">Recent confirmed credits (tagged by source):</div>'+rows.slice(0,10).map(function(x){
      var st=x.match_status||''; var pill=(st==='exact'||(x.unallocated_pence||0)===0)?'ok':(st==='duplicate'?'bad':'');
      var srcRaw=String(x.source||''); var src=srcRaw.indexOf('metro')>=0?'Metro bank':(srcRaw.indexOf('manual')>=0?'WordPress / manual':(srcRaw||'other'));
      var srcCol=srcRaw.indexOf('metro')>=0?'#5a8fd8':'#9a7bd0';
      var srcBadge='<span style="font-size:10px;border:1px solid '+srcCol+';color:'+srcCol+';border-radius:6px;padding:1px 5px;margin-right:5px">'+esc(src)+'</span>';
      return '<div class="row"><span style="max-width:60%">'+srcBadge+esc((x.reference||x.description||'').slice(0,34))+'<div class="muted" style="font-size:11px">'+esc(x.id||'')+'</div></span><span class="v"><span class="pill '+pill+'">'+esc(st||'?')+'</span> <span class="muted">'+esc(x.amount||'')+'</span></span></div>';
    }).join('');
  }catch(e){ var el=document.getElementById('op-bank'); if(el) el.textContent='Could not load bank credits.'; }
}
async function rtmMetroImport(btn){
  var pth=((document.getElementById('op-csv')||{}).value||'').trim(); var out=document.getElementById('op-import-out');
  if(!pth){ if(out)out.textContent='Enter the CSV path on the Mac.'; return; }
  btn.disabled=true; if(out)out.textContent='Importing\u2026';
  try{ var d=await api('/rtm/manage/metro-import',{method:'POST',body:JSON.stringify({path:pth}),timeoutMs:30000});
    if(d&&d.ok===false){ if(out)out.textContent=d.error||'Import failed.'; }
    else { if(out)out.textContent='Imported '+(d.imported!=null?d.imported:'')+' credit(s): '+(d.exact||0)+' exact, '+(d.partial||0)+' partial, '+(d.unmatched||0)+' unmatched, '+(d.duplicates||0)+' duplicate.'; }
  }catch(e){ if(out)out.textContent='Import failed.'; } finally{ btn.disabled=false; rtmBankLoad(); }
}
async function rtmAllocate(btn){
  var v=function(id){var e=document.getElementById(id);return e?e.value.trim():'';};
  var out=document.getElementById('op-al-out');
  var tx=v('op-al-tx'), doc=v('op-al-doc'), amt=v('op-al-amt');
  if(!tx||!doc||!amt){ if(out)out.textContent='Bank tx id, demand doc and amount are required.'; return; }
  btn.disabled=true; if(out)out.textContent='Allocating\u2026';
  try{ var d=await api('/rtm/manage/allocate',{method:'POST',body:JSON.stringify({bank_transaction_id:tx,allocations:[{document_number:doc,amount:amt}]})});
    if(out)out.textContent=(d&&d.ok!==false)?'Allocated. If the demand is now at zero, issue its receipt below.':((d&&d.error)||'Allocation failed.');
  }catch(e){ if(out)out.textContent='Allocation failed.'; } finally{ btn.disabled=false; rtmBankLoad(); }
}
async function rtmRecordPayment(btn){
  var v=function(id){var e=document.getElementById(id);return e?e.value.trim():'';};
  var out=document.getElementById('op-pay-out');
  var flat=v('op-flat'), doc=v('op-doc'), amt=v('op-amt'), ref=v('op-ref'), date=v('op-date');
  if(!flat||!doc||!amt){ if(out)out.textContent='Flat, document number and amount are required.'; return; }
  btn.disabled=true; if(out)out.textContent='Recording\u2026';
  try{ var d=await api('/rtm/receipt/build',{method:'POST',body:JSON.stringify({flat:flat,document_number:doc,payment_ref:ref,amount:amt,received_date:date||new Date().toISOString().slice(0,10)})});
    if(d&&d.ok){ if(out)out.innerHTML='Receipt issued. <button class="act" onclick="rtmReceiptPdf(\''+esc(flat)+'\',\''+esc(doc)+'\')">Download PDF</button>'; }
    else if(d&&d.reason==='demand_not_fully_paid'){ if(out)out.textContent='Demand not at zero yet ('+(d.outstanding||'')+' outstanding). Allocate the bank credit first.'; }
    else { if(out)out.textContent=(d&&(d.reason||d.error))||'Could not record the payment.'; }
  }catch(e){ if(out)out.textContent='Could not record the payment.'; } finally{ btn.disabled=false; }
}
async function rtmReceiptPdf(flat,doc){
  try{ var h={'X-Mongoose-Device-Token':token()}; try{ h['X-Cockpit-Nonce']=await nonce(); }catch(_e){}
    var r=await fetch('/rtm/receipt/download?flat='+encodeURIComponent(flat)+'&document='+encodeURIComponent(doc),{headers:h});
    if(!r.ok){ alert('Could not fetch the PDF.'); return; }
    var blob=await r.blob(); var u=URL.createObjectURL(blob); var a=document.createElement('a'); a.href=u; a.download='receipt-'+flat+'-'+doc+'.pdf'; a.click(); setTimeout(function(){URL.revokeObjectURL(u);},4000);
  }catch(e){ alert('Could not fetch the PDF.'); }
}
async function rtmStatement(btn){
  var f=(document.getElementById('op-st-flat')||{}).value||''; var out=document.getElementById('op-st-out');
  if(!f.trim()){ if(out)out.textContent='Enter a flat id.'; return; }
  btn.disabled=true;
  try{ var d=await api('/rtm/manage/statement/'+encodeURIComponent(f.trim()));
    var rows=d.lines||d.statement||d.entries||[]; var bal=d.balance||d.outstanding;
    if(out) out.innerHTML=(bal!=null?('<div class="row"><span>Balance</span><span class="muted">'+esc(String(bal))+'</span></div>'):'')+ (rows.length? rows.slice(-6).map(function(l){return '<div class="row"><span style="max-width:60%">'+esc(String(l.description||l.type||l.document_number||''))+'</span><span class="muted">'+esc(String(l.amount||l.amount_pence!=null?(l.amount||((l.amount_pence/100).toFixed(2))):''))+'</span></div>';}).join('') : '<div class="muted">No statement lines.</div>');
  }catch(e){ if(out)out.textContent='Could not load the statement.'; } finally{ btn.disabled=false; }
}
async function rtmBooksTx(btn){
  var v=function(id){var e=document.getElementById(id);return e?e.value:'';}; var out=document.getElementById('op-books-out');
  var amt=v('op-tx-amt').trim(); if(!amt){ if(out)out.textContent='Enter an amount.'; return; }
  btn.disabled=true; if(out)out.textContent='Recording\u2026';
  try{ var d=await api('/rtm/books/transaction',{method:'POST',body:JSON.stringify({fund:v('op-tx-fund'),direction:v('op-tx-dir'),amount:amt,description:v('op-tx-desc').trim()})});
    if(out)out.textContent=(d&&d.ok!==false)?'Recorded.':((d&&(d.error||d.reason))||'Failed.');
  }catch(e){ if(out)out.textContent='Failed.'; } finally{ btn.disabled=false; rtmOpsLoad(); }
}
async function rtmWeeklyReconcile(btn){
  var out=document.getElementById('op-books-out'); btn.disabled=true; if(out)out.textContent='Reconciling\u2026';
  try{ var d=await api('/rtm/reconcile/weekly',{method:'POST',body:JSON.stringify({}),timeoutMs:30000});
    var fl=(d&&d.flags)?Object.keys(d.flags).filter(function(k){return d.flags[k];}):[];
    if(out)out.textContent='Weekly reconciliation: '+((d&&d.status)||'done')+(fl.length?(' (flags: '+fl.join(', ')+')'):' (flags clear)');
  }catch(e){ if(out)out.textContent='Reconciliation failed.'; } finally{ btn.disabled=false; rtmOpsLoad(); }
}
async function rtmPortalKeypair(btn){
  var out=document.getElementById('op-setup-out'); btn.disabled=true; if(out)out.textContent='Generating keypair\u2026';
  try{ var d=await api('/rtm/portal/generate-keypair',{method:'POST',body:JSON.stringify({})});
    if(d&&d.ok){ if(out)out.textContent='Keypair created. Publish the PUBLIC key to WordPress: '+(d.public_key_b64||'').slice(0,16)+'\u2026 (full key via mongoose rtm portal public-key). The private key never leaves the Mac.'; }
    else { if(out)out.textContent=(d&&d.error)||'Keypair generation failed.'; }
  }catch(e){ if(out)out.textContent='Keypair generation failed.'; } finally{ btn.disabled=false; rtmOpsLoad(); } }
async function rtmPortalPublish(btn){
  var out=document.getElementById('op-setup-out'); btn.disabled=true; if(out)out.textContent='Building and pushing the snapshot\u2026';
  try{ var d=await api('/rtm/portal/publish',{method:'POST',body:JSON.stringify({}),timeoutMs:60000});
    if(out)out.textContent=(d&&d.ok)?('Published '+(d.flat_count!=null?d.flat_count:'')+' flats to the portal.'):((d&&d.error)||'Publish failed.');
  }catch(e){ if(out)out.textContent='Publish failed.'; } finally{ btn.disabled=false; rtmOpsLoad(); } }
async function rtmSecDiscover(btn){
  var out=document.getElementById('op-setup-out'); btn.disabled=true; if(out)out.textContent='Discovering\u2026';
  try{ var d=await api('/rtm/secretary/discover',{method:'POST',body:JSON.stringify({}),timeoutMs:30000});
    if(out)out.textContent=(d&&d.ok!==false)?'Discovery complete.':((d&&(d.error||d.reason))||'Discovery failed.');
  }catch(e){ if(out)out.textContent='Discovery failed.'; } finally{ btn.disabled=false; rtmOpsLoad(); }
}
function rtmTab(name){
  _rtmTab=name;
  // Switch tabs WITHOUT rebuilding the shell: just flip the active pill and refill the content pane.
  // Rebuilding the whole #rtm-root (the old behaviour) re-created the subtab bar and reset every scroll
  // position, which is what made the bar jump and the view snap back on every tap.
  var bar=document.querySelector('#rtm-root .subtabs');
  if(bar){ Array.prototype.forEach.call(bar.querySelectorAll('.subtab'), function(b){
    b.classList.toggle('on', b.getAttribute('data-tab')===name); }); }
  var c=document.getElementById('rtm-content'); if(c){ c.scrollTop=0; }
  rtmRender();
}
async function rtmReceiptDownload(flat, doc, btn){
  if(!flat||!doc)return;
  var o=btn?btn.textContent:''; if(btn){ btn.disabled=true; btn.textContent='Downloading…'; }
  try{
    await phoneDownload('/rtm/receipt/download?flat='+encodeURIComponent(flat)+'&document='+encodeURIComponent(doc), 'receipt-'+flat+'-'+doc+'.pdf');
  }catch(e){ alert('Download failed: '+((e&&e.message)||e)); }
  if(btn){ btn.disabled=false; btn.textContent=o||'⬇️ Download'; }
}
async function rtmRender(){
  var c=document.getElementById('rtm-content'); if(!c)return;
  // Coalesce rapid re-renders (the RTM overview was being painted several times on a single open, so the
  // same content flashed): each call takes a token, and a render that is no longer the latest drops its
  // paint instead of stacking another flash.
  var _tok=++_rtmRenderTok;
  c.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    if(_rtmTab==='dashboard'){
      var d=await api('/rtm/manage/dashboard'); if(_tok!==_rtmRenderTok)return;
      var st=d.status||{}; var arr=d.arrears||{}; var rvq=d.review_queue||{};
      var mk=function(k,v,col){ return '<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line)"><span class="muted">'+k+'</span><span style="font-weight:600'+(col?(';color:'+col):'')+'">'+esc(String(v))+'</span></div>'; };
      var outstanding=st.arrears_outstanding||'£0.00';
      var owed=(outstanding && outstanding!=='£0.00');
      var summary='<div class="card"><h3>At a glance</h3>'
        +mk('Demands raised', st.demands_raised||'£0.00')
        +mk('Payments received', st.payments_allocated||'£0.00')
        +mk('Outstanding', outstanding, owed?'var(--bad)':'var(--ok)')
        +mk('Flats in arrears (overdue)', (arr.count!=null?arr.count:0))
        +mk('Metro credits to review', (rvq.count!=null?rvq.count:0))
        +'<div class="muted small" style="margin-top:7px">As at '+esc(d.as_at||'')+'. Outstanding is what is unpaid now; a flat only counts as "in arrears" once its demand is past due (30 Jul).</div></div>';
      var acts=(d.next_actions||[]).filter(function(a){ return a.type!=='ok'; });
      var actsHtml = acts.length? ('<div class="card" style="padding-bottom:2px"><h3 style="margin:0 0 4px">Needs attention</h3></div>'+acts.map(function(a){ var u=(a.urgency==='high')?'bad':(a.urgency==='medium'?'':'ok'); return '<div class="card"><div class="row" style="border:none"><span style="max-width:72%">'+esc(a.title||a.type||'action')+'</span><span class="pill '+u+'">'+esc(a.urgency||'')+'</span></div>'+(a.action?('<div class="muted" style="margin-top:4px">'+esc(a.action)+'</div>'):'')+'</div>'; }).join('')) : '<div class="empty">Nothing needs attention right now.</div>';
      c.innerHTML = summary + actsHtml;
    } else if(_rtmTab==='arrears'){
      var d=await api('/rtm/manage/arrears'); var items=d.items||[];
      c.innerHTML = '<div class="card"><h3>Arrears</h3><div class="muted">'+(d.count!=null?d.count:items.length)+' outstanding'+(d.as_at?(' as at '+esc(d.as_at)):'')+'.</div></div>'+
        (items.length? items.map(function(it){ return rtmCard((pick(it,['leaseholder_name'])||'')+(it.flat_id?(' \u00b7 '+it.flat_id):''), [moneyOf(it), (it.days_overdue!=null?(it.days_overdue+' days overdue'):''), it.status]); }).join('') : '<div class="empty">No arrears.</div>');
    } else if(_rtmTab==='demands'){
      // Period-scoped: with two live cycles an unfiltered list interleaves them, and a blended
      // "outstanding" mixes genuine arrears with charges only just issued. Default to the current
      // period; _rtmPeriod==='' means show every period deliberately.
      var pq=(_rtmPeriod===null)?'':('&period='+encodeURIComponent(_rtmPeriod));
      var d=await api('/rtm/manage/demands?limit=200'+pq); var rows=d.demands||[];
      var pinfo=(d.periods||{}); var plist=(pinfo.periods||[]);
      if(_rtmPeriod===undefined){ _rtmPeriod=pinfo.current||null; return rtmRender(); }  // first load: current period
      var sel='';
      if(plist.length>1){
        sel='<div class="card"><div class="muted small" style="margin-bottom:6px">Charging period</div><div style="display:flex;gap:6px;flex-wrap:wrap">'
          + plist.map(function(p){ var on=(_rtmPeriod===p.period);
              return '<button class="subtab'+(on?' on':'')+'" onclick="rtmSetPeriod(\''+esc(p.period)+'\')">'+esc(p.period)+'</button>'; }).join('')
          + '<button class="subtab'+((_rtmPeriod===null)?' on':'')+'" onclick="rtmSetPeriod(\'\')">All periods</button></div>';
        var cur=plist.filter(function(p){return p.period===_rtmPeriod;})[0];
        if(cur) sel+='<div class="muted small" style="margin-top:8px">'+esc(cur.demands)+' demands · charged '+esc(cur.charged)+' · paid '+esc(cur.paid)+' · <b>outstanding '+esc(cur.outstanding)+'</b></div>';
        sel+='</div>';
      }
      var demHdr=sel+'<div class="card"><div class="row" style="border:none;padding:0"><h3 style="margin:0">Demands</h3><a href="https://{{site.domain}}/wp-admin/admin.php?page=pc-rtm-charges" target="_blank" rel="noopener" class="subtab" style="text-decoration:none">Open in WordPress &#8599;</a></div><div class="muted small" style="margin-top:6px">The service charge demands register. For the richer view (record payment, download PDFs, split), open it in the WordPress RTM plugin.</div></div>';
      c.innerHTML = demHdr + (rows.length? rows.map(function(r){
        var s=String(r.status||'').toLowerCase();
        var col = s==='paid'?'var(--ok)':(s==='overdue'||s==='in recovery'?'var(--bad)':'var(--dim)');
        var pill='<span style="flex:0 0 auto;font-size:11px;border:1px solid '+col+';color:'+col+';border-radius:99px;padding:2px 8px">'+esc(r.status||'')+'</span>';
        // Always show the period on the row: in "All periods" it is the only way to tell two cycles apart.
        var sub=[moneyOf(r),(r.period?esc(r.period):''),(r.payment_reference?('ref '+esc(r.payment_reference)):''),(r.due_date?('due '+esc(r.due_date)):'')].filter(Boolean).join(' \u00b7 ');
        return '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><span style="font-weight:600">'+esc(pick(r,['document_number'])||'demand')+(r.flat_id?(' \u00b7 '+esc(r.flat_id)):'')+'</span>'+pill+'</div><div class="muted small" style="margin-top:3px">'+sub+'</div></div>';
      }).join('') : '<div class="empty">No demands.</div>');
    } else if(_rtmTab==='ops'){
      rtmOpsRender(c); return;
    } else if(_rtmTab==='payables'){
      var d=await api('/rtm/manage/payables'); var rows=d.payables||[];
      var payHdr='<div class="card"><h3>Payables</h3><div class="muted small">Money the RTM OWES: supplier invoices and outgoings (insurance, cleaning, maintenance) to be paid from the service charge and reserve funds. Add an invoice in the Docs tab and Mongoose reads its amount + due date. Records only; Mongoose never moves money.</div></div>';
      c.innerHTML = payHdr + (rows.length? rows.map(function(r){ return rtmCard(pick(r,['supplier','payee','name']), [moneyOf(r), (r.due_date?('due '+r.due_date):''), r.status]); }).join('') : '<div class="empty">No payables recorded yet.</div>');
    } else if(_rtmTab==='flats'){
      var d=await api('/rtm/manage/flats'); var rows=d.flats||[];
      c.innerHTML = '<div class="card"><h3>Money position</h3><div class="muted small">Each flat: leaseholder, payment reference, balance, and whether they are paid up or in arrears.</div></div>'+
      (rows.length? rows.map(function(r){
        var pos=String(r.position||'').toLowerCase();
        var col = pos==='clear'?'var(--ok)':(pos==='arrears'||pos==='part-paid'||pos==='in recovery'?'var(--bad)':'var(--dim)');
        var label = pos==='clear'?'paid up':(r.position||'');
        var pill = label?('<span style="flex:0 0 auto;font-size:11px;border:1px solid '+col+';color:'+col+';border-radius:99px;padding:2px 8px">'+esc(label)+'</span>'):'';
        var sub=[(r.payment_reference?('ref '+esc(r.payment_reference)):''),(r.balance!=null&&r.balance!==undefined?('bal '+esc(r.balance)):'')].filter(Boolean).join(' \u00b7 ');
        return '<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><span style="font-weight:600">'+esc(pick(r,['leaseholder_name','leaseholder'])||'')+(r.flat_id?(' \u00b7 '+esc(r.flat_id)):'')+'</span>'+pill+'</div>'+(sub?('<div class="muted small" style="margin-top:3px">'+sub+'</div>'):'')+'</div>';
      }).join('') : '<div class="empty">No flats imported.</div>');
    } else if(_rtmTab==='docs'){
      var d=await api('/cockpit/rtm/documents?limit='+_docLimit+(_rtmDocFlat?('&flat_id='+encodeURIComponent(_rtmDocFlat)):'')); var ctx=d.context||{}; var docs=d.documents||[];
      var TYPES=ctx.doc_types||['receipt','invoice','contract','ticket','feedback','complaint','photo_evidence','other_evidence'];
      var inp='display:block;width:100%;margin:4px 0;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--text);font-size:16px';
      var opts=TYPES.map(function(t){return '<option value="'+esc(t)+'">'+esc(t.replace(/_/g,' '))+'</option>';}).join('');
      var GT=[['payment_receipt','Receipt (branded .docx)'],['welcome','Welcome letter'],['cover_email_receipt_with_demand','Cover email (draft)']];
      var gopts=GT.map(function(t){return '<option value="'+t[0]+'">'+esc(t[1])+'</option>';}).join('');
      var EF=[['xero','Xero'],['quickbooks','QuickBooks'],['gnucash','GnuCash'],['qif','QIF'],['ofx','OFX'],['xlsx','Excel'],['pdf','PDF report'],['json','JSON']];
      var efBoxes=EF.map(function(f){return '<label style="display:inline-flex;align-items:center;gap:4px;margin:2px 10px 2px 0;font-size:14px"><input type="checkbox" class="rtmexp-fmt" value="'+f[0]+'" checked> '+esc(f[1])+'</label>';}).join('');
      var exportCard='<div class="card" style="border-color:var(--gold)"><h3>\ud83d\udce6 Export for accountant</h3>'
        +'<div class="muted small" style="margin-bottom:6px">One ZIP with the accounts in every format your accountant uses (Xero, QuickBooks, GnuCash CSVs, QIF, OFX, Excel, a branded PDF and JSON) plus the source invoices and receipts as evidence. Bookkeeper-ready pack, not a signed statutory account.</div>'
        +'<div style="margin:4px 0 8px">'+efBoxes+'</div>'
        +'<label style="display:inline-flex;align-items:center;gap:6px;font-size:14px;margin-bottom:8px"><input type="checkbox" id="rtmexp-evidence" checked> Include the source-document evidence pack</label>'
        +'<div><button class="act go" onclick="rtmExportBuild(this)">Build export</button> <span id="rtmexp-out" class="small"></span></div>'
        +'<div id="rtmexp-result" style="margin-top:8px"></div></div>';
      var genCard='<div class="card"><h3>Generate a document</h3>'
        +'<div class="muted small" style="margin-bottom:6px">Branded receipt, welcome letter or cover email from real data. Produces a download link; nothing is sent.</div>'
        +'<select id="rtmgen-tpl" style="'+inp+'">'+gopts+'</select>'
        +'<input id="rtmgen-flat" placeholder="Flat (e.g. {{RTM_BLOCK_B}}F9)" style="'+inp+'">'
        +'<input id="rtmgen-email" placeholder="Recipient email (cover email only, optional)" style="'+inp+'">'
        +'<button class="act go" onclick="rtmDocGenerate()" style="margin-top:4px">Generate</button> <span id="rtmgen-out" class="small"></span>'
        +'<div id="rtmgen-result" style="margin-top:8px"></div></div>';
      c.innerHTML=exportCard+genCard+'<div class="card"><h3>Add a document</h3>'
        +'<div class="muted small" style="margin-bottom:6px">Any file. It is stored on the Mac, made searchable everywhere, and correlated with your calls and emails.</div>'
        +'<select id="rtmdoc-type" style="'+inp+'">'+opts+'</select>'
        +'<input id="rtmdoc-desc" placeholder="Description (e.g. cctv signs)" maxlength="200" style="'+inp+'">'
        +'<input id="rtmdoc-file" type="file" style="'+inp+'">'
        +'<button class="act go" onclick="rtmDocUpload()" style="margin-top:4px">Upload</button> <span id="rtmdoc-out" class="small"></span></div>'
        +'<div class="card"><div class="row" style="border:none;padding:0"><h3 style="margin:0">Documents</h3>'
        +'<select onchange="rtmDocFilterFlat(this.value)" style="'+inp+';width:auto;margin:0;padding:6px 8px">'
        +(function(){ var F=['{{RTM_FLAT_IDS_QUOTED}}']; return F.map(function(f){return '<option value="'+f+'"'+(f===_rtmDocFlat?' selected':'')+'>'+(f||'All flats')+'</option>';}).join(''); })()
        +'</select></div>'
        +'<div class="muted small" style="margin:6px 0 8px">'+(_rtmDocFlat?('Showing '+docs.length+' document'+(docs.length===1?'':'s')+' for '+esc(_rtmDocFlat)):((ctx.documents||0)+' stored \u00b7 '+(ctx.documents_correlated||0)+' correlated with calls/emails'))+'</div>'
        +(docs.length? ('<div class="docgrid" id="rtm-docgrid">'+docs.map(rtmDocCard).join('')+'</div>') : ('<div class="empty">'+(_rtmDocFlat?('No documents for '+esc(_rtmDocFlat)+' yet.'):'No documents yet.')+'</div>'))
        +(docs.length>=_docLimit? '<div style="text-align:center;margin-top:10px"><button class="act" id="rtm-docsmore" onclick="docsLoadMore(this)">Load more files</button></div>' : '')
        +'</div>';
      docs.forEach(function(x){ if(x&&x.has_thumb){ var el=document.getElementById('docthumb-'+x.id); if(el) rtmLoadThumb(x.id, el); } });
    } else if(_rtmTab==='receipts'){
      var d=await api('/cockpit/receipts-issued'); var rows=d.receipts||[];
      c.innerHTML='<div class="card"><div class="row" style="border:none;padding:0"><h3 style="margin:0">Issued receipts</h3><a href="https://{{site.domain}}/wp-admin/admin.php?page=pc-rtm-charges" target="_blank" rel="noopener" class="subtab" style="text-decoration:none">Open in WordPress &#8599;</a></div><div class="muted small" style="margin-top:6px">'+(d.count!=null?d.count:rows.length)+' receipt'+((d.count===1)?'':'s')+' issued, newest first. Tap Download for the branded PDF. Records only; nothing here moves money.</div></div>'+
        (rows.length? rows.map(function(r){
          var det=[r.amount,(r.created_at?String(r.created_at).slice(0,10):'')].filter(function(x){return x!==''&&x!=null;}).map(function(x){return esc(String(x));}).join(' · ');
          var dl=(r.flat_id&&r.document_number)
            ? '<button class="act" style="margin-top:6px;padding:4px 9px;font-size:12px" onclick="rtmReceiptDownload(\''+r.flat_id+'\',\''+r.document_number+'\',this)">⬇️ Download</button>'
            : '<div class="muted small" style="margin-top:6px">no file reference</div>';
          return '<div class="card"><div style="font-size:14px">'+esc(String(r.receipt_number||'receipt'))+(r.flat_id?(' · '+esc(r.flat_id)):'')+'</div>'+(det?('<div class="muted" style="margin-top:3px">'+det+'</div>'):'')+dl+'</div>';
        }).join('') : '<div class="empty">No receipts issued yet.</div>');
    } else if(_rtmTab==='voicemail'){
      var d=await api('/voicemails?limit=40'); var vms=d.voicemails||[]; var st=d.stats||{};
      c.innerHTML='<div class="card"><h3>Voicemail</h3><div class="muted small">'+(st.total!=null?st.total:vms.length)+' message'+((st.total===1)?'':'s')+' to the RTM line ({{site.rtm_email}}'+(st.rtm_tel?(' · '+esc(st.rtm_tel)):'')+'), newest first. No auto-transcription yet; tap Download to listen.</div></div>'+
        (vms.length? vms.map(rtmVoicemailCard).join('') : '<div class="empty">No voicemails.</div>');
    } else if(_rtmTab==='mail'){
      var mbox=(_mailBox==='sent')?'sent':'inbox';
      var d=await api('/email/recent?box='+mbox+'&limit='+_mailLimit); var msgs=d.results||[];
      var head='<div class="card"><div style="display:flex;gap:8px;flex-wrap:wrap">'+
        '<button class="subtab'+(mbox==='inbox'?' on':'')+'" onclick="rtmMailBox(\'inbox\')">Inbox</button>'+
        '<button class="subtab'+(mbox==='sent'?' on':'')+'" onclick="rtmMailBox(\'sent\')">Sent</button>'+
        '<button class="subtab" onclick="mailRefresh(this)" style="margin-left:auto">&#8635; Refresh</button></div>'+
        '<div class="muted small" style="margin-top:7px">'+(mbox==='sent'?'Last emails sent from':'Last emails to')+' {{site.rtm_email}}, newest first. Mail syncs in the background every few minutes; tap Refresh to pull the latest now.</div></div>';
      c.innerHTML = head + (msgs.length? msgs.map(function(m){
        var who=esc(rtmFromName(mbox==='sent'?(m.recipients||''):(m.sender||'')));
        var subj=esc(m.subject||'(no subject)');
        var snip=esc(String(m.snippet||'').replace(/\s+/g,' ').trim().slice(0,110));
        var when=esc(rtmMailDate(m.received_at||m.sent_at||''));
        return '<div class="card" style="cursor:pointer" onclick="rtmOpenEmail('+m.id+',this)">'+
          '<div class="row" style="border:none;padding:0"><span style="font-weight:600;max-width:66%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+(mbox==='sent'?'To: ':'')+who+'</span><span class="muted small">'+when+'</span></div>'+
          '<div style="margin-top:2px;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+subj+'</div>'+
          '<div class="muted small" style="margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+snip+'</div>'+
          '<div class="rtm-mailbody" onclick="event.stopPropagation()" style="display:none;margin-top:8px;border-top:1px solid var(--line);padding-top:8px"></div></div>';
      }).join('') : '<div class="empty">No '+mbox+' mail indexed yet.</div>')
      + ((msgs.length>=_mailLimit)
          ? '<div class="card" style="text-align:center"><button class="act" onclick="mailLoadMore()">Load older emails</button><div class="muted small" style="margin-top:6px">Showing '+msgs.length+', tap to load older.</div></div>'
          : (msgs.length? '<div class="muted small" style="text-align:center;padding:10px">Showing all '+msgs.length+' '+mbox+' emails.</div>' : ''));
    }
  }catch(e){ c.innerHTML=failCard(e,'RTM'); }
}
function rtmFromName(s){
  s=String(s||'');
  var m=s.match(/^\s*"?([^"<]+?)"?\s*</);
  if(m&&m[1].trim())return m[1].trim();
  var a=s.match(/<([^>]+)>/);
  return a?a[1]:(s||'(unknown sender)');
}
function rtmMailDate(s){
  if(!s)return'';
  var d=new Date(s);
  if(isNaN(d.getTime()))return String(s).slice(0,16);
  var now=new Date();
  if(d.toDateString()===now.toDateString())return d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  return d.toLocaleDateString([], {day:'2-digit',month:'short'});
}
async function rtmOpenEmail(id, card){
  var box=card&&card.querySelector('.rtm-mailbody'); if(!box)return;
  if(box.style.display!=='none'){ box.style.display='none'; box.innerHTML=''; return; }
  box.style.display='block'; box.innerHTML='<span class="muted">Loading&hellip;</span>';
  try{
    var d=await api('/email/message/'+encodeURIComponent(id),{timeoutMs:20000});
    var m=(d&&d.message)||{}; var atts=m.attachments||[];
    var attBtns=atts.map(function(a){
      var aid=a.id||a.attachment_id; var fn=String(a.filename||'attachment');
      var safe=fn.replace(/[^a-zA-Z0-9._ -]/g,'_');
      return '<button class="ghost" onclick="event.stopPropagation();phoneDownload(\'/email/attachment/'+encodeURIComponent(aid)+'/download\',\''+safe+'\')">↓ '+esc(fn)+'</button>';
    }).join('');
    var dl='<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">'+
      '<button class="ghost" onclick="event.stopPropagation();phoneDownload(\'/email/message/'+id+'/download?format=txt\',\'email-'+id+'.txt\')">Download .txt</button>'+
      '<button class="ghost" onclick="event.stopPropagation();phoneDownload(\'/email/message/'+id+'/download?format=eml\',\'email-'+id+'.eml\')">Download .eml</button>'+
      '<button class="ghost" onclick="event.stopPropagation();phoneDownload(\'/email/message/'+id+'/download?format=pdf\',\'email-'+id+'.pdf\')">Download .pdf</button>'+attBtns+'</div>';
    var whenTxt=(m.received_at||m.sent_at)?String(m.received_at||m.sent_at):'';
    box.innerHTML='<div class="mail-hdr">'+
        '<div class="mail-from">'+esc(m.sender||m.from||'(unknown sender)')+'</div>'+
        (m.recipients?('<div class="mail-meta muted"><span class="mail-label">To</span>'+esc(m.recipients)+'</div>'):'')+
        (whenTxt?('<div class="mail-meta muted"><span class="mail-label">Date</span>'+esc(whenTxt)+'</div>'):'')+
        '<div class="mail-subj">'+esc(m.subject||'(no subject)')+'</div>'+
        (atts.length?('<div class="mail-meta muted">📎 '+atts.length+' attachment'+(atts.length>1?'s':'')+'</div>'):'')+
      '</div>'+
      '<div class="mail-read" id="mailread-'+id+'"></div>'+dl;
    mailRenderBody(id, String(m.body||m.text||m.snippet||''));
  }catch(e){ box.innerHTML='<span class="bad">Could not open this message: '+esc(String((e&&e.message)||e))+'</span>'; }
}
function rtmDocIcon(t){
  t=String(t||'');
  if(/receipt|invoice/.test(t)) return '🧾';
  if(/contract|lease/.test(t)) return '📜';
  if(/photo|image|evidence/.test(t)) return '🖼️';
  if(/complaint|feedback|ticket/.test(t)) return '📩';
  return '📄';
}
function rtmDocCard(x){
  x=x||{};
  var dtxt=(x.description&&String(x.description).trim())||'';
  var dt=String(x.doc_type||'').replace(/_/g,' ');
  var thumb = x.has_thumb
    ? '<img id="docthumb-'+esc(x.id)+'" alt="" style="display:none">'
    : '<div class="ph">'+rtmDocIcon(x.doc_type)+'</div>';
  var links=x.links||0;
  var meta=[(dtxt?('<b class="docdesc">'+esc(dtxt)+'</b>'):''), dt, (x.flat_id?('flat '+esc(x.flat_id)):''), (links?(links+' linked'):'no links yet')].filter(function(p){return p;}).join(' · ');
  var descr='<a href="#" class="small" style="margin-left:4px" onclick="event.preventDefault();event.stopPropagation();rtmDocEditDesc('+esc(x.id)+')">'+(dtxt?'edit':'add description')+'</a>';
  var sum = (x.summary&&String(x.summary).trim())
    ? ('<div class="docsum">'+esc(x.summary)+'</div>')
    : '<div class="docsum analysing">analysing…</div>';
  var chips = (x.labels&&x.labels.length)
    ? ('<div class="docchips">'+x.labels.slice(0,6).map(function(l){return '<span class="docchip">'+esc(l)+'</span>';}).join('')+'</div>')
    : '';
  var kf=x.key_facts||{}; var fp=[];
  var conf=String(kf.total_confidence||'');
  var isPay=(x.doc_type==='invoice'||x.doc_type==='receipt');
  if(kf.total){
    var mark = (conf==='confirmed') ? '<span style="color:#1e8e3e">\u2713 set by you</span>'
      : (conf==='verified') ? '<span style="color:#1e8e3e">\u2713 verified</span>'
      : (conf==='conflict') ? '<span style="color:#c0392b">\u26a0 does not match the payment</span>'
      : '<span style="color:#b7791f">check</span>';
    var why=esc(kf.total_basis||'');
    var acts = (conf==='confirmed'||conf==='verified') ? ''
      : ' <a href="#" class="small" onclick="event.preventDefault();event.stopPropagation();rtmDocConfirmTotal('+esc(x.id)+',\''+esc(kf.total)+'\')">Confirm</a>';
    acts += ' <a href="#" class="small" onclick="event.preventDefault();event.stopPropagation();rtmDocSetTotal('+esc(x.id)+',\''+esc(kf.total)+'\')">Change</a>';
    fp.push('<span title="'+why+'" onclick="event.stopPropagation();alert(this.title)" style="cursor:help"><b>Total '+esc(kf.total)+'</b> '+mark+'</span>'+acts);
  } else if(isPay){
    fp.push('<span class="muted">No total found</span> <a href="#" class="small" onclick="event.preventDefault();event.stopPropagation();rtmDocSetTotal('+esc(x.id)+',\'\')">Set total</a>');
  }
  if(kf.sort_code) fp.push('Sort '+esc(kf.sort_code));
  if(kf.account) fp.push('Acct '+esc(kf.account));
  var facts = fp.length ? ('<div class="docfacts">'+fp.join(' &middot; ')+'</div>') : '';
  var dlname=String(x.filename||('document-'+x.id)).replace(/[^a-zA-Z0-9._ -]/g,'_');
  var dl='<button class="ghost" style="margin-top:7px;padding:4px 11px;font-size:12px" onclick="event.stopPropagation();rtmDocDownload('+esc(x.id)+',\''+dlname+'\')">&#8595; Download</button>';
  var del=' <button class="ghost" style="margin-top:7px;padding:4px 11px;font-size:12px;color:#c0392b" onclick="event.stopPropagation();rtmDocDelete('+esc(x.id)+',\''+dlname+'\')">&#215; Delete</button>';
  var _DF=['{{RTM_FLAT_IDS_QUOTED}}'];
  var flatSel='<label style="display:block;margin-top:7px;font-size:11px;color:var(--dim)">Flat: <select onclick="event.stopPropagation()" onchange="event.stopPropagation();rtmDocSetFlat('+esc(x.id)+',this.value)" style="font-size:11px;padding:2px 4px;border:1px solid var(--line);border-radius:6px;background:var(--panel2);color:var(--text)">'+_DF.map(function(f){return '<option value="'+f+'"'+(f===String(x.flat_id||'')?' selected':'')+'>'+(f||'none')+'</option>';}).join('')+'</select></label>';
  return '<div class="doccard" id="doccard-'+esc(x.id)+'">'
    +'<div class="docthumb">'+thumb+'</div>'
    +'<div class="docbody">'
    +'<div class="docname">'+esc(x.filename||'(unnamed)')+'</div>'
    +'<div class="docmeta">'+meta+descr+'</div>'
    +facts+sum+chips+flatSel+dl+del
    +'</div></div>';
}
async function rtmDocDownload(id, name){
  try{ await phoneDownload('/cockpit/rtm/documents/'+encodeURIComponent(id)+'/download', name||('document-'+id)); }
  catch(e){ alert('Download failed: '+((e&&e.message)||e)); }
}
async function rtmDocRefresh(){ var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0; await rtmRender(); if(mn) mn.scrollTop=y; }
async function rtmDocEditDesc(id){
  var card=document.getElementById('doccard-'+id); var cur=card?((card.querySelector('.docdesc')||{}).textContent||''):'';
  var v=prompt('Description for this document (leave empty to clear):', cur); if(v===null) return;
  try{
    var r=await api('/cockpit/rtm/documents/'+encodeURIComponent(id)+'/description',{method:'POST',body:JSON.stringify({description:v})});
    if(!r||r.ok!==true){ alert('Could not save the description: '+((r&&r.error)||'unknown')); return; }
    await rtmDocRefresh();
  }catch(e){ alert('Could not save the description: '+((e&&e.message)||e)); }
}
async function rtmDocSaveTotal(id, v){
  try{
    var r=await api('/cockpit/rtm/documents/'+encodeURIComponent(id)+'/total',{method:'POST',body:JSON.stringify({total:v})});
    if(!r||r.ok!==true){ alert('Could not save the total: '+((r&&r.error)||'unknown')); return; }
    await rtmDocRefresh();
  }catch(e){ alert('Could not save the total: '+((e&&e.message)||e)); }
}
async function rtmDocConfirmTotal(id, cur){
  if(!confirm('Confirm '+cur+' as this document\'s total?')) return;
  await rtmDocSaveTotal(id, String(cur||'').replace(/[^0-9.]/g,''));
}
async function rtmDocSetTotal(id, cur){
  var v=prompt('Total for this document in pounds (leave empty to go back to the one read from the file):', String(cur||'').replace(/[^0-9.]/g,''));
  if(v===null) return;
  await rtmDocSaveTotal(id, v.trim());
}
async function rtmDocSetFlat(id, flat){
  try{
    var r=await api('/cockpit/rtm/documents/'+encodeURIComponent(id)+'/flat',{method:'POST',body:JSON.stringify({flat_id:flat||''})});
    if(!r||r.ok!==true){ alert('Could not assign flat: '+((r&&r.error)||'unknown')); }
  }catch(e){ alert('Could not assign flat: '+((e&&e.message)||e)); }
}
async function rtmDocDelete(id, name){
  if(!confirm('Delete this document?\n\n'+(name||('document-'+id))+'\n\nRemoves only THIS upload; if it is a duplicate the original stays. Cannot be undone.')) return;
  try{
    var r=await api('/cockpit/rtm/documents/'+encodeURIComponent(id)+'/delete',{method:'POST',body:JSON.stringify({})});
    if(r&&r.ok){ var el=document.getElementById('doccard-'+id); if(el) el.remove(); }
    else alert('Delete failed: '+((r&&r.error)||'unknown'));
  }catch(e){ alert('Delete failed: '+((e&&e.message)||e)); }
}
async function docsLoadMore(btn){
  var grid=document.getElementById('rtm-docgrid'); if(!grid) return;
  var offset=grid.children.length;
  btn.disabled=true; var o=btn.textContent; btn.textContent='Loading…';
  try{
    var page=24; var d=await api('/cockpit/rtm/documents?limit='+page+'&offset='+offset+(_rtmDocFlat?('&flat_id='+encodeURIComponent(_rtmDocFlat)):''));
    var docs=(d&&d.documents)||[];
    // Append the new cards WITHOUT re-rendering the grid, so the scroll position is kept.
    var tmp=document.createElement('div'); tmp.innerHTML=docs.map(rtmDocCard).join('');
    while(tmp.firstChild){ grid.appendChild(tmp.firstChild); }
    docs.forEach(function(x){ if(x&&x.has_thumb){ var el=document.getElementById('docthumb-'+x.id); if(el) rtmLoadThumb(x.id, el); } });
    _docLimit=offset+docs.length;
    if(docs.length<page){ btn.parentNode.removeChild(btn); } else { btn.disabled=false; btn.textContent=o; }
  }catch(e){ btn.disabled=false; btn.textContent=o; }
}
async function rtmLoadThumb(id, img){
  // A plain <img src> cannot send the X-Mongoose-Device-Token / X-Cockpit-Nonce headers the thumb
  // route requires, so fetch it through the same authorised path api() uses and hand the bytes to
  // the <img> as an object URL. No unauthenticated image route is introduced.
  try{
    var n=await nonce();
    var r=await fetch('/cockpit/rtm/documents/'+encodeURIComponent(id)+'/thumb',{headers:{'X-Mongoose-Device-Token':token(),'X-Cockpit-Nonce':n}});
    if(!r.ok) return;
    var b=await r.blob();
    if(!b||!b.size) return;
    var u=URL.createObjectURL(b);
    img.onload=function(){ img.style.display='block'; setTimeout(function(){ URL.revokeObjectURL(u); },2000); };
    img.onerror=function(){ URL.revokeObjectURL(u); };
    img.src=u;
  }catch(e){}
}
async function rtmDocUpload(){
  var out=document.getElementById('rtmdoc-out');
  var fi=document.getElementById('rtmdoc-file');
  var type=(document.getElementById('rtmdoc-type')||{}).value||'';
  var desc=((document.getElementById('rtmdoc-desc')||{}).value||'').trim();
  if(!fi||!fi.files||!fi.files[0]){ if(out) out.innerHTML='<span class="warn">Choose a file first.</span>'; return; }
  var f=fi.files[0];
  if(f.size>25*1024*1024){ if(out) out.innerHTML='<span class="bad">File too big (max 25MB).</span>'; return; }
  if(out) out.textContent='Uploading\u2026';
  var reader=new FileReader();
  reader.onload=async function(){
    try{
      var d=await api('/cockpit/rtm/documents/upload',{method:'POST',body:JSON.stringify({filename:f.name,doc_type:type,description:desc,content_b64:reader.result}),timeoutMs:40000});
      if(d&&d.ok){ if(out) out.innerHTML='<span class="ok">\u2713 Added '+esc(d.filename||'')+(d.correlations?(' \u00b7 '+d.correlations+' correlated'):'')+'</span>'; setTimeout(async function(){ var mn=document.getElementById('main'); var y=mn?mn.scrollTop:0; await rtmRender(); if(mn) mn.scrollTop=y; },800); }
      else { if(out) out.innerHTML='<span class="bad">\u2717 '+esc((d&&d.error)||'upload failed')+'</span>'; }
    }catch(e){ if(out) out.innerHTML='<span class="bad">\u2717 '+esc(String(e&&e.message||e))+'</span>'; }
  };
  reader.onerror=function(){ if(out) out.innerHTML='<span class="bad">Could not read the file.</span>'; };
  reader.readAsDataURL(f);
}
async function rtmDocGenerate(){
  var out=document.getElementById('rtmgen-out'); var resEl=document.getElementById('rtmgen-result');
  var tpl=((document.getElementById('rtmgen-tpl')||{}).value)||'';
  var flat=(((document.getElementById('rtmgen-flat')||{}).value)||'').trim();
  var email=(((document.getElementById('rtmgen-email')||{}).value)||'').trim();
  if(!flat){ if(out) out.innerHTML='<span class="warn">Enter a flat first (e.g. {{RTM_BLOCK_B}}F9).</span>'; return; }
  if(out) out.textContent='Generating…'; if(resEl) resEl.innerHTML='';
  var box='width:100%;height:150px;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--panel2);color:var(--text);font-size:13px';
  try{
    var d=await api('/cockpit/rtm/templates/generate',{method:'POST',body:JSON.stringify({template:tpl,flat_id:flat,recipient_email:email}),timeoutMs:40000});
    if(!d||!d.ok){ if(out) out.innerHTML='<span class="bad">✗ '+esc((d&&(d.hint||d.reason))||'failed')+'</span>'; return; }
    if(out) out.innerHTML='<span class="ok">✓ done</span>';
    if(d.kind==='email'){
      resEl.innerHTML='<div class="muted small" style="margin-bottom:4px">'+(d.ready_to_send?'Ready to review and copy:':'DRAFT (no recipient on file, nothing sent):')+'</div><textarea readonly style="'+box+'">'+esc(d.text||'')+'</textarea>';
    } else if(d.download_url){
      resEl.innerHTML='<a class="act" href="'+esc(d.download_url)+'" download style="display:inline-block">Download '+esc(d.filename||'document')+'</a> <span class="muted small">link valid ~1h</span>';
    }
  }catch(e){ if(out) out.innerHTML='<span class="bad">✗ '+esc(String((e&&e.message)||e))+'</span>'; }
}
async function rtmExportBuild(btn){
  var out=document.getElementById('rtmexp-out'); var resEl=document.getElementById('rtmexp-result');
  var fmts=Array.prototype.slice.call(document.querySelectorAll('.rtmexp-fmt')).filter(function(x){return x.checked;}).map(function(x){return x.value;});
  if(!fmts.length){ if(out) out.innerHTML='<span class="warn">Pick at least one format.</span>'; return; }
  var ev=!!((document.getElementById('rtmexp-evidence')||{}).checked);
  if(btn) btn.disabled=true; if(out) out.textContent='One moment, building the pack\u2026'; if(resEl) resEl.innerHTML='';
  try{
    var d=await api('/cockpit/rtm/export',{method:'POST',body:JSON.stringify({formats:fmts,include_evidence:ev}),timeoutMs:120000});
    if(!d||!d.ok){ if(out) out.innerHTML='<span class="bad">\u2717 '+esc((d&&d.error)||'failed')+'</span>'; return; }
    if(out) out.innerHTML='<span class="ok">\u2713 done</span>';
    var m=d.manifest||{}; var mb=(d.size?(d.size/1048576):0).toFixed(1);
    var meta=(m.formats||[]).join(', ')+(m.evidence_files?(' \u00b7 '+m.evidence_files+' evidence files'):'')+' \u00b7 '+mb+' MB';
    resEl.innerHTML='<a class="act" href="'+esc(d.zip_download_url)+'" download style="display:inline-block">Download '+esc(d.filename||'export.zip')+'</a> <div class="muted small" style="margin-top:4px">'+esc(meta)+' \u00b7 link valid ~1h</div>';
  }catch(e){ if(out) out.innerHTML='<span class="bad">\u2717 '+esc(String((e&&e.message)||e))+'</span>'; }
  finally{ if(btn) btn.disabled=false; }
}
// ---- Build (status + deploy staged updates) ----
async function loadBuild(){
  var root=document.getElementById('build-root'); root.innerHTML='<div class="muted">Loading&hellip;</div>';
  try{
    var d=await api('/build/status'); var recent=d.recent_addenda||[]; var staged=d.staged_updates||[]; var ver=d.version||'';
    var h='<div class="card"><h3>Build</h3><div class="muted">'+(ver?('Version '+esc(ver)):'Current build')+'. This is Mongoose’s OWN updates.</div></div>';
    // The Example Court WordPress plugin is a DIFFERENT thing and lives in the RTM tab (build + deploy +
    // verify all there). Point at it so nobody hunts for it here.
    h+='<div class="card" style="border-color:var(--gold)"><h3>🏢 {{rtm.name}} plugin</h3>'
      +'<div class="muted" style="margin-bottom:8px">The WordPress plugin builds and deploys from the RTM tab, not here.</div>'
      +'<button class="act go" onclick="go(\'rtm\')">Open RTM → build &amp; deploy plugin</button></div>';
    if(staged.length){
      h+='<div class="card"><h3>Staged updates</h3>'+staged.map(function(s){ var sid=esc(String(s.stage_id||s.id||'')); return '<div class="row"><span style="max-width:55%">'+esc(s.title||s.name||sid||'candidate')+'</span><button class="act" onclick="deployStage(\''+sid+'\',this)">Deploy</button></div>'; }).join('')+
        '<div class="muted" style="margin-top:8px">Deploy runs backup &rarr; apply &rarr; verify and restarts Mongoose; the phone reconnects after.</div></div>';
    }
    h+= recent.length? ('<div class="card"><h3>Recent changes</h3>'+recent.slice(0,8).map(function(r){ return '<div style="padding:7px 0;border-top:1px solid var(--line)"><div style="font-size:14px">'+esc(r.title||'')+'</div>'+(r.summary?('<div class="muted">'+esc(r.summary)+'</div>'):'')+'</div>'; }).join('')+'</div>') : '';
    h+='<div class="card"><button class="ghost" onclick="macOnly(\'Detailed build editing\')">What needs the Mac? &rarr;</button></div>';
    root.innerHTML=h;
  }catch(e){ root.innerHTML=failCard(e,'build'); }
}
async function deployStage(sid,btn){ if(!sid)return; if(!confirm('Deploy this staged update? Mongoose will back up, apply, verify and restart.'))return;
  btn.disabled=true; btn.textContent='Deploying\u2026';
  try{ var d=await api('/build/deploy',{method:'POST',body:JSON.stringify({stage_id:sid}),timeoutMs:120000});
    if(d&&d.ok===false){ btn.textContent='Deploy'; btn.disabled=false; alert('Deploy failed: '+(d.error||'unknown')); }
    else { btn.textContent='Deployed'; }
  }catch(e){ btn.textContent='Deploy'; btn.disabled=false; alert('Could not confirm deploy (Mongoose may be restarting). Re-open Build shortly.'); } }
// ===== Ribbon search: surface any menu item, section, or function and jump straight to it =====
// Distinct from the Search tile (which queries the unified document model). This finds NAVIGATION:
// the area tiles (menu items), their sub-sections, and the actions/functions inside them.
var _ribIndex=[]; var _ribMatches=[];
function buildRibIndex(){
  var idx=[];
  AREAS.forEach(function(a){ idx.push({ic:a.ic,label:a.lb,kind:'Menu',kw:('menu '+a.lb).toLowerCase(),run:function(){ tileClick(a); }}); });
  [['Overview','dashboard'],['Arrears','arrears'],['Demands','demands'],['Payables','payables'],['Flats','flats']].forEach(function(s){
    idx.push({ic:'\uD83C\uDFE2',label:'RTM \u00b7 '+s[0],kind:'Section',kw:('rtm {{RTM_NAME_LOWER}} '+s[0]).toLowerCase(),run:function(){ _rtmTab=s[1]; go('rtm'); }});
  });
  var fns=[
    {ic:'\uD83E\uDE7A',label:'Run doctor',kw:'doctor health diagnostics check status',view:'system'},
    {ic:'\uD83D\uDD04',label:'Sync all connectors',kw:'sync all connectors refresh accounts integrations enable disable',view:'setup'},
    {ic:'\u2B07\uFE0F',label:'Install missing dependencies',kw:'install missing dependencies deps packages setup',view:'setup'},
    {ic:'\uD83D\uDE80',label:'Deploy staged update',kw:'deploy update staged build release upgrade',view:'build'},
    {ic:'\uD83C\uDFE2',label:'RTM build plan',kw:'rtm build wordpress management source truth requirements claude chat ui',view:'build'},
    {ic:'\uD83D\uDDD1\uFE0F',label:'Revoke a device',kw:'revoke device remove unpair paired guest',view:'devices'},
    {ic:'\uD83D\uDD17',label:'Generate guest invite',kw:'guest invite url link trusted onboarding qr claim pair repair',view:'devices'},
    {ic:'\uD83D\uDD11',label:'Reset a password (opens chat: type reset NAME password)',kw:'reset password repair pair forgot forgotten locked out login sign in credentials guest sam new change home screen pwa',href:'/device/launchpad'},
    {ic:'\u2705',label:'Approve or cancel pending',kw:'approve cancel pending approvals whatsapp draft',view:'approvals'},
    {ic:'\uD83D\uDCDC',label:'View logs',kw:'logs log activity tail recent',view:'activity'},
    {ic:'\u23F0',label:'Scheduler jobs',kw:'scheduler jobs cron schedule running pause resume free memory',view:'scheduler'},
    {ic:'\uD83D\uDFE2',label:'WhatsApp activity',kw:'whatsapp messages drafts chat',view:'whatsapp'},
    {ic:'\uD83D\uDD0E',label:'Document/email search',kw:'documents files attachments email voicemail search find imap gmail',view:'search'},
    {ic:'\uD83C\uDFA4',label:'Voicemail search',kw:'voicemail transcripts calls pi ha imap search',view:'rtm'},
    {ic:'v3',label:'Protocol v3 coordination',kw:'v3 protocol pi peers chat code mongoose locks bus readiness',view:'system'},
    {ic:'\uD83D\uDD14',label:'Notifications & push',kw:'notification notifications push alerts ntfy reminders ping phone pwa device web push',view:'system'},
    {ic:'\uD83D\uDDA5\uFE0F',label:'System health',kw:'system health connectors warnings benchmark models resources memory ram model stuck ollama looping process',view:'system'},
    {ic:'\uD83D\uDC1B',label:'Bug workflow',kw:'bug bugs log bug add bug detail diagnose bug attempt fix apply fix patch peers v3',view:'build'},
    {ic:'\uD83D\uDCE6',label:'Data dump / export',kw:'data dump export download journal support bug mongoose zip private',view:'system'},
    {ic:'\uD83E\uDDD8',label:'Wellness journal',kw:'wellness mindful mindfulness journal journaling self journal checkin check-in mood structured brief rituals',view:'activity'},
    {ic:'\uD83D\uDD01',label:'File crawler freshness',kw:'file crawl crawler crawling index reindex stale deleted folders search refresh filesystem',view:'search'},
    {ic:'\u2728',label:'Surpriser nudges',kw:'surpriser surprise nudges delight recommendations cadence not creepy stale noisy',view:'system'}
  ];
  fns.forEach(function(f){ idx.push({ic:f.ic,label:f.label,kind:'Function',kw:(f.label+' '+f.kw).toLowerCase(),run:function(){ if(f.href){ location.href=f.href; } else if(f.ui){ go(f.ui); } else { go(f.view); } }}); });
  return idx;
}
function ribRender(q){
  var res=document.getElementById('ribbon-results'); if(!res)return; q=(q||'').trim().toLowerCase();
  if(!q){ res.style.display='none'; res.innerHTML=''; return; }
  var terms=q.split(/\s+/);
  _ribMatches=_ribIndex.filter(function(it){ return terms.every(function(t){ return it.kw.indexOf(t)>=0; }); }).slice(0,30);
  if(!_ribMatches.length){ res.innerHTML='<div class="rib-opt"><div class="tx"><div class="lb" style="color:var(--dim)">No menu, section or function matches.</div></div></div>'; res.style.display='block'; return; }
  res.innerHTML=_ribMatches.map(function(it,i){ return '<div class="rib-opt'+(i===0?' sel':'')+'" data-i="'+i+'"><div class="ic">'+it.ic+'</div><div class="tx"><div class="lb">'+esc(it.label)+'</div></div><span class="rib-kind">'+esc(it.kind)+'</span></div>'; }).join('');
  res.style.display='block';
  var opts=res.querySelectorAll('.rib-opt'); for(var k=0;k<opts.length;k++){ (function(el){ el.onclick=function(){ ribPick(parseInt(el.getAttribute('data-i'),10)); }; })(opts[k]); }
}
function ribPick(i){ var m=_ribMatches[i]; var inp=document.getElementById('ribbon-search'); var res=document.getElementById('ribbon-results');
  if(inp) inp.value=''; if(res){ res.style.display='none'; res.innerHTML=''; } if(inp) inp.blur(); if(m&&m.run) m.run(); }
function ribbonInit(){
  _ribIndex=buildRibIndex();
  var inp=document.getElementById('ribbon-search'); var res=document.getElementById('ribbon-results'); if(!inp)return;
  inp.addEventListener('input',function(){ ribRender(inp.value); });
  inp.addEventListener('keydown',function(e){ if(e.key==='Enter'){ e.preventDefault(); if(_ribMatches.length) ribPick(0); } else if(e.key==='Escape'){ inp.value=''; if(res){res.style.display='none';} inp.blur(); } });
  document.addEventListener('click',function(e){ var h=document.querySelector('header'); if(res && h && !h.contains(e.target)){ res.style.display='none'; } });
}
// ---- boot + owner gating (cosmetic; the guard enforces owner-only on /cockpit/* regardless) ----
async function boot(){
  renderGrid();
  // If we arrived deep-linked to a screen (e.g. from the launchpad Cockpit tab: /device/cockpit#view=rtm),
  // switch to it IMMEDIATELY, before the whoami round-trip, so we never flash the home tile grid, which
  // looked like "a different cockpit" when opening a tile from the launchpad.
  openHashView();
  var who=document.getElementById('who');
  if(!token()){ who.innerHTML='Not paired on this phone. <a class="lnk" href="/device/launchpad">Set up</a>'; return; }
  try{
    var w=await api('/device/whoami'); var dev=(w&&w.device)||{};
    if(dev.profile!=='primary_phone'){ document.getElementById('grid').style.display='none';
      who.innerHTML='This is the owner\'s cockpit. <a class="lnk" href="/device/launchpad">Open your Mongoose</a>'; return; }
    who.textContent=(dev.name?(dev.name+' \u00b7 '):'')+'full cockpit on this phone';
  }catch(e){ who.innerHTML='Could not confirm this device. <a class="lnk" href="/device/launchpad">Open launchpad</a>'; }
}
// Deep link: another surface (e.g. the launchpad find box) can link straight to a cockpit screen
// with /device/cockpit#view=devices. Honour it once the owner check has resolved.
function openHashView(){
  try{
    if(!location.hash) return;
    var hp=new URLSearchParams(location.hash.slice(1));
    var v=hp.get('view'); var el=v && document.getElementById('v-'+v);
    // Idempotent: boot() opens the view before whoami AND again once whoami resolves. Re-running go()
    // on the view already showing re-ran its loader, wiping the page back to "Loading" and repainting,
    // which is the 2 or 3 flickers on every deep-linked screen (RTM, System, Benchmarks, all of them).
    if(el && !el.classList.contains('on')) go(v);
  }catch(_e){}
}
boot().then(openHashView).catch(function(){});
ribbonInit();
</script></body></html>"""


def phone_cockpit_html() -> str:
    return _externalize_assets(_profile_page(PHONE_COCKPIT_HTML))


# The guided first-run flow. ONE path from "the app just opened" to "my phone is paired".
# It reads live onboarding state (so steps tick green as they're satisfied and the person
# resumes where they left off), and folds the PHONE HANDOFF in as the closing step, the QR
# is right here, not in a separate tab. Local-first styling, no external assets.
FRONTDOOR_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mongoose</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<style>
  :root{ --ink:#16140F; --panel:#1F1C16; --panel2:#26221B; --line:#322D24; --text:#ECE6DA;
         --dim:#9C9486; --ok:#7CB893; --gold:#C9A35B; --gold-dim:#7d6736; --bad:#D0785E; }
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.65 "Inter",system-ui,sans-serif;background:var(--ink);color:var(--text);
       min-height:100vh;display:grid;place-items:center;padding:26px 18px;background-image:radial-gradient(900px 520px at 50% -10%,rgba(201,163,91,.10),transparent 70%)}
  @font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}
  @font-face{font-family:"Inter";font-weight:600;font-display:swap;src:url("/static/fonts/inter-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url("/static/fonts/fraunces-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}
  .wrap{width:100%;max-width:760px}.brand{display:flex;align-items:center;gap:14px;margin-bottom:18px}.brand img{image-rendering:auto}
  h1{font-family:"Fraunces",serif;font-size:34px;margin:0}.sub{font-family:"IBM Plex Mono",monospace;color:var(--gold);font-size:11px;letter-spacing:.22em;text-transform:uppercase}
  .hero{background:linear-gradient(180deg,var(--panel),#1c1914);border:1px solid var(--line);border-radius:22px;padding:24px;box-shadow:0 20px 60px rgba(0,0,0,.25)}
  .lede{font-size:17px;color:#d9d1c4;margin:8px 0 18px;max-width:620px}.muted{color:var(--dim);font-size:14px}.notice{margin:12px 0 0;padding:11px 13px;border:1px solid var(--gold-dim);border-radius:13px;background:rgba(201,163,91,.08);color:#d9c99f;font-size:13px}
  .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:20px}.tile{display:block;text-decoration:none;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:16px;padding:16px;min-height:118px}.tile:hover{border-color:var(--gold-dim)}
  .tile b{display:block;font-size:16px;margin-bottom:5px}.tile span{display:block;color:var(--dim);font-size:13px;line-height:1.45}.tile.primary{background:linear-gradient(180deg,rgba(201,163,91,.20),var(--panel2));border-color:var(--gold-dim)}
  .examples{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:14px}.ex{font-family:"IBM Plex Mono",monospace;font-size:12px;color:#d9c184;background:#141109;border:1px solid var(--line);border-radius:11px;padding:9px 10px}
  code{font-family:"IBM Plex Mono",monospace;color:#ead8ad}@media(max-width:640px){.grid,.examples{grid-template-columns:1fr}.hero{padding:20px}h1{font-size:30px}}
</style></head><body><main class="wrap">
  <div class="brand"><img src="/static/img/mongoose-64.png" alt="" width="54" height="54"><div><h1>Mongoose</h1><div class="sub">local assistant · Sillygoose</div></div></div>
  <section class="hero">
    <div class="lede">This is the front door. Use it when you have just installed Mongoose, opened only the bare address, or simply forgot where everything lives.</div>
    <div class="notice" id="context">If this is the Mac running Mongoose, open the cockpit or continue setup. If this is a phone or tablet, open the phone surface.</div>
    <div class="grid" aria-label="Mongoose starting points">
      <a class="tile primary" id="owner" href="/ui"><b>Open the cockpit</b><span>Main browser surface: chat, search, setup, approvals, scheduler, RTM, build and system status.</span></a>
      <a class="tile" id="guided" href="/welcome"><b>Continue guided setup</b><span>Shows what is ready, what still needs connecting, and how to pair your phone.</span></a>
      <a class="tile" id="phone" href="/device/launchpad"><b>Open phone surface</b><span>For paired phones/tablets: chat, voice, search, notifications, approvals and quick actions.</span></a>
      <a class="tile" id="install" href="/install"><b>Install progress / pairing code</b><span>Use this if you just installed or need the live setup and phone-pairing screen again.</span></a>
    </div>
    <div style="margin-top:22px"><b>What can I ask?</b><div class="examples" aria-label="Example prompts">
      <div class="ex">what changed today?</div><div class="ex">search my email for service charge</div>
      <div class="ex">log bug install page dead</div><div class="ex">remind me to journal tonight</div>
    </div></div>
    <p class="muted" style="margin-top:18px">Owner cockpit is local-only by default. From another device, use the phone surface or deliberately enable remote owner access in Setup.</p>
  </section>
</main><script>
function localOwnerBase(){ const p=location.port||((location.protocol==="https:")?"443":"80");
  // Preserve the loopback host the user is ALREADY on. localhost and 127.0.0.1 are different
  // browser origins with separate service workers and separate certificate acceptances; sending
  // someone who is happily on 127.0.0.1 over to localhost lands them on an origin they have not
  // trusted (and possibly a stale worker). Only fall back to localhost from a non-loopback host.
  const h=location.hostname;
  const loop=(h==="localhost"||h==="127.0.0.1"||h==="[::1]"||h==="::1");
  return location.protocol+"//"+(loop?h:"localhost")+":"+p; }
function localUrl(path){ return localOwnerBase()+path; }
(function(){
  const host=(location.hostname||"").toLowerCase();
  const local=(host==="localhost"||host==="127.0.0.1"||host==="[::1]"||host==="::1");
  const owner=document.getElementById("owner"), guided=document.getElementById("guided"), install=document.getElementById("install"), phone=document.getElementById("phone"), ctx=document.getElementById("context");
  if(owner) owner.href=localUrl("/ui"); if(guided) guided.href=localUrl("/welcome"); if(install) install.href=localUrl("/install");
  let hasDevice=false; try{ hasDevice=!!localStorage.getItem("mongoose_device_token"); }catch(e){}
  if(!local && ctx){ ctx.innerHTML = hasDevice ? "This browser looks paired. Open the phone surface below." : "You are not on the Mac-local address. Owner setup/cockpit lives at <code>https://127.0.0.1:"+(location.port||"8765")+"</code> on the Mac running Mongoose; phones use the phone surface after pairing."; }
  if(hasDevice && phone){ phone.classList.add("primary"); if(owner) owner.classList.remove("primary"); }
})();
</script></body></html>"""


def frontdoor_html() -> str:
    return FRONTDOOR_HTML


WELCOME_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Welcome to Mongoose</title>
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.getRegistrations().then(function(rs){ rs.forEach(function(r){ r.unregister(); }); }).catch(function(){});
}
</script>
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<style>
  :root{ --ink:#16140F; --panel:#1F1C16; --panel2:#26221B; --line:#322D24; --text:#ECE6DA;
         --dim:#9C9486; --ok:#7CB893; --gold:#C9A35B; --gold-dim:#7d6736; --bad:#D0785E; }
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.65 "Inter",system-ui,sans-serif;background:var(--ink);color:var(--text);
       min-height:100vh;display:flex;justify-content:center;padding:28px 18px;
       background-image:radial-gradient(1100px 600px at 50% -10%, rgba(201,163,91,0.09), transparent 70%)}
  @font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}
  @font-face{font-family:"Inter";font-weight:500;font-display:swap;src:url("/static/fonts/inter-latin-500-normal.woff2") format("woff2")}
  @font-face{font-family:"Inter";font-weight:600;font-display:swap;src:url("/static/fonts/inter-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url("/static/fonts/fraunces-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}
  .wrap{width:100%;max-width:620px}
  .mark{display:flex;align-items:center;gap:12px;margin-bottom:6px}
  .pulse{width:12px;height:12px;border-radius:50%;background:var(--ok);position:relative}
  .pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--ok);
                animation:breathe 2.6s ease-in-out infinite}
  @keyframes breathe{0%{transform:scale(1);opacity:.5}70%{transform:scale(2.6);opacity:0}100%{opacity:0}}
  h1{font-family:"Fraunces",serif;font-size:30px;margin:0;font-weight:600}
  .sub{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--gold)}
  .lede{color:var(--dim);font-size:15px;margin:10px 0 22px}
  .bar{height:6px;background:var(--panel2);border-radius:6px;overflow:hidden;margin-bottom:24px;border:1px solid var(--line)}
  .bar>div{height:100%;background:linear-gradient(90deg,var(--gold-dim),var(--gold));transition:width .5s ease}
  .step{background:linear-gradient(180deg,var(--panel),#1c1914);border:1px solid var(--line);
        border-radius:15px;padding:20px;margin-bottom:14px;transition:border-color .3s,background .3s}
  .step.active{border-color:var(--gold-dim);background:linear-gradient(180deg,rgba(201,163,91,.10),#1c1914)}
  /* Completed steps stay legible and build a visible green column (no fade-out). */
  .step.done{border-color:rgba(124,184,147,.34);border-left:3px solid var(--ok);background:linear-gradient(180deg,rgba(124,184,147,.06),#1b1a16)}
  .step.done .stitle{color:#cfe9d4}
  .shead{display:flex;align-items:center;gap:12px}
  .num{width:30px;height:30px;border-radius:50%;border:1px solid var(--line);display:flex;
       align-items:center;justify-content:center;font-family:"IBM Plex Mono",monospace;font-size:13px;color:var(--dim);flex:none;transition:background .25s,border-color .25s}
  .step.active .num{border-color:var(--gold);color:var(--gold)}
  .step.done .num{background:var(--ok);border-color:var(--ok);color:#10231a;font-size:16px;font-weight:700;animation:tickpop .32s ease}
  @keyframes tickpop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.18)}100%{transform:scale(1);opacity:1}}
  .stitle{font-weight:600;font-size:17px}
  .sdetail{color:var(--dim);font-size:14px;margin:8px 0 0 42px}
  .sbody{margin:14px 0 0 42px}
  button{background:var(--panel2);border:1px solid var(--gold-dim);color:#e9d9b4;border-radius:11px;
         padding:11px 16px;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit}
  button:hover{border-color:var(--gold)}
  button.ghost{border-color:var(--line);color:var(--dim)}
  a.link{color:var(--gold);text-decoration:none;font-size:14px}
  .qrwrap{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
  .qr{background:#fff;padding:10px;border-radius:10px;width:188px;height:188px;flex:none}
  .qr svg{width:100%;height:100%}
  .tok{font-family:"IBM Plex Mono",monospace;font-size:12px;word-break:break-all;background:#141109;
       border:1px solid var(--line);border-radius:9px;padding:10px;margin-top:8px;color:#d6cfc0}
  .done-card{text-align:center;padding:34px 20px;animation:cardrise .5s cubic-bezier(.2,.8,.2,1)}
  .done-card h2{font-family:"Fraunces",serif;font-size:26px;margin:0 0 8px}
  .done-card .goosebob{animation:goosebob 2.8s ease-in-out infinite}
  @keyframes cardrise{0%{transform:translateY(14px);opacity:0}100%{transform:translateY(0);opacity:1}}
  @keyframes goosebob{0%,100%{transform:translateY(0) rotate(-2deg)}50%{transform:translateY(-5px) rotate(2deg)}}
  .sparkle{display:inline-block;color:var(--gold);font-size:13px;letter-spacing:.18em;margin-bottom:6px;font-family:"IBM Plex Mono",monospace;text-transform:uppercase}
  .row2{display:flex;gap:10px;margin-top:6px;flex-wrap:wrap}
  .quick-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:16px}
  .quick{background:#141109;border:1px solid var(--line);border-radius:12px;padding:11px;text-align:left}
  .quick b{display:block;font-size:13px;color:var(--text);margin-bottom:3px}
  .quick span{display:block;font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--gold);line-height:1.35}
  @media(max-width:560px){.quick-grid{grid-template-columns:1fr}}
  .muted{color:var(--dim);font-size:13px}

  @media (prefers-reduced-motion:reduce){ *,*::before,*::after{ animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important; } }
</style></head><body>
<div class="wrap">
  <div class="mark"><img class="logo-emerge" src="/static/img/mongoose-64.png" alt="Mongoose" width="44" height="44" style="margin-right:6px"><div><h1>Mongoose</h1><span class="sub">first run</span></div></div>
  <div style="text-align:center;margin:-4px 0 10px">
    <div class="sillygoose-wrap" style="display:inline-flex">
      <img class="sillygoose-img" src="/static/img/sillygoose-256.png" alt="Sillygoose" width="40" height="40">
      <div class="sillygoose-sub">your household goose, getting set up</div>
    </div>
  </div>
  <div class="lede" id="lede">Welcome back. If you are new, this walks you through setup; if you have forgotten where things are, the shortcuts below get you moving again.</div>
  <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="barwrap"><div id="barfill" style="width:0%"></div></div>
  <div id="steps"></div>
  <div style="margin-top:18px;text-align:center"><a class="link" id="skipui" href="#" onclick="event.preventDefault(); location.href=localUiUrl('')">Open local cockpit &rarr;</a></div>
</div>
<script>
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
// The full cockpit (/ui) is localhost-only by default, so owner links from this first-run page must
// target localhost on the Mac. A relative /ui opened over the LAN address dead-ends on a 403. (The
// phone is handled separately via pairing; it must NOT use localhost.)
function localOwnerBase(){ const p=location.port||((location.protocol==="https:")?"443":"80");
  // Preserve the loopback host the user is ALREADY on. localhost and 127.0.0.1 are different
  // browser origins with separate service workers and separate certificate acceptances; sending
  // someone who is happily on 127.0.0.1 over to localhost lands them on an origin they have not
  // trusted (and possibly a stale worker). Only fall back to localhost from a non-loopback host.
  const h=location.hostname;
  const loop=(h==="localhost"||h==="127.0.0.1"||h==="[::1]"||h==="::1");
  return location.protocol+"//"+(loop?h:"localhost")+":"+p; }
function localUiUrl(suffix){ return localOwnerBase()+"/ui"+(suffix||""); }
async function j(u,o,timeoutMs){ timeoutMs=timeoutMs||15000;
  const ctrl=new AbortController(); const to=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{ const r=await fetch(u,Object.assign({},o||{},{signal:ctrl.signal})); if(!r.ok) throw r.status; return await r.json(); }
  finally{ clearTimeout(to); } }
let nonce=null;
async function getNonce(){ if(nonce) return nonce; try{ nonce=(await j("/cockpit/nonce")).cockpit_nonce; }catch(e){} return nonce; }

function stepCard(st, idx, active){
  const cls = st.done? "done" : (active? "active" : "");
  const mark = st.done? "✓" : (idx+1);
  let body = "";
  if(active && !st.done){
    if(st.key==="brain"){
      body = `<div class="sbody"><div class="muted">Mongoose runs its model on this machine. Once Ollama is running, this turns green on its own.</div>
        <div class="row2"><button class="ghost" onclick="refresh()">Check again</button></div></div>`;
    } else if(st.key==="connect"){
      body = `<div class="sbody"><div class="muted">Connect a source so Mongoose has something real to work with. You can add more later.</div>
        <div class="row2"><button onclick="location.href=localUiUrl('?tab=setup')">Connect information</button>
        <button class="ghost" onclick="skipConnect()">I'll do this later</button></div></div>`;
    } else if(st.key==="phone"){
      body = `<div class="sbody" id="phonebody"><div class="muted">Make your phone a Mongoose surface, approvals, voice, and what changed, in your pocket.</div>
        <div class="row2"><button onclick="startPair()">Show pairing code</button></div>
        <div id="qrbox" style="margin-top:14px"></div></div>`;
    }
  }
  return `<div class="step ${cls}"><div class="shead"><div class="num">${mark}</div>
    <div class="stitle">${esc(st.title || 'Setup step')}</div></div>
    <div class="sdetail">${esc(st.detail)}</div>${body}</div>`;
}

let skippedConnect = false;
function skipConnect(){ skippedConnect = true;
  // persist so the choice survives a reload (the state machine is meant to be resumable)
  fetch("/cockpit/onboarding/skip-connect",{method:"POST"}).catch(()=>{});
  render(lastState, true); }

let lastState=null;
let _completeShown=false;
function render(s, forcePhone){
  lastState=s;
  document.getElementById("barfill").style.width = (s.progress_pct||0)+"%";
  { var _bw=document.getElementById("barwrap"); if(_bw) _bw.setAttribute("aria-valuenow", String(Math.round(s.progress_pct||0))); }
  if(s.complete){
    if(_completeShown) return;  // draw the celebratory card once; later polls must not replay its entrance
    _completeShown=true;
    const sig=(s.signals||{});
    const connected = Number(sig.connected||0);
    const paired = Number(sig.paired||0);
    const readiness = connected>0
      ? "Mongoose has at least one real source connected and can start being useful."
      : "Mongoose is running and your phone is paired. Connect email, calendar, files or messages when you want richer answers.";
    document.getElementById("steps").innerHTML =
      `<div class="step done-card"><div class="sparkle">· welcome back ·</div>
       <img class="goosebob" src="/static/img/sillygoose-256.png" alt="" width="64" height="64" style="margin-bottom:10px">
       <h2>Mongoose is ready.</h2>
       <div class="muted" style="max-width:450px;margin:0 auto">${esc(readiness)} ${paired>0?"Your phone surface is available too.":"You can pair a phone from Devices."}</div>
       <div class="row2" style="justify-content:center;margin-top:18px">
         <button onclick="location.href=localUiUrl('#chat')">Ask / chat</button>
         <button onclick="location.href=localUiUrl('#search')">Search my stuff</button>
         <button onclick="location.href=localUiUrl('')">What changed?</button>
         <button class="ghost" onclick="location.href=localUiUrl('?tab=setup')">Connect more</button>
       </div>
       <div class="quick-grid" aria-label="Useful things to ask Mongoose">
         <div class="quick"><b>Catch up</b><span>what changed today?</span></div>
         <div class="quick"><b>Find something</b><span>search my email/files for RTM invoice</span></div>
         <div class="quick"><b>Log a problem</b><span>log bug phone search is confusing</span></div>
         <div class="quick"><b>Wellness ritual</b><span>remind me to journal tonight</span></div>
       </div></div>`;
    document.getElementById("lede").textContent = "Use the shortcuts below, or ask Mongoose in plain English.";
    return;
  }
  // find the active step: first not-done (or phone if connect was skipped). GUARD s.steps: over the LAN
  // address with an API token set, /cockpit/onboarding returns a COARSE privacy-safe view with NO steps
  // array, and s.steps.findIndex(...) then threw, crashing the whole page silently (the dead /welcome).
  // When steps are absent it means this page is being viewed remotely: the owner setup lives on the Mac
  // itself, so point there instead of crashing.
  if(!Array.isArray(s.steps)){
    const port=(location.port||'8765');
    document.getElementById("steps").innerHTML =
      `<div class="step"><div class="stitle">You're not on the Mac-local setup page</div>
       <div class="sdetail">Owner setup is private to the computer Mongoose is installed on. On that Mac, open
         <a href="https://127.0.0.1:${port}/welcome" style="color:var(--gold)">https://127.0.0.1:${port}/welcome</a>.
         If this is a phone or tablet that has already been paired, open the phone surface instead.</div>
       <div class="sbody"><div class="row2">
         <button onclick="location.href='/device/launchpad'">Open phone surface</button>
         <button class="ghost" onclick="location.href='https://127.0.0.1:${port}/welcome'">Open local setup on Mac</button>
       </div><div class="muted" style="margin-top:10px">Need to pair? Use the QR/code shown on the Mac install or Devices screen; this remote page cannot mint owner codes.</div></div></div>`;
    document.getElementById("lede").textContent = "This page is safe to open remotely, but setup actions happen on the Mac.";
    return;
  }
  let activeIdx = s.steps.findIndex(x=>!x.done);
  if((skippedConnect||forcePhone)){
    const pi = s.steps.findIndex(x=>x.key==="phone");
    if(pi>=0 && !s.steps[pi].done) activeIdx = pi;
  }
  document.getElementById("steps").innerHTML = s.steps.map((st,i)=>stepCard(st,i,i===activeIdx)).join("");
}

async function waitForMongooseThenReload(statusElId){
  const el=()=>document.getElementById(statusElId);
  for(let i=0;i<25;i++){
    await new Promise(r=>setTimeout(r,3000));
    const w=el(); if(w) w.textContent="Waiting for Mongoose to come back... "+((i+1)*3)+"s";
    try{ const r=await j("/health",{cache:"no-store"},2500); if(r){ location.reload(); return; } }catch(_e){}
  }
  const w=el();
  if(w) w.innerHTML='Still waiting. Tap to retry: <a href="'+location.pathname+'" style="color:var(--gold)">reopen this page</a>.'
    +'<div style="margin-top:6px">If your browser shows a certificate warning when you tap, choose Show Details, then visit the site. That happens when the local certificate changed during the restart, and accepting it once fixes it.</div>';
  else location.reload();
}
async function startPair(){
  const box=document.getElementById("qrbox"); box.innerHTML='<span class="muted">Creating a one-time code...</span>';
  try{
    const n=await getNonce();
    if(!n){
      box.innerHTML='<span style="color:var(--bad)">Pairing codes are created from the Mac-local cockpit. Open <span style="font-family:monospace;color:var(--gold)">https://127.0.0.1:'+(location.port||'8765')+'/welcome</span> on the Mac running Mongoose.</span>';
      return;
    }
    const r=await fetch("/cockpit/devices/pair-code",{method:"POST",
      headers:{"Content-Type":"application/json","X-Cockpit-Nonce":n},body:JSON.stringify({profile:"primary_phone",auto_repair:true})});
    const d=await r.json();
    if(!d.code){ box.innerHTML='<span style="color:var(--bad)">Could not create a code.</span>'; return; }
    const qr = d.qr_svg? `<div class="qr">${d.qr_svg}</div>` : "";
    const mins=Math.round((d.expires_in||300)/60);
    const reachMsg2 = d.reachability_warning==="not_lan_reachable"
      ? "The QR address is right, but this running server is not reachable from the network yet."
      : d.reachability_warning;
    const ar2 = d.auto_repair||{};
    const warn = d.reachability_warning? `<div style="margin-top:8px;padding:8px 10px;border:1px solid #d8a200;border-radius:8px;background:rgba(255,176,0,.1);color:#9a6b00;font-size:12px">⚠ ${esc(reachMsg2)}${ar2.restarting? `<div style="margin-top:6px;color:var(--ok)">Repairing now: applying LAN settings and restarting the listener.</div><div id="wiz-restartwait" style="margin-top:4px;font-size:11px">Waiting for Mongoose to come back...</div>` : ""}</div>` : "";
    if(ar2.restarting){ setTimeout(()=>waitForMongooseThenReload("wiz-restartwait"), 400); }
    box.innerHTML=`<div class="qrwrap">${qr}<div style="flex:1;min-width:200px">
      <div style="font-weight:600;margin-bottom:4px">Scan with your phone's camera</div>
      <div class="muted">or open this address on the phone:</div>
      <div class="tok">${esc(d.pair_url)}</div>
      ${warn}
      <div class="muted" style="margin-top:8px">Code expires in ${mins} min. This page will move on once your phone connects.</div>
    </div></div>`;
    pollFast();
  }catch(e){ box.innerHTML='<span style="color:var(--bad)">Failed: '+esc(e)+'</span>'; }
}

function showWelcomeError(e){
  const el=document.getElementById("steps");
  if(el) el.innerHTML=`<div class="step"><div class="stitle">Mongoose is running, but this welcome check failed</div>
    <div class="sdetail">${esc(String(e||"Could not load setup state"))}. You can still open the local cockpit, phone surface, or retry.</div>
    <div class="sbody"><div class="row2"><button onclick="refresh()">Retry</button><button class="ghost" onclick="location.href=localUiUrl('')">Open cockpit</button><button class="ghost" onclick="location.href='/device/launchpad'">Phone surface</button></div></div></div>`;
}
async function refresh(){ try{ render(await j("/cockpit/onboarding")); }catch(e){ showWelcomeError(e); } }
let fastTimer=null;
function pollFast(){ if(fastTimer) return; fastTimer=setInterval(refresh, 2500); }
async function boot(){ await refresh(); setInterval(refresh, 6000); }
boot();
</script>
</body></html>"""


def welcome_html() -> str:
    return WELCOME_HTML


INSTALL_HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Installing Mongoose</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/img/mongoose-32.png">
<link rel="apple-touch-icon" href="/static/img/mongoose-256.png">
<style>
  :root{ --ink:#16140F; --panel:#1F1C16; --panel2:#26221B; --line:#322D24; --text:#ECE6DA;
         --dim:#9C9486; --ok:#7CB893; --gold:#C9A35B; --gold-dim:#7d6736; --bad:#D0785E; }
  *{box-sizing:border-box}
  body{margin:0;font:16px/1.65 "Inter",system-ui,sans-serif;background:var(--ink);color:var(--text);
       min-height:100vh;display:flex;justify-content:center;padding:28px 18px;
       background-image:radial-gradient(1100px 600px at 50% -10%, rgba(201,163,91,0.09), transparent 70%)}
  @font-face{font-family:"Inter";font-weight:400;font-display:swap;src:url("/static/fonts/inter-latin-400-normal.woff2") format("woff2")}
  @font-face{font-family:"Inter";font-weight:500;font-display:swap;src:url("/static/fonts/inter-latin-500-normal.woff2") format("woff2")}
  @font-face{font-family:"Inter";font-weight:600;font-display:swap;src:url("/static/fonts/inter-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"Fraunces";font-weight:600;font-display:swap;src:url("/static/fonts/fraunces-latin-600-normal.woff2") format("woff2")}
  @font-face{font-family:"IBM Plex Mono";font-weight:500;font-display:swap;src:url("/static/fonts/ibm-plex-mono-latin-500-normal.woff2") format("woff2")}
  .wrap{width:100%;max-width:560px}
  .mark{display:flex;align-items:center;gap:12px;margin-bottom:6px}
  .pulse{width:12px;height:12px;border-radius:50%;background:var(--gold);position:relative}
  .pulse::after{content:"";position:absolute;inset:0;border-radius:50%;background:var(--gold);
                animation:breathe 2.6s ease-in-out infinite}
  .pulse.ok{background:var(--ok)} .pulse.ok::after{background:var(--ok);animation:none}
  @keyframes breathe{0%{transform:scale(1);opacity:.5}70%{transform:scale(2.6);opacity:0}100%{opacity:0}}
  h1{font-family:"Fraunces",serif;font-size:30px;margin:0;font-weight:600}
  .sub{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--gold)}
  .lede{color:var(--dim);font-size:15px;margin:10px 0 22px}
  .barhead{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px}
  .pct{font-family:'Fraunces',serif;font-size:30px;font-weight:600;color:var(--gold);line-height:1}
  .eta{font-size:13px;color:var(--dim)}
  .bar{height:10px;background:var(--panel2);border-radius:6px;overflow:hidden;margin-bottom:24px;border:1px solid var(--line)}
  .bar>div{height:100%;width:0;background:linear-gradient(90deg,var(--gold-dim),var(--gold));transition:width .6s ease}
  .row{display:flex;align-items:center;gap:14px;padding:14px 16px;border:1px solid var(--line);
       border-radius:13px;margin-bottom:10px;background:linear-gradient(180deg,var(--panel),#1c1914);transition:border-color .3s,background .3s}
  .row.active{border-color:var(--gold-dim);background:linear-gradient(180deg,rgba(201,163,91,.10),#1c1914)}
  /* A completed step stays fully legible (no fade): a green left-edge and a green tick, so the ticks
     accumulate into a visible column as install proceeds. The label softens slightly but stays readable. */
  .row.done{border-color:rgba(74,222,128,.32);border-left:3px solid var(--ok);background:linear-gradient(180deg,rgba(74,222,128,.05),#1b1a16)}
  .row.done .lbl{color:#cfe9d4}
  .dot{width:26px;height:26px;border-radius:50%;border:1px solid var(--line);flex:none;display:flex;
       align-items:center;justify-content:center;font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--dim);transition:background .25s,border-color .25s}
  .row.active .dot{border-color:var(--gold);color:var(--gold)}
  .row.done .dot{background:var(--ok);border-color:var(--ok);color:#10231a;font-size:15px;font-weight:700;animation:tickpop .32s ease}
  .row.failed .dot{background:var(--bad);border-color:var(--bad);color:#2a120c}
  @keyframes tickpop{0%{transform:scale(.4);opacity:0}60%{transform:scale(1.18)}100%{transform:scale(1);opacity:1}}
  .spin{width:14px;height:14px;border:2px solid var(--gold-dim);border-top-color:var(--gold);border-radius:50%;animation:sp .8s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}
  .lbl{font-weight:500;font-size:15px} .det{color:var(--dim);font-size:13px;margin-top:2px}
  .done-card{display:none;text-align:center;margin-top:8px}
  .done-card.show{display:block}
  .pair-card{display:none;text-align:center;margin-top:18px;padding:20px;border:1px solid var(--gold-dim);border-radius:14px;background:rgba(201,163,91,.06)}
  .pair-card.show{display:block}
  .pair-card.hide{display:none}
  .qr{background:#fff;border-radius:14px;padding:16px;display:inline-block;margin:16px 0 8px}
  .qr svg{width:200px;height:200px;display:block}
  .pill{font-family:"IBM Plex Mono",monospace;font-size:13px;background:var(--panel2);border:1px solid var(--line);
        border-radius:9px;padding:8px 12px;display:inline-block;margin-top:6px;letter-spacing:.08em}
  .row2{display:flex;gap:10px;margin-top:6px;flex-wrap:wrap;justify-content:center}
  a.btn{display:inline-block;margin-top:14px;background:var(--panel2);border:1px solid var(--gold-dim);color:#e9d9b4;
        border-radius:11px;padding:11px 18px;font-weight:600;text-decoration:none}
  .muted{color:var(--dim);font-size:13px;margin-top:14px}

  @media (prefers-reduced-motion:reduce){ *,*::before,*::after{ animation-duration:.001ms !important; animation-iteration-count:1 !important; transition-duration:.001ms !important; } }
</style></head>
<body><div class="wrap">
  <div class="mark"><img class="logo-emerge" src="/static/img/mongoose-64.png" alt="Mongoose" width="44" height="44" style="margin-right:6px"><span class="pulse" id="pulse"></span>
    <div><h1>Mongoose</h1><div class="sub" id="sub">Setting things up</div></div></div>
  <div class="lede" id="lede">Getting your local assistant running. This page updates itself, you can leave it open.</div>
  <div class="barhead"><span class="pct" id="pct">0%</span><span class="eta" id="eta"></span></div>
  <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="fillwrap"><div id="fill"></div></div>
  <div id="steps"></div>

  <div class="pair-card" id="paircard">
    <div style="font-family:'Fraunces',serif;font-size:20px;margin-bottom:4px">Pair your phone while it sets up</div>
    <div class="muted" style="margin-top:2px">No need to wait for the brain to finish downloading, scan now and you're ready the moment it's done.</div>
    <div class="qr" id="qr"></div>
    <div><span class="pill" id="code"></span></div>
    <div id="ownersec" style="display:none;margin-top:10px;padding:10px 12px;border:1px solid var(--gold-dim);border-radius:10px;background:rgba(201,163,91,.08);font-size:13px">
      This is <b>your</b> setup. The first phone to pair becomes the owner, so this code only works from this Mac.
      To pair the owner phone from elsewhere, it must enter this secret (shown only here):
      <div id="ownersecval" style="font-family:'IBM Plex Mono',monospace;font-size:20px;letter-spacing:3px;color:var(--gold);margin-top:6px"></div>
      <div class="muted" style="font-size:12px;margin-top:4px">Guests are added later from the cockpit, they never use this code.</div>
    </div>
  </div>

  <div class="done-card" id="done">
    <div style="font-family:'Fraunces',serif;font-size:22px;margin-bottom:4px">It's alive, and it's yours.</div>
    <div class="muted" style="margin-top:2px">Scan this with your phone's camera to carry Mongoose with you: glance, ask, approve, and talk to it from anywhere in the house.</div>
    <div><span class="pill" id="code2"></span></div>
    <div class="muted">Code refreshes itself until you pair. Prefer the desktop?</div>
    <div class="row2" style="justify-content:center;margin-top:10px">
      <a class="btn" href="#" onclick="event.preventDefault(); location.href=localWelcomeUrl()">Continue guided setup</a>
      <a class="btn" href="#" onclick="event.preventDefault(); location.href=localUiUrl('')" style="margin-left:8px">Open full cockpit</a>
      <a class="btn" href="#" onclick="event.preventDefault(); location.href=localUiUrl('#chat')" style="margin-left:8px">Open chat</a>
    </div>
    <div class="muted" style="margin-top:18px;font-size:12px;opacity:.8">You're starting on solid default models. As Mongoose learns how you actually use it, it benchmarks itself in the background and, later, suggests the best models for you, no need to choose now.</div>
  </div>
  <div class="muted" id="verline" data-version="__MONGOOSE_VERSION__"
       style="margin-top:22px;font-size:11px;opacity:.5;text-align:center">Mongoose v__MONGOOSE_VERSION__</div>
</div>
<script>
// Bootstrap must never be mediated by a cached/stale service worker (a leftover SW once turned a
// momentary timeout on this very screen into a false offline banner). Release any controller so
// /install always reflects the live server.
if('serviceWorker' in navigator){navigator.serviceWorker.getRegistrations().then(function(rs){rs.forEach(function(r){r.unregister();});}).catch(function(){});}

const STEPICON={pending:"",running:'<div class="spin"></div>',done:"✓",failed:"!",skipped:"-"};
// esc() is called by renderSteps() and showDone() but was NOT defined on this page. The first call in
// renderSteps threw ReferenceError, the tick() loop's catch swallowed it, and the page sat at 100% with
// no steps, no buttons, no handoff: the dead-screen bug. Define it here, before anything uses it. It
// HTML-escapes text so step labels/details and pairing strings can't break the markup.
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, function(c){
  return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
// Owner cockpit/welcome are localhost-only by default. The installer opens THIS page on localhost, so
// these resolve correctly; building them explicitly from localhost also keeps the handoff working if
// the page is ever reached via the LAN address.
function localOwnerBase(){ const p=location.port||((location.protocol==="https:")?"443":"80");
  // Preserve the loopback host the user is ALREADY on. localhost and 127.0.0.1 are different
  // browser origins with separate service workers and separate certificate acceptances; sending
  // someone who is happily on 127.0.0.1 over to localhost lands them on an origin they have not
  // trusted (and possibly a stale worker). Only fall back to localhost from a non-loopback host.
  const h=location.hostname;
  const loop=(h==="localhost"||h==="127.0.0.1"||h==="[::1]"||h==="::1");
  return location.protocol+"//"+(loop?h:"localhost")+":"+p; }
function localUiUrl(suffix){ return localOwnerBase()+"/ui"+(suffix||""); }
function localWelcomeUrl(){ return localOwnerBase()+"/welcome"; }
async function waitForMongooseThenReload(statusElId){
  const el=()=>document.getElementById(statusElId);
  for(let i=0;i<25;i++){
    await new Promise(r=>setTimeout(r,3000));
    const w=el(); if(w) w.textContent="Waiting for Mongoose to come back... "+((i+1)*3)+"s";
    try{
      const resp=await fetch("/health",{cache:"no-store"});
      if(resp && resp.ok){ location.reload(); return; }
    }catch(_e){}
  }
  const w=el();
  if(w) w.innerHTML='Still waiting. Tap to retry: <a href="'+location.pathname+'" style="color:var(--gold)">reopen this page</a>.';
  else location.reload();
}
let paired=false, qrShown=false, pairLoaded=false;

// Load the pairing code + QR EARLY: as soon as the server is reachable, not when the whole install
// finishes. The pairing code only needs the server (not the model download), so showing it now lets
// the user scan during the long brain-download instead of the QR popping in at 100%. Idempotent.
async function loadPairingCode(){
  if(pairLoaded) return; pairLoaded=true;
  const card=document.getElementById("paircard");
  if(card) card.classList.add("show");
  // Show the owner-pairing secret (loopback-only endpoint; returns nothing to remote callers).
  try{
    const os=await j("/install/owner-secret");
    if(os && os.active && os.secret){
      const box=document.getElementById("ownersec"); const val=document.getElementById("ownersecval");
      if(val) val.textContent=os.secret;
      if(box) box.style.display="block";
    }
  }catch(e){ /* not loopback or no secret; skip */ }
  const qrEl=document.getElementById("qr");
  if(qrEl) qrEl.innerHTML=`<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:200px;gap:12px">
    <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="16" stroke="rgba(255,255,255,0.15)" stroke-width="4"/>
      <path d="M20 4a16 16 0 0 1 16 16" stroke="var(--gold)" stroke-width="4" stroke-linecap="round">
        <animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20" dur="0.9s" repeatCount="indefinite"/>
      </path>
    </svg>
    <div style="font-size:12px;color:rgba(255,255,255,0.45)">Getting your pairing code…</div>
  </div>`;
  try{
    const d=await j("/install/pair-code?auto_repair=1",{method:"POST"});
    if(d.qr_svg){ qrEl.innerHTML=d.qr_svg; qrEl.dataset.loaded='1'; }
    else if(qrEl){
      qrEl.style.background="transparent"; qrEl.style.padding="0";
      qrEl.innerHTML='<div class="muted" style="font-size:13px;line-height:1.5">No QR to scan yet. '
        +'On the phone or tablet, open this address in the browser:'
        +(d.pair_url?('<div style="font-family:monospace;font-size:12px;word-break:break-all;color:var(--gold);margin:8px 0">'+d.pair_url+'</div>'):'')
        +'</div>';
    }
    if(d.code){ const c=document.getElementById("code"); if(c) c.textContent="code "+d.code;
                const c2=document.getElementById("code2"); if(c2) c2.textContent="code "+d.code; }
    if(d.reachability_warning && qrEl){
      // Reset the QR box chrome first: it is styled as a white square for QR contrast, and text in
      // the page's muted cream on that white square was unreadable. Sibling branches already do this.
      qrEl.style.background="transparent"; qrEl.style.padding="0";
      let why="";
      const g=d.reachability_diagnosis||{};
      if(g.env_file_host==="0.0.0.0" && g.effective_host!=="0.0.0.0"){
        why='Config already says LAN access is on ('+esc(g.env_path||".env")+'), but this running process started before that. A restart applies it.';
      } else if(g.effective_host && g.effective_host!=="0.0.0.0"){
        why='The server is listening on '+esc(g.effective_host)+' only, so a phone cannot reach it yet.';
      }
      qrEl.innerHTML='<div style="font-size:13px;line-height:1.6;color:var(--muted)">Pairing link not ready yet.<br>Tap the button below first.'+(why?('<div style="font-size:12px;margin-top:8px;color:var(--muted)">'+why+'</div>'):'')+'</div>';
      const existing=document.getElementById("reachwarn"); if(existing) existing.remove();
      const warn=document.createElement("div"); warn.id="reachwarn";
      warn.style.cssText="margin:14px auto 0;max-width:340px;padding:14px;border-radius:12px;background:var(--panel2);text-align:center";
      const ar3=d.auto_repair||{};
      if(ar3.restarting){
        // DEFENSE IN DEPTH (external triage): Mongoose already started repairing this the
        // moment the pairing code was requested (auto_repair=1 above); show that plainly and
        // poll for it coming back, rather than asking the owner to press a button for
        // something already in progress.
        qrEl.innerHTML='<div style="font-size:13px;line-height:1.6;color:var(--muted)">Repairing phone access now.<br>This takes about 10 seconds.</div>';
        warn.innerHTML=`<div style="font-size:14px;font-weight:600;margin-bottom:8px">Repairing phone access now</div>
          <div style="font-size:13px;color:var(--muted);margin-bottom:10px">Mongoose is applying the LAN settings and restarting the listener so your phone can connect.</div>
          <div id="restartwait" style="font-size:13px;color:var(--muted)">Waiting for Mongoose to come back...</div>`;
        qrEl.insertAdjacentElement("afterend", warn);
        waitForServerThenReload();
      } else {
        warn.innerHTML=`<div style="font-size:14px;font-weight:600;margin-bottom:8px">One step before scanning</div>
          <div style="font-size:13px;color:var(--muted);margin-bottom:14px">Mongoose needs to allow connections from your phone. This takes about 10 seconds and only happens once.</div>
          <button onclick="enablePhone(this,false)" style="width:100%;background:var(--gold);color:#1a1a1a;border:none;border-radius:10px;padding:12px 20px;font-size:15px;font-weight:700;cursor:pointer">Allow phone access →</button>`;
        qrEl.insertAdjacentElement("afterend", warn);
      }
    }
    pollPaired();
  }catch(e){
    pairLoaded=false;
    if(qrEl){
      qrEl.style.background="transparent"; qrEl.style.padding="0";
      qrEl.innerHTML='<div class="muted" style="font-size:13px;line-height:1.5">Could not create a pairing code yet. Mongoose is still usable on this Mac. <button onclick="pairLoaded=false;loadPairingCode()" style="margin-top:10px">Retry pairing code</button></div>';
    }
  }
}
// Shared tunnel/service helpers copied from the cockpit call ensureNonce(); this page previously
// used it WITHOUT defining it, an undefined reference that node --check cannot catch (parse-only;
// it's a runtime ReferenceError). Caught by a parallel build's embedded-JS gate; keep the
// lightweight same-origin helper here so the shared code works on this page too.
let NONCE=null;
async function ensureNonce(){ if(NONCE)return NONCE;
  try{ const r=await fetch("/cockpit/nonce",{cache:"no-store"}); if(r.ok){ NONCE=(await r.json()).cockpit_nonce; } }catch(e){}
  return NONCE; }
async function j(u,o,timeoutMs){ timeoutMs=timeoutMs||15000;
  const ctrl=new AbortController(); const to=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{ const r=await fetch(u,Object.assign({},o||{},{signal:ctrl.signal})); return await r.json(); }
  finally{ clearTimeout(to); } }
// postAction() is called by enablePhone() but was NOT defined on this page (it exists only on the
// cockpit page). The "Allow phone access" button therefore threw ReferenceError: the same dead-button
// class as the missing esc(). Define it here: a POST with a JSON body and an abort timeout.
async function postAction(u, body, timeoutMs){ timeoutMs=timeoutMs||15000;
  const ctrl=new AbortController(); const to=setTimeout(()=>ctrl.abort(),timeoutMs);
  try{ return await fetch(u,{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body||{}),signal:ctrl.signal}); }
  finally{ clearTimeout(to); } }
let _stepsSig="";
function renderSteps(p){
  // Re-render only when the set of step statuses actually changes. Without this, the 1.5s poll would
  // rebuild the list every tick and replay the green-tick pop animation on already-done steps, which
  // looks like flickering. Rebuilding on change only means each tick pops exactly once, when its step
  // completes.
  const sig=(p.steps||[]).map(s=>s.key+":"+s.status).join("|");
  if(sig===_stepsSig) return;
  _stepsSig=sig;
  const el=document.getElementById("steps"); el.innerHTML="";
  p.steps.forEach((s,i)=>{
    if(s.key==="ready") return;
    const cls=s.status==="done"||s.status==="skipped"?"done":(s.status==="running"?"active":(s.status==="failed"?"failed":""));
    const icon=s.status==="running"?STEPICON.running:(STEPICON[s.status]||(i+1));
    el.insertAdjacentHTML("beforeend",
      `<div class="row ${cls}"><div class="dot">${icon}</div>
       <div><div class="lbl">${esc(s.label || ('Step ' + (i+1)))}</div>${s.detail?`<div class="det">${esc(s.detail)}</div>`:""}</div></div>`);
  });
}
async function showDone(p){
  if(qrShown) return; qrShown=true;
  // Reveal the success card and its links FIRST, guarded individually, so that NOTHING below (a failed
  // pairing fetch, a missing element, a thrown await) can leave the user staring at a bare 100% bar with
  // no message and no way forward. This was a real failure: showDone threw early and the done block never
  // appeared. The static #done card already contains the headline, the pairing pill and the "Open the
  // cockpit" link, so revealing it is the load-bearing step and must happen before anything that can fail.
  try{ var _pulse=document.getElementById("pulse"); if(_pulse) _pulse.classList.add("ok"); }catch(e){}
  try{ var _sub=document.getElementById("sub"); if(_sub) _sub.textContent=(p && p.failed)? "Ready, with a few things to finish" : "Ready"; }catch(e){}
  try{ var _lede=document.getElementById("lede"); if(_lede) _lede.style.display="none"; }catch(e){}
  const doneEl=document.getElementById("done");
  if(doneEl){
    doneEl.classList.add("show");
    try{ doneEl.scrollIntoView({behavior:"smooth", block:"start"}); }catch(e){ try{ doneEl.scrollIntoView(); }catch(_){} }
  }
  // Spinner in the QR slot immediately: visible within 200ms of double-clicking
  const _earlyQr=document.getElementById("qr");
  if(_earlyQr && !_earlyQr.dataset.loaded) _earlyQr.innerHTML=`<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:200px;gap:12px">
    <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="20" cy="20" r="16" stroke="rgba(255,255,255,0.15)" stroke-width="4"/>
      <path d="M20 4a16 16 0 0 1 16 16" stroke="var(--gold)" stroke-width="4" stroke-linecap="round">
        <animateTransform attributeName="transform" type="rotate" from="0 20 20" to="360 20 20" dur="0.9s" repeatCount="indefinite"/>
      </path>
    </svg>
    <div style="font-size:12px;color:rgba(255,255,255,0.45)">Getting your pairing code…</div>
  </div>`;
  const failed = (p && p.steps? p.steps.filter(s=>s.status==="failed") : []);
  if(failed.length){
    const top = failed.slice(0,3).map(s=>esc(s.label + (s.detail?": "+s.detail:""))).join("<br>");
    const watchdogOnly = failed.length===1 && failed[0].key==="watchdog";
    const headline = watchdogOnly
      ? "Mongoose is installed and running. You can use it now."
      : "Mongoose is running, but a few optional setup steps didn't finish.";
    const note = watchdogOnly
      ? "The auto-restart watchdog didn't install, so Mongoose may not restart itself after a crash. Add it later with: mongoose server-watchdog --install"
      : top;
    document.getElementById("done").insertAdjacentHTML("afterbegin",
      `<div style="margin:0 0 14px;padding:10px 12px;border:1px solid var(--gold);border-radius:10px;background:rgba(201,163,91,.12);color:var(--text);font-size:13px;text-align:left">
         <div style="font-weight:600;margin-bottom:4px">${headline}</div>
         <div class="muted" style="color:#d7cabb">${note}</div>
       </div>`);
  }
  // The pairing QR was already loaded early (during the model download) into the pair-card. Ensure
  // it's loaded (idempotent), then relocate it under the success headline and hide the now-redundant
  // "pair while it sets up" card so there's a single QR on screen. ALL guarded: a pairing failure must
  // not blank the success screen that is already on display above.
  try{
    await loadPairingCode();
    const paircard=document.getElementById("paircard");
    const qrEl=document.getElementById("qr");
    const code2=document.getElementById("code2");
    if(qrEl && code2 && code2.parentElement){ code2.parentElement.insertAdjacentElement("beforebegin", qrEl); }
    const rw=document.getElementById("reachwarn");
    if(rw && code2 && code2.parentElement){ code2.parentElement.insertAdjacentElement("beforebegin", rw); }
    if(paircard) paircard.classList.add("hide");
  }catch(e){ /* the success card + cockpit link are already shown above; pairing can be retried by reload */ }
}
async function enablePhone(btn, force){
  btn.disabled=true; const orig=btn.textContent; btn.textContent= force? "repairing & restarting..." : "enabling & restarting...";
  const box=document.getElementById("reachwarn")||document.getElementById("dev-reachwarn");
  try{
    // The install page is public during first-run and has no cockpit nonce, so it must NOT call the
    // nonce-gated /cockpit/enable-phone. Use the loopback-only /install/enable-phone when on /install.
    const endpoint = (location.pathname.indexOf("/install")===0) ? "/install/enable-phone" : "/cockpit/enable-phone";
    const r=await postAction(endpoint,{force:!!force},60000);
    const d=await r.json();
    if(d.ok){
      if(d.already && !force){
        // config was already set but we didn't restart, tell the user the repair path exists
        btn.disabled=false; btn.textContent=orig;
        if(box) box.insertAdjacentHTML("beforeend", `<div style="color:var(--gold);margin-top:6px">${esc(d.detail||"Already configured. Use Repair / restart if the phone still can't connect.")}</div>`);
      } else {
        if(box) box.innerHTML=`<div style="color:var(--ok)">✓ ${esc(d.detail||"Enabled. Restarting Mongoose now.")}</div><div id="restartwait" style="color:var(--muted);font-size:13px;margin-top:8px">Waiting for Mongoose to come back...</div>`;
        waitForServerThenReload();
      }
    } else {
      btn.disabled=false; btn.textContent=orig;
      if(box) box.insertAdjacentHTML("beforeend", `<div style="color:var(--warn);margin-top:6px">${esc(d.error||"could not enable")}</div>`);
    }
  }catch(e){ /* the server is restarting; poll instead of blind-reloading onto a dead port */ waitForServerThenReload(); }
}
async function serviceInstall(btn){
  btn.disabled=true; const o=btn.textContent; btn.textContent="installing...";
  const box=document.getElementById("tunnelMsg");
  try{
    const d=await (await postAction("/cockpit/service/install",{},30000)).json();
    if(d.ok || d.loaded){ if(box) box.innerHTML=`<div style="color:var(--ok);margin-top:6px">Done. Mongoose (and the tunnel) will start automatically after a reboot.</div>`; setTimeout(()=>loadDevices(),800); }
    else { btn.disabled=false; btn.textContent=o; if(box) box.innerHTML=`<div style="color:var(--warn);margin-top:6px">${esc(d.error||d.detail||"could not install")}</div>`; }
  }catch(e){ btn.disabled=false; btn.textContent=o; if(box) box.innerHTML=`<div style="color:var(--warn);margin-top:6px">${esc(e)}</div>`; }
}
async function tunnelInstall(btn){
  btn.disabled=true; const o=btn.textContent; btn.textContent="downloading...";
  const box=document.getElementById("tunnelMsg");
  try{
    const d=await (await postAction("/cockpit/tunnel/install",{},180000)).json();
    if(d.ok){ if(box) box.innerHTML=`<div style="color:var(--ok);margin-top:6px">Connector ready.</div>`; setTimeout(()=>loadDevices(),800); }
    else { btn.disabled=false; btn.textContent=o; if(box) box.innerHTML=`<div style="color:var(--warn);margin-top:6px">${esc(d.error||"download failed")}</div>`; }
  }catch(e){ btn.disabled=false; btn.textContent=o; if(box) box.innerHTML=`<div style="color:var(--warn);margin-top:6px">${esc(e)}</div>`; }
}
async function tunnelLogin(btn){
  btn.disabled=true; const o=btn.textContent; btn.textContent="starting...";
  const box=document.getElementById("tunnelLoginBox");
  try{
    const d=await (await postAction("/cockpit/tunnel/login",{},30000)).json();
    if(d.ok && d.login_url){
      if(box) box.innerHTML=`<a class="act" href="${esc(d.login_url)}" target="_blank" rel="noopener" style="text-decoration:none;display:inline-block">Approve in Cloudflare (opens new tab)</a>`
        +`<div class="muted" style="font-size:12px;margin-top:6px">After approving, this card updates by itself.</div>`;
      // Poll until cert.pem lands, then refresh the card to Step 3.
      const poll=setInterval(async()=>{
        try{
          const pn=await ensureNonce();
          const s=await (await fetch("/cockpit/tunnel/state",{headers:{"X-Cockpit-Nonce":pn}})).json();
          if(s.state && s.state.logged_in){ clearInterval(poll); loadDevices(); }
        }catch(_e){}
      }, 3000);
      setTimeout(()=>clearInterval(poll), 300000);
    } else if(d.ok && d.already){ loadDevices(); }
    else { btn.disabled=false; btn.textContent=o; if(box) box.insertAdjacentHTML("beforeend", `<div style="color:var(--warn);margin-top:6px">${esc(d.error||"could not start login")}</div>`); }
  }catch(e){ btn.disabled=false; btn.textContent=o; }
}
async function tunnelProvision(btn, hostname){
  btn.disabled=true; const o=btn.textContent; btn.textContent="setting up...";
  const box=document.getElementById("tunnelMsg");
  try{
    const d=await (await postAction("/cockpit/tunnel/provision",{hostname:hostname},120000)).json();
    if(d.ok){
      if(box) box.innerHTML=`<div style="color:var(--ok);margin-top:6px">Live at ${esc(d.public_url)}. Pairing and guest invites now use this address.</div>`;
      setTimeout(()=>loadDevices(),1200);
    } else {
      btn.disabled=false; btn.textContent=o;
      if(box) box.innerHTML=`<div style="color:var(--warn);margin-top:6px">Stopped at step "${esc(d.step||"?")}": ${esc(d.error||"unknown")}</div>`;
    }
  }catch(e){ btn.disabled=false; btn.textContent=o; if(box) box.innerHTML=`<div style="color:var(--warn);margin-top:6px">${esc(e)}</div>`; }
}
async function repairRemoteAccess(btn){
  // The Cockpit-side equivalent of the phone's /device/remote/repair action, for the DDNS
  // route specifically. Walks listener -> DDNS refresh -> bounded UPnP, same chain, same
  // safety boundary (UPnP only attempted when DDNS is genuinely configured and nothing
  // simpler already worked). If the listener step restarts Mongoose, this reuses the SAME
  // wait-for-server pattern the phone/LAN repair already uses, rather than a second one.
  btn.disabled=true; const orig=btn.textContent; btn.textContent="repairing...";
  const box=document.getElementById("raRepairBox");
  try{
    const r=await postAction("/cockpit/remote-access/repair",{},60000);
    const d=await r.json();
    if(d.step==="listener" && d.restart){
      if(box) box.innerHTML=`<div style="color:var(--ok)">${esc(d.detail||"Repairing the listener and restarting Mongoose.")}</div><div id="restartwait" style="color:var(--muted);font-size:13px;margin-top:8px">Waiting for Mongoose to come back...</div>`;
      waitForServerThenReload();
      return;
    }
    btn.disabled=false; btn.textContent=orig;
    const color = d.ok ? "var(--ok)" : "var(--warn)";
    if(box) box.insertAdjacentHTML("beforeend", `<div style="color:${color};margin-top:6px;font-size:13px;line-height:1.4">${esc(d.detail||(d.ok?"Repaired.":"Could not repair from here."))}</div>`);
    if(d.ok) setTimeout(()=>{ if(typeof loadDevices==="function") loadDevices(); }, 1200);
  }catch(e){
    btn.disabled=false; btn.textContent=orig;
    if(box) box.insertAdjacentHTML("beforeend", `<div style="color:var(--warn);margin-top:6px">Could not reach Mongoose to run the repair: ${esc(e)}</div>`);
  }
}
async function waitForServerThenReload(){
  // A fixed-delay reload was a gamble: too early lands on a browser error page mid-restart, and if
  // the local certificate changed across the restart, fetch() here fails even once the server is UP
  // (only a real navigation can show the certificate prompt). So: poll /health briefly, reload the
  // moment it answers, and after ~75s hand over a same-origin link plus the certificate note instead
  // of stranding the user.
  const el=()=>document.getElementById("restartwait");
  for(let i=0;i<25;i++){
    await new Promise(r=>setTimeout(r,3000));
    const w=el(); if(w) w.textContent="Waiting for Mongoose to come back... "+((i+1)*3)+"s";
    try{ const r=await j("/health",{cache:"no-store"},2500); if(r){ location.reload(); return; } }catch(_e){}
  }
  const w=el();
  if(w) w.innerHTML='Still waiting. Tap to retry: <a href="'+location.pathname+'" style="color:var(--gold)">reopen this page</a>.'
    +'<div style="margin-top:6px">If Safari shows a certificate warning when you tap, choose Show Details, then visit the website. That happens when the Mac\'s local certificate changed during the restart, and accepting it once fixes it.</div>';
  else location.reload();
}
async function pollPaired(){
  if(paired) return;
  try{ const s=await j("/cockpit/onboarding");
    if(s.signals && s.signals.paired>0){ paired=true;
      let tut=null; try{ tut=await j("/cockpit/tutorial"); }catch(e){}
      let examples="";
      if(tut && tut.try_now){ examples='<div class="muted" style="margin-top:14px">Try saying:</div>'
        + tut.try_now.slice(0,3).map(x=>`<div style="font-family:'IBM Plex Mono',monospace;font-size:13px;margin:4px 0;color:var(--gold)">"${x.try}"</div>`).join("")
        + '<div style="font-family:\'IBM Plex Mono\',monospace;font-size:13px;margin:8px 0 0;color:var(--ok)">"Mongoose, do something cool"</div>'; }
      document.getElementById("done").innerHTML=
        '<img src="/static/img/sillygoose-256.png" alt="" width="48" height="48" style="margin-bottom:6px"><div style="font-family:\'Fraunces\',serif;font-size:23px">Your phone is paired.</div>'
        +'<div class="muted" style="margin-top:4px">Mongoose lives on your phone now. Say hello, or ask it to introduce itself.</div>'
        +examples
        +'<a class="btn" href="#" onclick="event.preventDefault(); location.href=localUiUrl(&quot;&quot;)" style="margin-top:16px">Open the cockpit</a>'; return; }
  }catch(e){}
  setTimeout(pollPaired,2500);
}
async function checkFresh(){
  // If THIS page was served by a different (older) server than the one now answering, the page is
  // stale, exactly the "I opened a previous /install" situation. Detect it and tell the user to
  // refresh, rather than silently showing the old flow.
  try{
    const h=await j("/health");
    const pageVer=(document.getElementById("verline")||{}).dataset?(document.getElementById("verline").dataset.version||""):"";
    if(h&&h.version&&pageVer&&h.version!==pageVer){
      const b=document.createElement("div");
      b.style.cssText="margin:14px 0;padding:10px 12px;border:1px solid var(--gold);border-radius:8px;background:rgba(201,163,91,.12);color:var(--gold);font-size:13px";
      b.innerHTML="This page is from a previous version (v"+pageVer+"), but the running server is v"+h.version+". <a href=\"/install\" style=\"color:var(--gold);text-decoration:underline\">Reload this page</a> to see the current install.";
      const w=document.querySelector(".wrap"); if(w) w.insertBefore(b, w.children[1]||null);
    }
  }catch(e){ /* health not up yet; ignore */ }
}
checkFresh();
async function tick(){
  try{
    const p=await j("/install/progress");
    document.getElementById("fill").style.width=p.progress_pct+"%";
    { var _fw=document.getElementById("fillwrap"); if(_fw) _fw.setAttribute("aria-valuenow", String(Math.round(p.progress_pct||0))); }
    // Show the percentage as a number and a rough time-remaining, so the bar isn't a content-free
    // sliver. The percentage is the weighted-work figure from the backend; the ETA is deliberately
    // fuzzy ("about N minutes") because the model download dominates and varies.
    { var _pct=document.getElementById("pct"); if(_pct) _pct.textContent=Math.round(p.progress_pct||0)+"%"; }
    { var _eta=document.getElementById("eta");
      if(_eta){
        if(p.ready){ _eta.textContent="done"; }
        else if(typeof p.eta_seconds==="number" && p.eta_seconds>0){
          var m=Math.round(p.eta_seconds/60);
          _eta.textContent = m<=1 ? "about a minute left" : ("about "+m+" minutes left");
        } else if(p.current){
          // before an ETA is meaningful, name what's happening so there's always SOMETHING to read
          var lbl=(p.steps||[]).filter(function(s){return s.key===p.current;}).map(function(s){return s.label;})[0];
          _eta.textContent = lbl ? lbl+"…" : "";
        } else { _eta.textContent=""; }
      }
    }
    renderSteps(p);
    // The server is up (we just got progress), so show the pairing QR NOW: during the long model
    // download, not after it. The code only needs the server, not the brain.
    loadPairingCode();
    if(p.failed && !p.ready){ document.getElementById("sub").textContent="Something needs attention";
      document.getElementById("lede").innerHTML='An install step failed. Check the Terminal window for details, or re-run the installer. You can also <a href="/welcome" style="color:var(--gold)">open guided setup</a>.'; }
    if(p.ready){ await showDone(p); return; }
  }catch(e){ /* server may still be starting; keep polling */ }
  setTimeout(tick,1500);
}
tick();
</script>
</body></html>"""


def install_html() -> str:
    # Stamp the running version into the page so a STALE install screen (served by an old server, or
    # a cached copy) is visibly identifiable rather than a silent mystery. The footer shows it; a
    # data attribute lets the poller flag a mismatch.
    from. import __version__
    html = INSTALL_HTML.replace("__MONGOOSE_VERSION__", __version__)
    return html


# Pre-register externalised assets at import so /ui-asset works even if a browser requests an asset
# before its page renders (e.g. a service-worker precache) after a restart. Idempotent and guarded.
try:
    for _t in (DEVICE_LAUNCHPAD_HTML, PHONE_COCKPIT_HTML, COCKPIT_HTML):
        _externalize_assets(_profile_page(_t))  # rendered, so the hashes match what the pages serve
except Exception:
    pass
