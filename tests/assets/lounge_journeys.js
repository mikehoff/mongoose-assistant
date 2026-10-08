// Lounge user-journey harness. Loads the REAL shipped JS block (extracted verbatim from
// cockpit.py at test time so it can never drift from production) into a real DOM and walks the
// journeys a person actually takes. Exits nonzero with a named failure on the first broken
// expectation.
const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");

const dom = new JSDOM(`<!DOCTYPE html><html><body>
  <div id="tabs"><button class="tab on" id="tab-talk"></button><button class="tab" id="tab-irc"></button></div>
  <div class="pane on" id="talk-pane"></div>
  <div class="pane" id="irc-pane">
    <div id="irc-log" style="height:100px;overflow-y:auto"></div>
    <input id="irc-input"><button id="irc-send"></button>
  </div></body></html>`, { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;

// scripted api double: a queue of canned responses per endpoint, recording every call
const calls = [];
let db = [];            // server-side room state: [{id,sender,text,created_at,mongoose}]
let nextId = 1;
let whoami = { device: { display_name: "Alex", name: "Alex's phone", profile: "primary_phone" } };
global.api = async (path, opts) => {
  calls.push(path);
  if (path.startsWith("/device/whoami")) return { device: whoami.device };
  if (path.startsWith("/device/irc/history")) {
    const since = parseInt(new URL("http://x" + path).searchParams.get("since_id") || "0", 10);
    return { ok: true, messages: db.filter(m => m.id > since) };
  }
  if (path === "/device/irc/send") {
    const text = JSON.parse(opts.body).text;
    db.push({ id: nextId++, sender: "Alex", text, created_at: new Date().toISOString(), mongoose: false });
    if (/mongoose/i.test(text) || text.trim().endsWith("?")) {
      db.push({ id: nextId++, sender: "mongoose", text: "Hey Alex!", created_at: new Date().toISOString(), mongoose: true });
      return { ok: true, sender: "Alex", replied: true, reply: "Hey Alex!" };
    }
    return { ok: true, sender: "Alex", replied: false };
  }
  throw new Error("unexpected api call " + path);
};

// load the REAL shipped code
eval(fs.readFileSync(process.argv[2], "utf8"));

const log = () => document.getElementById("irc-log");
const lines = () => [...log().children].map(el => el.textContent);
const fail = (name, extra) => { console.error("FAIL: " + name, extra || ""); process.exit(1); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  // JOURNEY 1: a guest opens a quiet, empty room. Expect the friendly hint, not a blank void.
  document.getElementById("irc-pane").classList.add("on");
  await ircOpen();
  if (!lines().some(l => l.includes("the room is quiet"))) fail("empty-room hint missing", lines());

  // JOURNEY 2: mongoose's starter is already in the room when someone opens it later.
  db.push({ id: nextId++, sender: "mongoose", text: "trivia time: octopuses have three hearts.", created_at: new Date().toISOString(), mongoose: true });
  await ircPoll();
  if (!lines().some(l => l.includes("<mongoose>") && l.includes("octopuses"))) fail("starter not rendered", lines());

  // JOURNEY 3: send a plain line: it appears, attributed, exactly once; mongoose stays quiet.
  document.getElementById("irc-input").value = "made it home";
  await ircSend();
  const mine = lines().filter(l => l.includes("<Alex>") && l.includes("made it home"));
  if (mine.length !== 1) fail("own message should render exactly once", lines());
  if (lines().filter(l => l.includes("Hey Alex!")).length !== 0) fail("mongoose replied uninvited");

  // JOURNEY 4: ask a question: mongoose's reply arrives and renders exactly once.
  document.getElementById("irc-input").value = "anyone around?";
  await ircSend();
  if (lines().filter(l => l.includes("Hey Alex!")).length !== 1) fail("reply should render exactly once", lines());

  // JOURNEY 5 (the race): interval poll and send-triggered poll in flight TOGETHER must not
  // duplicate lines. Fire both concurrently against a room that just gained a message.
  db.push({ id: nextId++, sender: "Jo", text: "hi from the sofa", created_at: new Date().toISOString(), mongoose: false });
  await Promise.all([ircPoll(), ircPoll()]);
  const jo = lines().filter(l => l.includes("hi from the sofa"));
  if (jo.length !== 1) fail("RACE: concurrent polls duplicated a line", jo.length);

  // JOURNEY 6: hostile content stays inert text (no element injection via sender or body).
  db.push({ id: nextId++, sender: "<img src=x onerror=alert(1)>", text: "<script>window.__pwned=1</script>", created_at: new Date().toISOString(), mongoose: false });
  await ircPoll();
  if (window.__pwned) fail("XSS executed");
  if (log().querySelector("img,script")) fail("hostile markup became real elements");
  if (!lines().some(l => l.includes("<script>window.__pwned=1</script>"))) fail("hostile text should still be VISIBLE as text");

  // JOURNEY 7: leaving the tab stops the poll timer; returning resumes without duplication.
  ircClose();
  if (typeof _ircTimer !== "undefined" && _ircTimer !== null) fail("timer must be cleared on tab leave");
  const before = lines().length;
  await ircOpen(); // returning: log already populated, must NOT re-render history
  if (lines().length !== before) fail("returning to the tab duplicated history", [before, lines().length]);

  // JOURNEY 8: send failure surfaces to the person instead of silently eating the message.
  const realApi = global.api;
  global.api = async (path, opts) => { if (path === "/device/irc/send") throw new Error("wifi died"); return realApi(path, opts); };
  document.getElementById("irc-input").value = "does this get lost?";
  await ircSend();
  if (!lines().some(l => l.includes("was not sent") || l.includes("could not send"))) fail("send failure was silent", lines());
  global.api = realApi;

  // JOURNEY 9 (guest identity): a claimed guest (whoami: trusted_guest "Jo") opens the room:
  // her own lines render with her name in the self colour, others dim, mongoose green.
  whoami = { device: { display_name: "Jo", name: "guest_web abc", profile: "trusted_guest" } };
  const dom2 = new JSDOM(`<div class="pane on" id="irc-pane"><div id="irc-log" style="height:100px;overflow-y:auto"></div><input id="irc-input"><button id="irc-send"></button></div>`, { pretendToBeVisual: true });
  global.document = dom2.window.document; global.window = dom2.window;
  _ircSince = 0; _ircTimer = null; _ircMyName = ""; _ircBusy = false;
  db = [
    { id: nextId++, sender: "Alex", text: "welcome Jo", created_at: new Date().toISOString(), mongoose: false },
    { id: nextId++, sender: "Jo", text: "hi all!", created_at: new Date().toISOString(), mongoose: false },
    { id: nextId++, sender: "mongoose", text: "Hey Jo, welcome!", created_at: new Date().toISOString(), mongoose: true },
  ];
  await ircOpen();
  const rows = [...document.getElementById("irc-log").children];
  const nick = r => r.children[1];
  const camRow = rows.find(r => r.textContent.includes("<Jo>"));
  const alexRow = rows.find(r => r.textContent.includes("<Alex>"));
  const mgRow = rows.find(r => r.textContent.includes("<mongoose>"));
  if (!camRow || !alexRow || !mgRow) fail("guest room missing participants", rows.map(r=>r.textContent));
  if (!nick(camRow).style.color.includes("--gold")) fail("guest's OWN name must be self-coloured", nick(camRow).style.color);
  if (!nick(alexRow).style.color.includes("--dim")) fail("other humans must be dim", nick(alexRow).style.color);
  if (!nick(mgRow).style.color.includes("--ok")) fail("mongoose must be green", nick(mgRow).style.color);

  console.log("ALL 9 LOUNGE JOURNEYS PASS");
  process.exit(0);
})().catch(e => { console.error("HARNESS ERROR:", e); process.exit(1); });
