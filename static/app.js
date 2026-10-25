"use strict";

const $ = (id) => document.getElementById(id);

const REPEAT_DELAY_MS = 380;
const REPEAT_EVERY_MS = 110;
const LONG_OK_MS = 450;
const SWIPE_STEP_PX = 34;

let ws = null;
let wsTimer = 0;
let backoff = 500;
let hiddenAt = 0;
let state = {};
let apps = { launch: [], names: {} };

// ---------- connection ----------

function connect() {
  clearTimeout(wsTimer);
  if (ws) {
    ws.onclose = null;
    try { ws.close(); } catch (_) { /* already gone */ }
  }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { backoff = 500; render(); };
  ws.onmessage = (e) => {
    let m;
    try { m = JSON.parse(e.data); } catch (_) { return; }
    if (m.t === "state") { state = m; render(); }
    else if (m.t === "error") toast(m.msg);
    else if (m.t === "info") toast(m.msg, 8000);
  };
  ws.onclose = () => {
    render();
    wsTimer = setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 4000);
  };
  render();
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  toast("Нет связи с пультом на Pi");
  return false;
}

const key = (k) => send({ t: "key", k });

// iOS kills sockets of backgrounded home-screen apps but may still report them open.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { hiddenAt = Date.now(); return; }
  const wasAway = hiddenAt && Date.now() - hiddenAt > 3000;
  hiddenAt = 0;
  if (ws && ws.readyState === WebSocket.CONNECTING) return;
  if (!ws || ws.readyState !== WebSocket.OPEN || wasAway) connect();
});
window.addEventListener("pageshow", (e) => { if (e.persisted) connect(); });

// ---------- feedback ----------

// Safari has no vibration API; toggling an iOS 18 "switch" checkbox gives a system haptic tick.
// iOS only plays it inside a user activation, and a touch activates on release (pointerup,
// click), not on pointerdown or from a timer, so callers tick when the finger lifts.
const haptic = (() => {
  if (navigator.vibrate) return () => navigator.vibrate(10);
  return () => {
    const label = document.createElement("label");
    label.ariaHidden = "true";
    label.style.display = "none";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.setAttribute("switch", "");
    label.appendChild(input);
    document.head.appendChild(label);
    label.click();
    label.remove();
  };
})();

let toastTimer = 0;
function toast(text, ms = 2200) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), ms);
}

// ---------- rendering ----------

function appName(pkg) {
  if (!pkg) return "";
  const known = apps.launch.find((a) => a.id === pkg);
  return known ? known.name : (apps.names[pkg] || pkg);
}

function render() {
  const online = ws && ws.readyState === WebSocket.OPEN;
  const dot = $("dot");
  const sub = $("sub");
  dot.className = "dot";
  if (!online) {
    dot.classList.add("is-bad");
    sub.textContent = "Нет связи с Pi — ты в домашнем Wi-Fi?";
  } else if (state.pairing) {
    dot.classList.add("is-warn");
    sub.textContent = "Подключение к телевизору…";
  } else if (!state.connected) {
    dot.classList.add("is-warn");
    sub.textContent = state.paired === false ? "Нужно подключить пульт" : "Телевизор не отвечает…";
  } else if (state.on === false) {
    sub.textContent = "Спит";
  } else {
    dot.classList.add("is-ok");
    sub.textContent = appName(state.app) || "Включён";
  }

  $("power").classList.toggle("is-off", online && state.connected && state.on === false);

  const vol = state.volume;
  // The Shield reports 0/0 when the TV owns the volume; a level is only shown if it means something.
  $("vol-level").textContent = vol && vol.max > 0 ? String(vol.level) : "";
  const muted = !!(vol && vol.muted);
  $("mute").classList.toggle("muted", muted);
  $("mute-icon").firstElementChild.setAttribute("href", muted ? "#i-mute" : "#i-vol");

  for (const tile of document.querySelectorAll(".app-tile")) {
    tile.classList.toggle("current", tile.dataset.app === state.app);
  }

  const needPair = online && state.paired === false;
  $("pair").hidden = !needPair;
  if (needPair) {
    $("pair-step1").hidden = !!state.pairing;
    $("pair-step2").hidden = !state.pairing;
  }
}

// ---------- press helpers ----------

function capture(el, e) {
  try { el.setPointerCapture(e.pointerId); } catch (_) { /* pointer already gone */ }
}

function pressable(el, onDown, onUp) {
  el.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    capture(el, e);
    el.classList.add("on");
    onDown(e);
  });
  const end = (e) => {
    if (!el.classList.contains("on")) return;
    el.classList.remove("on");
    if (onUp) onUp(e);
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}

function repeater(fn) {
  let delay = 0;
  let every = 0;
  return {
    start() {
      fn();
      delay = setTimeout(() => { every = setInterval(fn, REPEAT_EVERY_MS); }, REPEAT_DELAY_MS);
    },
    stop() { clearTimeout(delay); clearInterval(every); },
  };
}

// ---------- plain buttons ----------

for (const el of document.querySelectorAll("[data-key]")) {
  const k = el.dataset.key;
  if (el.hasAttribute("data-repeat")) {
    const rep = repeater(() => key(k));
    pressable(el, () => rep.start(), () => { rep.stop(); haptic(); });
  } else {
    pressable(el, () => key(k), haptic);
  }
}

$("power").addEventListener("click", () => {
  haptic();
  key(state.connected && state.on === false ? "WAKEUP" : "POWER");
});

// ---------- D-pad ring ----------

const DIRS = ["DPAD_UP", "DPAD_RIGHT", "DPAD_DOWN", "DPAD_LEFT"];
const ring = $("ring");
const wedge = $("wedge");
const okEl = $("ok");
let ringPress = null;

function ringZone(e) {
  const r = ring.getBoundingClientRect();
  const x = e.clientX - (r.left + r.width / 2);
  const y = e.clientY - (r.top + r.height / 2);
  if (Math.hypot(x, y) < r.width * 0.21) return -1; // the OK disc is 40% wide
  const quarter = Math.round(Math.atan2(x, -y) / (Math.PI / 2));
  return ((quarter % 4) + 4) % 4;
}

ring.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || ringPress) return;
  e.preventDefault();
  capture(ring, e);
  const zone = ringZone(e);
  if (zone === -1) {
    okEl.classList.add("on");
    ringPress = { ok: true, held: false };
    ringPress.timer = setTimeout(() => {
      ringPress.held = true;
      okEl.classList.add("long");
      send({ t: "hold", k: "DPAD_CENTER", d: "start" });
    }, LONG_OK_MS);
  } else {
    wedge.style.transform = `rotate(${zone * 90}deg)`;
    wedge.classList.add("on");
    const rep = repeater(() => key(DIRS[zone]));
    rep.start();
    ringPress = { ok: false, rep };
  }
});

function ringEnd() {
  if (!ringPress) return;
  haptic();
  if (ringPress.ok) {
    clearTimeout(ringPress.timer);
    if (ringPress.held) send({ t: "hold", k: "DPAD_CENTER", d: "end" });
    else key("DPAD_CENTER");
    okEl.classList.remove("on", "long");
  } else {
    ringPress.rep.stop();
    wedge.classList.remove("on");
  }
  ringPress = null;
}
ring.addEventListener("pointerup", ringEnd);
ring.addEventListener("pointercancel", ringEnd);
ring.addEventListener("contextmenu", (e) => e.preventDefault());

// ---------- touchpad ----------

const pad = $("touchpad");
let swipe = null;
const padDot = document.createElement("div");
padDot.className = "tp-dot";
padDot.hidden = true;
pad.appendChild(padDot);

function placeDot(e) {
  const r = pad.getBoundingClientRect();
  padDot.style.left = `${e.clientX - r.left}px`;
  padDot.style.top = `${e.clientY - r.top}px`;
}

pad.addEventListener("pointerdown", (e) => {
  if (e.button !== 0 || swipe) return;
  e.preventDefault();
  capture(pad, e);
  pad.classList.add("on");
  padDot.hidden = false;
  placeDot(e);
  swipe = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, moved: false, held: false };
  swipe.timer = setTimeout(() => {
    if (swipe.moved) return;
    swipe.held = true;
    send({ t: "hold", k: "DPAD_CENTER", d: "start" });
  }, LONG_OK_MS + 150);
});

pad.addEventListener("pointermove", (e) => {
  if (!swipe) return;
  placeDot(e);
  if (Math.hypot(e.clientX - swipe.x0, e.clientY - swipe.y0) > 12) {
    swipe.moved = true;
    clearTimeout(swipe.timer);
  }
  const dx = e.clientX - swipe.x;
  const dy = e.clientY - swipe.y;
  if (Math.max(Math.abs(dx), Math.abs(dy)) < SWIPE_STEP_PX) return;
  if (Math.abs(dx) > Math.abs(dy)) key(dx > 0 ? "DPAD_RIGHT" : "DPAD_LEFT");
  else key(dy > 0 ? "DPAD_DOWN" : "DPAD_UP");
  swipe.x = e.clientX;
  swipe.y = e.clientY;
});

function padEnd() {
  if (!swipe) return;
  clearTimeout(swipe.timer);
  haptic();
  if (swipe.held) send({ t: "hold", k: "DPAD_CENTER", d: "end" });
  else if (!swipe.moved) key("DPAD_CENTER");
  swipe = null;
  pad.classList.remove("on");
  padDot.hidden = true;
}
pad.addEventListener("pointerup", padEnd);
pad.addEventListener("pointercancel", padEnd);
pad.addEventListener("contextmenu", (e) => e.preventDefault());

// ---------- D-pad / swipe mode ----------

function setMode(mode) {
  const swipeMode = mode === "swipe";
  ring.hidden = swipeMode;
  pad.hidden = !swipeMode;
  ring.parentElement.classList.toggle("swipe", swipeMode);
  const btn = $("mode");
  btn.classList.toggle("active", swipeMode);
  btn.querySelector("use").setAttribute("href", swipeMode ? "#i-dpad" : "#i-swipe");
  btn.querySelector("span").textContent = swipeMode ? "Кнопки" : "Свайпы";
  try { localStorage.setItem("mode", mode); } catch (_) { /* private mode */ }
}

$("mode").addEventListener("click", () => {
  haptic();
  setMode(pad.hidden ? "swipe" : "dpad");
});

let savedMode = "dpad";
try { savedMode = localStorage.getItem("mode") || "dpad"; } catch (_) { /* private mode */ }
setMode(savedMode);

// ---------- apps ----------

async function loadApps() {
  try {
    const r = await fetch("api/apps");
    apps = await r.json();
  } catch (_) {
    return;
  }
  const box = $("apps");
  box.textContent = "";
  for (const a of apps.launch) {
    const b = document.createElement("button");
    b.className = "app-tile";
    b.dataset.app = a.id;
    b.style.setProperty("--tile", a.color);
    b.textContent = a.name;
    // click, not pointerdown: scrolling through the tiles must not launch anything
    b.addEventListener("click", () => {
      haptic();
      if (send({ t: "app", id: a.id })) toast(`Открываю ${a.name}`);
    });
    box.appendChild(b);
  }
  render();
}

// ---------- keyboard ----------

const kb = $("kb");
const kbInput = $("kb-input");
let kbTimer = 0;
let kbSent = "";

function openKb() {
  kb.hidden = false;
  $("kb-backdrop").hidden = false;
  kbInput.value = "";
  kbSent = "";
  kbInput.focus();
}

function closeKb() {
  kbInput.blur();
  kb.hidden = true;
  $("kb-backdrop").hidden = true;
}

// The TV inserts what it gets at its cursor rather than replacing the field, so send only
// the change: erase back to where the texts differ, then type the new rest.
function flushText() {
  clearTimeout(kbTimer);
  const now = Array.from(kbInput.value);
  const before = Array.from(kbSent);
  let same = 0;
  while (same < now.length && same < before.length && now[same] === before[same]) same++;
  for (let i = same; i < before.length; i++) key("DEL");
  if (now.length > same) send({ t: "text", v: now.slice(same).join("") });
  kbSent = kbInput.value;
}

$("kb-open").addEventListener("click", () => { haptic(); openKb(); });
$("kb-close").addEventListener("click", closeKb);
$("kb-backdrop").addEventListener("click", closeKb);
kbInput.addEventListener("input", () => {
  clearTimeout(kbTimer);
  kbTimer = setTimeout(flushText, 120);
});
$("kb-clear").addEventListener("click", () => {
  kbInput.value = "";
  flushText();
  kbInput.focus();
});
kb.addEventListener("submit", (e) => {
  e.preventDefault();
  flushText();
  key("ENTER");
  kbInput.value = "";
  kbSent = "";
  closeKb();
});

// ---------- pairing ----------

$("pair-start").addEventListener("click", () => {
  haptic();
  $("pair-code").value = "";
  send({ t: "pair_start" });
});
$("pair-step2").addEventListener("submit", (e) => {
  e.preventDefault();
  const code = $("pair-code").value.trim().toUpperCase();
  if (code.length !== 6) { toast("Код — 6 символов"); return; }
  send({ t: "pair_finish", code });
});
$("pair-cancel").addEventListener("click", () => send({ t: "pair_cancel" }));

// ---------- start ----------

connect();
loadApps();
