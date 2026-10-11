// Licitações Brasil PWA Service Worker - Cache Resiliente e Fallback Offline
const CACHE_NAME = 'licitacoesbrasil-pwa-v2';

// Páginas com dados privados do usuário (funil, agenda, painel, favoritos, alertas):
// nunca vão para o cache, para que outra conta no mesmo aparelho não veja esses dados.
const PRIVATE_PATHS = ['/negocios', '/agenda', '/dashboard', '/favorites', '/alerts', '/documentos', '/admin'];
const isPrivatePath = (pathname) => PRIVATE_PATHS.some((p) => pathname === p || pathname.startsWith(p + '/'));

const STATIC_ASSETS = [
  '/',
  '/search',
  '/disputa',
  '/static/manifest.json',
  '/static/icons/icon.svg',
  'https://cdn.tailwindcss.com',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_ASSETS).catch((err) => {
        console.warn('[SW] Cache parcial de inicializacao:', err);
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Nao intercepta chamadas de API ou streams SSE para garantir dados em tempo real
  if (url.pathname.startsWith('/api/') || event.request.method !== 'GET') {
    return;
  }
  if (url.origin === self.location.origin && isPrivatePath(url.pathname)) {
    return;
  }

  event.respondWith(
    fetch(event.request)
      .then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const clone = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, clone);
          });
        }
        return networkResponse;
      })
      .catch(() => {
        return caches.match(event.request).then((cached) => {
          if (cached) return cached;
          if (event.request.mode === 'navigate') {
            return caches.match('/');
          }
          return new Response('Licitações Brasil Offline', {
            status: 503,
            statusText: 'Offline',
            headers: { 'Content-Type': 'text/plain; charset=utf-8' }
          });
        });
      })
  );
});
