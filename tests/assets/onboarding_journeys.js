// Guest onboarding journeys: the REAL rendered /guest/invite page (produced by
// guest_invite_html at test time) running its own scripts in a real DOM, network mocked.
const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const fs = require("fs");

const fail = (n, x) => { console.error("FAIL: " + n, x === undefined ? "" : x); process.exit(1); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

function makePage(file, fetchImpl, preToken) {
  const dom = new JSDOM(fs.readFileSync(file, "utf8"), {
    runScripts: "dangerously", url: "https://example.duckdns.org/guest/invite/whatever",
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = fetchImpl;
      window.confirm = () => true;
      if (preToken) window.localStorage.setItem("mongoose_device_token", preToken);
    },
  });
  return dom;
}

(async () => {
  const landing = process.argv[2], revoked = process.argv[3], loungeBlock = process.argv[4];

  // J1: fresh invite: intro reveals, Ready prefills BOTH names with the owner's suggestion.
  let fetches = [];
  let dom = makePage(landing, async (url, opts) => {
    fetches.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    return { json: async () => ({ ok: true, device_token: "tok-jo-999", public_name: "Jo" }) };
  });
  let d = dom.window.document;
  await sleep(3300); // the staged reveal animation timers
  if (!d.getElementById("welcome").classList.contains("show")) fail("intro never revealed");
  d.getElementById("readyBtn").click();
  if (d.getElementById("mongooseName").value !== "Jo") fail("suggested name not prefilled (mongoose_name)");
  if (d.getElementById("publicName").value !== "Jo") fail("suggested name not prefilled (public_name)");

  // J2: missing fun fact: inline message, NO network call, nothing stored.
  d.getElementById("guestForm").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await sleep(30);
  if (!d.getElementById("formMsg").textContent.includes("fun fact")) fail("missing-field message absent", d.getElementById("formMsg").textContent);
  if (fetches.length !== 0) fail("validation failure must not hit the network");
  if (dom.window.localStorage.getItem("mongoose_device_token")) fail("nothing may be stored before a successful claim");

  // J3: happy claim: POSTs the invite token + form, stores the guest pass, shows the way in.
  d.getElementById("funFact").value = "loves kayaking";
  d.getElementById("guestForm").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await sleep(50);
  if (fetches.length !== 1) fail("claim should be exactly one POST", fetches.length);
  if (!fetches[0].url.includes("/guest/invite/test-invite-tok-123/claim")) fail("claim URL must carry the INVITE_TOKEN constant", fetches[0].url);
  if (fetches[0].body.fun_fact !== "loves kayaking" || fetches[0].body.mongoose_name !== "Jo") fail("claim body wrong", fetches[0].body);
  if (dom.window.localStorage.getItem("mongoose_device_token") !== "tok-jo-999") fail("guest token not stored");
  if (dom.window.localStorage.getItem("mongoose_device_profile") !== "trusted_guest") fail("profile marker not stored");
  if (!d.getElementById("guestForm").innerHTML.includes("/device/launchpad")) fail("no way into the launchpad after claim");

  // J4: THE SEAM: the launchpad's own token() reader, executed against this same storage,
  // must return exactly what the landing page stored.
  const tokenReader = fs.readFileSync(loungeBlock, "utf8");
  const readerSrc = tokenReader.match(/function token\(\)\{[^}]*\}/)[0];
  const got = new Function("localStorage", readerSrc + "; return token();")(dom.window.localStorage);
  if (got !== "tok-jo-999") fail("SEAM BROKEN: launchpad token() reads a different key", got);

  // J5: server rejection (already claimed): message shown, button re-enabled, nothing stored.
  dom = makePage(landing, async () => ({ json: async () => ({ ok: false, error: "already_claimed", message: "That invite has already been used." }) }));
  d = dom.window.document;
  d.getElementById("readyBtn").click();
  d.getElementById("funFact").value = "x";
  d.getElementById("mongooseName").value = "Jo"; d.getElementById("publicName").value = "Jo";
  d.getElementById("guestForm").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await sleep(50);
  if (!d.getElementById("formMsg").textContent.includes("already been used")) fail("server rejection message absent");
  if (d.getElementById("finishBtn").disabled) fail("button must re-enable after rejection");
  if (dom.window.localStorage.getItem("mongoose_device_token")) fail("rejection must store nothing");

  // J6: hostile server message stays inert text.
  dom = makePage(landing, async () => ({ json: async () => ({ ok: false, message: "<img src=x onerror=window.__pwned=1>" }) }));
  d = dom.window.document;
  d.getElementById("readyBtn").click();
  ["mongooseName", "publicName", "funFact"].forEach(id => d.getElementById(id).value = "x");
  d.getElementById("guestForm").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await sleep(50);
  if (dom.window.__pwned) fail("XSS executed via server message");
  if (d.getElementById("formMsg").querySelector("img")) fail("hostile markup became an element");

  // J7: network down: honest failure, button re-enabled.
  dom = makePage(landing, async () => { throw new Error("net down"); });
  d = dom.window.document;
  d.getElementById("readyBtn").click();
  ["mongooseName", "publicName", "funFact"].forEach(id => d.getElementById(id).value = "x");
  d.getElementById("guestForm").dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await sleep(50);
  if (!d.getElementById("formMsg").textContent.includes("Could not reach Mongoose")) fail("network failure was silent");
  if (d.getElementById("finishBtn").disabled) fail("button must re-enable after network failure");

  // J8: revoked invite: form and intro suppressed, honest explanation shown.
  dom = makePage(revoked, async () => fail("revoked page must not call the network"));
  d = dom.window.document;
  await sleep(30);
  if (d.getElementById("invalid").style.display !== "block") fail("revoked invite must show the invalid panel");
  if (!d.getElementById("invalidMsg").textContent.includes("revoked")) fail("revoked message absent");

  // J9: already-in short-circuit: a browser holding a token gets the door, not the form.
  dom = makePage(landing, async () => ({ json: async () => ({}) }), "existing-tok");
  d = dom.window.document;
  await sleep(30);
  if (!d.getElementById("intro").innerHTML.includes("already in")) fail("already-in short-circuit missing");
  if (!d.getElementById("intro").innerHTML.includes("/device/launchpad")) fail("already-in must link to the launchpad");

  console.log("ALL 9 ONBOARDING JOURNEYS PASS");
  process.exit(0);
})().catch(e => { console.error("HARNESS ERROR:", e); process.exit(1); });
