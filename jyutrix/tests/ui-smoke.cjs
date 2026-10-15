/* DOM integration checks. Not a substitute for visual browser QA. */
const assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path");
const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");
const root = path.resolve(__dirname, ".."),
  base = process.env.UI_TEST_URL || "http://127.0.0.1:8001";
const html = fs.readFileSync(path.join(root, "frontend/index.html"), "utf8");
const dom = new JSDOM(html, {
  url: base,
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const w = dom.window;
let cookie = "",
  errors = [];
const apiCalls = [];
w.fetch = async (url, options = {}) => {
  const headers = { ...options.headers };
  if (cookie) headers.Cookie = cookie;
  const r = await fetch(new URL(url, base), { ...options, headers });
  const set = r.headers.get("set-cookie");
  if (set) cookie = set.split(";")[0];
  apiCalls.push([url, r.status]);
  return r;
};
w.confirm = () => true;
w.alert = () => {};
w.HTMLDialogElement.prototype.showModal = function () {
  this.open = true;
};
w.HTMLDialogElement.prototype.close = function () {
  this.open = false;
};
w.addEventListener("error", (e) => errors.push(e.message));
w.console.error = (...a) => errors.push(a.join(" "));
w.eval(fs.readFileSync(path.join(root, "frontend/app.js"), "utf8"));
const $ = (s) => w.document.querySelector(s);
async function waitFor(fn, label) {
  for (let i = 0; i < 200; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 20));
  }
  throw Error("Timed out: " + label + " / " + errors.join("; "));
}
function click(selector) {
  assert($(selector), "Missing " + selector);
  $(selector).click();
}
function submit(form) {
  $(form).dispatchEvent(
    new w.Event("submit", { bubbles: true, cancelable: true }),
  );
}
(async () => {
  await waitFor(() => !$("#demo-login").hidden, "demo login");
  $("#login-form [name=username]").value = "admin";
  $("#login-form [name=password]").value = "ui-test-password-12345";
  submit("#login-form");
  await waitFor(
    () => !$("#workspace").hidden && $("#content .hero"),
    "login/overview",
  );
  assert($("#content").textContent.includes("Demo Dental Clinic"));
  click("nav [data-view=demo]");
  assert($("#demo-form"));
  for (const text of ["BOOK", "1", "YES"]) {
    $("#demo-form [name=text]").value = text;
    submit("#demo-form");
    await waitFor(
      () => $("#demo-chat").textContent.startsWith("You: " + text),
      "demo " + text,
    );
  }
  assert($("#demo-chat").textContent.includes("Confirmed:"));
  click("nav [data-view=appointments]");
  click("#refresh");
  await waitFor(() => $("#appointment-rows tr"), "appointments");
  assert($("#appointment-rows").textContent.includes("Demo patient"));
  click("nav [data-view=settings]");
  assert($("#settings-form"));
  $("#settings-form [name=name]").value =
    'Demo <img src=x onerror="window.xss=true"> Dental';
  submit("#settings-form");
  await waitFor(
    () => $("#toast").textContent === "Clinic saved",
    "save settings",
  );
  assert.equal(w.xss, undefined);
  assert.equal($("#clinic-select img"), null);
  click("nav [data-view=overview]");
  assert.equal($("#content .hero img"), null);
  // Patient-controlled text must render as text in schedule and conversation views.
  click("nav [data-view=demo]");
  $("#demo-form [name=contact]").value = "919999000002";
  $("#demo-form [name=name]").value = '<img src=x onerror="window.xss=true">';
  for (const text of ["BOOK", "1", "YES"]) {
    $("#demo-form [name=text]").value = text;
    submit("#demo-form");
    await waitFor(
      () => $("#demo-chat").textContent.startsWith("You: " + text),
      "second demo " + text,
    );
  }
  click("nav [data-view=appointments]");
  click("#refresh");
  await waitFor(
    () => $("#appointment-rows")?.textContent.includes("<img"),
    "patient name",
  );
  assert.equal($("#appointment-rows img"), null);
  click("nav [data-view=inbox]");
  await waitFor(() => $("#chat-stream"), "inbox");
  assert.equal($("#thread-list img"), null);
  click("#conversation [data-action=takeover]");
  await waitFor(() => $("#conversation [data-action=resume]"), "takeover");
  $("#reply-text").value = "Reception here";
  submit("#reply-form");
  await waitFor(
    () => $("#chat-stream")?.textContent.includes("Reception here"),
    "staff reply",
  );
  click("nav [data-view=delivery]");
  assert($("#content").textContent.includes("Every message, accounted for"));
  click("#logout");
  await waitFor(() => !$("#login").hidden, "logout");
  assert.equal(errors.length, 0, errors.join("; "));
  console.log(
    "DOM smoke checks passed: login, overview, confirmed booking, settings, XSS escaping, inbox takeover, staff reply, delivery view and logout.",
  );
  console.log("HTTP requests:", apiCalls.length);
  dom.window.close();
})().catch((e) => {
  console.error(e);
  dom.window.close();
  process.exitCode = 1;
});
