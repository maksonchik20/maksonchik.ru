'use strict';
const CACHE = 'owner-panel-public-v8';
const OFFLINE = '/static/owner_panel/offline.html';
// Private HTML, API responses and tokens are never written to Cache Storage.
const ASSETS = [OFFLINE, '/static/owner_panel/app.css?v=8', '/static/owner_panel/app.js?v=8', '/static/owner_panel/icon-192.png', '/static/owner_panel/icon-512.png'];
self.addEventListener('install', event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener('activate', event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('owner-panel-public-') && k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener('fetch', event => {
  const u = new URL(event.request.url);
  if (u.origin !== self.location.origin || event.request.method !== 'GET') return;
  if (event.request.mode === 'navigate' && u.pathname.startsWith('/panel/')) {
    event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
    return
  }
  if (ASSETS.includes(u.pathname + u.search)) {
    event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request)))
  }
});
self.addEventListener('push', event => {
  let p = {};
  try {
    p = event.data.json()
  } catch {}
  const target = new URL(p.url || '/panel/', self.location.origin);
  const url = target.origin === self.location.origin && target.pathname === '/panel/' ? target.href : '/panel/';
  event.waitUntil(self.registration.showNotification(p.title || 'Control', {
    body: p.body || 'Новое событие в панели',
    icon: '/static/owner_panel/icon-192.png',
    badge: '/static/owner_panel/icon-192.png',
    tag: p.tag || 'control',
    data: {
      url
    }
  }))
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({
    type: 'window',
    includeUncontrolled: true
  }).then(async clients => {
    const url = event.notification.data?.url || '/panel/';
    for (const client of clients) {
      if (new URL(client.url).pathname.startsWith('/panel/')) {
        await client.navigate(url);
        return client.focus()
      }
    }
    return self.clients.openWindow(url)
  }))
});
