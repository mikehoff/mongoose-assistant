const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");
const fail=(n,x)=>{console.error("FAIL: "+n,x===undefined?"":x);process.exit(1);};
const dom = new JSDOM(`<div id="approvals-root"></div>`, { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
global.esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
global.failCard = (e,w) => "fail:"+w;
let nextResp = {ok:true};
let resolved = false;  // simulates the draft actually being gone server-side after success
global.api = async (path, opts) => {
  if (path === "/cockpit/approvals") return { approvals: resolved ? [] : [
    { kind:"whatsapp_draft", label:"Jo", body:"hi", actions:[{label:"Approve & send",style:"go",url:"/whatsapp/outbox/d1/approve"}] }
  ]};
  if (nextResp.ok) resolved = true;
  return nextResp;
};
eval(fs.readFileSync(process.argv[2], "utf8"));
(async () => {
  await loadApprovals();
  if (!document.getElementById("ap-0")) fail("card not rendered from actions[]");

  nextResp = {ok:false, error:"Draft already sent"};
  let alerted=null; global.alert = m => alerted = m;
  await runPhoneApproval(0);
  if (!document.getElementById("ap-0")) fail("FALSE-GREEN: card removed on ok:false");
  if (!alerted || !alerted.includes("already sent")) fail("server reason not surfaced", alerted);

  nextResp = {ok:true};
  await runPhoneApproval(0);
  if (document.getElementById("ap-0")) fail("card should be removed on success");

  console.log("ALL 3 PHONE APPROVAL HONESTY JOURNEYS PASS");
  process.exit(0);
})().catch(e=>{console.error("HARNESS ERROR:",e);process.exit(1);});
