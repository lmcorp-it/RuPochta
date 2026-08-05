// RuPochta Mail service worker — PWA offline shell + Web Push notifications
const CACHE_VERSION = 'rupochta-mail-v27-transparent-banner';
const SHELL_ASSETS = [
  '/',
  '/static/index.html',
  '/static/workspace.css?v=20260618c',
  '/static/style.css?v=20260803-login-banner2',
  '/static/focus.css?v=20260618c',
  '/static/app.js?v=20260803-compose-session1',
  '/static/vendor/gsap.min.js?v=3.15.0',
  '/static/brand/mail-mark.svg',
  '/static/brand/mail-flight.png',
  '/static/brand/mail-workspace-background.webp',
  '/static/brand/rupochta-wordmark.svg',
  '/static/brand/mail-login-banner.webp?v=20260803-transparent1',
  '/static/brand/mail-login-organized.webp',
  '/static/brand/icon-192.png',
  '/static/brand/icon-512.png',
  '/manifest.webmanifest',
];
const SHELL_PATHS = new Set(
  SHELL_ASSETS.map((asset) => new URL(asset, self.location.origin).pathname)
);
const NETWORK_ONLY_PATHS = new Set(['/health', '/ready', '/sw.js']);

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      cache.addAll(SHELL_ASSETS).catch(() => {
        // Non-critical: continue install even if some URLs 404
      })
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

async function networkFirstShell(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const response = await fetch(new Request(request, {cache: 'no-store'}));
    if (response && response.ok && response.type === 'basic') {
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const requestUrl = new URL(request.url);
    const cached = await cache.match(request);
    const fallback = cached || (
      requestUrl.pathname === '/' ? await cache.match('/') : null
    );
    return fallback || new Response('offline', {
      status: 503,
      headers: {'Content-Type': 'text/plain; charset=utf-8'},
    });
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  // Only handle GET to same origin
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // Runtime truth must never come from Cache Storage.
  if (NETWORK_ONLY_PATHS.has(url.pathname)) {
    event.respondWith(
      fetch(new Request(req, {cache: 'no-store'})).catch(() =>
        new Response(JSON.stringify({ok: false, error: 'offline'}), {
          status: 503,
          headers: {'Content-Type': 'application/json', 'Cache-Control': 'no-store'},
        })
      )
    );
    return;
  }
  // Network-first for API, cache-first for the static application shell.
  // /sso/ is a one-time-use redirect (state/nonce/PKCE per click) and must
  // never be served from Cache Storage.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/sso/') ||
    url.pathname.startsWith('/admin/sso/')
  ) {
    event.respondWith(
      fetch(req).catch(() =>
        new Response(JSON.stringify({error: 'offline'}), {
          status: 503,
          headers: {'Content-Type': 'application/json'},
        })
      )
    );
    return;
  }
  if (SHELL_PATHS.has(url.pathname)) {
    event.respondWith(networkFirstShell(req));
    return;
  }
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) {
        // Refresh in background
        fetch(req).then((resp) => {
          if (resp && resp.ok) {
            caches.open(CACHE_VERSION).then((c) => c.put(req, resp.clone()));
          }
        }).catch(() => {});
        return cached;
      }
      return fetch(req).then((resp) => {
        if (resp && resp.ok && resp.type === 'basic') {
          const clone = resp.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(req, clone));
        }
        return resp;
      });
    })
  );
});

self.addEventListener('push', (event) => {
  if (!event.data) return;
  let payload;
  try {
    payload = event.data.json();
  } catch (e) {
    payload = {title: 'RuPochta Mail', body: event.data.text()};
  }
  const title = payload.title || 'RuPochta Mail';
  const options = {
    body: payload.body || '',
    icon: payload.icon || '/static/brand/icon-192.png',
    badge: payload.badge || '/static/brand/icon-192.png',
    tag: payload.tag || 'rupochta-mail',
    data: { url: payload.url || '/' },
    requireInteraction: payload.requireInteraction || false,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({type: 'window', includeUncontrolled: true}).then((wins) => {
      for (const win of wins) {
        if (win.url.includes(self.location.origin)) {
          win.focus();
          if (win.navigate) win.navigate(targetUrl);
          return;
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(targetUrl);
    })
  );
});
