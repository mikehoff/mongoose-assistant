const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");
const fail=(n,x)=>{console.error("FAIL: "+n,x===undefined?"":x);process.exit(1);};

const dom = new JSDOM(`<div id="setup" class="pane active"></div><div id="overview" class="pane"></div>`, { pretendToBeVisual: true });
global.window = dom.window; global.document = dom.window.document;
let activateCalls = 0;
global.activateTab = () => { activateCalls++; };
global.wizExpand = () => {};
eval(fs.readFileSync(process.argv[2], "utf8"));

// Already on Setup (class="active" present): must NOT call activateTab (no destructive reload).
gotoDdnsSetup();
if (activateCalls !== 0) fail("called activateTab while already on setup -- destructive reload not avoided", activateCalls);

// On a DIFFERENT tab: must still switch normally.
document.getElementById("setup").classList.remove("active");
gotoDdnsSetup();
if (activateCalls !== 1) fail("did not switch tabs when NOT already on setup", activateCalls);

console.log("ALL 2 GOTO-DDNS-SETUP JOURNEYS PASS");
process.exit(0);
