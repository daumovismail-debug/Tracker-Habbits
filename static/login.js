'use strict';

const pwd = document.getElementById('pwd');
const btn = document.getElementById('enter');
const err = document.getElementById('err');

async function enter() {
  const password = pwd.value;
  err.textContent = '';
  if (!password) {
    err.textContent = 'Введите пароль';
    return;
  }
  btn.disabled = true;
  try {
    const res = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    if (res.ok) {
      window.location.href = '/';
      return;
    }
    const data = await res.json().catch(() => ({}));
    err.textContent = data.error || 'Ошибка входа';
  } catch (e) {
    err.textContent = 'Ошибка сети';
  }
  btn.disabled = false;
}

btn.onclick = enter;
pwd.onkeydown = e => { if (e.key === 'Enter') enter(); };
pwd.focus();
