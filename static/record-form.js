'use strict';

(() => {
  const form = document.getElementById('record-form');
  if (!form) return;
  const message = document.getElementById('session-message');
  const text = document.getElementById('session-message-text');
  const login = document.getElementById('session-login');
  const submit = form.querySelector('button[type="submit"]');
  const buttonLabel = submit.textContent;
  const memberSelect = form.elements.namedItem('member_id');
  if (memberSelect) {
    // A family member can be added in another tab without losing this draft.
    window.addEventListener('focus', async () => {
      try {
        const response = await fetch(memberSelect.dataset.membersUrl, {
          credentials: 'same-origin', cache: 'no-store',
        });
        if (!response.ok) return;
        const state = await response.json();
        const selected = memberSelect.value;
        const legacy = memberSelect.querySelector('option[value="legacy"]');
        const options = [new Option('请选择家人', '')];
        if (legacy) options.push(legacy.cloneNode(true));
        for (const member of state.members) {
          const label = member.name + (member.relationship ? ' · ' + member.relationship : '');
          options.push(new Option(label, String(member.id)));
        }
        memberSelect.replaceChildren(...options);
        memberSelect.value = selected;
      } catch { /* Keep the last loaded choices and every form field. */ }
    });
  }
  let pending = false;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (pending) return;
    const files = form.elements.namedItem('documents')?.files;
    if (files && (files.length > 3 || [...files].some(file => file.size > 10 * 1024 * 1024))) {
      text.textContent = '每次最多选择 3 个病例单，每个不能超过 10 MB。请调整后再保存，填写内容仍保留在本页。';
      message.hidden = false;
      login.hidden = true;
      return;
    }
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
        button.textContent = buttonLabel;
      }
    }
  });
})();
