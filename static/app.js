'use strict';

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let state = null;
let activeTab = 'today';

const STATUS = {
  done:     { icon: '✅', label: 'Да' },
  not_done: { icon: '❌', label: 'Нет' },
  skip:     { icon: '⏭', label: 'Скип' },
};

/* ─── helpers ─── */

function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

function plural(n) {
  const a = Math.abs(n) % 100, b = a % 10;
  if (a > 10 && a < 20) return 'дней';
  if (b > 1 && b < 5) return 'дня';
  if (b === 1) return 'день';
  return 'дней';
}

function emptyState(icon, title, sub) {
  return `<div class="empty">
    <div class="empty-icon">${icon}</div>
    <div class="empty-title">${title}</div>
    <div class="empty-sub">${sub}</div>
  </div>`;
}

let toastTimer;
function toast(msg, type = 'ok') {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'show ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 2800);
}

/* ─── API ─── */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (res.status === 401) {
    window.location.href = '/login';
    await new Promise(() => {});
  }
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
  return data;
}

async function load() {
  try {
    state = await api('/api/state');
    renderAll();
  } catch (e) {
    renderFatal(e.message);
  }
}

/* ─── render ─── */

function renderAll() {
  const fatal = $('#fatal');
  if (fatal) fatal.remove();
  $('#date').textContent = cap(`${state.weekday}, ${state.date}`);
  renderHero();
  renderToday();
  renderStats();
  renderSettings();
}

function renderHero() {
  const total = state.habits.length;
  const done = state.done_today;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const r = 54;
  const circ = 2 * Math.PI * r;
  $('#hero').innerHTML = `
    <svg class="ring" viewBox="0 0 124 124">
      <circle class="ring-track" cx="62" cy="62" r="${r}"/>
      <circle class="ring-fill" cx="62" cy="62" r="${r}"
              stroke-dasharray="${circ.toFixed(1)}"
              stroke-dashoffset="${(circ * (1 - pct / 100)).toFixed(1)}"/>
    </svg>
    <div class="ring-center">
      <span class="ring-pct">${pct}<small>%</small></span>
      <span class="ring-sub">${done} из ${total}</span>
    </div>`;
}

function renderToday() {
  const wrap = $('#tab-today');
  if (!state.habits.length) {
    wrap.innerHTML = emptyState('🫙', 'Привычек пока нет',
      'Нажмите «+», чтобы добавить первую');
    return;
  }
  wrap.innerHTML = state.habits.map(h => {
    const meta = [];
    if (h.step > 0) {
      meta.push(`🎯 ${h.target}`);
      if (h.day_in_cycle) meta.push(`день ${h.day_in_cycle}`);
    }
    const buttons = Object.entries(STATUS).map(([key, s]) => `
      <button class="st-btn ${key} ${h.status === key ? 'active' : ''}"
              data-habit="${h.id}" data-status="${key}">
        <span>${s.icon}</span>${s.label}
      </button>`).join('');
    return `
      <div class="card habit ${h.status || 'none'}">
        <div class="habit-head">
          <span class="habit-name">${esc(h.name)}</span>
          ${meta.length ? `<span class="habit-meta">${meta.join(' · ')}</span>` : ''}
        </div>
        <div class="st-row">${buttons}</div>
      </div>`;
  }).join('');

  $$('.st-btn', wrap).forEach(b => {
    b.onclick = () => setStatus(Number(b.dataset.habit), b.dataset.status);
  });
}

function renderStats() {
  const wrap = $('#tab-stats');
  if (!state.stats.length) {
    wrap.innerHTML = emptyState('📊', 'Нет данных',
      'Добавьте привычку, чтобы видеть статистику');
    return;
  }
  wrap.innerHTML = state.stats.map(s => `
    <div class="card stat">
      <div class="habit-head">
        <span class="habit-name">${esc(s.name)}</span>
        <span>
          ${s.target > 1 ? `<span class="habit-meta">🎯 ${s.target}　</span>` : ''}
          <button class="icon-btn del" data-id="${s.id}"
                  data-name="${esc(s.name)}" title="Удалить">🗑</button>
        </span>
      </div>
      <div class="bar"><div class="bar-fill" style="width:${s.rate}%"></div></div>
      <div class="stat-row">
        <span class="rate">${s.rate}%</span>
        <span class="counts">✅ ${s.done}　❌ ${s.not_done}　⏭ ${s.skip}</span>
        <span class="streak ${s.streak >= 3 ? 'hot' : ''}">🔥 ${s.streak} ${plural(s.streak)}</span>
      </div>
    </div>`).join('');

  $$('.del', wrap).forEach(b => {
    b.onclick = () => deleteHabit(Number(b.dataset.id), b.dataset.name);
  });
}

function renderSettings() {
  const s = state.settings;
  const hourOpts = (from, to, sel) => {
    let out = '';
    for (let i = from; i <= to; i++) {
      const label = String(i % 24).padStart(2, '0') + ':00';
      out += `<option value="${i}" ${i === sel ? 'selected' : ''}>${label}</option>`;
    }
    return out;
  };
  $('#tab-settings').innerHTML = `
    <div class="card setting">
      <div class="set-row">
        <div>
          <div class="set-title">Напоминания в Telegram</div>
          <div class="set-hint">Бот пишет о неотмеченных привычках</div>
        </div>
        <label class="switch">
          <input type="checkbox" id="s-enabled" ${s.reminder_enabled ? 'checked' : ''}>
          <span class="slider"></span>
        </label>
      </div>
      <div class="set-row">
        <div class="set-title">Начало</div>
        <select id="s-start">${hourOpts(0, 23, s.reminder_start_hour)}</select>
      </div>
      <div class="set-row">
        <div class="set-title">Конец</div>
        <select id="s-end">${hourOpts(1, 24, s.reminder_end_hour)}</select>
      </div>
      <div class="set-row">
        <div class="set-title">Интервал</div>
        <div class="num-field">
          <input type="number" id="s-interval" min="1" max="120"
                 value="${s.reminder_interval_minutes}"><span>мин</span>
        </div>
      </div>
      <button class="btn primary" id="s-save">Сохранить</button>
    </div>
    <button class="btn ghost" id="s-logout">Выйти из аккаунта</button>`;
  $('#s-save').onclick = saveSettings;
  $('#s-logout').onclick = () => { window.location.href = '/logout'; };
}

function renderFatal(msg) {
  const existing = $('#fatal-msg');
  if (existing) { existing.textContent = msg; return; }
  const d = document.createElement('div');
  d.id = 'fatal';
  d.innerHTML = `<div class="fatal-box">
    <div class="fatal-icon">⚠️</div>
    <div class="fatal-title">Не удалось загрузить</div>
    <div class="fatal-msg" id="fatal-msg">${esc(msg)}</div>
    <button class="btn primary" id="fatal-retry">Повторить</button>
  </div>`;
  document.body.appendChild(d);
  $('#fatal-retry').onclick = load;
}

/* ─── actions ─── */

async function setStatus(id, status) {
  const h = state.habits.find(x => x.id === id);
  const next = h && h.status === status ? 'clear' : status;
  try {
    await api('/api/checkin', {
      method: 'POST',
      body: JSON.stringify({ habit_id: id, status: next }),
    });
    await load();
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function deleteHabit(id, name) {
  if (!confirm(`Удалить привычку «${name}»?\nСтатистика тоже исчезнет.`)) return;
  try {
    await api('/api/habits/' + id, { method: 'DELETE' });
    toast('Привычка удалена');
    await load();
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function saveSettings() {
  const body = {
    reminder_enabled: $('#s-enabled').checked,
    reminder_start_hour: Number($('#s-start').value),
    reminder_end_hour: Number($('#s-end').value),
    reminder_interval_minutes: Number($('#s-interval').value),
  };
  if (body.reminder_end_hour <= body.reminder_start_hour) {
    return toast('Конец должен быть позже начала', 'err');
  }
  try {
    await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    toast('Настройки сохранены');
    await load();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ─── modal ─── */

function openModal() {
  $('#m-name').value = '';
  $('#m-prog').checked = false;
  $('#m-target').value = 20;
  $('#m-cycle').value = 10;
  $('#m-step').value = 2;
  toggleProgFields();
  $('#modal').classList.remove('hidden');
  $('#m-name').focus();
}

function closeModal() {
  $('#modal').classList.add('hidden');
}

function toggleProgFields() {
  $('#m-prog-fields').classList.toggle('hidden', !$('#m-prog').checked);
}

async function createHabit() {
  const name = $('#m-name').value.trim();
  if (!name) return toast('Введите название', 'err');

  const body = { name, progression: $('#m-prog').checked };
  if (body.progression) {
    body.target = Number($('#m-target').value);
    body.cycle_days = Number($('#m-cycle').value);
    body.step = Number($('#m-step').value);
  }
  try {
    await api('/api/habits', { method: 'POST', body: JSON.stringify(body) });
    closeModal();
    toast('Привычка добавлена');
    await load();
  } catch (e) {
    toast(e.message, 'err');
  }
}

/* ─── tabs ─── */

function switchTab(name) {
  activeTab = name;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  $$('.panel').forEach(p => p.classList.toggle('hidden', p.id !== 'tab-' + name));
}

/* ─── init ─── */

function init() {
  $$('.tab').forEach(t => { t.onclick = () => switchTab(t.dataset.tab); });
  $('#fab').onclick = openModal;
  $('#m-cancel').onclick = closeModal;
  $('#m-create').onclick = createHabit;
  $('#m-prog').onchange = toggleProgFields;
  $('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
  $('#m-name').onkeydown = e => { if (e.key === 'Enter') createHabit(); };
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeModal();
  });

  load();

  setInterval(() => {
    if (activeTab === 'settings') return;
    if (!$('#modal').classList.contains('hidden')) return;
    load();
  }, 45000);
}

document.addEventListener('DOMContentLoaded', init);
