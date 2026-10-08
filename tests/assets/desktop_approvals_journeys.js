const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");
const fail=(n,x)=>{console.error("FAIL: "+n,x===undefined?"":x);process.exit(1);};
global.esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const dom = new JSDOM(`<div class="row" id="r0"><button onclick="runApproval(0)">Approve</button></div>`, { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
window.__cockpitStale = false;
global.ensureNonce = async () => "nonce123";
let nextResp = {ok:true};
let loadCalls = 0;
global.loadApprovals = () => { loadCalls++; };
global.fetch = async (url, opts) => ({ ok: true, json: async () => nextResp, status: 200 });
window.__approvalActions = [{url:"/cockpit/knowledge/review/resolve", body:{review_id:"r1",action:"approve"}}];
eval(fs.readFileSync(process.argv[2], "utf8"));

(async () => {
  // J1: ok:false must NOT reload/clear, must alert with the reason
  nextResp = {ok:false, message:"That item was already resolved."};
  let alerted = null;
  global.alert = (m) => { alerted = m; };
  await runApproval(0);
  if (loadCalls !== 0) fail("loadApprovals called despite ok:false (false-green)", loadCalls);
  if (!alerted || !alerted.includes("already resolved")) fail("server reason not surfaced", alerted);
  const card = document.getElementById("r0");
  if (card.style.opacity !== "1") fail("card should be restored to full opacity on failure", card.style.opacity);

  // J2: ok:true must reload
  nextResp = {ok:true};
  alerted = null;
  await runApproval(0);
  if (loadCalls !== 1) fail("loadApprovals NOT called on success", loadCalls);
  if (alerted) fail("no alert should fire on success", alerted);

  console.log("ALL 2 DESKTOP APPROVAL HONESTY JOURNEYS PASS");
  process.exit(0);
})().catch(e => { console.error("HARNESS ERROR:", e); process.exit(1); });
