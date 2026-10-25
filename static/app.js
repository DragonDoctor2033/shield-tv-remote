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
  stopVoice(false);
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
    else if (m.t === "error") { toast(m.msg); if (voice && !voice.listening) stopVoice(); }
    else if (m.t === "voice") voiceState(m.state);
    else if (m.t === "info") toast(m.msg, 8000);
  };
  ws.onclose = () => {
    stopVoice(false);
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
  if (document.hidden) { hiddenAt = Date.now(); stopVoice(); return; }
  const wasAway = hiddenAt && Date.now() - hiddenAt > 3000;
  hiddenAt = 0;
  if (ws && ws.readyState === WebSocket.CONNECTING) return;
  if (!ws || ws.readyState !== WebSocket.OPEN || wasAway) connect();
});
window.addEventListener("pageshow", (e) => { if (e.persisted) connect(); });

// ---------- feedback ----------

// Android vibrates on request; callers tick when the finger lifts.
const haptic = navigator.vibrate ? () => navigator.vibrate(10) : () => {};

// Safari has no vibration API, but iOS ticks when a real tap toggles a "switch" checkbox,
// also through its <label>. A script calling .click() no longer counts (iOS 26), so each
// control carries an invisible label for one hidden switch and the finger toggles it itself.
// The switch sits outside the controls, so its own click doesn't bubble into their handlers.
const tickSwitch = document.createElement("input");
tickSwitch.type = "checkbox";
tickSwitch.setAttribute("switch", "");
tickSwitch.id = "tick";
tickSwitch.hidden = true;
tickSwitch.tabIndex = -1;
document.body.appendChild(tickSwitch);

function tickable(el) {
  if (navigator.vibrate) return;
  const label = document.createElement("label");
  label.className = "tick";
  label.htmlFor = tickSwitch.id;
  label.ariaHidden = "true";
  el.appendChild(label);
}

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
  if (online && voice) sub.textContent = "Слушаю…";
  $("voice").classList.toggle("listening", !!voice);

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

// Touch already sticks to the element it started on. Capturing it on the control would
// retarget the release, and then the tap never reaches the tick label inside.
function capture(el, e) {
  if (e.pointerType !== "mouse") return;
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

// Phones that saved the old http:// address move to the https one (the microphone needs it),
// but only if they can reach it: a phone whose DNS skips the home resolver stays on http
// and keeps a working remote, just without voice.
async function moveToHttps(url) {
  if (location.protocol !== "http:" || !url) return;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    await fetch(new URL("api/apps", url), { mode: "no-cors", cache: "no-store", signal: ctrl.signal });
  } catch (_) {
    return;
  } finally {
    clearTimeout(timer);
  }
  location.replace(new URL(location.pathname + location.search, url));
}

async function loadApps() {
  try {
    const r = await fetch("api/apps");
    apps = await r.json();
  } catch (_) {
    return;
  }
  moveToHttps(apps.https_url);
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
    tickable(b);
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

// ---------- voice ----------

// The Shield takes 16-bit mono PCM at 8 kHz. The phone's audio context runs at its own rate
// (48 kHz on an iPhone, but never assumed), so the page filters and decimates it itself.
const VOICE_RATE = 8000;
const VOICE_FRAME = 800; // samples per message: 100 ms
const VOICE_MAX_MS = 12000;
let voice = null;

// Streaming windowed-sinc resampler to VOICE_RATE: push(Float32Array) returns the new samples
// as Int16Array. Its low-pass edge is 0.46 of the output rate, so tones above ~3.7 kHz are
// cut instead of folding back into the voice band as aliasing.
function makeResampler(inRate) {
  const ratio = inRate / VOICE_RATE;
  const wide = Math.max(ratio, 1);
  const cut = 0.46 / wide; // low-pass edge, cycles per input sample
  const half = Math.ceil(12 * wide); // kernel half-width, input samples
  const STEPS = 64; // kernel table entries per input sample
  const table = new Float32Array(half * STEPS + 2);
  for (let i = 0; i < table.length; i++) {
    const x = i / STEPS;
    const a = 2 * Math.PI * cut * x;
    const t = Math.min(x / half, 1);
    const win = 0.42 + 0.5 * Math.cos(Math.PI * t) + 0.08 * Math.cos(2 * Math.PI * t); // Blackman
    table[i] = 2 * cut * (x ? Math.sin(a) / a : 1) * win;
  }
  let buf = new Float32Array(0);
  let pos = 0; // where the next output sample sits in buf, in input samples
  return (input) => {
    const all = new Float32Array(buf.length + input.length);
    all.set(buf);
    all.set(input, buf.length);
    buf = all;
    const out = new Int16Array(Math.ceil((buf.length - pos) / ratio) + 1);
    let n = 0;
    while (pos + half < buf.length) {
      let sum = 0;
      for (let i = Math.max(0, Math.ceil(pos - half)); i <= pos + half; i++) {
        const d = Math.abs(i - pos) * STEPS;
        const k = Math.floor(d);
        sum += buf[i] * (table[k] + (table[k + 1] - table[k]) * (d - k));
      }
      out[n++] = Math.max(-32768, Math.min(32767, Math.round(sum * 32767)));
      pos += ratio;
    }
    const drop = Math.max(0, Math.ceil(pos - half));
    buf = buf.slice(drop);
    pos -= drop;
    return out.subarray(0, n);
  };
}

// Runs on the audio thread: hands the mic over in 50 ms pieces.
const VOICE_TAP = `
class VoiceTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(Math.round(sampleRate / 20));
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0][0];
    for (let i = 0; ch && i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice()); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("voice-tap", VoiceTap);
`;

async function openTap(v) {
  if (v.ctx.audioWorklet) {
    try {
      const url = URL.createObjectURL(new Blob([VOICE_TAP], { type: "text/javascript" }));
      try { await v.ctx.audioWorklet.addModule(url); } finally { URL.revokeObjectURL(url); }
      const node = new AudioWorkletNode(v.ctx, "voice-tap", { channelCount: 1, channelCountMode: "explicit" });
      node.port.onmessage = (e) => voiceFeed(v, e.data);
      return node;
    } catch (_) { /* worklet refused: use the old node below */ }
  }
  const node = v.ctx.createScriptProcessor(2048, 1, 1);
  node.onaudioprocess = (e) => voiceFeed(v, e.inputBuffer.getChannelData(0));
  return node;
}

function voiceFeed(v, floats) {
  if (voice !== v) return;
  v.got = true;
  const pcm = v.resample(floats);
  v.queue.push(pcm);
  v.queued += pcm.length;
  if (v.queued >= VOICE_FRAME) voiceFlush(v);
}

function voiceFlush(v) {
  if (!v.queued) return;
  const frame = new Int16Array(v.queued);
  let at = 0;
  for (const part of v.queue) { frame.set(part, at); at += part.length; }
  v.queue = [];
  v.queued = 0;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(frame.buffer);
}

// Let go of the mic and the audio context; stopping every track clears iOS's orange dot.
function release(v) {
  for (const node of [v.source, v.tap]) {
    try { node.disconnect(); } catch (_) { /* never connected */ }
  }
  if (v.tap && v.tap.port) v.tap.port.onmessage = null;
  if (v.stream) for (const track of v.stream.getTracks()) track.stop();
  try { Promise.resolve(v.ctx.close()).catch(() => {}); } catch (_) { /* already closed */ }
}

async function startVoice() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    toast("Голосовой ввод работает только по https-адресу пульта");
    return;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    toast("Нет связи с пультом на Pi");
    return;
  }
  const v = { queue: [], queued: 0, started: false, listening: false, got: false };
  voice = v;
  render();
  try {
    // This much runs inside the tap: iOS starts audio and the mic only from a user activation.
    v.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const resumed = v.ctx.resume().catch(() => {});
    const media = navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    v.stream = await media;
    await resumed;
    if (voice !== v) { release(v); return; } // tapped again while the permission prompt was up
    v.resample = makeResampler(v.ctx.sampleRate);
    v.source = v.ctx.createMediaStreamSource(v.stream);
    v.tap = await openTap(v);
    if (voice !== v) { release(v); return; }
    if (!send({ t: "voice", d: "start" })) { stopVoice(); return; }
    v.started = true;
    v.source.connect(v.tap);
    v.tap.connect(v.ctx.destination); // it outputs silence; some browsers only run connected nodes
    v.timer = setTimeout(() => stopVoice(), VOICE_MAX_MS);
    v.watch = setTimeout(() => {
      if (v.got) return;
      toast("Микрофон не отвечает");
      stopVoice();
    }, 2500);
  } catch (err) {
    if (voice !== v) { release(v); return; }
    toast(err && err.name === "NotAllowedError" ? "Нет доступа к микрофону. Разреши его в настройках браузера" : "Не удалось включить микрофон", 4000);
    stopVoice();
  }
}

// notify=false when the server already ended the session (or the socket is gone).
function stopVoice(notify = true) {
  const v = voice;
  if (!v) return;
  voice = null;
  clearTimeout(v.timer);
  clearTimeout(v.watch);
  if (v.started && notify) {
    voiceFlush(v);
    send({ t: "voice", d: "end" });
  }
  release(v);
  render();
}

function voiceState(s) {
  if (!voice) return;
  if (s === "listening") voice.listening = true;
  else if (s === "done" && voice.listening) stopVoice(false); // the server's time limit or a lost TV
}

// Start before the haptic: the mic and the audio context are requested inside this tap.
$("voice").addEventListener("click", () => {
  if (voice) stopVoice();
  else startVoice();
  haptic();
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

// Not the keyboard's submit button: a label inside it would take the tap instead of the form.
for (const el of document.querySelectorAll(
  "[data-key], #power, #mode, #kb-open, #voice, #ring, #touchpad, #pair-start, #kb-clear")) {
  tickable(el);
}

connect();
loadApps();
