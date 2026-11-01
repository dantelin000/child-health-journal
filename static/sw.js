'use strict';

// This worker never caches requests, records, forms, tokens, or responses.
// Only failed same-origin navigations receive this generic offline notice.
const offlinePage = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>暂时无法连接 · 孩子健康记录</title>
<style>body{margin:0;background:#f6f8f4;color:#263e39;font-family:system-ui,-apple-system,sans-serif}main{max-width:430px;margin:14vh auto;padding:28px}section{background:#fff;border:1px solid #e5ece5;border-radius:16px;padding:32px}h1{font-size:26px;margin:0 0 14px}p{line-height:1.7;color:#60776b}a{display:inline-block;background:#326f5b;color:#fff;border-radius:10px;padding:12px 20px;text-decoration:none;font-weight:700}</style></head>
<body><main><section><h1>暂时无法连接</h1><p>请检查网络连接后重试。健康记录需要联网查看和保存。</p><a href="/">重新打开</a></section></main></body></html>`;

self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET' || request.mode !== 'navigate') return;
  if (new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(fetch(request).catch(() => new Response(offlinePage, {
    status: 200,
    headers: {
      'Content-Type': 'text/html; charset=utf-8',
      'Cache-Control': 'no-store',
      'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
      'X-Content-Type-Options': 'nosniff',
    },
  })));
});
