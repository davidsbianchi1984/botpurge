// Bot Purge service worker: the app opens instantly and offline; your data always comes fresh from the server.
const VERSION = "bp-1";
const SHELL = ["/", "/static/style.css", "/static/common.js", "/static/icon-192.png", "/static/icon-512.png", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // Only this site's own pages and files; never API calls, sign-ins or anything with private data.
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok && (SHELL.includes(url.pathname) || url.pathname.startsWith("/static/"))) {
        const copy = res.clone();
        caches.open(VERSION).then(c => c.put(url.pathname === "/" ? "/" : e.request, copy));
      }
      return res;
    }).catch(() => caches.match(url.pathname === "/" || e.request.mode === "navigate" ? "/" : e.request))
  );
});
