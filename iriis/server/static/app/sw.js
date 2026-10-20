// Iriis service worker: makes the app installable, keeps the shell for offline starts, shows push notifications
// (including incoming calls from Iriis: they vibrate and stay until answered or declined).
const CACHE = "iriis-v4";
const SHELL = ["/app", "/app/manifest.webmanifest", "/app/icon.svg", "/app/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
// network first (always the latest app and data), cached shell when offline
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin || u.pathname.startsWith("/v1/")) return;
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok && SHELL.includes(u.pathname)) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(u.pathname, copy)); }
    return r;
  }).catch(() => caches.match(u.pathname).then((m) => m || caches.match("/app"))));
});

const windows = () => clients.matchAll({ type: "window", includeUncontrolled: true });

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) { d = { body: e.data && e.data.text() }; }
  if (d.kind === "call" && d.call_id) {
    const id = d.call_id;
    e.waitUntil(Promise.all([
      self.registration.showNotification(d.title || "Iriis is calling", {
        body: d.body || "Incoming voice call", icon: "/app/icon-192.png", badge: "/app/icon-96.png",
        tag: "iriis-call-" + id, renotify: true, requireInteraction: true,
        vibrate: [800, 400, 800, 400, 800, 400, 800, 400, 800],
        actions: [{ action: "answer", title: "Answer" }, { action: "decline", title: "Decline" }],
        data: { url: d.url || "/app?call=" + id, kind: "call", call_id: id },
      }),
      windows().then((ws) => ws.forEach((w) => w.postMessage({ kind: "call", call_id: id }))),   // an open app rings at once
    ]));
    return;
  }
  e.waitUntil(self.registration.showNotification(d.title || "Iriis", {
    body: d.body || "", icon: "/app/icon-192.png", badge: "/app/icon-96.png", data: { url: d.url || "/app" },
    tag: "iriis", renotify: true,
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const data = e.notification.data || {};
  if (data.kind === "call") {
    const id = data.call_id;
    if (e.action === "decline") {
      e.waitUntil(fetch(`/v1/calls/${id}/decline`, { method: "POST", credentials: "include" }).catch(() => {}));
      return;
    }
    const answer = e.action === "answer";
    const url = `/app?call=${id}` + (answer ? "&answer=1" : "");
    e.waitUntil(windows().then((ws) => {
      for (const w of ws) if (w.url.includes("/app")) { w.postMessage({ kind: "call", call_id: id, answer }); return w.focus(); }
      return clients.openWindow(url);
    }));
    return;
  }
  const url = data.url || "/app";
  e.waitUntil(windows().then((ws) => {
    for (const w of ws) if (w.url.includes("/app")) return w.focus();
    return clients.openWindow(url);
  }));
});
