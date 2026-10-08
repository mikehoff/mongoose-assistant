const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");
const fail=(n,x)=>{console.error("FAIL: "+n,x===undefined?"":x);process.exit(1);};
const dom = new JSDOM(``, { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
window.__cockpitStale = false;
global.ensureNonce = async () => "n";
global.confirm = () => true;
let nextResp = {ok:true};
let loadCalls = 0;
global.loadDevices = () => loadCalls++;
global.fetch = async () => ({ ok:true, json: async () => nextResp, status:200 });
eval(fs.readFileSync(process.argv[2], "utf8"));
(async () => {
  nextResp = {ok:false, message:"Device not found."};
  let alerted=null; global.alert = m => alerted = m;
  await revokeDevice("dev1");
  if (loadCalls !== 0) fail("loadDevices called despite ok:false");
  if (!alerted || !alerted.includes("did NOT complete")) fail("failure not surfaced clearly", alerted);
  if (!alerted.includes("may still have access")) fail("security-relevant caveat missing", alerted);

  nextResp = {ok:true};
  await revokeDevice("dev1");
  if (loadCalls !== 1) fail("loadDevices not called on real success");
  console.log("ALL 2 REVOKE HONESTY JOURNEYS PASS");
  process.exit(0);
})().catch(e=>{console.error("HARNESS ERROR:",e);process.exit(1);});
