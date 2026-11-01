'use strict';

(() => {
  const form = document.getElementById('record-form');
  if (!form) return;
  const message = document.getElementById('session-message');
  const text = document.getElementById('session-message-text');
  const login = document.getElementById('session-login');
  let pending = false;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending) return;
    pending = true;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = '正在确认登录…';
    message.hidden = true;
    login.hidden = true;
    let submitting = false;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(form.dataset.sessionUrl, {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      if (response.status === 401) {
        text.textContent = '登录已过期，填写内容仍保留在本页。请在新标签页登录，再返回这里点击保存。';
        message.hidden = false;
        login.hidden = false;
        return;
      }
      if (!response.ok) throw new Error('Session check failed');
      const state = await response.json();
      if (!state.authenticated || typeof state.csrf !== 'string' || !state.csrf) {
        throw new Error('Invalid session response');
      }
      form.elements.namedItem('csrf').value = state.csrf;
      button.textContent = '正在保存…';
      // Only this explicit submit sends the form. No background draft storage.
      HTMLFormElement.prototype.submit.call(form);
      submitting = true;
    } catch {
      text.textContent = '暂时无法确认登录状态，内容仍保留在本页，请稍后再次保存。';
      message.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (!submitting) {
        pending = false;
        button.disabled = false;
        button.textContent = '保存记录';
      }
    }
  });
})();
