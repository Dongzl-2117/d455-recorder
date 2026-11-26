/* D455 Studio front end. Plain browser JavaScript, no build step, no network access beyond this server. */
"use strict";

// ---------------------------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------------------------
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const clamp = (value, low, high) => Math.min(Math.max(value, low), high);
const esc = (text) => String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ICONS = {
  play: '<path d="M8 5.5v13l11-6.5z" class="fillpath"/>',
  pause: '<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>',
  back: '<path d="M7 6v12M18 6l-8 6 8 6z"/>',
  fwd: '<path d="M17 6v12M6 6l8 6-8 6z"/>',
  sliders: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  folder: '<path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8h.01"/>',
  layout: '<rect x="3.5" y="5" width="10" height="14" rx="2"/><rect x="15.5" y="5" width="5" height="6" rx="1.5"/><rect x="15.5" y="13" width="5" height="6" rx="1.5"/>',
  columns: '<rect x="3" y="6" width="5" height="12" rx="1.5"/><rect x="9.5" y="6" width="5" height="12" rx="1.5"/><rect x="16" y="6" width="5" height="12" rx="1.5"/>',
  expand: '<path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/>',
  shrink: '<path d="M4 9h5V4M20 9h-5V4M4 15h5v5M20 15h-5v5"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4"/>',
  camera: '<rect x="3" y="7" width="18" height="12" rx="3"/><circle cx="8.5" cy="13" r="2.4"/><circle cx="15.5" cy="13" r="2.4"/><path d="M8 7l1-2h6l1 2"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
  plug: '<path d="M9 3v5M15 3v5M7 8h10v3a5 5 0 0 1-10 0zM12 16v5"/>',
  trash: '<path d="M4.5 7h15M10 4h4M6.5 7l.8 11.2a2 2 0 0 0 2 1.8h5.4a2 2 0 0 0 2-1.8L17.5 7M10 11v5M14 11v5"/>',
  upload: '<path d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 14v3.5A2.5 2.5 0 0 0 7.5 20h9a2.5 2.5 0 0 0 2.5-2.5V14"/>',
  check: '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M8.5 12.2l2.4 2.4 4.8-5"/>',
  power: '<path d="M12 3.5v8M7.2 6.4a7.5 7.5 0 1 0 9.6 0"/>',
  chevron: '<path d="M14.5 5.5 8 12l6.5 6.5"/>',
  ok: '<circle cx="12" cy="12" r="8.5"/><path d="M8.3 12.3l2.5 2.5 4.9-5.1"/>',
};
function icon(name, extra = "") {
  const body = ICONS[name] || "";
  const filled = name === "play";
  return `<svg class="icon ${filled ? "fill" : ""} ${extra}" viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
}

const LABELS = { color: "彩色", depth: "深度", ir_left: "红外", ir_right: "右红外" };
const ORDER = ["color", "depth", "ir_left", "ir_right"];
const SWATCH = { color: "#6d9bff", depth: "#ff9f43", ir_left: "#b7bcc6", ir_right: "#b7bcc6" };
const GRADIENTS = {
  turbo: ["#30123b", "#4662d7", "#36aaf9", "#1ae4b6", "#72fe5e", "#c8ef34", "#faba39", "#f66b19", "#ca2a04", "#7a0403"],
  viridis: ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
  inferno: ["#000004", "#420a68", "#932667", "#dd513a", "#fca50a", "#fcffa4"],
  jet: ["#00007f", "#0000ff", "#007fff", "#00ffff", "#7fff7f", "#ffff00", "#ff7f00", "#ff0000", "#7f0000"],
  gray: ["#ffffff", "#2a2a2a"],
};

const store = {
  get(key, fallback) {
    try { const value = localStorage.getItem("d455studio." + key); return value === null ? fallback : JSON.parse(value); } catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem("d455studio." + key, JSON.stringify(value)); } catch { /* private mode */ } },
};

const api = {
  async get(path) {
    const response = await fetch(path, { cache: "no-store" });
    if (!response.ok) throw await api.error(response);
    return response.json();
  },
  async post(path, body) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (!response.ok) throw await api.error(response);
    return response.json();
  },
  async error(response) {
    let message = response.statusText || "HTTP " + response.status;
    try { message = (await response.json()).error || message; } catch { /* not JSON */ }
    return new Error(message);
  },
};

function pad(value, width = 2) { return String(Math.floor(value)).padStart(width, "0"); }
function fmtClock(seconds) {
  seconds = Math.max(0, seconds || 0);
  return `${pad(seconds / 3600)}:${pad((seconds % 3600) / 60)}:${pad(seconds % 60)}`;
}
function fmtT(ms) {
  ms = Math.max(0, ms || 0);
  const total = ms / 1000;
  const hours = Math.floor(total / 3600);
  const body = `${pad((total % 3600) / 60)}:${pad(total % 60)}.${pad(ms % 1000, 3)}`;
  return hours ? `${hours}:${body}` : body;
}
function fmtDuration(seconds) {
  if (seconds == null) return "";
  seconds = Math.round(seconds);
  const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60), secs = seconds % 60;
  return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
}
function fmtLongDuration(seconds) {
  if (!isFinite(seconds) || seconds == null) return "—";
  const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60);
  if (hours >= 1) return `约 ${hours} 小时 ${minutes} 分`;
  return `约 ${Math.max(1, minutes)} 分钟`;
}
function fmtBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes, unit = 0;
  while (value >= 1000 && unit < units.length - 1) { value /= 1000; unit++; }
  return `${value >= 100 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}
function fmtNumber(value) { return value == null ? "—" : Number(value).toLocaleString("zh-CN"); }
function parseDate(text) { const date = new Date(text); return isNaN(date) ? null : date; }
function dayLabel(date) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(date); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return "今天";
  if (diff === 1) return "昨天";
  return `${date.getFullYear()} 年 ${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}
function timeOf(date) { return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`; }
function throttle(fn, ms) {
  let last = 0, timer = null, pending = null;
  return (...args) => {
    pending = args;
    const now = performance.now();
    const run = () => { last = performance.now(); timer = null; fn(...pending); };
    if (now - last >= ms) run();
    else if (!timer) timer = setTimeout(run, ms - (now - last));
  };
}

// ---------------------------------------------------------------------------------------------
// display preferences (depth colouring), kept in the browser
// ---------------------------------------------------------------------------------------------
const display = {
  cmap: store.get("cmap", "turbo"),
  near: store.get("near", 0.2),
  far: store.get("far", 6.0),
  listeners: [],
  query() { return `near=${this.near}&far=${this.far}&cmap=${this.cmap}`; },
  set(values) {
    Object.assign(this, values);
    if (this.far <= this.near + 0.05) this.far = +(this.near + 0.1).toFixed(2);
    store.set("cmap", this.cmap); store.set("near", this.near); store.set("far", this.far);
    this.listeners.forEach((fn) => fn());
  },
  gradient() { return `linear-gradient(90deg, ${(GRADIENTS[this.cmap] || GRADIENTS.turbo).join(", ")})`; },
};

// ---------------------------------------------------------------------------------------------
// toasts
// ---------------------------------------------------------------------------------------------
const FINDER = /Mac/i.test(navigator.platform || navigator.userAgent || "") ? "访达" : "文件夹";
const fmtStamp = (text) => (text ? String(text).replace("T", " ").slice(0, 19) : null);

const STATE_TEXT = { starting: "启动中", waiting: "等待相机接入", opening: "正在打开", streaming: "正常出图", idle: "待机", error: "出错", stopped: "已退出" };
const STATUS_TEXT = { recording: "未结束", complete: "已完成", recovered: "已修复", unknown: "" };
function sessionTag(item) {
  if (item.live) return ["live", "录制中"];
  if (item.needs_repair) return ["repair", "未正常结束"];
  if (item.result) return [item.result, item.result];
  return ["none", STATUS_TEXT[item.status] ?? item.status ?? ""];
}

function toast(message, { level = "info", sub = "", actions = [], timeout = 5000 } = {}) {
  const element = document.createElement("div");
  element.className = `toast ${level}`;
  element.innerHTML = `<div><div class="msg">${esc(message)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div><div class="acts"></div>`;
  const holder = $(".acts", element);
  for (const action of actions) {
    const button = document.createElement("button");
    button.className = "btn small" + (action.primary ? " primary" : " ghost");
    button.textContent = action.label;
    button.onclick = () => { action.run(); element.remove(); };
    holder.appendChild(button);
  }
  const close = document.createElement("button");
  close.className = "icon-btn";
  close.innerHTML = icon("close");
  close.style.width = close.style.height = "28px";
  close.onclick = () => element.remove();
  holder.appendChild(close);
  $("#toasts").appendChild(element);
  if (timeout) setTimeout(() => element.remove(), timeout);
  while ($("#toasts").children.length > 4) $("#toasts").firstElementChild.remove();
  return element;
}
function reportError(error, prefix = "") { toast(prefix + (error && error.message ? error.message : String(error)), { level: "error", timeout: 7000 }); }

// ---------------------------------------------------------------------------------------------
// panels and stages
// ---------------------------------------------------------------------------------------------
const displayed = new Set();     // bitmaps currently on screen (never closed by the cache)

class Panel {
  constructor(name, stage) {
    this.name = name;
    this.stage = stage;
    this.bitmap = null;
    this.owned = false;
    this.rect = null;
    const element = this.el = document.createElement("figure");
    element.className = "panel";
    element.dataset.stream = name;
    element.innerHTML = `
      <canvas></canvas>
      <div class="empty"><span class="spinner"></span><span class="empty-text">等待画面</span></div>
      <div class="stale" hidden></div>
      <figcaption class="label"><i class="swatch" style="background:${SWATCH[name]}"></i><b>${LABELS[name] || name}</b><span class="meta"></span></figcaption>
      <div class="corner"><span class="chip info" hidden></span><button class="tool expand" title="单独放大（再点一次还原）">${icon("expand")}</button></div>
      ${name === "depth" ? '<div class="legend" title="调整深度着色"><span class="near"></span><span class="bar"></span><span class="far"></span></div><div class="crosshair" hidden></div><div class="probe" hidden></div>' : ""}`;
    this.canvas = $("canvas", element);
    this.ctx = this.canvas.getContext("2d");
    this.empty = $(".empty", element);
    this.stale = $(".stale", element);
    $(".expand", element).onclick = (event) => { event.stopPropagation(); stage.toggleSingle(name); };
    element.addEventListener("click", () => stage.promote(name));
    element.addEventListener("dblclick", () => stage.toggleSingle(name));
    new ResizeObserver(() => this.resize()).observe(element);
    if (name === "depth") {
      this.updateLegend();
      $(".legend", element).onclick = (event) => { event.stopPropagation(); settings.open("display"); };
      const probe = throttle((x, y) => stage.onProbe && stage.onProbe(this, x, y), 70);
      this.canvas.addEventListener("mousemove", (event) => {
        const point = this.toImage(event);
        if (!point) { this.hideProbe(); return; }
        this.probeAt = { x: event.offsetX, y: event.offsetY };
        probe(point.x, point.y);
      });
      this.canvas.addEventListener("mouseleave", () => this.hideProbe());
    }
  }
  resize() {
    const box = this.el.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(box.width * ratio)), height = Math.max(1, Math.round(box.height * ratio));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width; this.canvas.height = height;
      this.draw();
    }
  }
  show(bitmap, owned) {
    const previous = this.bitmap;
    if (previous && previous !== bitmap) {
      displayed.delete(previous);
      if (this.owned || !player.cache.holds(previous)) { try { previous.close(); } catch { /* closed */ } }
    }
    this.bitmap = bitmap;
    this.owned = owned;
    if (bitmap) displayed.add(bitmap);
    this.setEmpty(null);
    this.draw();
  }
  clear(text) {
    if (this.bitmap) {
      displayed.delete(this.bitmap);
      if (this.owned || !player.cache.holds(this.bitmap)) { try { this.bitmap.close(); } catch { /* closed */ } }
    }
    this.bitmap = null;
    this.draw();
    this.setEmpty(text === undefined ? "等待画面" : text, true);
  }
  draw() {
    const { ctx, canvas, bitmap } = this;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!bitmap || !bitmap.width) { this.rect = null; return; }
    const scale = Math.min(canvas.width / bitmap.width, canvas.height / bitmap.height);
    const width = bitmap.width * scale, height = bitmap.height * scale;
    const x = (canvas.width - width) / 2, y = (canvas.height - height) / 2;
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    try { ctx.drawImage(bitmap, x, y, width, height); } catch { /* bitmap was closed */ }
    const ratio = window.devicePixelRatio || 1;
    this.rect = { x: x / ratio, y: y / ratio, width: width / ratio, height: height / ratio };
  }
  toImage(event) {
    const rect = this.rect;
    if (!rect) return null;
    const x = (event.offsetX - rect.x) / rect.width, y = (event.offsetY - rect.y) / rect.height;
    return x >= 0 && x <= 1 && y >= 0 && y <= 1 ? { x, y } : null;
  }
  showProbe(text) {
    if (!this.probeAt) return;
    const probe = $(".probe", this.el), cross = $(".crosshair", this.el);
    probe.textContent = text;
    probe.style.left = this.probeAt.x + "px"; probe.style.top = this.probeAt.y + "px";
    cross.style.left = this.probeAt.x + "px"; cross.style.top = this.probeAt.y + "px";
    probe.hidden = cross.hidden = false;
  }
  hideProbe() {
    this.probeAt = null;
    const probe = $(".probe", this.el), cross = $(".crosshair", this.el);
    if (probe) probe.hidden = true;
    if (cross) cross.hidden = true;
  }
  setEmpty(text, spinner = true) {
    this.empty.hidden = text == null;
    if (text != null) {
      $(".empty-text", this.empty).textContent = text;
      $(".spinner", this.empty).hidden = !spinner;
    }
  }
  setMeta(text) { $(".meta", this.el).textContent = text || ""; }
  setInfo(text, level = "") {
    const chip = $(".chip.info", this.el);
    chip.hidden = !text;
    chip.textContent = text || "";
    chip.className = "chip info " + level;
  }
  setStale(flag) { this.stale.hidden = !flag; }
  updateLegend() {
    if (this.name !== "depth") return;
    $(".legend .near", this.el).textContent = display.near.toFixed(1);
    $(".legend .far", this.el).textContent = display.far.toFixed(1) + " m";
    $(".legend .bar", this.el).style.background = display.gradient();
  }
}

class Stage {
  constructor(element, key) {
    this.el = element;
    this.key = key;
    this.panels = new Map();
    this.layout = store.get(key + ".layout", "main");
    this.lastLayout = this.layout === "single" ? "main" : this.layout;
    this.primary = store.get(key + ".primary", "color");
    this.names = [];
    this.onProbe = null;
    this.apply();
  }
  setStreams(names) {
    names = ORDER.filter((name) => names.includes(name));
    if (names.join() === this.names.join()) return false;
    for (const [name, panel] of this.panels) {
      if (!names.includes(name)) { panel.clear(); panel.el.remove(); this.panels.delete(name); }
    }
    for (const name of names) {
      if (!this.panels.has(name)) this.panels.set(name, new Panel(name, this));
    }
    for (const name of names) this.el.appendChild(this.panels.get(name).el);  // keep display order
    this.names = names;
    this.apply();
    return true;
  }
  apply() {
    if (!this.names.includes(this.primary)) this.primary = this.names[0] || "color";
    this.el.dataset.layout = this.layout;
    this.el.dataset.count = String(this.names.length);
    this.el.style.setProperty("--count", String(Math.max(1, this.names.length)));
    for (const [name, panel] of this.panels) {
      panel.el.classList.toggle("primary", name === this.primary);
      $(".expand", panel.el).innerHTML = icon(this.layout === "single" ? "shrink" : "expand");
    }
    // primary first in main layout so the grid places it in the big cell
    if (this.layout === "main" && this.panels.has(this.primary)) this.el.prepend(this.panels.get(this.primary).el);
    else for (const name of this.names) this.el.appendChild(this.panels.get(name).el);
    const overlay = $(".stage-overlay", this.el);
    if (overlay) this.el.appendChild(overlay);
    store.set(this.key + ".layout", this.layout);
    store.set(this.key + ".primary", this.primary);
  }
  promote(name) {
    if (this.layout !== "main" || name === this.primary) return;
    this.primary = name;
    this.apply();
  }
  toggleSingle(name) {
    if (this.layout === "single" && this.primary === name) this.layout = this.lastLayout || "main";
    else { if (this.layout !== "single") this.lastLayout = this.layout; this.layout = "single"; this.primary = name; }
    this.apply();
  }
  cycleLayout() {
    this.layout = this.layout === "main" ? "row" : "main";
    this.lastLayout = this.layout;
    this.apply();
  }
  setOverlay(html) {
    let overlay = $(".stage-overlay", this.el);
    if (!html) { if (overlay) overlay.remove(); return; }
    if (!overlay) { overlay = document.createElement("div"); overlay.className = "stage-overlay"; this.el.appendChild(overlay); }
    if (overlay.dataset.html !== html) { overlay.innerHTML = html; overlay.dataset.html = html; }
  }
}

// ---------------------------------------------------------------------------------------------
// live view
// ---------------------------------------------------------------------------------------------
const live = {
  stage: new Stage($("#live-stage"), "live"),
  generation: 0,
  running: false,
  streams: [],
  setStreams(names) {
    const changed = this.stage.setStreams(names);
    this.streams = this.stage.names.slice();
    if (changed && this.running) { this.stop(); this.start(); }
  },
  start() {
    if (this.running || app.closed) return;
    this.running = true;
    const generation = ++this.generation;
    for (const name of this.streams) this.loop(name, generation);
  },
  stop() { this.running = false; this.generation++; },
  colorWidth(panel) {
    const ratio = window.devicePixelRatio || 1;
    const wanted = Math.ceil((panel.el.clientWidth * ratio) / 160) * 160;
    return clamp(wanted || 960, 320, 1920);
  },
  async loop(name, generation) {
    let after = 0;
    while (this.running && generation === this.generation) {
      const panel = this.stage.panels.get(name);
      if (!panel) return;
      let query = `after=${after}`;
      if (name === "depth") query += "&" + display.query();
      if (name === "color") query += `&w=${this.colorWidth(panel)}`;
      try {
        const response = await fetch(`/api/live/${name}?${query}`, { cache: "no-store" });
        if (generation !== this.generation) return;
        if (response.status === 204) continue;
        if (!response.ok) { await sleep(800); continue; }
        after = Number(response.headers.get("X-Seq")) || after;
        const bitmap = await createImageBitmap(await response.blob());
        if (generation !== this.generation) { bitmap.close(); return; }
        panel.show(bitmap, true);
      } catch {
        await sleep(1000);
      }
    }
  },
  async probe(panel, x, y) {
    try {
      const value = await api.get(`/api/live/depth_at?x=${x.toFixed(4)}&y=${y.toFixed(4)}`);
      panel.showProbe(value.raw ? `${value.m.toFixed(3)} m` : "无测量");
    } catch { panel.hideProbe(); }
  },
};
live.stage.onProbe = (panel, x, y) => live.probe(panel, x, y);

// ---------------------------------------------------------------------------------------------
// state polling and the live controls
// ---------------------------------------------------------------------------------------------
const app = { state: null, eventId: -1, failures: 0, lastResultKey: null, recordClickAt: 0, tab: "live", polling: false, closed: false };

// No requests at all while the tab is hidden or closed: the macOS service exits after a few minutes without
// requests and launchd starts it again on the next one, so a forgotten background tab costs nothing.
async function poll() {
  if (app.closed || document.hidden) { app.polling = false; return; }
  app.polling = true;
  try {
    const since = app.eventId < 0 ? 0 : app.eventId;
    // want=1 keeps the camera streaming (the server pauses it when nobody has looked for a minute)
    const want = !document.hidden && (app.tab === "live" || $("#settings").classList.contains("open")) ? 1 : 0;
    const state = await api.get(`/api/state?since=${since}&want=${want}`);
    app.failures = 0;
    onState(state);
  } catch {
    app.failures++;
    if (app.failures >= 3) onOffline();
  }
  setTimeout(poll, 500);
}
function resumePolling() { if (!app.polling && !app.closed && !document.hidden) poll(); }

function onOffline() {
  setDevice("bad", "连接不到 D455 Studio");
  live.stage.setOverlay(`<div class="notice"><div class="glyph">${icon("plug")}</div><div class="big">连接不到 D455 Studio</div>
    <p>程序没有响应。Mac 上装成后台服务的，打开这个网页时会自动启动，等十几秒再刷新；还不行就在终端运行 <code>bash install_service.sh doctor</code>，它会写明原因。用 studio.sh 或 studio.bat 在终端里启动的，它可能已经退出，重新运行一次即可。</p></div>`);
  $("#rec-btn").disabled = true;
}

function showClosed(service) {
  app.closed = true;
  live.stop();
  player.pause();
  $("#closed-glyph").innerHTML = icon("power");
  $("#closed-title").textContent = service ? "后台服务已关闭" : "D455 Studio 已退出";
  $("#closed-text").textContent = service
    ? "相机已经交还给系统，CPU 和内存也都释放了。再打开或刷新这个网页时会自动启动，不需要密码。"
    : "相机已经释放。要再用，在终端重新运行 studio.sh（Windows：studio.bat）。";
  $("#closed-reopen").hidden = !service;
  $("#closed").hidden = false;
}
$("#closed-reopen").onclick = () => location.reload();

async function quitService() {
  const service = !!(app.state && app.state.camera.service);
  if (!service && !(await confirmDialog({ title: "退出 D455 Studio？", html: "<p>退出后要在终端重新运行 studio.sh 才能再用。</p>", confirm: "退出" }))) return;
  try { await api.post("/api/service/quit"); showClosed(service); } catch (error) { reportError(error); }
}

// A small modal. buttons: [{label, value, kind, run}], the first one is the safe choice and gets the focus
// (a stray Enter must never confirm a deletion); Escape and a click outside resolve null. run() is called
// inside the click itself, which a file picker needs.
function choiceDialog({ title, html = "", buttons }) {
  return new Promise((resolve) => {
    const modal = $("#modal");
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = html;
    const actions = $("#modal-actions");
    actions.innerHTML = "";
    const finish = (value) => { modal.hidden = true; document.removeEventListener("keydown", onKey, true); resolve(value); };
    const onKey = (event) => { if (event.key === "Escape") { event.stopPropagation(); finish(null); } };
    const elements = buttons.map((spec) => {
      const button = document.createElement("button");
      button.className = "btn " + (spec.kind || "");
      button.textContent = spec.label;
      button.onclick = () => { if (spec.run) spec.run(); finish(spec.value); };
      return button;
    });
    actions.append(...elements);
    modal.onclick = (event) => { if (event.target === modal) finish(null); };
    document.addEventListener("keydown", onKey, true);
    modal.hidden = false;
    elements[0].focus();
  });
}
async function confirmDialog({ title, html = "", confirm = "确定", cancel = "取消", danger = false }) {
  return (await choiceDialog({ title, html, buttons: [{ label: cancel, value: false },
    { label: confirm, value: true, kind: danger ? "danger" : "primary" }] })) === true;
}

function setDevice(level, text, sub = "") {
  $("#device-dot").className = "dot " + level;
  $("#device-text").innerHTML = esc(text) + (sub ? ` <span class="sub">${esc(sub)}</span>` : "");
}

function onState(state) {
  const previous = app.state;
  app.state = state;
  $("#version").textContent = "v" + state.version;
  const camera = state.camera;

  // device pill
  const simulated = camera.simulated ? " · 模拟相机" : "";
  if (camera.state === "streaming") {
    const usb = camera.usb ? `USB ${camera.usb}` : "";
    setDevice(camera.usb && camera.usb.startsWith("2") ? "busy" : "ok",
      `${(camera.name || "RealSense").replace("Intel RealSense ", "")} · ${camera.serial || ""}`, usb + simulated);
  } else if (camera.state === "error") setDevice("bad", camera.message || "相机出错");
  else if (camera.state === "waiting") setDevice("busy", "等待相机接入");
  else if (camera.state === "idle") setDevice("", "相机待机", "打开「实时」页会自动唤醒");
  else setDevice("busy", camera.message || "正在打开相机");

  // panels
  const video = state.streams.filter((stream) => stream.kind === "video");
  if (video.length) live.setStreams(video.map((stream) => stream.name));
  for (const stream of video) {
    const panel = live.stage.panels.get(stream.name);
    if (!panel) continue;
    panel.setMeta(`${stream.width}×${stream.height}`);
    const low = camera.state === "streaming" && stream.rate < 0.85 * stream.fps;
    panel.setInfo(camera.state === "streaming" ? `${stream.rate.toFixed(1)} fps` : "", low ? "warn" : "");
    panel.el.classList.toggle("recording", !!state.recording);
  }
  liveOverlay(camera);

  // recording
  renderRecording(state);
  renderStats(state);
  $("#tab-rec").hidden = !state.recording;

  // events
  const fresh = app.eventId < 0 ? [] : state.events;
  app.eventId = state.event_id;
  for (const event of fresh) {
    if (event.source === "studio" && event.level !== "info") toast(event.message, { level: event.level, sub: event.hint || "", timeout: event.hint ? 15000 : 8000 });
  }
  settings.renderLog(state.events, previous == null);

  // finished recording
  const result = state.last_result;
  const key = result ? result.session_id + result.time : null;
  if (previous && key && key !== app.lastResultKey) showResult(result);
  if (!previous) app.lastResultKey = key;
  else app.lastResultKey = key;

  if (!previous) initFromSettings(state.settings);
  settings.onState(state, previous);
  if (previous && previous.recording && !state.recording) library.refresh();
}

function liveOverlay(camera) {
  const stage = live.stage;
  if (camera.state === "streaming") { stage.setOverlay(null); return; }
  let html;
  if (camera.state === "waiting") {
    html = `<div class="notice"><div class="glyph">${icon("camera")}</div><div class="big">等待相机接入</div>
      <p>用 USB 3 数据线把 D455 直接插到这台电脑上，几秒后会自动连接。</p>
      <ul><li>线要标 5 Gbps 或更高，很多充电线只有 USB 2</li><li>D455 的 USB-C 口比外壳深，插头要推到底</li><li>不要经过扩展坞或 Hub</li></ul>
      <div class="hints checks"></div></div>`;
  } else if (camera.state === "idle") {
    html = `<div class="notice"><span class="spinner"></span><div class="big">正在唤醒相机</div>
      <p>一分钟没人看时相机会暂停出图（投射器也关掉），回到这一页几秒后自动恢复。</p></div>`;
  } else if (camera.state === "error") {
    html = `<div class="notice"><div class="glyph">${icon("alert")}</div><div class="big">相机出错</div><p>${esc(camera.message)}</p>
      <button class="btn" onclick="restartCamera()">重新打开相机</button><div class="hints checks"></div></div>`;
  } else {
    html = `<div class="notice"><span class="spinner"></span><div class="big">${esc(camera.message || "正在打开相机")}</div>
      <p>第一次接管相机时彩色可能要重启一次数据流，属于正常。</p></div>`;
  }
  stage.setOverlay(html);
  for (const panel of stage.panels.values()) if (panel.bitmap) panel.clear("");
  // still no camera after a few seconds: ask the server what the system sees, and keep that up to date
  const stuck = camera.state === "waiting" || camera.state === "error";
  const now = performance.now();
  if (!stuck) { app.stuckSince = 0; return; }
  if (!app.stuckSince) { app.stuckSince = now; app.hintAt = 0; }
  if (app.tab === "live" && now - app.stuckSince > 5000 && now - (app.hintAt || 0) > 10000) { app.hintAt = now; doctor.overlayHints(); }
}

const doctor = {
  findings: null,
  loading: null,
  at: 0,
  load(fresh) {
    if (this.loading) return this.loading;
    if (!fresh && this.findings && performance.now() - this.at < 6000) { this.render(); return Promise.resolve(this.findings); }
    if (!this.findings) $("#doctor").innerHTML = '<p class="note">正在检查…</p>';
    this.loading = api.get("/api/diagnostics" + (fresh ? "?fresh=1" : "")).then((data) => {
      this.findings = data.findings; this.at = performance.now(); this.render(); return this.findings;
    }).catch((error) => { $("#doctor").innerHTML = `<p class="note">${esc(error.message)}</p>`; return null; })
      .finally(() => { this.loading = null; });
    return this.loading;
  },
  html(items) {
    return items.map((item) => `<div class="check ${item.level}"><span class="mark"></span><div class="what">${esc(item.title)}</div>
      ${item.detail && item.level !== "ok" ? `<div class="why">${esc(item.detail)}</div>` : ""}
      ${item.fix && item.level !== "ok" ? `<div class="fix">办法：${esc(item.fix)}</div>` : ""}</div>`).join("");
  },
  render() { if (this.findings) $("#doctor").innerHTML = this.html(this.findings) || '<p class="note">没有可检查的项目</p>'; },
  // Under the "waiting for the camera" notice: what the system itself sees, refreshed while the notice stays up.
  async overlayHints() {
    const findings = await this.load(true);
    const holder = $("#live-stage .notice .hints");
    if (!holder || !findings) return;
    const relevant = findings.filter((item) => (item.level === "bad" || item.level === "warn") && ["usb", "camera", "guard", "usb_sdk", "hang"].includes(item.key));
    holder.innerHTML = this.html(relevant);
  },
};
$("#doctor-run").onclick = () => doctor.load(true);

async function restartCamera() {
  try { await api.post("/api/camera/restart"); toast("正在重新打开相机"); } catch (error) { reportError(error); }
}
window.restartCamera = restartCamera;

function renderRecording(state) {
  const button = $("#rec-btn"), clock = $("#rec-clock"), hint = $("#rec-hint");
  const recording = state.recording;
  const ready = state.camera.state === "streaming";
  button.classList.toggle("on", !!recording && recording.phase !== "saving");
  button.classList.toggle("saving", !!recording && recording.phase === "saving");
  button.disabled = recording ? recording.phase === "saving" : !ready;
  button.title = recording ? "停止录制" : "开始录制";
  clock.classList.toggle("on", !!recording);
  for (const id of ["#scene-location", "#scene-notes", "#rec-duration"]) $(id).disabled = !!recording;
  if (!recording) {
    clock.textContent = "00:00:00";
    hint.textContent = ready ? "点击开始录制" : "相机就绪后可录制";
    return;
  }
  clock.textContent = fmtClock(recording.elapsed);
  if (recording.phase === "starting") hint.textContent = "正在开始…";
  else if (recording.phase === "saving") hint.textContent = "正在保存和体检…";
  else if (recording.duration > 0) hint.textContent = `定时 ${fmtDuration(recording.duration)}，剩 ${fmtDuration(Math.max(0, recording.duration - recording.elapsed))}`;
  else hint.textContent = "点击停止";
}

function renderStats(state) {
  const recording = state.recording;
  const items = [];
  if (recording) {
    const lost = recording.device_gaps + recording.recorder_drops;
    const delta = recording.color_depth_ms;
    items.push(["已写入", fmtBytes(recording.bytes)]);
    items.push(["写盘", recording.rate_bps ? `${(recording.rate_bps / 1e6).toFixed(1)} MB/s` : "—", "", "optional"]);
    items.push(["可录", recording.remaining_s ? fmtLongDuration(recording.remaining_s) : "—", recording.remaining_s && recording.remaining_s < 900 ? "bad" : ""]);
    items.push(["丢帧", `${recording.device_gaps} / ${recording.recorder_drops}`, lost ? "warn" : "", "", "相机或 USB 丢的 / 本机来不及写的"]);
    if (delta != null) items.push(["彩色−深度", `${delta >= 0 ? "+" : ""}${delta.toFixed(2)} ms`, Math.abs(delta) > 8.3 ? "warn" : "", "optional", "最近一帧彩色与深度的时间差"]);
  } else {
    items.push(["剩余空间", fmtBytes(state.disk.free), state.disk.free != null && state.disk.free < 20e9 ? "warn" : ""]);
  }
  const html = items.map(([key, value, level = "", extra = "", title = ""]) =>
    `<div class="stat ${extra}" ${title ? `title="${esc(title)}"` : ""}><span class="k">${key}</span><span class="v ${level}">${esc(value)}</span></div>`).join("")
    + `<button class="icon-btn" id="live-layout" title="切换布局">${icon(live.stage.layout === "row" ? "layout" : "columns")}</button>`;
  const holder = $("#live-stats");
  if (holder.dataset.html !== html) {
    holder.innerHTML = html; holder.dataset.html = html;
    $("#live-layout").onclick = () => { live.stage.cycleLayout(); holder.dataset.html = ""; renderStats(app.state); };
  }
}

function showResult(result) {
  if (result.empty) {
    toast("这次没有录到数据", { level: "error", sub: result.stop_reason || "", timeout: 12000 });
    return;
  }
  const parts = [];
  if (result.duration_s != null) parts.push(fmtDuration(result.duration_s));
  if (result.bytes != null) parts.push(fmtBytes(result.bytes));
  parts.push("体检 " + (result.result || "?"));
  const note = (result.problems || [])[0] || (result.warnings || [])[0] || (result.ok ? "" : "提前结束：" + result.stop_reason);
  toast("已保存 · " + parts.join(" · "), {
    level: result.result === "FAIL" || !result.ok ? "warning" : "info", sub: note, timeout: 15000,
    actions: [
      { label: "回放", primary: true, run: () => { setTab("library"); library.open(result.session_id); } },
      { label: `在${FINDER}中显示`, run: () => api.post(`/api/sessions/${encodeURIComponent(result.session_id)}/reveal`).catch(reportError) },
    ],
  });
}

function initFromSettings(values) {
  $("#scene-location").value = values.scene.location || "";
  $("#scene-notes").value = values.scene.notes || "";
  const duration = String(Math.round(values.duration || 0));
  $("#rec-duration").value = $$("#rec-duration option").some((option) => option.value === duration) ? duration : "0";
}

function sceneFromForm() {
  const current = (app.state && app.state.settings.scene) || {};
  return { location: $("#scene-location").value.trim(), activity: current.activity || "", notes: $("#scene-notes").value.trim() };
}

async function toggleRecording() {
  const state = app.state;
  if (!state) return;
  const now = performance.now();
  if (now - app.recordClickAt < 900) return;  // a double click must not start and stop at once
  app.recordClickAt = now;
  try {
    if (state.recording) await api.post("/api/record/stop");
    else await api.post("/api/record/start", { scene: sceneFromForm(), duration: Number($("#rec-duration").value) });
  } catch (error) { reportError(error); }
}

$("#rec-btn").addEventListener("click", toggleRecording);
for (const id of ["#scene-location", "#scene-notes"]) {
  $(id).addEventListener("change", () => api.post("/api/settings", { scene: sceneFromForm() }).catch(() => {}));
}
$("#rec-duration").addEventListener("change", () => api.post("/api/settings", { duration: Number($("#rec-duration").value) }).catch(() => {}));

// ---------------------------------------------------------------------------------------------
// settings drawer
// ---------------------------------------------------------------------------------------------
const settings = {
  role: "depth",
  options: null,
  profiles: null,
  draft: null,
  isOpen: false,
  open(section) {
    openDrawer("#settings");
    this.isOpen = true;
    this.loadOptions();
    this.loadProfiles();
    this.syncForms(true);
    doctor.load(false);
    if (section === "display") setTimeout(() => $("#cmap").scrollIntoView({ block: "center", behavior: "smooth" }), 250);
  },
  async loadOptions() {
    try { this.options = await api.get("/api/options"); } catch { this.options = null; }
    this.renderOptions();
  },
  async loadProfiles() {
    try { this.profiles = await api.get("/api/profiles"); } catch { this.profiles = null; }
    this.renderStreams();
  },
  renderOptions() {
    const holder = $("#opt-rows");
    const entries = this.options && this.options[this.role];
    $$("#opt-role button").forEach((button) => {
      button.classList.toggle("active", button.dataset.role === this.role);
      button.disabled = !(this.options && this.options[button.dataset.role]);
    });
    if (!entries || !entries.length) {
      holder.innerHTML = `<p class="note">${app.state && app.state.camera.state === "streaming" ? "这个传感器没有可调的参数。" : "相机出图后才能调参数。"}</p>`;
      $("#opt-reset").disabled = true;
      return;
    }
    $("#opt-reset").disabled = false;
    const values = Object.fromEntries(entries.map((entry) => [entry.name, entry.value]));
    holder.innerHTML = "";
    for (const entry of entries) {
      const row = document.createElement("div");
      const locked = entry.auto_parent && values[entry.auto_parent] >= 1;
      row.className = "row" + (locked ? " disabled" : "");
      const unit = `<span class="unit">${esc(entry.unit || "")}</span>`;
      if (entry.kind === "toggle") {
        row.innerHTML = `<label>${esc(entry.label)}</label><div class="ctl"><label class="switch"><input type="checkbox" ${entry.value >= 1 ? "checked" : ""}><span></span></label></div>`;
        $("input", row).onchange = (event) => this.setOption(entry, event.target.checked ? 1 : 0);
      } else if (entry.kind === "select") {
        row.innerHTML = `<label>${esc(entry.label)}</label><div class="ctl"><select>${entry.choices.map((choice) =>
          `<option value="${choice.value}" ${choice.value === Math.round(entry.value) ? "selected" : ""}>${esc(choice.label)}</option>`).join("")}</select></div>`;
        $("select", row).onchange = (event) => this.setOption(entry, Number(event.target.value));
      } else {
        row.innerHTML = `<label>${esc(entry.label)}</label><div class="ctl"><input type="range" min="${entry.min}" max="${entry.max}" step="${entry.step}" value="${entry.value}" ${locked ? "disabled" : ""}>
          <input type="number" min="${entry.min}" max="${entry.max}" step="${entry.step}" value="${entry.value}" ${locked ? "disabled" : ""}>${unit}</div>`;
        const range = $("input[type=range]", row), number = $("input[type=number]", row);
        const send = throttle((value) => this.setOption(entry, value, true), 160);
        range.oninput = () => { number.value = range.value; send(Number(range.value)); };
        range.onchange = () => this.setOption(entry, Number(range.value));
        number.onchange = () => { const value = clamp(Number(number.value), entry.min, entry.max); range.value = value; this.setOption(entry, value); };
        if (locked) row.title = "先关掉自动模式";
      }
      holder.appendChild(row);
    }
  },
  async setOption(entry, value, quiet = false) {
    try {
      const options = await api.post("/api/options", { role: this.role, name: entry.name, value });
      if (!quiet) { this.options = options; this.renderOptions(); }
    } catch (error) { reportError(error, `${entry.label}：`); this.loadOptions(); }
  },
  renderStreams() {
    const values = this.draft || (app.state && app.state.settings);
    if (!values) return;
    const recording = !!(app.state && app.state.recording);
    const chips = $("#stream-chips");
    chips.innerHTML = ["color", "depth", "ir_left"].map((name) =>
      `<label><input type="checkbox" value="${name}" ${values.streams.includes(name) ? "checked" : ""} ${recording ? "disabled" : ""}>${LABELS[name]}</label>`).join("");
    $$("input", chips).forEach((input) => input.onchange = () => this.editStreams());
    const profiles = this.profiles || { depth: {}, color: {} };
    const fill = (select, sizes, current) => {
      const list = Object.keys(sizes);
      if (!list.includes(current)) list.unshift(current);
      select.innerHTML = list.map((size) => `<option value="${size}" ${size === current ? "selected" : ""}>${size.replace("x", " × ")}</option>`).join("");
      select.disabled = recording;
    };
    fill($("#depth-size"), profiles.depth, values.depth_size);
    fill($("#color-size"), profiles.color, values.color_size);
    const depthRates = profiles.depth[values.depth_size] || [];
    const colorRates = profiles.color[values.color_size] || [];
    let rates = values.streams.includes("color") && colorRates.length ? colorRates : depthRates;
    if (values.streams.includes("color") && colorRates.length && depthRates.length && (values.streams.includes("depth") || values.streams.includes("ir_left"))) {
      rates = depthRates.filter((rate) => colorRates.includes(rate));
    }
    if (!rates.length) rates = [values.fps];
    if (!rates.includes(values.fps)) rates = [values.fps, ...rates];
    $("#fps").innerHTML = rates.map((rate) => `<option value="${rate}" ${rate === values.fps ? "selected" : ""}>${rate} fps</option>`).join("");
    $("#fps").disabled = recording;
    const saved = app.state.settings;
    const dirty = this.draft && (this.draft.streams.join() !== saved.streams.join() || this.draft.depth_size !== saved.depth_size ||
      this.draft.color_size !== saved.color_size || this.draft.fps !== saved.fps);
    $("#apply-streams").disabled = !dirty || recording;
    $("#stream-note").textContent = recording ? "录制中不能改，停止录制后再调整。" : "修改后相机会重新打开，约几秒。";
  },
  editStreams() {
    const base = this.draft || app.state.settings;
    const streams = $$("#stream-chips input").filter((input) => input.checked).map((input) => input.value);
    this.draft = { streams: streams.length ? streams : base.streams, depth_size: $("#depth-size").value, color_size: $("#color-size").value, fps: Number($("#fps").value) };
    if (!streams.length) toast("至少保留一路画面", { level: "warning" });
    this.renderStreams();
  },
  async applyStreams() {
    if (!this.draft) return;
    try {
      const result = await api.post("/api/settings", this.draft);
      this.draft = null;
      if (result.restart) toast("正在按新设置重新打开相机");
      app.state.settings = result.settings;
      this.renderStreams();
    } catch (error) { reportError(error); }
  },
  syncForms(force) {
    const values = app.state && app.state.settings;
    if (!values) return;
    $$("#color-codec button").forEach((button) => button.classList.toggle("active", button.dataset.v === values.color_codec));
    $$("#ir-codec button").forEach((button) => button.classList.toggle("active", button.dataset.v === values.ir_codec));
    if (force || document.activeElement !== $("#jpeg-quality-n")) { $("#jpeg-quality").value = values.jpeg_quality; $("#jpeg-quality-n").value = values.jpeg_quality; }
    const split = String(Math.round(values.split_minutes));
    $("#split-minutes").value = $$("#split-minutes option").some((option) => option.value === split) ? split : "10";
    $("#root-path").textContent = app.state.disk.path;
    $("#cmap").value = display.cmap;
    $("#near").value = display.near;
    $("#far").value = display.far;
    if (force) this.renderStreams();
    this.renderCamera();
  },
  renderCamera() {
    const camera = app.state.camera;
    const rows = [["型号", camera.name || "—"], ["序列号", camera.serial || "—"],
      ["固件", camera.firmware ? camera.firmware + (camera.recommended_firmware && camera.recommended_firmware !== camera.firmware ? `（SDK 推荐 ${camera.recommended_firmware}）` : "") : "—"],
      ["USB", camera.usb || "—"], ["深度单位", camera.depth_scale ? `${camera.depth_scale * 1000} mm / 每个数值` : "—"],
      ["状态", (STATE_TEXT[camera.state] || camera.state) + (camera.message ? " · " + camera.message : "")], ["录制器", "record_d455.py " + app.state.recorder_version]];
    if (camera.macos) { rows.push(["IMU 防护", camera.macos.hid_guard]); rows.push(["USB 接管", camera.macos.usb_hold]); }
    $("#camera-kv").innerHTML = rows.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("");
    $("#service-restart").hidden = !camera.service;
    $("#camera-reset").disabled = camera.state !== "streaming" || !!app.state.recording;
    $("#service-quit").innerHTML = icon("power") + (camera.service ? "关闭后台服务" : "退出程序");
    $("#service-quit").disabled = !!app.state.recording;
    $("#service-note").textContent = camera.service
      ? (app.state.exit_after ? `后台服务按需运行：打开这个网页时自动启动，网页关掉 ${Math.round(app.state.exit_after / 60)} 分钟后（没有在录制）自动退出，退出后不占 CPU 和内存，相机交还系统。` : "")
      : "这个程序是在终端里启动的，关掉终端窗口或按 Ctrl+C 也会退出。";
  },
  renderLog(events, reset) {
    if (reset) this.log = [];
    this.log = (this.log || []).concat(events.filter((event) => !(this.log || []).some((item) => item.id === event.id))).slice(-80);
    $("#log").innerHTML = this.log.slice().reverse().map((event) =>
      `<div><time>${esc(event.time.slice(11, 19))}</time><span class="${event.level}">${esc(event.message)}</span></div>`).join("") || '<p class="note">暂无</p>';
  },
  onState(state, previous) {
    if (!this.isOpen) return;
    const streamingChanged = previous && previous.camera.state !== state.camera.state;
    if (streamingChanged && state.camera.state === "streaming") { this.loadOptions(); this.loadProfiles(); }
    if (previous && !!previous.recording !== !!state.recording) this.renderStreams();
    this.syncForms(false);
  },
};

$$("#opt-role button").forEach((button) => button.onclick = () => { settings.role = button.dataset.role; settings.renderOptions(); });
$("#opt-reset").onclick = async () => {
  try { settings.options = await api.post("/api/options/reset", { role: settings.role }); settings.renderOptions(); toast("已恢复默认参数"); } catch (error) { reportError(error); }
};
for (const id of ["#depth-size", "#color-size", "#fps"]) $(id).addEventListener("change", () => settings.editStreams());
$("#apply-streams").onclick = () => settings.applyStreams();
for (const [id, key] of [["#color-codec", "color_codec"], ["#ir-codec", "ir_codec"]]) {
  $$(id + " button").forEach((button) => button.onclick = async () => {
    try { const result = await api.post("/api/settings", { [key]: button.dataset.v }); app.state.settings = result.settings; settings.syncForms(true); } catch (error) { reportError(error); }
  });
}
$("#jpeg-quality").oninput = () => { $("#jpeg-quality-n").value = $("#jpeg-quality").value; };
const saveQuality = (value) => api.post("/api/settings", { jpeg_quality: clamp(Number(value), 70, 100) }).then((result) => { app.state.settings = result.settings; }).catch(reportError);
$("#jpeg-quality").onchange = () => saveQuality($("#jpeg-quality").value);
$("#jpeg-quality-n").onchange = () => { $("#jpeg-quality").value = $("#jpeg-quality-n").value; saveQuality($("#jpeg-quality-n").value); };
$("#split-minutes").onchange = () => api.post("/api/settings", { split_minutes: Number($("#split-minutes").value) }).catch(reportError);
$("#reveal-root").onclick = () => api.post("/api/reveal-root").catch(reportError);
$("#cmap").onchange = () => display.set({ cmap: $("#cmap").value });
$("#near").onchange = () => display.set({ near: clamp(Number($("#near").value), 0, 20) });
$("#far").onchange = () => display.set({ far: clamp(Number($("#far").value), 0.2, 40) });
$("#camera-restart").onclick = restartCamera;
$("#camera-reset").onclick = async () => {
  try { await api.post("/api/camera/reset"); toast("已发硬件复位，相机约 10 秒后重新连上"); } catch (error) { reportError(error); }
};
$("#service-quit").onclick = quitService;
$("#service-restart").onclick = async () => {
  try { await api.post("/api/service/restart"); toast("后台服务正在重启，几秒后自动恢复", { timeout: 15000 }); } catch (error) { reportError(error); }
};
display.listeners.push(() => {
  for (const stage of [live.stage, player.stage]) for (const panel of stage.panels.values()) panel.updateLegend();
  $("#near").value = display.near; $("#far").value = display.far; $("#cmap").value = display.cmap;
  player.redraw();
});

// ---------------------------------------------------------------------------------------------
// drawers and tabs
// ---------------------------------------------------------------------------------------------
function openDrawer(selector) {
  $$(".drawer").forEach((drawer) => drawer.classList.toggle("open", drawer.matches(selector)));
  $("#scrim").classList.add("open");
}
function closeDrawers() {
  $$(".drawer").forEach((drawer) => drawer.classList.remove("open"));
  $("#scrim").classList.remove("open");
  settings.isOpen = false;
  settings.draft = null;
}
$("#scrim").onclick = closeDrawers;
$$("[data-close]").forEach((button) => { button.innerHTML = icon("close"); button.onclick = closeDrawers; });
$("#open-settings").innerHTML = icon("sliders");
$("#open-settings").onclick = () => settings.open();

function setTab(name) {
  app.tab = name;
  $$(".tabs button").forEach((button) => button.classList.toggle("active", button.dataset.tab === name));
  $("#view-live").classList.toggle("active", name === "live");
  $("#view-library").classList.toggle("active", name === "library");
  if (name === "live" && !document.hidden) live.start(); else live.stop();
  if (name === "library") library.refresh(); else player.pause();
  store.set("tab", name);
}
$$(".tabs button").forEach((button) => button.onclick = () => setTab(button.dataset.tab));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { live.stop(); return; }
  if (app.closed) return;
  resumePolling();
  if (app.tab === "live") live.start();
});

// ---------------------------------------------------------------------------------------------
// library
// ---------------------------------------------------------------------------------------------
const thumbObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    const element = entry.target;
    element.style.backgroundImage = `url("${element.dataset.src}")`;
    thumbObserver.unobserve(element);
  }
}, { root: $("#session-list"), rootMargin: "200px" });

const library = {
  items: [],
  root: "",
  selected: null,
  selecting: false,
  chosen: new Set(),
  async refresh() {
    try {
      const data = await api.get("/api/sessions");
      this.items = data.sessions;
      this.root = data.root;
      for (const id of this.chosen) if (!this.item(id)) this.chosen.delete(id);
      this.render();
    } catch (error) { reportError(error, "读取录制列表失败："); }
  },
  visible() {
    const query = $("#search").value.trim().toLowerCase();
    return this.items.filter((item) => !query || [item.id, item.scene.location, item.scene.notes, item.scene.activity, item.device.serial]
      .some((text) => (text || "").toLowerCase().includes(query)));
  },
  render() {
    const items = this.visible();
    $("#session-count").textContent = this.items.length ? String(this.items.length) : "";
    $("#list-title").hidden = this.selecting;
    $("#select-title").hidden = !this.selecting;
    $("#select-bar").hidden = !this.selecting;
    $("#view-library").classList.toggle("selecting", this.selecting);
    this.renderSelection();
    const list = $("#session-list");
    if (!items.length) {
      list.innerHTML = `<div class="empty-list">${this.items.length ? "没有匹配的录制" : `还没有录制。<br>去「实时」页点红色按钮录一段，<br>或者把别处拷来的录像文件夹、.mcap 文件拖到这里。<br><button class="btn small" id="empty-import">${icon("upload")}导入录像</button>`}</div>`;
      const button = $("#empty-import");
      if (button) button.onclick = () => importer.choose();
      return;
    }
    let html = "", lastDay = "";
    for (const item of items) {
      const date = parseDate(item.created) || new Date();
      const day = dayLabel(date);
      if (day !== lastDay) { html += `<div class="day">${day}</div>`; lastDay = day; }
      const [kind, text] = sessionTag(item);
      const tag = `<span class="tag ${kind}">${esc(text)}</span>`;
      const scene = [item.scene.location, item.scene.notes].filter(Boolean).join(" · ");
      const origin = [item.folder ? `${item.folder}/` : "", item.kind === "file" ? item.name : ""].join("");
      const facts = [fmtBytes(item.bytes), item.device.serial].filter(Boolean).join(" · ");
      const line2 = scene || origin || facts;
      const line3 = line2 === facts ? (scene ? "" : origin && origin !== line2 ? origin : "") : facts;
      const chosen = this.chosen.has(item.id);
      html += `<button class="card ${item.id === this.selected && !this.selecting ? "active" : ""} ${chosen ? "chosen" : ""} ${item.live ? "is-live" : ""}" data-id="${esc(item.id)}" title="${esc(item.id)}">
        <div class="thumb" data-src="/api/sessions/${encodeURIComponent(item.id)}/thumb">${item.duration_s != null ? `<span class="dur">${fmtDuration(item.duration_s)}</span>` : ""}<span class="tick">${icon("ok")}</span></div>
        <div class="meta"><div class="line1"><span class="time">${timeOf(date)}</span>${tag}</div><div class="line2">${esc(line2)}</div>
        <div class="line2">${esc(line3)}</div></div></button>`;
    }
    list.innerHTML = html;
    $$(".card", list).forEach((card) => card.onclick = () => this.selecting ? this.toggle(card.dataset.id) : this.open(card.dataset.id));
    $$(".thumb", list).forEach((thumb) => thumbObserver.observe(thumb));
  },
  item(id) { return this.items.find((item) => item.id === id); },
  async open(id) {
    this.selected = id;
    $$(".card").forEach((card) => card.classList.toggle("active", card.dataset.id === id));
    if (!this.items.length) await this.refresh();
    const item = this.item(id);
    if (item && item.live) { toast("这个 session 正在录制，停止后才能回放", { level: "warning" }); return; }
    await player.open(id, item);
  },
  // ---- selecting and deleting
  setSelecting(on) {
    this.selecting = on;
    if (!on) this.chosen.clear();
    this.render();
  },
  toggle(id) {
    const item = this.item(id);
    if (!item) return;
    if (item.live) { toast("正在录制的不能删除", { level: "warning" }); return; }
    if (this.chosen.has(id)) this.chosen.delete(id); else this.chosen.add(id);
    const card = $(`.card[data-id="${CSS.escape(id)}"]`);
    if (card) card.classList.toggle("chosen", this.chosen.has(id));
    this.renderSelection();
  },
  renderSelection() {
    const chosen = [...this.chosen].map((id) => this.item(id)).filter(Boolean);
    const bytes = chosen.reduce((sum, item) => sum + (item.bytes || 0), 0);
    $("#select-count").textContent = chosen.length ? `已选 ${chosen.length} 个` : "点选要删除的录制";
    $("#select-size").textContent = chosen.length ? `共 ${fmtBytes(bytes)}` : "";
    $("#select-delete").disabled = !chosen.length;
    const all = this.visible().filter((item) => !item.live);
    $("#select-all").textContent = all.length && all.every((item) => this.chosen.has(item.id)) ? "全不选" : "全选";
  },
  selectAll() {
    const all = this.visible().filter((item) => !item.live);
    const every = all.every((item) => this.chosen.has(item.id));
    for (const item of all) { if (every) this.chosen.delete(item.id); else this.chosen.add(item.id); }
    this.render();
  },
  async remove(ids) {
    const items = ids.map((id) => this.item(id)).filter(Boolean);
    if (!items.length) return;
    const bytes = items.reduce((sum, item) => sum + (item.bytes || 0), 0);
    const names = items.slice(0, 6).map((item) => {
      const date = parseDate(item.created);
      const label = [date ? `${dayLabel(date)} ${timeOf(date)}` : item.id, [item.scene.location, item.scene.notes].filter(Boolean).join(" · ")].filter(Boolean).join("　");
      return `<li><span>${esc(label)}</span><span class="muted">${esc(fmtBytes(item.bytes))}</span></li>`;
    }).join("") + (items.length > 6 ? `<li class="muted">还有 ${items.length - 6} 个</li>` : "");
    const ok = await confirmDialog({
      title: items.length === 1 ? "删除这个录制？" : `删除 ${items.length} 个录制？`,
      html: `<ul class="list-names">${names}</ul><p>共 ${esc(fmtBytes(bytes))}。会从磁盘上直接删除，<b>不经过废纸篓，不能恢复</b>。</p>`,
      confirm: "删除", danger: true,
    });
    if (!ok) return;
    let result;
    try { result = await api.post("/api/sessions/delete", { ids: items.map((item) => item.id) }); } catch (error) { reportError(error, "删除失败："); return; }
    if (result.deleted.length) toast(`已删除 ${result.deleted.length} 个录制，空出 ${fmtBytes(result.freed)}`);
    for (const failure of result.failed) toast(`没有删除 ${failure.id}`, { level: "error", sub: failure.error, timeout: 10000 });
    if (result.deleted.includes(player.id)) player.close();
    for (const id of result.deleted) this.chosen.delete(id);
    if (this.selecting && !this.chosen.size) this.selecting = false;
    await this.refresh();
  },
};
$("#search").addEventListener("input", () => library.render());
$("#search-icon").innerHTML = icon("search");
$("#refresh-list").innerHTML = icon("refresh");
$("#refresh-list").onclick = () => library.refresh();
$("#select-btn").innerHTML = icon("check");
$("#select-btn").onclick = () => library.setSelecting(true);
$("#select-done").onclick = () => library.setSelecting(false);
$("#select-all").onclick = () => library.selectAll();
$("#select-delete").innerHTML = icon("trash") + "删除";
$("#select-delete").onclick = () => library.remove([...library.chosen]);

// ---------------------------------------------------------------------------------------------
// importing recordings copied from elsewhere: the browser reads the files and uploads them, so this works
// even where the macOS service itself may not look (Desktop, Downloads, a USB stick)
// ---------------------------------------------------------------------------------------------
const importer = {
  running: false,
  cancelled: false,
  xhr: null,
  current: null,
  choose() {
    if (this.running) { toast("正在导入，等这一批完成", { level: "warning" }); return; }
    choiceDialog({
      title: "导入录像",
      html: `<p>选一个录像文件夹（里面有 recordings 的那种，或者装着多个录像的上一级文件夹），或者直接选 .mcap 文件。也可以把它们拖到左边的列表上。</p>
        <p class="muted">会复制一份到 ${esc(library.root || "录像文件夹")}，原来的文件不动。</p>`,
      buttons: [{ label: "取消" }, { label: "选择 .mcap 文件…", run: () => $("#import-files").click() },
        { label: "选择文件夹…", kind: "primary", run: () => $("#import-folder").click() }],
    });
  },
  fromInput(input) {
    const files = [...input.files].map((file) => ({ file, path: file.webkitRelativePath || file.name }));
    input.value = "";
    this.run(files);
  },
  async fromDrop(dataTransfer) {
    const entries = [...dataTransfer.items].map((item) => item.webkitGetAsEntry && item.webkitGetAsEntry()).filter(Boolean);
    const files = [];
    const walk = async (entry, depth) => {
      if (entry.name.startsWith(".")) return;
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        files.push({ file, path: entry.fullPath.replace(/^\/+/, "") });
      } else if (entry.isDirectory && depth < 6) {
        const reader = entry.createReader();
        for (;;) {
          const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!batch.length) break;
          for (const child of batch) await walk(child, depth + 1);
        }
      }
    };
    try { for (const entry of entries) await walk(entry, 0); } catch (error) { reportError(error, "读取拖进来的文件失败："); return; }
    this.run(files);
  },
  // Which session folders and loose .mcap files are in a set of files with relative paths.
  group(files) {
    const roots = new Set();
    for (const { path } of files) {
      const parts = path.split("/").filter(Boolean);
      const at = parts.indexOf("recordings");
      if (at >= 1 && at < parts.length - 1) roots.add(parts.slice(0, at).join("/"));
      else if (parts.length >= 2 && parts[parts.length - 1] === "manifest.json") roots.add(parts.slice(0, -1).join("/"));
    }
    const groups = new Map();
    for (const item of files) {
      const parts = item.path.split("/").filter(Boolean);
      if (!parts.length || parts.some((part) => part.startsWith("."))) continue;  // .DS_Store, ._file (exFAT)
      let root = null;
      for (let length = parts.length - 1; length >= 1 && !root; length--) {
        const prefix = parts.slice(0, length).join("/");
        if (roots.has(prefix)) root = prefix;
      }
      if (root) {
        const name = root.split("/").pop();
        const group = groups.get("d:" + root) || { name, kind: "folder", files: [], bytes: 0 };
        group.files.push({ file: item.file, rel: parts.slice(root.split("/").length).join("/") });
        group.bytes += item.file.size;
        groups.set("d:" + root, group);
      } else if (/\.mcap$/i.test(parts[parts.length - 1])) {
        const name = parts[parts.length - 1];
        groups.set("f:" + item.path, { name, kind: "file", files: [{ file: item.file, rel: name }], bytes: item.file.size });
      }
    }
    return [...groups.values()];
  },
  async run(files) {
    if (this.running) { toast("正在导入，等这一批完成", { level: "warning" }); return; }
    const groups = this.group(files);
    if (!groups.length) { toast("里面没有找到录像", { level: "warning", sub: "要么是带 recordings 文件夹的录像文件夹，要么是 .mcap 文件", timeout: 9000 }); return; }
    const existing = new Set(library.items.map((item) => item.id));
    const fresh = groups.filter((group) => !existing.has(group.name));
    const skipped = groups.length - fresh.length;
    if (!fresh.length) { toast(groups.length === 1 ? "这个录像已经在列表里了" : "这些录像都已经在列表里了", { level: "warning" }); return; }
    const total = fresh.reduce((sum, group) => sum + group.bytes, 0);
    const names = fresh.slice(0, 6).map((group) => `<li><span>${esc(group.name)}</span><span class="muted">${esc(fmtBytes(group.bytes))}</span></li>`).join("")
      + (fresh.length > 6 ? `<li class="muted">还有 ${fresh.length - 6} 个</li>` : "");
    const ok = await confirmDialog({
      title: `导入 ${fresh.length} 个录像？`,
      html: `<ul class="list-names">${names}</ul><p>共 ${esc(fmtBytes(total))}，会复制到 ${esc(library.root)}。${skipped ? `另有 ${skipped} 个已经在列表里，跳过。` : ""}</p>`,
      confirm: "导入",
    });
    if (!ok) return;
    this.running = true; this.cancelled = false;
    $("#import-box").hidden = false;
    let done = 0, imported = 0;
    const failures = [];
    const started = performance.now();
    const progress = (extra) => {
      const sent = done + extra;
      const rate = sent / Math.max(0.5, (performance.now() - started) / 1000);
      const left = rate > 0 ? (total - sent) / rate : 0;
      $("#import-text").textContent = `正在导入 ${imported + failures.length + 1}/${fresh.length} · ${fmtBytes(sent)} / ${fmtBytes(total)}` + (sent > 0 && left > 3 ? ` · 约 ${fmtDuration(left)}` : "");
      $("#import-bar").style.width = `${total ? (100 * sent) / total : 0}%`;
    };
    for (const group of fresh) {
      if (this.cancelled) break;
      this.current = group.name;
      const before = done;
      progress(0);
      try {
        await api.post("/api/import/begin", { name: group.name, bytes: group.bytes, kind: group.kind });
        for (const { file, rel } of group.files) {
          if (this.cancelled) throw new Error("已取消");
          await this.upload(group.name, rel, file, (sent) => progress(sent));
          done += file.size;
        }
        await api.post("/api/import/finish", { name: group.name, kind: group.kind });
        imported++;
      } catch (error) {
        failures.push([group.name, error.message]);
        api.post("/api/import/cancel", { name: group.name }).catch(() => {});
        done = before + group.bytes;
      }
    }
    this.running = false; this.current = null;
    $("#import-box").hidden = true;
    if (imported) toast(`已导入 ${imported} 个录像`, { sub: skipped ? `${skipped} 个已存在，跳过` : "" });
    for (const [name, message] of failures.slice(0, 3)) toast(`没有导入 ${name}`, { level: message === "已取消" ? "warning" : "error", sub: message, timeout: 10000 });
    await library.refresh();
  },
  upload(name, rel, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      this.xhr = xhr;
      const query = new URLSearchParams({ name, path: rel, mtime: String((file.lastModified || 0) / 1000) });
      xhr.open("POST", "/api/import/file?" + query.toString());
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (event) => onProgress(event.loaded);
      xhr.onload = () => {
        this.xhr = null;
        if (xhr.status >= 200 && xhr.status < 300) { resolve(); return; }
        let message = "HTTP " + xhr.status;
        try { message = JSON.parse(xhr.responseText).error || message; } catch { /* not JSON */ }
        reject(new Error(message));
      };
      xhr.onerror = () => { this.xhr = null; reject(new Error("连接中断")); };
      xhr.onabort = () => { this.xhr = null; reject(new Error("已取消")); };
      xhr.send(file);
    });
  },
  cancel() {
    this.cancelled = true;
    if (this.xhr) this.xhr.abort();
  },
};
$("#import-btn").innerHTML = icon("upload");
$("#import-btn").onclick = () => importer.choose();
$("#import-folder").onchange = () => importer.fromInput($("#import-folder"));
$("#import-files").onchange = () => importer.fromInput($("#import-files"));
$("#import-cancel").onclick = () => importer.cancel();
(function dropTarget() {
  const view = $("#view-library");
  let depth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  view.addEventListener("dragenter", (event) => { if (!hasFiles(event)) return; event.preventDefault(); depth++; view.classList.add("dropping"); });
  view.addEventListener("dragover", (event) => { if (!hasFiles(event)) return; event.preventDefault(); event.dataTransfer.dropEffect = "copy"; });
  view.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) view.classList.remove("dropping"); });
  view.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault(); depth = 0; view.classList.remove("dropping");
    importer.fromDrop(event.dataTransfer);
  });
  // a file dropped anywhere else must not make the browser navigate away from the app
  window.addEventListener("dragover", (event) => { if (hasFiles(event)) event.preventDefault(); });
  window.addEventListener("drop", (event) => { if (hasFiles(event)) event.preventDefault(); });
})();

// ---------------------------------------------------------------------------------------------
// playback
// ---------------------------------------------------------------------------------------------
class FrameCache {
  constructor(limit) { this.limit = limit; this.map = new Map(); this.inflight = new Map(); this.members = new WeakSet(); }
  holds(bitmap) { return this.members.has(bitmap); }
  has(url) { return this.map.has(url) || this.inflight.has(url); }
  get(url) {
    if (this.map.has(url)) {
      const bitmap = this.map.get(url);
      this.map.delete(url); this.map.set(url, bitmap);
      return Promise.resolve(bitmap);
    }
    if (this.inflight.has(url)) return this.inflight.get(url);
    const promise = fetch(url).then(async (response) => {
      if (!response.ok) throw await api.error(response);
      return createImageBitmap(await response.blob());
    }).then((bitmap) => {
      this.inflight.delete(url);
      this.map.set(url, bitmap);
      this.members.add(bitmap);
      while (this.map.size > this.limit) {
        const [key, old] = this.map.entries().next().value;
        this.map.delete(key);
        this.members.delete(old);
        if (!displayed.has(old)) old.close();
      }
      return bitmap;
    }, (error) => { this.inflight.delete(url); throw error; });
    this.inflight.set(url, promise);
    return promise;
  }
  clear() {
    for (const bitmap of this.map.values()) { this.members.delete(bitmap); if (!displayed.has(bitmap)) bitmap.close(); }
    this.map.clear();
  }
}

function lowerBound(array, value) {
  let low = 0, high = array.length;
  while (low < high) { const mid = (low + high) >> 1; if (array[mid] < value) low = mid + 1; else high = mid; }
  return low;
}
function nearestIndex(array, value) {
  const index = lowerBound(array, value);
  if (index <= 0) return 0;
  if (index >= array.length) return array.length - 1;
  return value - array[index - 1] <= array[index] - value ? index - 1 : index;
}

const player = {
  stage: new Stage($("#play-stage"), "play"),
  cache: new FrameCache(84),
  id: null,
  item: null,
  timeline: null,
  names: [],
  times: {},
  anchor: null,
  t: 0,
  shown: -1,
  playing: false,
  speed: 1,
  busy: false,
  pending: null,
  token: 0,
  lastTick: 0,
  dragging: false,

  async open(id, item) {
    $("#view-library").classList.add("has-player");
    const token = ++this.token;
    this.pause();
    this.cache.clear();
    this.id = id;
    this.item = item || null;
    $("#player-welcome").hidden = false;
    $("#player-welcome").textContent = "正在读取录制…";
    $("#player-head").hidden = $("#play-stage").hidden = $("#transport").hidden = true;
    $("#player-banner").hidden = true;
    let timeline;
    try {
      timeline = await api.get(`/api/sessions/${encodeURIComponent(id)}/timeline`);
    } catch (error) {
      if (token !== this.token) return;
      $("#player-welcome").textContent = "读不出这个录制：" + error.message;
      if (item && item.needs_repair) this.showBanner(item);
      return;
    }
    if (token !== this.token) return;
    this.timeline = timeline;
    this.names = ORDER.filter((name) => timeline.streams[name]);
    this.times = Object.fromEntries(this.names.map((name) => [name, Float64Array.from(timeline.streams[name].t)]));
    this.anchor = timeline.anchor;
    this.duration = timeline.duration_ms;
    this.tolerance = 500 / ((timeline.streams[this.anchor] || {}).fps || 30);
    $("#player-welcome").hidden = true;
    $("#player-head").hidden = $("#play-stage").hidden = $("#transport").hidden = false;
    this.stage.setStreams(this.names);
    for (const name of this.names) {
      const stream = timeline.streams[name];
      const panel = this.stage.panels.get(name);
      panel.setMeta(stream.width ? `${stream.width}×${stream.height}` : "");
      panel.clear("");
    }
    this.renderHead();
    this.renderLanes();
    this.t = 0;
    this.shown = -1;
    $("#t-total").textContent = fmtT(this.duration);
    this.seek(0);
    if (item && item.needs_repair) this.showBanner(item);
  },
  close() {
    $("#view-library").classList.remove("has-player");
    ++this.token;
    this.pause();
    this.cache.clear();
    this.id = null;
    this.item = null;
    $("#player-welcome").hidden = false;
    $("#player-welcome").textContent = "从左侧选择一次录制";
    $("#player-head").hidden = $("#play-stage").hidden = $("#transport").hidden = $("#player-banner").hidden = true;
  },
  showBanner(item) {
    $("#player-banner").hidden = false;
    $("#player-banner-text").textContent = "这次录制没有正常结束（程序被强制退出或断电）。下面能看到已写入的部分；修复会补齐文件索引并重新生成体检报告。";
  },
  renderHead() {
    const item = this.item || {};
    const date = parseDate(item.created);
    $("#player-title").textContent = date ? `${dayLabel(date)} ${timeOf(date)}` : this.id;
    const tag = $("#player-tag");
    const [kind, text] = sessionTag(item);
    tag.className = "tag " + kind;
    tag.textContent = text;
    tag.hidden = !text;
    const streams = this.names.map((name) => {
      const stream = this.timeline.streams[name];
      return `${LABELS[name]} ${stream.width}×${stream.height}`;
    }).join(" · ");
    const fps = (this.timeline.streams[this.anchor] || {}).fps;
    const scene = item.scene ? [item.scene.location, item.scene.notes].filter(Boolean).join(" · ") : "";
    $("#player-sub").textContent = [scene, item.device && item.device.serial, streams, fps ? fps + " fps" : "", fmtBytes(item.bytes)].filter(Boolean).join("  ·  ");
  },
  renderLanes() {
    const grid = $("#tl-grid");
    $$(".lane-name, .lane-canvas, .lane-count", grid).forEach((element) => element.remove());
    for (const name of this.names) {
      const times = this.times[name];
      const label = document.createElement("span"); label.className = "lane-name"; label.textContent = LABELS[name];
      const canvas = document.createElement("canvas"); canvas.className = "lane-canvas";
      const count = document.createElement("span"); count.className = "lane-count";
      grid.append(label, canvas, count);
      const fps = this.timeline.streams[name].fps || 30;
      const period = 1000 / fps;
      let missing = 0;
      const gaps = [];
      for (let index = 1; index < times.length; index++) {
        const step = times[index] - times[index - 1];
        if (step > 1.5 * period) { missing += Math.round(step / period) - 1; gaps.push([times[index - 1], times[index]]); }
      }
      count.textContent = `${fmtNumber(times.length)} 帧` + (missing ? ` · 缺 ${fmtNumber(missing)}` : "");
      count.classList.toggle("warn", missing > 0);
      count.title = missing ? `按时间间隔推算缺 ${missing} 帧（相邻两帧间隔超过 1.5 个帧周期）` : "按时间间隔没有缺帧";
      requestAnimationFrame(() => {
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.round(canvas.clientWidth * ratio));
        canvas.height = Math.max(1, Math.round(canvas.clientHeight * ratio));
        const ctx = canvas.getContext("2d");
        const span = Math.max(this.duration, 1);
        const start = times.length ? times[0] : 0, end = times.length ? times[times.length - 1] : 0;
        ctx.fillStyle = "rgba(255,255,255,0.07)";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = SWATCH[name] + "66";
        ctx.fillRect((start / span) * canvas.width, 0, Math.max(1, ((end - start) / span) * canvas.width), canvas.height);
        ctx.fillStyle = "#ff6a5f";
        for (const [from, to] of gaps) {
          const x = (from / span) * canvas.width;
          ctx.fillRect(x, 0, Math.max(2 * ratio, ((to - from) / span) * canvas.width), canvas.height);
        }
      });
    }
  },
  indexAt(t) {
    const times = this.times[this.anchor];
    if (!times || !times.length) return 0;
    return clamp(lowerBound(times, t + 0.0005) - 1, 0, times.length - 1);
  },
  url(name, index) {
    const base = `/api/sessions/${encodeURIComponent(this.id)}/frame/${name}/${index}`;
    return name === "depth" ? `${base}?${display.query()}` : base;
  },
  targets(k) {
    const time = this.times[this.anchor][k];
    return this.names.map((name) => {
      const index = name === this.anchor ? k : nearestIndex(this.times[name], time);
      return { name, index, delta: this.times[name][index] - time };
    });
  },
  async render(k) {
    if (this.busy) { this.pending = k; return; }
    this.busy = true;
    const token = this.token;
    const targets = this.targets(k);
    const results = await Promise.allSettled(targets.map((target) => this.cache.get(this.url(target.name, target.index))));
    if (token !== this.token) { this.busy = false; return; }
    results.forEach((result, position) => {
      const target = targets[position];
      const panel = this.stage.panels.get(target.name);
      if (!panel) return;
      if (result.status === "fulfilled") panel.show(result.value, false);
      else panel.clear("这一帧读不出来");
      const off = Math.abs(target.delta) > this.tolerance;
      if (target.name === this.anchor) panel.setInfo(`#${fmtNumber(target.index + 1)}`);
      else panel.setInfo(`${target.delta >= 0 ? "+" : "−"}${Math.abs(target.delta).toFixed(1)} ms`, off ? "warn" : "");
      panel.setStale(off);
      panel.current = target.index;
    });
    this.shown = k;
    this.renderClock();
    this.busy = false;
    if (this.pending != null) {
      const next = this.pending;
      this.pending = null;
      if (next !== k) this.render(next);
    }
    if (this.playing) this.prefetch(k);
  },
  prefetch(k) {
    const total = this.times[this.anchor].length;
    const step = Math.max(1, Math.round(this.speed));
    for (let ahead = 1; ahead <= 8; ahead++) {
      const next = k + ahead * step;
      if (next >= total) break;
      for (const target of this.targets(next)) {
        if (this.cache.inflight.size >= 6) return;
        const url = this.url(target.name, target.index);
        if (!this.cache.has(url)) this.cache.get(url).catch(() => {});
      }
    }
  },
  redraw() {
    if (this.id && this.shown >= 0) { const k = this.shown; this.shown = -1; this.render(k); }
  },
  renderClock() {
    const times = this.times[this.anchor];
    if (!times) return;
    const shownTime = this.shown >= 0 ? times[this.shown] : 0;
    const t = this.playing ? this.t : shownTime;
    $("#t-now").textContent = fmtT(t);
    $("#frame-no").textContent = `帧 ${fmtNumber(Math.max(0, this.shown) + 1)} / ${fmtNumber(times.length)}`;
    if (!this.dragging) this.renderKnob(t);
  },
  renderKnob(t) {
    const fraction = this.duration > 0 ? clamp(t / this.duration, 0, 1) : 0;
    $("#tl-fill").style.width = fraction * 100 + "%";
    $("#tl-knob").style.left = fraction * 100 + "%";
  },
  seek(t) {
    if (!this.timeline) return;
    this.t = clamp(t, 0, this.duration);
    const k = this.indexAt(this.t);
    if (!this.playing) this.t = this.times[this.anchor][k];
    this.renderKnob(this.t);
    if (k !== this.shown) this.render(k);
  },
  step(frames) {
    if (!this.timeline) return;
    this.pause();
    const times = this.times[this.anchor];
    const k = clamp((this.shown < 0 ? 0 : this.shown) + frames, 0, times.length - 1);
    this.t = times[k];
    this.renderKnob(this.t);
    this.render(k);
  },
  play() {
    if (!this.timeline || this.playing) return;
    if (this.t >= this.duration - 1) this.t = 0;
    this.playing = true;
    this.lastTick = performance.now();
    $("#play-btn").innerHTML = icon("pause");
    requestAnimationFrame((now) => this.tick(now));
  },
  pause() {
    if (!this.playing) return;
    this.playing = false;
    $("#play-btn").innerHTML = icon("play");
    if (this.shown >= 0) this.t = this.times[this.anchor][this.shown];
    this.renderClock();
  },
  toggle() { this.playing ? this.pause() : this.play(); },
  tick(now) {
    if (!this.playing) return;
    const elapsed = Math.min(now - this.lastTick, 250);
    this.lastTick = now;
    this.t += elapsed * this.speed;
    if (this.t >= this.duration) { this.t = this.duration; this.seek(this.t); this.pause(); return; }
    const k = this.indexAt(this.t);
    if (k !== this.shown && !this.busy) this.render(k);
    $("#t-now").textContent = fmtT(this.t);
    if (!this.dragging) this.renderKnob(this.t);
    requestAnimationFrame((time) => this.tick(time));
  },
  setSpeed(speed) {
    this.speed = speed;
    $$("#speed button").forEach((button) => button.classList.toggle("active", Number(button.dataset.speed) === speed));
  },
  async probe(panel, x, y) {
    if (panel.current == null) return;
    try {
      const value = await api.get(`/api/sessions/${encodeURIComponent(this.id)}/depth_at?i=${panel.current}&x=${x.toFixed(4)}&y=${y.toFixed(4)}`);
      panel.showProbe(value.raw ? `${value.m.toFixed(3)} m` : "无测量");
    } catch { panel.hideProbe(); }
  },
};
player.stage.onProbe = (panel, x, y) => player.probe(panel, x, y);

$("#play-btn").innerHTML = icon("play");
$("#play-btn").onclick = () => player.toggle();
$("#step-back").innerHTML = icon("back");
$("#step-fwd").innerHTML = icon("fwd");
$("#step-back").onclick = () => player.step(-1);
$("#step-fwd").onclick = () => player.step(1);
$$("#speed button").forEach((button) => button.onclick = () => player.setSpeed(Number(button.dataset.speed)));
player.setSpeed(1);
$("#play-layout").innerHTML = icon("columns");
$("#play-layout").onclick = () => { player.stage.cycleLayout(); $("#play-layout").innerHTML = icon(player.stage.layout === "row" ? "layout" : "columns"); };
$("#player-reveal").innerHTML = icon("folder") + `<span class="lbl">在${FINDER}中显示</span>`;
$("#player-reveal").onclick = () => player.id && api.post(`/api/sessions/${encodeURIComponent(player.id)}/reveal`).catch(reportError);
$("#player-info").innerHTML = icon("info") + '<span class="lbl">详情</span>';
$("#player-info").onclick = () => player.id && details.open(player.id);
$("#player-delete").innerHTML = icon("trash");
$("#player-back").innerHTML = icon("chevron");
$("#player-back").onclick = () => { player.close(); library.selected = null; library.render(); };
$("#player-delete").onclick = () => player.id && library.remove([player.id]);
$("#player-repair").onclick = async () => {
  const id = player.id;
  if (!id) return;
  const note = toast("正在修复…", { timeout: 0 });
  try {
    const result = await api.post(`/api/sessions/${encodeURIComponent(id)}/repair`);
    note.remove();
    toast(result.code === 0 ? "修复完成" : "修复结束，体检有问题，详见详情", { level: result.code === 0 ? "info" : "warning" });
    await library.refresh();
    library.open(id);
  } catch (error) { note.remove(); reportError(error, "修复失败："); }
};

// timeline scrubbing
(() => {
  const element = $("#timeline"), hover = $("#tl-hover");
  const timeAt = (event) => {
    const box = element.getBoundingClientRect();
    return clamp((event.clientX - box.left) / box.width, 0, 1) * (player.duration || 0);
  };
  element.addEventListener("pointerdown", (event) => {
    if (!player.timeline) return;
    element.setPointerCapture(event.pointerId);
    player.dragging = true;
    player.wasPlaying = player.playing;
    player.pause();
    const t = timeAt(event);
    player.renderKnob(t);
    player.seek(t);
  });
  element.addEventListener("pointermove", (event) => {
    if (!player.timeline) return;
    const t = timeAt(event);
    const box = element.getBoundingClientRect();
    hover.hidden = false;
    hover.style.left = clamp(event.clientX - box.left, 0, box.width) + "px";
    hover.textContent = fmtT(t);
    if (player.dragging) { player.renderKnob(t); player.seek(t); }
  });
  const end = () => {
    if (!player.dragging) return;
    player.dragging = false;
    player.renderClock();
    if (player.wasPlaying) player.play();
  };
  element.addEventListener("pointerup", end);
  element.addEventListener("pointercancel", end);
  element.addEventListener("pointerleave", () => { hover.hidden = true; });
})();

document.addEventListener("keydown", (event) => {
  const tag = (event.target.tagName || "").toLowerCase();
  if (["input", "select", "textarea"].includes(tag) || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape") { closeDrawers(); return; }
  if (app.tab === "library" && player.timeline) {
    const fps = (player.timeline.streams[player.anchor] || {}).fps || 30;
    if (event.key === " ") { event.preventDefault(); player.toggle(); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); player.step(event.shiftKey ? -fps : -1); }
    else if (event.key === "ArrowRight") { event.preventDefault(); player.step(event.shiftKey ? fps : 1); }
    else if (event.key === "Home") { player.pause(); player.seek(0); }
    else if (event.key === "End") { player.pause(); player.seek(player.duration); }
    else if (event.key === "l" || event.key === "L") $("#play-layout").click();
    else if (/^[1-4]$/.test(event.key)) { const name = player.names[Number(event.key) - 1]; if (name) player.stage.toggleSingle(name); }
  } else if (app.tab === "live") {
    if (event.key === "l" || event.key === "L") { const button = $("#live-layout"); if (button) button.click(); }
    else if (/^[1-4]$/.test(event.key)) { const name = live.stage.names[Number(event.key) - 1]; if (name) live.stage.toggleSingle(name); }
  }
});

// ---------------------------------------------------------------------------------------------
// session details drawer
// ---------------------------------------------------------------------------------------------
const details = {
  async open(id) {
    openDrawer("#details");
    const body = $("#details-body");
    body.innerHTML = '<div class="section"><span class="spinner"></span></div>';
    let report;
    try { report = await api.get(`/api/sessions/${encodeURIComponent(id)}/report`); } catch (error) { body.innerHTML = `<p class="note">${esc(error.message)}</p>`; return; }
    const manifest = report.manifest || {};
    const verify = report.verify || {};
    const device = manifest.device || {};
    const capture = manifest.capture || {};
    const streams = verify.streams || {};
    const alignment = verify.alignment || {};
    const kv = (rows) => `<dl class="kv">${rows.filter((row) => row[1] !== undefined && row[1] !== null && row[1] !== "")
      .map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`;
    let html = "";
    html += `<div class="section"><h3>概况</h3>${kv([
      ["开始", fmtStamp(manifest.created_local)], ["时长", manifest.duration_s != null ? fmtDuration(manifest.duration_s) + `（${manifest.duration_s} 秒）` : null],
      ["状态", manifest.status], ["结束原因", manifest.stop_reason], ["数据量", fmtBytes(manifest.bytes_written)],
      ["文件", (manifest.recordings || []).length + " 个 .mcap"], ["地点", (manifest.scene || {}).location], ["备注", (manifest.scene || {}).notes],
    ])}</div>`;
    const result = verify.result;
    const problems = (verify.problems || []).map((text) => `<li style="color:var(--bad)">${esc(text)}</li>`).join("");
    const warnings = (verify.warnings || []).map((text) => `<li style="color:var(--warn)">${esc(text)}</li>`).join("");
    html += `<div class="section"><h3>体检 ${result ? `<span class="tag ${result}">${result}</span>` : ""}</h3>
      ${problems || warnings ? `<ul class="list-plain">${problems}${warnings}</ul>` : `<p class="note">${result ? "没有发现问题。" : "还没有体检报告。"}</p>`}</div>`;
    const streamRows = Object.entries(streams).map(([name, item]) => [LABELS[name] || name,
      `${fmtNumber(item.frames)} 帧 · ${item.measured_fps ?? "—"} fps · 缺 ${item.missing_frames ?? 0} · ${fmtBytes(item.bytes)}`]);
    if (streamRows.length) html += `<div class="section"><h3>各路画面</h3>${kv(streamRows)}</div>`;
    const alignRows = Object.entries(alignment).filter(([, value]) => value && typeof value === "object").map(([name, item]) => {
      const exact = item.device_clock_abs_delta_ms || item.abs_delta_ms || {};
      return [`${LABELS[name] || name} 对 ${LABELS[alignment.anchor] || alignment.anchor}`,
        `中位 ${exact.median ?? "—"} ms · p95 ${exact.p95 ?? "—"} ms · 未配上 ${item.unmatched ?? 0}`];
    });
    if (alignRows.length) html += `<div class="section"><h3>时间对齐</h3><p class="note">|时间差|，按相机硬件时钟计算。半帧以内算配上。</p>${kv(alignRows)}</div>`;
    const clock = verify.device_clock || {};
    if (clock.drift_ppm != null) html += `<div class="section"><h3>相机时钟</h3>${kv([["相对电脑漂移", clock.drift_ppm + " ppm"], ["拟合残差 p95", ((clock.residual_ms || {}).p95 ?? "—") + " ms"], ["计数器回绕", clock.hw_clock_wraps]])}</div>`;
    html += `<div class="section"><h3>设备</h3>${kv([["型号", device.name], ["序列号", device.serial_number], ["固件", device.firmware_version], ["USB", device.usb_type_descriptor],
      ["录制器", (manifest.host || {}).recorder], ["SDK", (manifest.host || {}).pyrealsense2]])}</div>`;
    const lost = Object.entries(capture).filter(([, item]) => item && (item.dropped_by_recorder || item.encode_errors))
      .map(([name, item]) => [LABELS[name] || name, `本机来不及写丢了 ${item.dropped_by_recorder + (item.encode_errors || 0)} 帧`]);
    if (lost.length) html += `<div class="section"><h3>写盘</h3>${kv(lost)}</div>`;
    const events = (report.events || []).map((event) => `<div><time>${esc((event.time || "").slice(11, 19))}</time><span class="${esc(event.level)}">${esc(event.message)}</span></div>`).join("");
    if (events) html += `<div class="section"><h3>录制时的提示</h3><div class="log">${events}</div></div>`;
    html += `<div class="section"><h3>位置</h3><div class="path">${esc(report.path)}</div>
      <div class="actions"><button class="btn small" id="copy-path">复制路径</button><button class="btn small" id="details-reveal">在${FINDER}中显示</button></div></div>`;
    body.innerHTML = html;
    $("#copy-path").onclick = () => navigator.clipboard.writeText(report.path).then(() => toast("已复制路径")).catch(() => toast("复制失败，请手动选中", { level: "warning" }));
    $("#details-reveal").onclick = () => api.post(`/api/sessions/${encodeURIComponent(id)}/reveal`).catch(reportError);
  },
};

// ---------------------------------------------------------------------------------------------
// start
// ---------------------------------------------------------------------------------------------
(function start() {
  $("#reveal-root").textContent = `在${FINDER}中打开`;
  const tab = store.get("tab", "live");
  setTab(tab === "library" ? "library" : "live");
  poll();
})();
