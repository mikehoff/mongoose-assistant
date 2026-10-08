const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");
const fail=(n,x)=>{console.error("FAIL: "+n,x===undefined?"":x);process.exit(1);};
global.esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function run(readinessResp) {
  const dom = new JSDOM(`<div id="remoteChase"></div>`, { pretendToBeVisual: true });
  global.window = dom.window; global.document = dom.window.document;
  global.j = async () => readinessResp;
  eval(fs.readFileSync(process.argv[2], "utf8"));
  await chaseRemoteAccess();
  return document.getElementById("remoteChase").innerHTML;
}

(async () => {
  // The owner's exact scenario, updated to the current (more correct) codes: DDNS/HTTPS fine, only
  // the server-not-network-bound gap remains (the old, architecturally-wrong remote_disabled
  // code was removed entirely, see test_dispatch_reconciliation.py).
  let html = await run({ ok: false, problems: [
    { code: "server_not_network_bound", message: "Dynamic DNS is configured, but this Mongoose server is still bound to this Mac only.", action: "Use Enable LAN access, or Repair / restart, before creating a guest link." }
  ]});
  if (html.includes("Dynamic DNS + HTTPS are what let you")) fail("still shows the generic DDNS/HTTPS blame when only the network-bound gap fired", html);
  if (!html.includes("already set up correctly")) fail("does not acknowledge DDNS/HTTPS are already fine", html);
  if (!html.includes("Turn on reaching Mongoose")) fail("server_not_network_bound row itself missing", html);

  // Genuine DDNS-missing case: the original generic wording IS correct here.
  html = await run({ ok: false, problems: [
    { code: "no_dynamic_dns", message: "Set up Dynamic DNS first.", action: "x" }
  ]});
  if (!html.includes("Dynamic DNS + HTTPS are what let you")) fail("genuine DDNS-missing case lost its correct generic wording", html);

  // Fully ready: card renders empty.
  html = await run({ ok: true });
  if (html !== "") fail("ready state should render nothing", html);

  console.log("ALL 3 REMOTE-ACCESS WORDING JOURNEYS PASS");
})().catch(e => { console.error("HARNESS ERROR:", e); process.exit(1); });
