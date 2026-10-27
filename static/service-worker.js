const APP_ORIGIN = self.location.origin;
self.addEventListener("install", event => {
  event.waitUntil(self.skipWaiting());
});
self.addEventListener("activate", event => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  const requestUrl = new URL(event.request.url);
  if (requestUrl.origin === APP_ORIGIN) {
    // Network only: authenticated work records are never stored in an offline cache.
    event.respondWith(fetch(event.request));
  }
});
