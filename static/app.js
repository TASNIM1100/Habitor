/* Habitor front-end. Vanilla JS on top of htmx, no build step.
   Mobile-first: themes, haptics, bottom sheets (Android back closes them), pull-to-refresh,
   ripple + press feedback, wake lock for focus sessions, offline banner, install prompt,
   QR + NFC checkpoints and friend codes, camera, GPS, timer, maps, recap export, push. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const nfcSupported = "NDEFReader" in window;
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } },
  };
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  /* ------------------------------------------------------------ preferences: sound + haptics */
  const prefs = {
    get sound() { return store.get("habitor:muted", "1") === "0"; },
    set sound(on) { store.set("habitor:muted", on ? "0" : "1"); },
    get haptics() { return store.get("habitor:haptics", "1") === "1"; },
    set haptics(on) { store.set("habitor:haptics", on ? "1" : "0"); },
  };

  function haptic(pattern = 8) {
    if (!prefs.haptics || !navigator.vibrate) return;
    try { navigator.vibrate(pattern); } catch { /* unsupported */ }
  }

  const Sfx = {
    ctx: null,
    ensure() {
      if (!this.ctx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return null;
        this.ctx = new AC();
      }
      if (this.ctx.state === "suspended") this.ctx.resume();
      return this.ctx;
    },
    tone(freq, start, dur, type = "triangle", vol = 0.06, slideTo = null) {
      const ctx = this.ctx, t0 = ctx.currentTime + start;
      const osc = ctx.createOscillator(), gain = ctx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, t0);
      if (slideTo) osc.frequency.exponentialRampToValueAtTime(slideTo, t0 + dur);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(vol, t0 + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t0);
      osc.stop(t0 + dur + 0.02);
    },
    sounds: {
      checkin(s) { [660, 880, 1320].forEach((f, i) => s.tone(f, i * 0.07, 0.18)); },
      levelup(s) { [523, 659, 784, 1047].forEach((f, i) => s.tone(f, i * 0.1, 0.32, "triangle", 0.07)); },
      coin(s) { s.tone(988, 0, 0.08); s.tone(1319, 0.08, 0.25); },
      vote(s) { s.tone(740, 0, 0.09, "sine", 0.05); },
      taunt(s) { s.tone(520, 0, 0.25, "triangle", 0.05, 330); },
      boss(s) { s.tone(140, 0, 0.25, "square", 0.04, 70); },
      error(s) { s.tone(300, 0, 0.12, "sine", 0.05); s.tone(220, 0.13, 0.2, "sine", 0.05); },
      slide(s) { s.tone(700, 0, 0.4, "triangle", 0.05, 200); },
      thud(s) { s.tone(110, 0, 0.25, "sine", 0.08, 60); },
      wail(s) { s.tone(600, 0, 0.5, "triangle", 0.04, 350); },
      bawk(s) { [900, 1200, 900].forEach((f, i) => s.tone(f, i * 0.09, 0.07, "triangle", 0.04)); },
      blip(s) { s.tone(880, 0, 0.1, "sine", 0.05); s.tone(660, 0.12, 0.14, "sine", 0.05); },
    },
    play(name) {
      if (!prefs.sound || !this.sounds[name] || !this.ensure()) return;
      try { this.sounds[name](this); } catch { /* best-effort */ }
    },
  };

  function syncPrefControls() {
    $$('[data-pref="sound"]').forEach((c) => { c.checked = prefs.sound; });
    $$('[data-pref="haptics"]').forEach((c) => { c.checked = prefs.haptics; });
  }
  document.addEventListener("change", (e) => {
    const c = e.target.closest("[data-pref]");
    if (!c) return;
    prefs[c.dataset.pref] = c.checked;
    if (c.dataset.pref === "sound" && c.checked) Sfx.play("coin");
    if (c.dataset.pref === "haptics" && c.checked) haptic([10, 40, 10]);
  });

  /* ------------------------------------------------------------ themes */
  function applyTheme(key, color) {
    document.documentElement.dataset.theme = key;
    const meta = $('meta[name="theme-color"]');
    if (meta && color) meta.content = color;
    document.cookie = `habitor_theme=${key}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;
    $$("[data-theme-value]").forEach((b) => {
      const on = b.dataset.themeValue === key;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }
  document.addEventListener("click", (e) => {
    const opt = e.target.closest("[data-theme-form] [data-theme-value]");
    if (!opt) return;
    e.preventDefault();
    applyTheme(opt.dataset.themeValue, opt.dataset.themeColor);
    haptic(12);
    toast(`${opt.title.split(" —")[0]} theme on.`, "good");
  });

  /* ------------------------------------------------------------ toasts + effects */
  function toast(text, kind = "good") {
    const root = $("#toasts");
    if (!root) return;
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.setAttribute("role", "status");
    el.textContent = text;
    root.appendChild(el);
  }
  const toastRoot = $("#toasts");
  if (toastRoot) {
    new MutationObserver((muts) => muts.forEach((m) => m.addedNodes.forEach((n) => {
      if (n.nodeType !== 1) return;
      setTimeout(() => n.remove(), 4300);
      // swipe sideways to dismiss
      let x0 = null;
      n.addEventListener("pointerdown", (ev) => { x0 = ev.clientX; });
      n.addEventListener("pointermove", (ev) => { if (x0 !== null) n.style.transform = `translateX(${ev.clientX - x0}px)`; });
      n.addEventListener("pointerup", (ev) => {
        if (x0 !== null && Math.abs(ev.clientX - x0) > 80) n.remove(); else n.style.transform = "";
        x0 = null;
      });
    }))).observe(toastRoot, { childList: true });
  }

  function restartAnim(el, cls) {
    if (!el) return;
    el.classList.remove(cls);
    void el.offsetWidth;
    el.classList.add(cls);
  }

  function confettiBurst(power = 1) {
    if (reducedMotion || typeof window.confetti !== "function") return;
    window.confetti({
      particleCount: Math.round(80 * power), spread: 70, startVelocity: 34, origin: { y: 0.65 }, shapes: ["square"],
      colors: [cssVar("--primary"), cssVar("--accent"), cssVar("--surface-3"), cssVar("--success")], disableForReducedMotion: true,
    });
  }

  function showLevelUp(level) {
    const banner = $("[data-levelup]");
    if (!banner) return;
    $("[data-levelup-num]", banner).textContent = `Level ${level}`;
    restartAnim(banner, "show");
    setTimeout(() => banner.classList.remove("show"), 2700);
    confettiBurst(1.3);
    haptic([30, 60, 30, 60, 80]);
  }

  function showStamp(text) {
    const stamp = $("[data-stamp]");
    if (!stamp) return;
    stamp.textContent = text;
    restartAnim(stamp, "show");
    setTimeout(() => stamp.classList.remove("show"), 1900);
  }

  function showTaunt(t) {
    const dlg = $("[data-taunt-dialog]");
    if (!dlg) return;
    $("[data-taunt-from]", dlg).textContent = t.from;
    $("[data-taunt-sticker]", dlg).textContent = t.sticker;
    $("[data-taunt-text]", dlg).textContent = `“${t.text}”`;
    openDialog(dlg);
    Sfx.play(t.sound || "taunt");
    haptic([80, 50, 80]);
  }

  function showProveIt(info) {
    if (location.pathname === "/prove") return;
    const key = `habitor:prove:${new Date().toDateString()}`;
    try { if (sessionStorage.getItem(key)) return; sessionStorage.setItem(key, "1"); } catch { /* ignore */ }
    const dlg = $("[data-prove-dialog]");
    if (!dlg) return;
    startCountdown($("[data-prove-countdown]", dlg), info.seconds, () => dlg.open && dlg.close());
    openDialog(dlg);
    Sfx.play("levelup");
    haptic([120, 60, 120]);
  }

  function applyFx(d) {
    if (!d) return;
    (d.toasts || []).forEach((t) => toast(t.text, t.kind));
    if (d.sfx) Sfx.play(d.sfx);
    if (d.sfx === "checkin") haptic([18, 50, 28]);
    if (d.sfx === "error") haptic([60, 40, 60]);
    if (d.levelUp) setTimeout(() => showLevelUp(d.levelUp), 300);
    if (d.stamp) setTimeout(() => showStamp(d.stamp), d.levelUp ? 2900 : 250);
    if (d.shake && !reducedMotion) restartAnim($("main"), "shake");
    if (d.confetti) confettiBurst();
    if (d.sfx === "checkin" && !d.levelUp) confettiBurst(0.45);
    if (d.bossHit) setTimeout(() => { $$("[data-boss]").forEach((b) => restartAnim(b, "hit")); Sfx.play("boss"); }, 450);
    if (d.taunted) showTaunt(d.taunted);
    if (d.proveIt) showProveIt(d.proveIt);
  }
  document.addEventListener("habitor", (e) => applyFx(e.detail));

  /* ------------------------------------------------------------ dialogs as bottom sheets (Android back closes) */
  function openDialog(dlg) {
    if (!dlg || dlg.open) return;
    dlg.showModal();
    history.pushState({ habitorDialog: true }, "");
    dlg.dataset.pushed = "1";
  }
  window.addEventListener("popstate", () => {
    const open = $$("dialog[open]").pop();
    if (open) { open.dataset.pushed = ""; open.close(); }
  });
  document.addEventListener("close", (e) => {
    const d = e.target;
    if (d instanceof HTMLDialogElement && d.dataset.pushed) { d.dataset.pushed = ""; history.back(); }
  }, true);
  // Tap on the dimmed backdrop closes a sheet.
  document.addEventListener("click", (e) => {
    if (e.target instanceof HTMLDialogElement && e.target.open) {
      const r = e.target.getBoundingClientRect();
      if (e.clientY < r.top || e.clientY > r.bottom || e.clientX < r.left || e.clientX > r.right) e.target.close();
    }
  });

  // Quick check-in sheet behind the centre tab button.
  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-quick-open]");
    if (opener) {
      const sheet = $("#quick-sheet");
      if (!sheet) return;
      openDialog(sheet);
      if (window.htmx) window.htmx.trigger(document.body, "quick-open");
      haptic(10);
      return;
    }
    const link = e.target.closest("[data-quick-link]");
    if (link) {
      const target = new URL(link.href, location.href);
      const sheet = link.closest("dialog");
      if (location.pathname === "/" && $(target.hash)) {
        e.preventDefault();
        if (sheet) sheet.close();
        setTimeout(() => focusQuest(target.hash), 120);
      } else if (sheet) {
        sheet.dataset.pushed = "";  // let the browser navigate instead of going back
      }
    }
  });

  function focusQuest(hash) {
    const el = $(hash);
    if (!el) return;
    el.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    restartAnim(el, "is-highlight");
  }

  /* ------------------------------------------------------------ press feedback: ripple + haptic tick */
  document.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".btn, .quick-action, .tab-fab, .proof-tab, .seg-item, .theme-option, .option, .reaction, .emoji-choice, .hub-tile, .quick-item");
    if (!el || el.disabled) return;
    if (e.pointerType === "touch") haptic(6);
    if (reducedMotion || !el.matches(".btn, .quick-action, .tab-fab, .hub-tile, .quick-item, .option")) return;
    const r = el.getBoundingClientRect();
    const size = Math.max(r.width, r.height);
    const dot = document.createElement("span");
    dot.className = "ripple";
    dot.style.cssText = `width:${size}px;height:${size}px;left:${e.clientX - r.left - size / 2}px;top:${e.clientY - r.top - size / 2}px`;
    if (getComputedStyle(el).position === "static") el.style.position = "relative";
    el.style.overflow = "hidden";
    el.appendChild(dot);
    setTimeout(() => dot.remove(), 600);
  }, { passive: true });

  /* ------------------------------------------------------------ chrome: loading bar, offline banner, install */
  const loadbar = $("#loadbar");
  let inflight = 0;
  const quietRequest = (elt) => elt && elt.matches && elt.matches("#poller, [data-poll]");
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (quietRequest(e.detail.elt) || !loadbar) return;
    inflight += 1;
    loadbar.classList.remove("is-done");
    loadbar.classList.add("is-on");
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    if (quietRequest(e.detail.elt) || !loadbar) return;
    inflight = Math.max(0, inflight - 1);
    if (!inflight) {
      loadbar.classList.replace("is-on", "is-done");
      setTimeout(() => loadbar.classList.remove("is-done"), 700);
    }
  });

  const offline = $("[data-offline]");
  const syncOnline = () => { if (offline) offline.hidden = navigator.onLine; };
  window.addEventListener("online", () => { syncOnline(); toast("Back online.", "good"); });
  window.addEventListener("offline", syncOnline);
  syncOnline();

  let installPrompt = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    installPrompt = e;
    $$("[data-install]").forEach((b) => { b.hidden = false; });
    if (store.get("habitor:install-dismissed", "0") !== "1") $$("[data-install-card]").forEach((c) => { c.hidden = false; });
  });
  window.addEventListener("appinstalled", () => { $$("[data-install-card]").forEach((c) => { c.hidden = true; }); toast("Habitor installed.", "good"); });
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {}));
  }

  /* ------------------------------------------------------------ pull to refresh (Today, Feed, Leaderboards) */
  function initPullToRefresh(main) {
    const ind = $(".ptr-indicator", main);
    if (!ind || !("ontouchstart" in window)) return;
    let y0 = null, dy = 0;
    main.addEventListener("touchstart", (e) => {
      y0 = window.scrollY <= 0 && !$("dialog[open]") ? e.touches[0].clientY : null;
      dy = 0;
    }, { passive: true });
    main.addEventListener("touchmove", (e) => {
      if (y0 === null) return;
      dy = Math.max(0, e.touches[0].clientY - y0);
      if (!dy) return;
      const pull = Math.min(dy * 0.5, 90);
      ind.classList.add("is-pulling");
      ind.style.transform = `translateY(${pull - 40}px) rotate(${pull * 4}deg)`;
    }, { passive: true });
    main.addEventListener("touchend", () => {
      if (y0 === null) return;
      if (dy * 0.5 > 70) {
        ind.classList.add("is-loading");
        haptic(15);
        location.reload();
      } else {
        ind.classList.remove("is-pulling");
        ind.style.transform = "";
      }
      y0 = null;
    });
  }

  /* ------------------------------------------------------------ number count-up + progress rings */
  function countUp(el) {
    const target = Number(el.dataset.count);
    if (reducedMotion || !Number.isFinite(target) || target <= 0) return;
    const start = performance.now(), dur = 700, fmt = new Intl.NumberFormat();
    const step = (now) => {
      const t = Math.min(1, (now - start) / dur);
      el.textContent = fmt.format(Math.round(target * (1 - Math.pow(1 - t, 3))));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  function animateRing(circle) {
    if (reducedMotion) return;
    const final = circle.getAttribute("stroke-dashoffset");
    circle.style.transition = "none";
    circle.setAttribute("stroke-dashoffset", circle.dataset.ring);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      circle.style.transition = "";
      circle.setAttribute("stroke-dashoffset", final);
    }));
  }

  /* ------------------------------------------------------------ helpers */
  function getPosition() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error("no geolocation"));
      navigator.geolocation.getCurrentPosition(resolve, reject, { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
    });
  }

  function startCountdown(el, seconds, onDone) {
    if (!el) return;
    const end = Date.now() + seconds * 1000;
    const tick = () => {
      const left = Math.max(0, Math.round((end - Date.now()) / 1000));
      el.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
      if (left <= 0) { el.textContent = "0:00"; if (onDone) onDone(); return; }
      setTimeout(tick, 1000);
    };
    tick();
  }

  function post(url, data) {
    const body = new FormData();
    Object.entries(data || {}).forEach(([k, v]) => body.append(k, v));
    return fetch(url, { method: "POST", body, headers: { "X-CSRF-Token": csrf() }, credentials: "same-origin" });
  }

  function htmxPost(url, values, opts = {}) {
    if (!window.htmx) return;
    window.htmx.ajax("POST", url, { values, swap: "none", headers: { "X-CSRF-Token": csrf() }, ...opts });
  }

  /* ------------------------------------------------------------ verification tabs */
  function initProofTabs(container) {
    const tabs = $$(":scope > .proof-tabs > [data-proof-tab]", container);
    if (!tabs.length) return;
    const select = (tab) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute("aria-selected", String(on));
        t.tabIndex = on ? 0 : -1;
        const panel = $(`[data-proof-panel="${t.dataset.proofTab}"]`, container);
        if (panel) panel.hidden = !on;
      });
    };
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => select(t));
      t.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        next.focus();
        select(next);
      });
    });
    select(tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0]);
  }

  /* ------------------------------------------------------------ camera (live photo only) */
  const streams = new Set();
  function stopStreams() { streams.forEach((s) => s.getTracks().forEach((t) => t.stop())); streams.clear(); }

  function initCamera(el) {
    const video = $(".camera-video", el), img = $(".camera-preview", el), placeholder = $(".camera-placeholder", el);
    const startBtn = $("[data-cam-start]", el), snapBtn = $("[data-cam-snap]", el), retakeBtn = $("[data-cam-retake]", el);
    const input = $("[data-cam-input]", el), fallback = $(".camera-fallback", el);
    const form = el.closest("form"), submit = form && $("[data-needs-photo]", form);
    let stream = null;
    const setReady = (ok) => { if (submit) submit.disabled = !ok; el.classList.toggle("has-photo", ok); };
    const stop = () => { if (stream) { stream.getTracks().forEach((t) => t.stop()); streams.delete(stream); stream = null; } };
    const useFallback = (msg) => { fallback.hidden = false; startBtn.hidden = true; if (msg) toast(msg, "pink"); };
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof DataTransfer === "undefined") useFallback();

    startBtn.addEventListener("click", async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 } }, audio: false });
        streams.add(stream);
        video.srcObject = stream;
        video.hidden = false;
        await video.play();
        placeholder.hidden = true;
        img.hidden = true;
        startBtn.hidden = true;
        snapBtn.hidden = false;
        retakeBtn.hidden = true;
      } catch {
        useFallback("Camera access was blocked — use your phone camera instead.");
      }
    });

    snapBtn.addEventListener("click", () => {
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return;
      const scale = Math.min(1, 1280 / Math.max(w, h));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(w * scale);
      canvas.height = Math.round(h * scale);
      canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => {
        if (!blob) return;
        const dt = new DataTransfer();
        dt.items.add(new File([blob], `proof-${Date.now()}.jpg`, { type: "image/jpeg" }));
        input.files = dt.files;
        img.src = URL.createObjectURL(blob);
        img.hidden = false;
        video.hidden = true;
        stop();
        snapBtn.hidden = true;
        retakeBtn.hidden = false;
        setReady(true);
        Sfx.play("vote");
        haptic(20);
      }, "image/jpeg", 0.88);
    });

    retakeBtn.addEventListener("click", () => {
      input.value = "";
      img.hidden = true;
      setReady(false);
      retakeBtn.hidden = true;
      startBtn.hidden = false;
      startBtn.click();
    });

    input.addEventListener("change", () => {
      const f = input.files && input.files[0];
      if (!f) return setReady(false);
      img.src = URL.createObjectURL(f);
      img.hidden = false;
      placeholder.hidden = true;
      setReady(true);
    });
  }

  /* ------------------------------------------------------------ GPS-gated htmx forms */
  document.addEventListener("htmx:confirm", (e) => {
    const form = e.detail.elt;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-geo-form")) return;
    const lat = form.querySelector('[name="lat"]'), lon = form.querySelector('[name="lon"]');
    if (!lat || lat.value) return;
    e.preventDefault();
    const status = $(".geo-status", form);
    const required = form.hasAttribute("data-geo-required");
    if (status) status.textContent = "Getting your location…";
    getPosition().then((pos) => {
      lat.value = pos.coords.latitude.toFixed(6);
      lon.value = pos.coords.longitude.toFixed(6);
      if (status) status.textContent = `Location found (±${Math.round(pos.coords.accuracy)} m).`;
      e.detail.issueRequest(true);
    }).catch(() => {
      if (required) {
        if (status) status.textContent = "Location access is needed. Allow it in your browser and try again.";
        toast("Allow location access to check in.", "bad");
        Sfx.play("error");
        haptic([60, 40, 60]);
      } else {
        e.detail.issueRequest(true); // GPS was only a bonus for photo proof
      }
    });
  });

  /* ------------------------------------------------------------ QR + NFC */
  function setStatus(el, text) { const s = $(".qr-status, .nfc-status", el); if (s) s.textContent = text; }

  // A scanned/tapped Habitor URL → the right action for this widget's mode (checkin | reprove | friend | auto).
  function handleCode(el, text) {
    const mode = el.dataset.mode || "checkin";
    const isInvite = /\/invite\/[A-Za-z0-9_-]+/.test(text);
    if (mode === "friend" || (mode === "auto" && isInvite)) {
      if (!isInvite) { setStatus(el, "That isn't a Habitor friend code."); Sfx.play("error"); haptic([60, 40, 60]); return; }
      setStatus(el, "Connecting…");
      htmxPost("/friends/connect", { code: text }, { source: el });
      return;
    }
    const kind = text.includes("/nfc/") ? "nfc" : text.includes("/qr/") ? "qr" : null;
    if (!kind) { setStatus(el, "That isn't a Habitor code."); Sfx.play("error"); haptic([60, 40, 60]); return; }
    const token = text.split(`/${kind}/`).pop().split("?")[0];
    setStatus(el, "Verifying…");
    if (mode === "reprove") {
      htmxPost(`/checkins/${el.dataset.reprove}/reprove`, { method: kind, [`${kind}_token`]: token },
        { source: el, target: el.dataset.target || `#quest-${el.dataset.habit}`, swap: "outerHTML" });
    } else {
      htmxPost(`/${kind}/checkin`, { token }, { source: el });
    }
  }

  function initQrScan(el) {
    const btn = $("[data-qr-start]", el), label = $("span", btn), reader = $(".qr-reader", el);
    const idle = label.textContent;
    let scanner = null;
    const reset = async () => {
      if (scanner) { try { await scanner.stop(); } catch { /* already stopped */ } try { scanner.clear(); } catch { /* noop */ } }
      scanner = null;
      label.textContent = idle;
    };
    btn.addEventListener("click", async () => {
      if (scanner) return reset();
      if (!window.Html5Qrcode) { toast("The QR scanner couldn't load. Check your connection.", "bad"); return; }
      scanner = new window.Html5Qrcode(reader.id);
      label.textContent = "Stop camera";
      setStatus(el, "Point your camera at the code.");
      try {
        await scanner.start({ facingMode: "environment" }, { fps: 10, qrbox: 240 }, async (text) => {
          await reset();
          Sfx.play("vote");
          haptic([20, 40, 20]);
          handleCode(el, text);
        }, () => {});
      } catch {
        await reset();
        setStatus(el, "Camera access was blocked. You can also scan with your phone's camera app.");
      }
    });
  }

  function decodeRecord(rec) {
    try {
      if (rec.recordType === "url" || rec.recordType === "absolute-url") return new TextDecoder().decode(rec.data);
      if (rec.recordType === "text") return new TextDecoder(rec.encoding || "utf-8").decode(rec.data);
    } catch { /* unreadable record */ }
    return "";
  }

  function initNfcScan(el) {
    const btn = $("[data-nfc-start]", el), label = $("span", btn), fallback = $("[data-nfc-fallback]", el);
    if (!nfcSupported) { el.classList.add("is-unsupported"); return; }
    btn.hidden = false;
    if (fallback) fallback.hidden = true;
    const idle = label.textContent;
    let ctrl = null;
    const reset = () => { if (ctrl) ctrl.abort(); ctrl = null; label.textContent = idle; };
    btn.addEventListener("click", async () => {
      if (ctrl) { reset(); setStatus(el, ""); return; }
      try {
        const reader = new window.NDEFReader();
        ctrl = new AbortController();
        await reader.scan({ signal: ctrl.signal });
        label.textContent = "Cancel";
        setStatus(el, "Ready — hold the back of your phone against the tag.");
        reader.onreading = (ev) => {
          const url = ev.message.records.map(decodeRecord).find((t) => /\/(nfc|qr|invite)\//.test(t));
          reset();
          if (!url) { setStatus(el, "This tag doesn't contain a Habitor link."); Sfx.play("error"); haptic([60, 40, 60]); return; }
          Sfx.play("vote");
          haptic([20, 40, 20]);
          handleCode(el, url);
        };
        reader.onreadingerror = () => setStatus(el, "Couldn't read that tag. Hold it still and try again.");
      } catch (err) {
        reset();
        setStatus(el, err && err.name === "NotAllowedError" ? "NFC permission was denied." : "NFC isn't available — check that NFC is turned on.");
      }
    });
    if (location.hash === "#nfc" && el.closest("#nfc")) btn.focus();
  }

  function initNfcWrite(el) {
    const btn = $("[data-nfc-write-start]", el), fallback = $("[data-nfc-fallback]", el);
    if (!nfcSupported) { el.classList.add("is-unsupported"); return; }
    btn.hidden = false;
    if (fallback) fallback.hidden = true;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      setStatus(el, "Hold a writable NFC tag against your phone…");
      try {
        const writer = new window.NDEFReader();
        await writer.write({ records: [{ recordType: "url", data: el.dataset.url }] });
        setStatus(el, "Tag written. Tap it with your phone to test.");
        toast("NFC tag written.", "good");
        Sfx.play("coin");
        haptic([20, 40, 20]);
      } catch (err) {
        setStatus(el, `Couldn't write the tag${err && err.message ? `: ${err.message}` : "."}`);
        Sfx.play("error");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function initQrRender(el) {
    if (typeof window.qrcode !== "function") return;
    const qr = window.qrcode(0, "M");
    qr.addData(el.dataset.text);
    qr.make();
    el.innerHTML = qr.createSvgTag(8, 0);
  }

  /* ------------------------------------------------------------ maps */
  const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const TILE_ATTR = "© OpenStreetMap contributors";
  const pinIcon = () => window.L.divIcon({ className: "map-pin", html: "📍", iconSize: [32, 32], iconAnchor: [16, 30] });

  function initMapPicker(el) {
    if (!window.L) return;
    const color = cssVar("--primary");
    const latIn = $("[data-picker-lat]"), lonIn = $("[data-picker-lon]");
    const lat0 = parseFloat(el.dataset.lat), lon0 = parseFloat(el.dataset.lon);
    const has = !Number.isNaN(lat0) && !Number.isNaN(lon0);
    const map = window.L.map(el).setView(has ? [lat0, lon0] : [20, 0], has ? 16 : 2);
    window.L.tileLayer(TILE_URL, { maxZoom: 19, attribution: TILE_ATTR }).addTo(map);
    let marker = null, circle = null;
    const place = (lat, lon) => {
      latIn.value = lat.toFixed(6);
      lonIn.value = lon.toFixed(6);
      if (!marker) {
        marker = window.L.marker([lat, lon], { icon: pinIcon() }).addTo(map);
        circle = window.L.circle([lat, lon], { radius: 150, color, weight: 2, fillOpacity: 0.14 }).addTo(map);
      } else {
        marker.setLatLng([lat, lon]);
        circle.setLatLng([lat, lon]);
      }
      haptic(10);
    };
    if (has) place(lat0, lon0);
    else getPosition().then((p) => map.setView([p.coords.latitude, p.coords.longitude], 16)).catch(() => {});
    map.on("click", (e) => place(e.latlng.lat, e.latlng.lng));
    const locate = $("[data-locate]");
    if (locate) locate.addEventListener("click", () => getPosition().then((p) => {
      place(p.coords.latitude, p.coords.longitude);
      map.setView([p.coords.latitude, p.coords.longitude], 17);
    }).catch(() => toast("Location access was blocked.", "bad")));
  }

  async function initConquestMap(el) {
    if (!window.L) return;
    const mine = cssVar("--primary");
    const map = window.L.map(el).setView([20, 0], 2);
    window.L.tileLayer(TILE_URL, { maxZoom: 19, attribution: TILE_ATTR }).addTo(map);
    try {
      const data = await (await fetch(el.dataset.src, { credentials: "same-origin" })).json();
      const layers = data.tiles.map((t) => {
        const color = t.mine ? mine : t.color;
        const rect = window.L.rectangle(t.bounds, { color, weight: t.mine ? 2.5 : 1.5, fillColor: color, fillOpacity: t.mine ? 0.35 : 0.22 });
        const pop = document.createElement("div");
        pop.textContent = `${t.owner || "Unclaimed"} · ${t.owner_count} check-ins${t.mine ? "" : ` · you: ${t.my_count}`}`;
        return rect.bindPopup(pop).addTo(map);
      });
      if (data.home) map.setView(data.home, 14);
      else if (layers.length) map.fitBounds(window.L.featureGroup(layers).getBounds());
      else getPosition().then((p) => map.setView([p.coords.latitude, p.coords.longitude], 14)).catch(() => {});
    } catch {
      toast("Couldn't load the map.", "bad");
    }
  }

  /* ------------------------------------------------------------ focus timer (keeps the screen awake) */
  function initTimer(el) {
    const display = $("[data-timer-display]", el), status = $("[data-timer-status]", el), bar = $(".progress > span", el);
    const startBtn = $("[data-timer-start]", el), pauseBtn = $("[data-timer-pause]", el), resetBtn = $("[data-timer-reset]", el);
    const claim = $("[data-timer-claim]", el), sidInput = $("[data-timer-sid]", el);
    const still = $("[data-still-here]", el), stillYes = $("[data-still-yes]", el), stillCount = $("[data-still-count]", el);
    const minMinutes = Number(el.dataset.min) || 10;
    const proof = el.dataset.proof === "1";
    let target = minMinutes * 60, elapsed = 0, running = false, sid = null, last = 0, interval = null, nextPrompt = Infinity, promptDeadline = 0;
    let wakeLock = null;
    const holdAwake = async () => {
      try { if ("wakeLock" in navigator && !wakeLock) { wakeLock = await navigator.wakeLock.request("screen"); wakeLock.addEventListener("release", () => { wakeLock = null; }); } } catch { /* denied */ }
    };
    const letSleep = () => { if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; } };
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && running) holdAwake(); });

    const fmt = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
    const render = () => {
      display.textContent = fmt(Math.max(0, target - elapsed));
      if (bar) bar.style.width = `${Math.min(100, (100 * elapsed) / target)}%`;
    };
    const ping = () => sid && post(`/timer/${sid}/ping`).catch(() => {});
    const schedulePrompt = () => { nextPrompt = target >= 300 ? elapsed + 180 + Math.random() * 300 : Infinity; };
    const setRunning = (on) => {
      running = on;
      el.classList.toggle("running", on);
      el.classList.toggle("paused", !on && elapsed > 0 && elapsed < target);
      startBtn.hidden = on;
      pauseBtn.hidden = !on;
      if (on) holdAwake(); else letSleep();
      if (!on && interval) { clearInterval(interval); interval = null; }
    };
    const pause = (msg) => { setRunning(false); if (still.open) still.close(); status.textContent = msg || "Paused."; startBtn.textContent = "Resume"; };
    const finish = () => {
      setRunning(false);
      elapsed = target;
      render();
      ping();
      Sfx.play("levelup");
      haptic([40, 60, 40, 60, 120]);
      confettiBurst(0.5);
      const earned = proof && sid && target >= minMinutes * 60;
      status.textContent = earned ? "Session complete. Record your verified check-in below." : "Session complete. Nice focus.";
      if (earned) { sidInput.value = sid; claim.hidden = false; }
    };
    const tick = () => {
      const now = performance.now();
      const dt = (now - last) / 1000;
      last = now;
      if (still.open) {
        const left = Math.ceil((promptDeadline - Date.now()) / 1000);
        stillCount.textContent = Math.max(0, left);
        if (left <= 0) pause("Paused — we didn't hear back. Tap Resume to continue.");
        return;
      }
      elapsed += dt;
      if (elapsed >= nextPrompt && elapsed < target) {
        still.showModal();
        promptDeadline = Date.now() + 60000;
        Sfx.play("blip");
        haptic([100, 80, 100]);
      }
      if (elapsed >= target) finish(); else render();
    };

    $$("[data-timer-preset]", el).forEach((b) => b.addEventListener("click", () => {
      if (running || elapsed > 0) return;
      target = Number(b.dataset.timerPreset) * 60;
      $$("[data-timer-preset]", el).forEach((x) => x.classList.toggle("is-on", x === b));
      render();
    }));
    startBtn.addEventListener("click", async () => {
      if (!sid) {
        try {
          const res = await post("/timer/start", { habit_id: el.dataset.habit });
          if (!res.ok) throw new Error("start failed");
          sid = (await res.json()).sid;
        } catch {
          toast("Couldn't start the session.", "bad");
          return;
        }
        schedulePrompt();
      } else {
        ping();
      }
      last = performance.now();
      setRunning(true);
      interval = setInterval(tick, 250);
      status.textContent = target >= 300 ? "Focus. Screen stays on; you'll get an occasional “Still focusing?” check." : "Focus.";
      Sfx.play("coin");
    });
    pauseBtn.addEventListener("click", () => pause());
    resetBtn.addEventListener("click", () => {
      setRunning(false);
      elapsed = 0;
      sid = null;
      claim.hidden = true;
      startBtn.textContent = "Start";
      el.classList.remove("paused");
      status.textContent = "Ready when you are.";
      render();
    });
    stillYes.addEventListener("click", () => { still.close(); ping(); schedulePrompt(); Sfx.play("vote"); });
    render();
  }

  /* ------------------------------------------------------------ weekly recap */
  function initRecap(el) {
    const track = $("[data-recap-track]", el), slides = $$(".slide", track), dots = $("[data-recap-dots]", el);
    dots.innerHTML = slides.map(() => "<i></i>").join("");
    const dotEls = $$("i", dots);
    const current = () => Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
    const go = (i) => {
      const idx = Math.max(0, Math.min(slides.length - 1, i));
      track.scrollTo({ left: slides[idx].offsetLeft - track.offsetLeft - 4, behavior: reducedMotion ? "auto" : "smooth" });
      haptic(6);
    };
    const update = () => dotEls.forEach((d, i) => d.classList.toggle("on", i === current()));
    track.addEventListener("scroll", () => requestAnimationFrame(update), { passive: true });
    $("[data-recap-prev]", el).addEventListener("click", () => go(current() - 1));
    $("[data-recap-next]", el).addEventListener("click", () => go(current() + 1));
    track.tabIndex = 0;
    track.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight") go(current() + 1);
      if (e.key === "ArrowLeft") go(current() - 1);
    });
    track.addEventListener("click", (e) => {
      if (e.target.closest("button, a")) return;
      const rect = track.getBoundingClientRect();
      go(current() + (e.clientX - rect.left > rect.width / 2 ? 1 : -1));
    });
    $("[data-recap-download]", el).addEventListener("click", () => downloadRecap().catch(() => toast("Export failed.", "bad")));
    update();
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = src;
    });
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  async function downloadRecap() {
    const d = JSON.parse($("#recap-data").textContent);
    await Promise.all(['800 italic 40px "Barlow Condensed"', '700 40px "Barlow Condensed"', "500 32px Barlow"].map((f) => document.fonts.load(f).catch(() => {})));
    const primary = cssVar("--primary"), accent = cssVar("--accent"), onPrimary = cssVar("--on-primary");
    const W = 1080, H = 1920;
    const c = document.createElement("canvas");
    c.width = W;
    c.height = H;
    const ctx = c.getContext("2d");
    ctx.fillStyle = primary;
    ctx.fillRect(0, 0, W, H);
    ctx.save();  // race stripes
    ctx.globalAlpha = 0.07;
    ctx.fillStyle = onPrimary;
    ctx.translate(W / 2, H / 2);
    ctx.rotate(-Math.PI / 7.5);
    for (let x = -1600; x < 1600; x += 90) ctx.fillRect(x, -1400, 45, 2800);
    ctx.restore();
    ctx.fillStyle = accent;
    ctx.fillRect(0, H - 28, W, 28);
    ctx.textAlign = "center";
    const display = (size, color, value, y, x = W / 2) => { ctx.font = `800 italic ${size}px "Barlow Condensed", sans-serif`; ctx.fillStyle = color; ctx.fillText(String(value).toUpperCase(), x, y); };
    const body = (size, color, value, y, x = W / 2) => { ctx.font = `500 ${size}px Barlow, sans-serif`; ctx.fillStyle = color; ctx.fillText(value, x, y); };

    display(44, accent, "Habitor · weekly recap", 150);
    body(36, onPrimary, `${d.week_start}  →  ${d.week_end}`, 205);
    try {
      const av = await loadImage(d.avatar);
      ctx.save();
      ctx.beginPath();
      ctx.arc(W / 2, 400, 135, 0, Math.PI * 2);
      ctx.clip();
      ctx.drawImage(av, W / 2 - 135, 265, 270, 270);
      ctx.restore();
      ctx.strokeStyle = accent;
      ctx.lineWidth = 10;
      ctx.beginPath();
      ctx.arc(W / 2, 400, 140, 0, Math.PI * 2);
      ctx.stroke();
    } catch { /* optional */ }
    display(96, onPrimary, d.username, 640);
    display(46, accent, `Level ${d.level}`, 700);

    const stats = [
      ["Total XP", `+${d.xp}`], ["Check-ins", String(d.checkins)],
      ["Best streak", `${d.best_streak}d`], ["Friend rank", `#${d.rank_now}`],
      ["Taunts sent", String(d.taunts_sent)], ["Taunts received", String(d.taunts_received)],
    ];
    let y = 770;
    stats.forEach(([label, value], i) => {
      const col = i % 2, x = col === 0 ? 80 : W / 2 + 20, w = W / 2 - 100;
      ctx.fillStyle = "rgba(255,255,255,0.10)";
      roundRect(ctx, x, y, w, 170, 30);
      ctx.fill();
      ctx.textAlign = "left";
      body(30, onPrimary, label.toUpperCase(), y + 58, x + 36);
      display(76, col === 0 ? accent : onPrimary, value, y + 140, x + 36);
      ctx.textAlign = "center";
      if (col === 1) y += 194;
    });
    display(44, onPrimary, `Top habit: ${d.top_habit}`, y + 46);
    if (d.photo) {
      try {
        const ph = await loadImage(d.photo);
        const bw = 920, bh = 420, bx = (W - bw) / 2, by = y + 90;
        const s = Math.max(bw / ph.width, bh / ph.height);
        ctx.save();
        roundRect(ctx, bx, by, bw, bh, 34);
        ctx.clip();
        ctx.drawImage(ph, (ph.width - bw / s) / 2, (ph.height - bh / s) / 2, bw / s, bh / s, bx, by, bw, bh);
        ctx.restore();
      } catch { /* optional */ }
    }
    const blob = await new Promise((r) => c.toBlob(r, "image/png"));
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `habitor-recap-${d.week_start}.png`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("Recap image saved.", "good");
    haptic([20, 40, 20]);
  }

  /* ------------------------------------------------------------ push */
  function b64ToUint8(b64) {
    const pad = "=".repeat((4 - (b64.length % 4)) % 4);
    const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
  }

  async function subscribePush() {
    const key = document.body.dataset.vapid;
    if (!key || !("serviceWorker" in navigator) || !("PushManager" in window)) { toast("Push notifications aren't supported on this device.", "bad"); return; }
    if ((await Notification.requestPermission()) !== "granted") { toast("Notifications are blocked for this site.", "bad"); return; }
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(key) });
    const res = await fetch("/push/subscribe", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() }, body: JSON.stringify(sub),
    });
    toast(res.ok ? "Push notifications enabled." : "Couldn't enable push notifications.", res.ok ? "good" : "bad");
  }

  /* ------------------------------------------------------------ component wiring */
  function init(root) {
    const scope = root && root.querySelectorAll ? root : document;
    const each = (sel, fn) => {
      const list = $$(sel, scope);
      if (scope.matches && scope.matches(sel)) list.unshift(scope);
      list.forEach((el) => { if (el.dataset.ready) return; el.dataset.ready = "1"; fn(el); });
    };
    each(".quest, .reprove", initProofTabs);
    each("[data-camera]", initCamera);
    each("[data-qr-scan]", initQrScan);
    each("[data-nfc-scan]", initNfcScan);
    each("[data-nfc-write]", initNfcWrite);
    each("[data-qr-render]", initQrRender);
    each("[data-map-picker]", initMapPicker);
    each("[data-conquest-map]", initConquestMap);
    each("[data-timer]", initTimer);
    each("[data-recap]", initRecap);
    each("[data-ptr]", initPullToRefresh);
    each("[data-count]", countUp);
    each("[data-ring]", animateRing);
    each("[data-countdown]", (el) => startCountdown($("[data-countdown-text]", el), Number(el.dataset.countdown)));
    each("dialog[data-autoshow]", (dlg) => {
      dlg.addEventListener("close", () => setTimeout(() => dlg.remove(), 0));
      openDialog(dlg);
    });
    each(".habit-form", (form) => {
      const sync = () => { const f = form.querySelector("[data-freq]:checked"); form.dataset.freq = f ? f.value : "daily"; };
      form.addEventListener("change", sync);
      sync();
      const cat = $("[data-category-select]", form), icon = $("[data-icon-select]", form);
      if (cat && icon) {
        let touched = false;
        icon.addEventListener("change", () => { touched = true; });
        cat.addEventListener("change", () => {
          const wanted = cat.selectedOptions[0].dataset.icon;
          if (!touched && $$("option", icon).some((o) => o.value === wanted)) icon.value = wanted;
        });
      }
    });
    each("[data-prove-form]", (form) => {
      const sel = $("[data-prove-habit]", form);
      const sync = () => { form.action = `/habits/${sel.value}/checkin`; };
      sel.addEventListener("change", sync);
      sync();
    });
    each("[data-share]", (btn) => {
      if (!navigator.share) return;
      btn.hidden = false;
      btn.addEventListener("click", () => navigator.share({ title: "Train with me on Habitor", url: btn.dataset.shareUrl }).catch(() => {}));
    });
    $$("[data-install]", scope).forEach((b) => { b.hidden = !installPrompt; });
    syncPrefControls();
  }

  document.addEventListener("click", (e) => {
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      const src = copy.parentElement.querySelector("[data-copy-src]");
      if (src) {
        (navigator.clipboard ? navigator.clipboard.writeText(src.value) : Promise.reject()).then(
          () => { toast("Copied to clipboard.", "good"); haptic(12); },
          () => { src.select(); toast("Select the link and copy it.", "pink"); },
        );
      }
    }
    if (e.target.closest("[data-print]")) window.print();
    if (e.target.closest("[data-push-subscribe]")) subscribePush().catch(() => toast("Couldn't enable push notifications.", "bad"));
    const back = e.target.closest("[data-back]");
    if (back && history.length > 1) { e.preventDefault(); history.back(); }
    const install = e.target.closest("[data-install]");
    if (install && installPrompt) { installPrompt.prompt(); installPrompt = null; install.hidden = true; }
    if (e.target.closest("[data-install-dismiss]")) {
      store.set("habitor:install-dismissed", "1");
      $$("[data-install-card]").forEach((c) => { c.hidden = true; });
    }
  });

  document.addEventListener("submit", (e) => {
    const form = e.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) e.preventDefault();
  });

  // Don't refresh the feed under the user's fingers (open dialog, focused control, hidden tab).
  document.addEventListener("htmx:beforeRequest", (e) => {
    const elt = e.detail.elt;
    if (elt && elt.matches && elt.matches("[data-poll]") &&
        (document.hidden || document.querySelector("dialog[open]") || elt.contains(document.activeElement))) {
      e.preventDefault();
    }
  });
  document.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target && e.detail.target.querySelector && e.detail.target.querySelector("video")) stopStreams();
  });
  window.addEventListener("pagehide", stopStreams);

  function boot() {
    init(document);
    if (window.htmx) window.htmx.onLoad((el) => init(el));
    const fx = $("#fx-data");
    if (fx) { try { applyFx(JSON.parse(fx.textContent)); } catch { /* ignore */ } }
    if (/^#quest-\d+$/.test(location.hash)) setTimeout(() => focusQuest(location.hash), 200);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
