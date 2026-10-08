const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");
const fail=(n,x)=>{console.error("FAIL: "+n,x===undefined?"":x);process.exit(1);};
global.esc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function run(resp) {
  const dom = new JSDOM(`<div id="qrbox"></div>`, { pretendToBeVisual: true });
  global.window = dom.window; global.document = dom.window.document;
  global.postAction = async () => ({ json: async () => resp });
  eval(fs.readFileSync(process.argv[2], "utf8"));
  await newDevice("primary_phone");
  return document.getElementById("qrbox").innerHTML;
}

(async () => {
  // The owner's exact case: DDNS used, AND a reachability warning present (both true simultaneously).
  let html = await run({ code: "ABC123", expires_in: 300, ddns_used: true,
    reachability_warning: "not_lan_reachable", pair_url: "https://yourname.duckdns.org/pair?code=ABC123",
    fingerprint: "aa:bb", profile: "primary_phone" });
  if (html.includes("scannable from anywhere")) fail("still claims scannable-from-anywhere while a reachability warning is present", html);
  if (!html.includes("won't be reachable until the server is listening")) fail("honest fallback message missing", html);
  if (!html.includes("not reachable from the network yet")) fail("the translated warning text should show", html);

  // The genuinely-working case: DDNS used, no warning: the checkmark should still appear.
  html = await run({ code: "ABC123", expires_in: 300, ddns_used: true,
    pair_url: "https://yourname.duckdns.org/pair?code=ABC123", fingerprint: "aa:bb", profile: "primary_phone" });
  if (!html.includes("scannable from anywhere")) fail("checkmark should appear when actually reachable", html);

  console.log("ALL 2 QR-HONESTY JOURNEYS PASS"); process.exit(0);
})().catch(e => { console.error("HARNESS ERROR:", e); process.exit(1); });
