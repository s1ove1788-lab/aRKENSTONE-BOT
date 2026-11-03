// Нужен, чтобы телефон предлагал «Установить приложение». Данные всегда свежие: ничего не кешируем.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
