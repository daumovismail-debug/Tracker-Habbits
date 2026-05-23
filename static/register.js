'use strict';

const uid = document.getElementById('uid');
const pwd = document.getElementById('pwd');
const btn = document.getElementById('enter');
const err = document.getElementById('err');

async function enter() {
  const user_id = uid.value.trim();
  const password = pwd.value;
  err.textContent = '';
  if (!user_id) { err.textContent = 'Введите Telegram ID'; return; }
  if (password.length < 4) { err.textContent = 'Пароль не короче 4 символов'; return; }
  btn.disabled = true;
  try {
    const res = await fetch('/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: Number(user_id), password }),
    });
    if (res.ok) {
      window.location.href = '/';
      return;
    }
    const data = await res.json().catch(() => ({}));
    err.textContent = data.error || 'Ошибка регистрации';
  } catch (e) {
    err.textContent = 'Ошибка сети';
  }
  btn.disabled = false;
}

btn.onclick = enter;
[uid, pwd].forEach(el => {
  el.onkeydown = e => { if (e.key === 'Enter') enter(); };
});
uid.focus();
