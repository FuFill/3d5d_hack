/* Run with Node + Playwright. Exercises the shipped page against a local snapshot;
   no slicer mutations, API key or external model is involved. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const root = path.resolve(__dirname, "../..");
const messages = require(path.join(root, "AIAssistant/web/presets.js")).STEPresets;
const playwright = require(require.resolve("playwright", {paths: [root,
  path.resolve(path.dirname(process.execPath), "../node_modules")]}));

const reason = "hardware change required: material needs nozzle >= 0.6 mm, installed 0.4 mm";
const options = ["install the nozzle physically and select the matching nozzle in STE, then plan again",
  "choose a material compatible with the installed nozzle"];
assert.equal(messages.reason(reason, "ru"), "Нужно сопло от 0,6 мм; установлено 0,4 мм");
assert.equal(messages.reason(reason, "en"), reason);
assert.equal(messages.reason("profile fixes printing_mode=classic, plan requires cylindrical_full", "ru"),
  "Пресет рассчитан на 3D, а план требует 5D");
assert.match(messages.reason("new diagnostic", "ru"), /несовместим/);
for (const text of options) assert.doesNotMatch(messages.option(text, "ru"), /install|choose|nozzle/);

const events = [
  {seq: 1, type: "user", text: "Подбери другой материал"},
  {seq: 2, type: "tool", name: "plan_print", status: "ok", args: {}, result: {
    status: "infeasible", chosen_material: {label: "Fiberpart TPU G30"}, route: {mode: "classic"},
    base_preset: {status: "needs_input", reason, options}}},
  {seq: 3, type: "message", note: true, text: "Подбираю совместимый вариант"},
  {seq: 4, type: "tool", name: "plan_print", status: "ok", args: {}, result: {
    status: "candidate", chosen_material: {label: "PETG"}, route: {mode: "classic"},
    base_preset: {status: "selected", profile: "Fine", rejected: [{label: "Normal",
      reason: "not available for the installed nozzle/material on every enabled extruder"}]}}},
  {seq: 5, type: "question", status: "pending", questions: [{text:
    "План готов: Fine · PETG · 3D. Перейти к выполнению: применить план и продолжить подготовку печати?",
    options: ["Перейти к выполнению", "Остаться в планировании"], allow_custom: false}]}
];
let posted = null;
const output = path.resolve(process.argv[2] || path.join(root, ".runtime/preset_execution_ui"));
fs.mkdirSync(output, {recursive: true});
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  if (url.pathname === "/api/events") {
    // A genuine long poll; it is deliberately interrupted when the test browser closes.
    return;
  }
  if (url.pathname === "/api/answer") {
    let body = "";
    req.on("data", chunk => { body += chunk; });
    req.on("end", () => { posted = JSON.parse(body); res.writeHead(200, {"Content-Type": "application/json"}); res.end("{}"); });
    return;
  }
  if (url.pathname === "/api/state") {
    res.writeHead(200, {"Content-Type": "application/json; charset=utf-8"});
    res.end(JSON.stringify({settings: {language: "ru", theme: "light"}, busy: true, events, seq: 5}));
    return;
  }
  const files = {"/": ["index.html", "text/html"], "/app.js": ["app.js", "text/javascript"],
    "/presets.js": ["presets.js", "text/javascript"], "/style.css": ["style.css", "text/css"]};
  const file = files[url.pathname];
  if (!file) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, {"Content-Type": file[1] + "; charset=utf-8"});
  res.end(fs.readFileSync(path.join(root, "AIAssistant/web", file[0])));
});

(async function () {
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const executablePath = process.env.STE_BROWSER;
  const browser = await playwright.chromium.launch(executablePath ? {executablePath, headless: true} :
    {channel: "msedge", headless: true});
  const errors = [];
  try {
    const page = await browser.newPage({viewport: {width: 420, height: 900}});
    page.on("pageerror", error => errors.push(String(error)));
    await page.goto("http://127.0.0.1:" + server.address().port);
    const rejected = page.locator(".step > .preset-rejected").first();
    await rejected.waitFor({state: "visible"});
    const text = await rejected.innerText();
    assert.match(text, /Fiberpart TPU G30 · 3D/);
    assert.match(text, /Нужно сопло от 0,6 мм; установлено 0,4 мм/);
    assert.doesNotMatch(text, /Пресет не подобран|hardware|install|choose|невыполнимо/);
    const closedHeight = (await rejected.boundingBox()).height;
    assert.ok(closedHeight <= 84, "The rejected preset must stay compact: " + closedHeight);
    assert.equal(await rejected.locator(".preset-rejected-options").isVisible(), false);
    await rejected.locator("summary").click();
    assert.equal(await rejected.locator(".preset-rejected-options").isVisible(), true);
    assert.match(await rejected.innerText(), /Физически установите/);
    await rejected.locator("summary").click();
    await page.locator(".preset-rejections > summary").click();
    assert.match(await page.locator(".preset-rejections").innerText(), /Не подходит сопло или материал/);
    await page.locator(".preset-rejections > summary").click();
    assert.equal(await page.locator(".ask-custom").count(), 0);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(output, "chat_420.png"), fullPage: true});
    await page.setViewportSize({width: 360, height: 900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const narrowHeight = (await rejected.boundingBox()).height;
    assert.ok(narrowHeight <= 100, "Narrow rejected preset height: " + narrowHeight);
    await page.screenshot({path: path.join(output, "chat_360.png"), fullPage: true});
    const submitted = page.waitForResponse(response => response.url().endsWith("/api/answer"));
    await page.getByRole("button", {name: "Остаться в планировании", exact: true}).click();
    await submitted;
    assert.deepEqual(posted, {seq: 5, answers: [{text: "Остаться в планировании", choice: 1}]});
    assert.deepEqual(errors, []);
    const report = {status: "pass", external_requests: false, real_slicer_mutations: false,
      widths_px: [420, 360], rejected_height_px: [closedHeight, narrowHeight],
      rejected_reasons_russian: true, rejected_options_collapsed: true, confirmation_submission_verified: true,
      browser_errors: errors};
    fs.writeFileSync(path.join(output, "report.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); server.closeAllConnections(); server.close(); process.exitCode = 1; });
