/* Habitor service worker: offline shell + web push. */
const CACHE = "habitor-v5";
const SHELL = ["/offline", "/static/style.css", "/static/app.js", "/icon.svg", "/icon-192.png", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (req.mode === "navigate") {
    // Pages are always fresh (they're personal); fall back to the offline screen.
    event.respondWith(fetch(req).catch(() => caches.match("/offline")));
    return;
  }
  if (url.origin === self.location.origin && (url.pathname.startsWith("/static/") || url.pathname.startsWith("/icon"))) {
    // Assets are versioned (?v=…), so an exact cache hit is always current. Otherwise go to the
    // network, and only when offline fall back to any cached version of the same file.
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const exact = await cache.match(req);
        if (exact) return exact;
        try {
          const res = await fetch(req);
          if (res.ok) cache.put(req, res.clone());
          return res;
        } catch (err) {
          const any = await cache.match(req, { ignoreSearch: true });
          if (any) return any;
          throw err;
        }
      }),
    );
  }
});

self.addEventListener("push", (event) => {
  let data = { title: "Habitor", body: "You have a new update.", url: "/" };
  try { data = { ...data, ...event.data.json() }; } catch { /* plain text payload */ }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body, icon: "/icon-192.png", badge: "/icon-192.png", data: { url: data.url }, tag: data.url,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) { w.navigate(target); return w.focus(); }
      }
      return self.clients.openWindow(target);
    }),
  );
});
