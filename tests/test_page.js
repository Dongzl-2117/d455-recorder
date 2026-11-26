// The web page in a real browser, against a server with the simulated camera.
//   npm install --no-save playwright && npx playwright install chromium && node tests/test_page.js
// Records through the page, plays back, imports a file and a folder, deletes with the confirmation,
// checks the self-check panel and closes the program from the page. Fails on any page error.
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const SHOTS = path.join(__dirname, "screenshots");
const PORT = 18455 + Math.floor(Math.random() * 500);
const BASE = `http://127.0.0.1:${PORT}`;
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const failures = [];
const check = (ok, what, detail = "") => { console.log((ok ? "  ok    " : "  FAIL  ") + what + (ok || !detail ? "" : `  (${detail})`)); if (!ok) failures.push(what); };

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "studio-page-"));
  const server = spawn(python, [path.join(ROOT, "d455_studio.py"), "--simulate", "--no-browser", "--port", String(PORT),
    "--sessions", path.join(work, "sessions"), "--settings", path.join(work, "settings.json"), "--log-dir", path.join(work, "logs")],
  { cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PYTHONUTF8: "1" } });
  let log = "";
  server.stdout.on("data", (chunk) => { log += chunk; });
  server.stderr.on("data", (chunk) => { log += chunk; });
  let exited = null;
  server.on("exit", (code) => { exited = code; });

  for (let i = 0; i < 120; i++) {
    try { if ((await fetch(BASE + "/api/ping")).ok) break; } catch { /* not up yet */ }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (error) => errors.push("pageerror: " + error.message));
  page.on("console", (message) => { if (message.type() === "error" && !/Failed to load resource/.test(message.text())) errors.push("console: " + message.text()); });
  try {
    await page.goto(BASE + "/");
    await page.waitForFunction(() => !document.querySelector("#rec-btn").disabled, null, { timeout: 40000 });
    check(true, "camera streams on the live page");
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(SHOTS, "live.png") });

    console.log("record through the page");
    await page.fill("#scene-location", "page test");
    await page.click("#rec-btn");
    await page.waitForFunction(() => document.querySelector("#rec-btn").classList.contains("on"), null, { timeout: 20000 });
    await page.waitForTimeout(3500);
    await page.click("#rec-btn");
    await page.waitForSelector(".toast .msg:has-text('已保存')", { timeout: 60000 });
    check(true, "recording saved (toast)");

    console.log("playback");
    await page.click("button[data-tab=library]");
    await page.waitForSelector("#session-list .card", { timeout: 20000 });
    await page.click("#session-list .card");
    await page.waitForSelector("#transport:not([hidden])", { timeout: 20000 });
    await page.click("#play-btn");
    await page.waitForTimeout(1500);
    await page.click("#play-btn");
    const frame = await page.textContent("#t-now");
    check(frame !== "00:00.000", "playback advances", frame);
    await page.screenshot({ path: path.join(SHOTS, "player.png") });

    console.log("import a file and a folder");
    const sessions = path.join(work, "sessions");
    const first = fs.readdirSync(sessions).find((name) => !name.startsWith("."));
    const mcap = fs.readdirSync(path.join(sessions, first, "recordings")).find((name) => name.endsWith(".mcap"));
    const elsewhere = path.join(work, "elsewhere", "copied session");
    fs.cpSync(path.join(sessions, first), elsewhere, { recursive: true });
    fs.copyFileSync(path.join(sessions, first, "recordings", mcap), path.join(work, "elsewhere", "loose.mcap"));
    await page.setInputFiles("#import-files", path.join(work, "elsewhere", "loose.mcap"));
    await page.waitForSelector("#modal:not([hidden])");
    await page.screenshot({ path: path.join(SHOTS, "import_confirm.png") });
    await page.click("#modal-actions .btn.primary");
    await page.waitForSelector("#session-list .card[data-id='loose.mcap']", { timeout: 30000 });
    check(true, "loose .mcap imported and listed");
    try {
      await page.setInputFiles("#import-folder", elsewhere);
      await page.waitForSelector("#modal:not([hidden])", { timeout: 5000 });
      await page.click("#modal-actions .btn.primary");
      await page.waitForSelector("#session-list .card[data-id='copied session']", { timeout: 30000 });
      check(true, "session folder imported and listed");
    } catch (error) {
      check(false, "session folder import", error.message.split("\n")[0]);
    }

    console.log("delete two with the confirmation");
    await page.click("#select-btn");
    await page.click("#session-list .card[data-id='loose.mcap']");
    await page.click("#session-list .card[data-id='copied session']");
    await page.screenshot({ path: path.join(SHOTS, "select.png") });
    await page.click("#select-delete");
    await page.waitForSelector("#modal:not([hidden])");
    await page.waitForTimeout(300);
    const focused = await page.evaluate(() => document.activeElement && document.activeElement.textContent);
    check(focused === "取消", "the safe button has the focus in the delete dialog", focused);
    await page.screenshot({ path: path.join(SHOTS, "delete_confirm.png") });
    await page.click("#modal-actions .btn.danger");
    await page.waitForSelector(".toast .msg:has-text('已删除 2 个')", { timeout: 20000 });
    await page.waitForFunction(() => document.querySelectorAll("#session-list .card").length === 1, null, { timeout: 10000 }).catch(() => {});
    const left = await page.$$eval("#session-list .card", (cards) => cards.map((card) => card.dataset.id));
    check(left.length === 1 && left[0] === first, "only the recording is left", left.join(", "));
    check(!fs.existsSync(path.join(sessions, "loose.mcap")) && !fs.existsSync(path.join(sessions, "copied session")), "files removed");

    console.log("narrow window");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(500);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(overflow <= 0, "no horizontal scroll at 390 px", overflow);
    await page.screenshot({ path: path.join(SHOTS, "narrow_library.png") });
    await page.setViewportSize({ width: 1440, height: 900 });

    console.log("settings, self check, close");
    await page.click("button[data-tab=live]");
    await page.click("#open-settings");
    await page.waitForSelector("#doctor .check", { timeout: 20000 });
    const checks = await page.$$eval("#doctor .check .what", (items) => items.map((item) => item.textContent));
    check(checks.length >= 3, "self check lists findings", checks.join(" | "));
    await page.$eval("#settings .body", (element) => { element.scrollTop = element.scrollHeight; });
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(SHOTS, "settings_doctor.png") });
    await page.click("#service-quit");
    await page.waitForSelector("#modal:not([hidden])");
    await page.click("#modal-actions .btn.primary");
    await page.waitForSelector("#closed:not([hidden])", { timeout: 10000 });
    await page.screenshot({ path: path.join(SHOTS, "closed.png") });
    for (let i = 0; i < 40 && exited === null; i++) await new Promise((resolve) => setTimeout(resolve, 250));
    check(exited === 0, "the program exits when closed from the page", exited);
  } catch (error) {
    check(false, "page flow", error.message.split("\n")[0]);
    await page.screenshot({ path: path.join(SHOTS, "failure.png") }).catch(() => {});
  }
  check(errors.length === 0, "no page errors", errors.join(" | "));
  await browser.close();
  if (exited === null) server.kill();
  if (failures.length) console.log("--- server log\n" + log.split("\n").slice(-40).join("\n"));
  console.log(failures.length ? `\nFAILED: ${failures.join(", ")}` : "\nALL PASSED");
  process.exit(failures.length ? 1 : 0);
})();
