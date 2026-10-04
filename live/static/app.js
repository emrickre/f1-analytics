'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let state = null;
let outline = { points: [], rotation: 0, corners: [] };
let selected = null;
let ws = null;

// ---------- WebSocket с переподключением ----------

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

let build = null;       // версия фронтенда с сервера — сменилась → перезагрузка

function connect(delay = 500) {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => {
    $('conn').classList.add('on'); delay = 500;
    send({ cmd: 'sessions', year: Number($('ses-year').value) });
    if (selected) send({ cmd: 'focus', driver: selected });
  };
  ws.onclose = () => {
    $('conn').classList.remove('on');
    setTimeout(() => connect(Math.min(delay * 2, 10000)), delay);
  };
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === 'hello') {
      if (build && build !== m.build) location.reload();
      build = m.build;
    } else if (m.type === 'state') { state = m; render(); }
    else if (m.type === 'pos') onPos(m);
    else if (m.type === 'tel') onTel(m);
    else if (m.type === 'outline') { outline = m; fitMap(); }
    else if (m.type === 'playback') onPlayback(m);
    else if (m.type === 'sessions') onSessions(m);
    else if (m.type === 'strategy') { strat = m; drawStrategy(); }
  };
}

// ---------- шапка, баннер, race control ----------

const BANNER = {
  sc: ['sc', 'Safety Car'],
  'sc-end': ['sc', 'Safety Car in this lap'],
  vsc: ['sc', 'Virtual Safety Car'],
  'vsc-end': ['sc', 'VSC ending'],
  red: ['red', 'Red flag'],
};

const LOCALE = 'en-GB';

function render() {
  const s = state;
  $('meeting').textContent = s.session.meeting || '—';
  $('session').textContent = s.session.name || '';
  $('sstatus').textContent = s.session.status || '';
  $('circuit').textContent = s.session.circuit || 'Track';
  $('lap').textContent = s.lap.current ? `${s.lap.current}${s.lap.total ? ' / ' + s.lap.total : ''}` : '–';
  $('clock').textContent = s.clock || '–';
  $('air').textContent = s.weather.air ? `${Math.round(s.weather.air)}°` : '–';
  $('trackt').textContent = s.weather.track ? `${Math.round(s.weather.track)}°` : '–';
  $('rain').hidden = !s.weather.rain;

  $('flag').className = `flag ${s.track.flag}`;
  $('flagtext').textContent = s.track.text;
  const ot = s.overtake;
  $('ot').hidden = !ot;
  if (ot) {
    $('ot').className = `ot ${ot.on ? 'on' : ''}`;
    $('ot').textContent = `${ot.name} ${ot.on ? 'enabled' : 'disabled'}`;
  }
  const b = BANNER[s.track.flag];
  const banner = $('banner');
  banner.hidden = !b;
  if (b) { banner.className = `banner ${b[0]}`; banner.textContent = b[1]; }

  renderRows(s.rows);
  renderTower(s.rows);
  renderDriverPanel();
  $('c-sess').textContent = [s.session.status, s.clock].filter(Boolean).join(' · ');
  $('c-air').textContent = s.weather.air
    ? `Air ${Math.round(s.weather.air)}° · Track ${Math.round(s.weather.track)}°` : '';

  $('rcm').innerHTML = s.rcm.map((m) => {
    const t = m.utc ? new Date(/Z|[+-]\d\d:\d\d$/.test(m.utc) ? m.utc : m.utc + 'Z')
      .toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
    const msg = m.msg || '';
    const extra = /SAFETY CAR|VSC/.test(msg) ? 'sc' : /PENALTY|STEWARDS/.test(msg) ? 'pen'
      : /^(OVERTAKE|DRS) (EN|DIS)ABLED/.test(msg) ? 'ot' : '';
    return `<li class="f-${esc((m.flag || '').replace(' ', '.'))} ${extra}"><time>${t}</time><span>${esc(msg)}</span></li>`;
  }).join('');

  if (!outline.done) fitMap();   // пока контура нет — масштаб по машинам
}

// ---------- таблица: строки по пилотам, без пересоздания ----------

const rowEls = {};      // num → <tr>
const lastPos = {};     // num → позиция в прошлом обновлении
const posDelta = {};    // num → {d, until}

function cls(t) { return t.ob ? 'ob' : t.pb ? 'pb' : ''; }

function lastName(full) {
  const parts = String(full || '').trim().split(/\s+/);
  const ln = parts.length > 1 ? parts.slice(1).join(' ') : parts[0] || '';
  return ln.charAt(0) + ln.slice(1).toLowerCase();
}

function rowHtml(r) {
  const tag = r.out ? '<span class="tag out">OUT</span>'
    : r.inPit ? '<span class="tag pit">PIT</span>'
    : r.pitOut ? '<span class="tag pit">OUT LAP</span>' : '';
  const tyre = r.tyre
    ? `<span class="tyre t-${esc(r.tyre)}" title="${esc(r.tyre)}${r.tyreNew ? ', new' : ''}">${esc(r.tyre[0])}</span><small>${r.tyreAge ?? ''}</small>`
    : '';
  const dl = posDelta[r.num];
  const delta = dl && dl.until > performance.now()
    ? `<span class="${dl.d > 0 ? 'up' : 'down'}">${dl.d > 0 ? '▲' : '▼'}${Math.abs(dl.d)}</span>` : '';
  const sectors = r.sectors.map((x) =>
    `<td class="sec c"><span class="${cls(x)}">${esc(x.v)}</span></td>`).join('');
  return `<td class="pos c"><span>${r.pos < 99 ? r.pos : '–'}</span></td>
    <td class="delta">${delta}</td>
    <td class="l"><span class="drv" style="--c:${esc(r.color)}"><i></i><b>${esc(r.tla)}</b><span class="nm">${esc(lastName(r.name))}</span>${tag}</span></td>
    <td class="gap">${r.pos === 1 && !r.gap ? '<span class="leader">Leader</span>' : esc(r.gap)}</td>
    <td class="int">${esc(r.interval)}</td>
    <td class="time ${cls(r.last)}">${esc(r.last.v)}</td>
    <td class="best">${esc(r.best)}</td>
    ${sectors}
    <td class="c">${tyre}</td>
    <td class="c">${r.pits || ''}</td>`;
}

function renderRows(rows) {
  const tbody = $('rows');
  const now = performance.now();
  const seen = new Set();
  rows.forEach((r) => {
    seen.add(r.num);
    let tr = rowEls[r.num];
    if (!tr) {
      tr = rowEls[r.num] = document.createElement('tr');
      tr.dataset.num = r.num;
    }
    // Смена позиции: стрелка на 6 с и вспышка строки.
    const prev = lastPos[r.num];
    if (prev && r.pos < 99 && prev < 99 && prev !== r.pos && !snapNext) {
      posDelta[r.num] = { d: prev - r.pos, until: now + 6000 };
      tr.classList.remove('up', 'down');
      void tr.offsetWidth;                       // перезапуск анимации
      tr.classList.add(prev > r.pos ? 'up' : 'down');
    }
    lastPos[r.num] = r.pos;
    tr.title = `${r.name} — ${r.team}`;
    tr.classList.toggle('sel', r.num === selected);
    tr.classList.toggle('out', r.out);
    const html = rowHtml(r);
    if (tr._html !== html) { tr.innerHTML = html; tr._html = html; }
    tbody.appendChild(tr);                       // порядок строк = порядок позиций
  });
  for (const num of Object.keys(rowEls)) {
    if (!seen.has(num)) { rowEls[num].remove(); delete rowEls[num]; delete lastPos[num]; }
  }
  snapNext = false;
}

function select(num) {
  selected = num || null;
  telBuf = [];
  telState = selected ? 'loading' : null;
  send({ cmd: 'focus', driver: selected });
  if (!selected && view.follow) { view.follow = false; resetView(); }
  if (state) render();
  drawStrategy();
  syncFollowBtn();
}

$('rows').addEventListener('click', (e) => {
  const tr = e.target.closest('tr');
  if (tr) select(selected === tr.dataset.num ? null : tr.dataset.num);
});
$('tower').addEventListener('click', (e) => {
  const li = e.target.closest('li');
  if (li) select(selected === li.dataset.num ? null : li.dataset.num);
});
$('d-close').onclick = () => select(null);

// ---------- вкладка «Телеметрия»: башня позиций и карточка пилота ----------

function renderTower(rows) {
  $('tower').innerHTML = rows.map((r) => `<li data-num="${esc(r.num)}" class="${
    r.num === selected ? 'sel' : ''} ${r.out ? 'out' : ''}" style="--c:${esc(r.color)}">
    <span class="tw-pos">${r.pos < 99 ? r.pos : ''}</span><i class="tw-bar"></i>
    <span>${esc(r.tla)}</span><span class="tw-tag">${r.inPit ? 'PIT' : ''}</span></li>`).join('');
}

function renderDriverPanel() {
  const r = selected && state ? state.rows.find((x) => x.num === selected) : null;
  $('drv').hidden = !r;
  $('view-tele').classList.toggle('nodrv', !r);
  $('m-hint').hidden = !!r;
  if (!r) return;
  $('drv').style.setProperty('--c', r.color);
  $('d-pos').textContent = r.pos < 99 ? `P${r.pos}` : '–';
  $('d-tla').textContent = r.tla;
  $('d-name').textContent = r.name;
  $('d-team').textContent = r.team;
  $('d-best').textContent = r.best || '–';
  $('d-tyre').innerHTML = r.tyre
    ? `<span class="tyre t-${esc(r.tyre)}">${esc(r.tyre[0])}</span> ${r.tyreAge ?? ''} laps` : '–';
  $('d-gap').textContent = r.pos === 1 ? 'Leader' : r.gap || '–';
  ['s1', 's2', 's3'].forEach((id, i) => {
    const x = r.sectors[i];
    $('d-' + id).className = cls(x);
    $('d-' + id).textContent = x.v || '—';
  });
  $('d-last').className = cls(r.last);
  $('d-last').textContent = r.last.v || '—';
  $('d-int').textContent = r.interval || '—';
}

// ---------- вкладки ----------

function setTab(tab) {
  document.querySelectorAll('.tab').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
  $('view-timing').hidden = tab !== 'timing';
  $('view-tele').hidden = tab !== 'tele';
  $('view-strat').hidden = tab !== 'strat';
  $(tab === 'tele' ? 'slot-tele' : 'slot-timing').appendChild($('mapbox'));
  if (tab !== 'tele') { view.follow = false; resetView(); }
  syncFollowBtn();
  if (tab === 'strat') drawStrategy();
  try { localStorage.setItem('f1tab', tab); } catch (e) { /* приватный режим */ }
}
document.querySelectorAll('.tab').forEach((b) => { b.onclick = () => setTab(b.dataset.tab); });

// ---------- плеер: время, скорость, перемотка ----------

let pb = { mode: 'replay', loaded: false, t: 0, playing: false, speed: 1 };
let dragging = false;
let snapNext = false;

function fmtT(sec) {
  const neg = sec < 0;
  sec = Math.floor(Math.abs(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
  const body = h ? `${h}:${String(m).padStart(2, '0')}:${String(ss).padStart(2, '0')}`
    : `${m}:${String(ss).padStart(2, '0')}`;
  return (neg ? '−' : '') + body;
}

function onPlayback(m) {
  // Скачок времени (перемотка) — не показываем «обгоны» и не тащим машины.
  if (pb.loaded && m.loaded && Math.abs(m.t - pb.t) > 5 + 2 * m.speed) snapNext = true;
  const sessionChanged = m.sessionKey !== pb.sessionKey;
  pb = m;
  const live = m.mode === 'live';
  $('pb-controls').classList.toggle('hidden', live);

  const status = $('pb-status');
  status.classList.toggle('err', !!m.error);
  status.textContent = m.error ? `Error: ${m.error}`
    : m.loading ? `⏳ ${m.loading}` : live ? 'live feed' : (m.title || '');
  $('ses-load').disabled = !!m.loading;
  if (live) return;

  const sp = $('pb-speed');
  if (sp.options.length !== m.speeds.length) {
    sp.innerHTML = m.speeds.map((v) => `<option value="${v}">${v}×</option>`).join('');
  }
  sp.value = String(m.speed);
  $('pb-play').textContent = m.playing ? '❚❚' : '▶';
  for (const id of ['pb-play', 'pb-back', 'pb-fwd', 'pb-range']) $(id).disabled = !m.loaded;

  const r = $('pb-range');
  r.min = Math.floor(m.min); r.max = Math.ceil(m.max);
  if (!dragging) { r.value = m.t; $('pb-time').textContent = fmtT(m.t); }
  $('pb-dur').textContent = fmtT(m.max);

  if (sessionChanged && m.sessionKey) {
    select(null);
    const year = Number((m.title || '').slice(0, 4));
    if (year && year !== Number($('ses-year').value)) {
      $('ses-year').value = year;
      send({ cmd: 'sessions', year });
    } else {
      $('ses-list').value = m.sessionKey;
    }
  }
}

$('pb-play').onclick = () => send({ cmd: 'toggle' });
$('pb-back').onclick = () => send({ cmd: 'step', dt: -30 });
$('pb-fwd').onclick = () => send({ cmd: 'step', dt: 30 });
$('pb-speed').onchange = (e) => send({ cmd: 'speed', value: Number(e.target.value) });
$('pb-range').oninput = (e) => { dragging = true; $('pb-time').textContent = fmtT(Number(e.target.value)); };
$('pb-range').onchange = (e) => { dragging = false; send({ cmd: 'seek', t: Number(e.target.value) }); };

document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, select, textarea') || pb.mode === 'live') return;
  const big = e.shiftKey ? 60 : 10;
  if (e.code === 'Space') { e.preventDefault(); send({ cmd: 'toggle' }); }
  else if (e.code === 'ArrowLeft') { e.preventDefault(); send({ cmd: 'step', dt: -big }); }
  else if (e.code === 'ArrowRight') { e.preventDefault(); send({ cmd: 'step', dt: big }); }
});

// ---------- выбор сессии (OpenF1) ----------

(function initYears() {
  const now = new Date().getFullYear();
  const sel = $('ses-year');
  for (let y = now; y >= 2023; y--) sel.add(new Option(String(y), String(y)));
  sel.onchange = () => send({ cmd: 'sessions', year: Number(sel.value) });
})();

function onSessions(m) {
  if (m.year !== Number($('ses-year').value)) return;
  const list = $('ses-list');
  if (!m.items.length) {
    list.innerHTML = `<option>${m.error && /live/i.test(m.error) ? 'OpenF1 locked during live session' : 'no data'}</option>`;
    list.title = m.error || '';
    return;
  }
  list.title = '';
  const groups = [];
  for (const it of m.items) {
    let g = groups.at(-1);
    if (!g || g.meeting !== it.meeting) groups.push(g = { meeting: it.meeting, items: [] });
    g.items.push(it);
  }
  list.innerHTML = groups.map((g) => `<optgroup label="${esc(g.meeting)}">${
    g.items.map((it) => {
      const day = new Date(it.start).toLocaleDateString(LOCALE, { day: 'numeric', month: 'short' });
      return `<option value="${it.key}" ${it.available ? '' : 'disabled'}>${
        esc(it.name)} · ${day}${it.available ? '' : ' (upcoming)'}</option>`;
    }).join('')}</optgroup>`).join('');
  // По умолчанию — открытая сессия, иначе последняя прошедшая.
  const avail = m.items.filter((it) => it.available);
  const cur = m.items.find((it) => it.key === pb.sessionKey);
  list.value = String(cur ? cur.key : (avail.at(-1) || m.items[0]).key);
}

$('ses-load').onclick = () => {
  const key = Number($('ses-list').value);
  if (key) send({ cmd: 'load', session_key: key });
};

// ---------- вкладка Strategy: race trace, стинты, модель шин ----------

let strat = null;
const TYRE = { SOFT: '#ff3b3b', MEDIUM: '#ffd23f', HARD: '#eef0f4', INTERMEDIATE: '#3ccf4e', WET: '#2e8bff' };
// Поля строки круга с сервера (live/strategy.py:strategy_view)
const L = { lap: 0, t: 1, pos: 2, gap: 3, down: 4, comp: 5, age: 6, stint: 7, flags: 8 };

function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  c.font = '600 11px Titillium Web, system-ui';
  return [c, w, h];
}

function drawStrategy() {
  if (!strat || $('view-strat').hidden) return;
  drawTrace();
  drawStints();
  renderModel();
}

function maxLap() {
  let m = strat.totalLaps || 0;
  for (const laps of Object.values(strat.laps)) if (laps.length) m = Math.max(m, laps.at(-1)[L.lap]);
  return Math.max(m, 5);
}

function drawTrace() {
  const [c, w, h] = setupCanvas($('c-trace'));
  const isRace = /race|sprint/i.test(strat.sessionType);
  if (!isRace) {
    c.fillStyle = '#8b92a1'; c.textAlign = 'center';
    c.fillText('Race trace is available for races and sprints', w / 2, h / 2);
    c.textAlign = 'start';
    return;
  }
  const pad = { l: 44, r: 46, t: 12, b: 26 };
  const N = maxLap();
  // Масштаб по отрыву: 95-й перцентиль, чтобы один отставший не сжал график.
  const gaps = [];
  for (const laps of Object.values(strat.laps)) for (const r of laps) if (r[L.gap] != null) gaps.push(r[L.gap]);
  gaps.sort((a, b) => a - b);
  const gMax = Math.max(10, Math.ceil((gaps[Math.floor(gaps.length * 0.95)] || 10) / 10) * 10);
  const X = (lap) => pad.l + (lap - 1) / Math.max(1, N - 1) * (w - pad.l - pad.r);
  const Y = (g) => pad.t + Math.min(g, gMax) / gMax * (h - pad.t - pad.b);

  // Нейтрализации — по кругам лидера
  const leaderLaps = new Map();
  for (const laps of Object.values(strat.laps)) for (const r of laps) {
    if (r[L.pos] === 1) leaderLaps.set(r[L.lap], r[L.flags] & 4);
  }
  c.fillStyle = '#ffd23f1f';
  const cw = (w - pad.l - pad.r) / Math.max(1, N - 1);
  for (const [lap, dirty] of leaderLaps) if (dirty) c.fillRect(X(lap) - cw / 2, pad.t, cw, h - pad.t - pad.b);

  // Оси
  c.strokeStyle = '#242833'; c.fillStyle = '#5d6372'; c.lineWidth = 1;
  for (let g = 0; g <= gMax; g += gMax > 60 ? 20 : 10) {
    c.beginPath(); c.moveTo(pad.l, Y(g)); c.lineTo(w - pad.r, Y(g)); c.stroke();
    c.fillText(`+${g}`, 8, Y(g) + 4);
  }
  const step = N > 40 ? 10 : 5;
  for (let lap = step; lap <= N; lap += step) c.fillText(lap, X(lap) - 6, h - 8);

  const nums = Object.keys(strat.laps).sort((a, b) => (a === selected) - (b === selected));
  for (const num of nums) {
    const laps = strat.laps[num];
    const d = strat.drivers[num] || { tla: num, color: '#888' };
    const sel = num === selected;
    c.globalAlpha = selected && !sel ? 0.35 : 0.9;
    c.strokeStyle = d.color; c.lineWidth = sel ? 3 : 1.4;
    c.beginPath();
    let pen = false, last = null;
    for (const r of laps) {
      if (r[L.gap] == null) { pen = false; continue; }      // круг отставания — разрыв
      const x = X(r[L.lap]), y = Y(r[L.gap]);
      pen ? c.lineTo(x, y) : c.moveTo(x, y);
      pen = true; last = [x, y];
    }
    c.stroke();
    if (last) {
      c.fillStyle = sel ? '#fff' : d.color;
      c.font = `${sel ? 900 : 700} ${sel ? 12 : 10}px Titillium Web, system-ui`;
      c.fillText(d.tla, last[0] + 4, last[1] + 4);
    }
  }
  c.globalAlpha = 1;
}

function drawStints() {
  const [c, w, h] = setupCanvas($('c-stints'));
  const order = state ? state.rows.map((r) => r.num) : Object.keys(strat.laps);
  const nums = order.filter((n) => strat.laps[n] && strat.laps[n].length);
  if (!nums.length) return;
  const pad = { l: 46, r: 14, t: 6, b: 22 };
  const N = maxLap();
  const rowH = (h - pad.t - pad.b) / nums.length;
  const X = (lap) => pad.l + (lap - 0.5) / N * (w - pad.l - pad.r);
  const lapW = (w - pad.l - pad.r) / N;

  nums.forEach((num, i) => {
    const y = pad.t + i * rowH;
    const d = strat.drivers[num] || { tla: num, color: '#888' };
    if (num === selected) { c.fillStyle = '#ffffff14'; c.fillRect(0, y, w, rowH); }
    c.fillStyle = num === selected ? '#fff' : '#8b92a1';
    c.font = `${num === selected ? 900 : 700} ${Math.min(11, rowH - 2)}px Titillium Web, system-ui`;
    c.fillText(d.tla, 8, y + rowH / 2 + 4);
    // Стинты: подряд идущие круги с одним номером стинта
    let start = null;
    const laps = strat.laps[num];
    laps.forEach((r, j) => {
      const next = laps[j + 1];
      if (start == null) start = r;
      if (!next || next[L.stint] !== r[L.stint]) {
        const x0 = X(start[L.lap]) - lapW / 2, x1 = X(r[L.lap]) + lapW / 2;
        c.fillStyle = TYRE[r[L.comp]] || '#5d6372';
        c.globalAlpha = 0.85;
        c.fillRect(x0 + 1, y + rowH * 0.18, x1 - x0 - 2, rowH * 0.64);
        c.globalAlpha = 1;
        if (x1 - x0 > 22 && rowH > 9) {
          c.fillStyle = '#0b0c10'; c.font = `700 ${Math.min(10, rowH - 4)}px Titillium Web, system-ui`;
          c.fillText((r[L.comp] || '?')[0], x0 + 4, y + rowH / 2 + 3.5);
        }
        start = null;
      }
    });
  });
  // Текущий круг
  if (strat.currentLap) {
    c.strokeStyle = '#e10600'; c.lineWidth = 1.5;
    const x = X(strat.currentLap);
    c.beginPath(); c.moveTo(x, pad.t); c.lineTo(x, h - pad.b); c.stroke();
  }
  c.fillStyle = '#5d6372'; c.font = '600 11px Titillium Web, system-ui';
  const step = N > 40 ? 10 : 5;
  for (let lap = step; lap <= N; lap += step) c.fillText(lap, X(lap) - 6, h - 6);
}

function renderModel() {
  const order = ['SOFT', 'MEDIUM', 'HARD', 'INTERMEDIATE', 'WET'];
  $('m-note').textContent = strat.cleanLaps ? `${strat.cleanLaps} clean laps` : '';
  const rows = order.filter((cpd) => strat.deg[cpd]).map((cpd) => {
    const d = strat.deg[cpd];
    const ci = d.se != null ? ` ± ${(1.96 * d.se).toFixed(3)}` : '';
    const badge = !d.enough ? '<span class="badge">few laps</span>'
      : d.significant ? '<span class="badge ok">wear</span>'
      : d.improving ? '<span class="badge evo" title="Laps get faster: the track gains grip quicker than the tyres wear">track improving</span>'
      : '<span class="badge">no clear wear</span>';
    return `<div class="deg-row"><span class="tyre t-${cpd}">${cpd[0]}</span>
      <div><b>${d.deg >= 0 ? '+' : ''}${d.deg.toFixed(3)} s/lap</b><small>${cpd}${ci} · ${d.n} laps</small></div>${badge}</div>`;
  });
  $('m-deg').innerHTML = rows.join('') || '<p class="fine">Waiting for clean laps…</p>';
  $('m-loss').innerHTML = `Pit stop costs <b>${strat.pitLoss.value.toFixed(1)} s</b> ${
    strat.pitLoss.stops ? `(median of ${strat.pitLoss.stops} green-flag stops)` : '(default until first stops)'}`;

  // Пилот: выбранный, иначе лидер
  const lead = state && state.rows.length ? state.rows[0].num : null;
  const num = selected || lead;
  const w = num && strat.windows[num];
  const d = num && strat.drivers[num];
  const box = $('m-driver');
  if (!w || !d) {
    box.innerHTML = `<p class="fine">${strat.totalLaps ? 'Pick a driver to see their pit window.'
      : 'Pit windows need the race distance (available in races).'}</p>`;
    box.style.removeProperty('--c');
    return;
  }
  box.style.setProperty('--c', d.color);
  const opts = w.options.length ? w.options.map((o) => {
    const good = o.gain > 0;
    const when = o.window[0] === o.window[1] ? `lap ${o.window[0]}` : `laps ${o.window[0]}–${o.window[1]}`;
    return `<div class="box-opt"><span class="tyre t-${o.compound}">${o.compound[0]}</span>
      <span>${good ? `<b>Box lap ${o.stop_lap}</b> · window ${when}` : '<b>Stay out</b> — no gain from stopping'}</span>
      <span class="gain ${good ? '' : 'neg'}">${good ? '+' : ''}${o.gain.toFixed(1)} s</span></div>`;
  }).join('') : `<p class="fine">Not enough data on ${w.wet ? 'wet-weather tyres' : 'other compounds'} yet.</p>`;
  const fl = w.fastestLap;
  let flHtml = '';
  if (fl) {
    const lapTxt = (v) => v == null ? '—' : `${Math.floor(v / 60)}:${(v % 60).toFixed(3).padStart(6, '0')}`;
    const when = fl.window[0] === fl.window[1] ? `lap ${fl.window[0]}` : `laps ${fl.window[0]}–${fl.window[1]}`;
    const gapTxt = fl.lapped ? (fl.behind ? `${esc(fl.behind)} is a lap down` : 'no car behind')
      : fl.gapBehind != null ? `${fl.gapBehind.toFixed(1)} s to ${esc(fl.behind)}` : 'gap behind unknown';
    const head = fl.mine ? '<b>Holds fastest lap</b> · stop only to defend it'
      : fl.status === 'free' ? `<b>Fastest-lap stop</b> · box ${when}`
      : fl.status === 'tight' ? `<b>Fastest-lap stop?</b> · box ${when}, may lose a place`
      : '<b>Fastest-lap stop</b> — would lose a place';
    const need = fl.need == null ? ''
      : fl.need > 0 ? ` · fresh tyres recover ${fl.recovered.toFixed(1)} s of wear, still ${fl.need.toFixed(1)} s off (+ soft grip)`
      : ` · fresh tyres recover ${fl.recovered.toFixed(1)} s of wear — enough`;
    flHtml = `<div class="box-opt fl ${fl.status}"><span class="tyre t-SOFT">S</span>
      <span>${head}<small>${gapTxt} vs pit loss ${fl.loss.toFixed(1)} s · fastest ${lapTxt(fl.record)}${
        fl.holder ? ` (${esc(fl.holder)})` : ''} · pace ${lapTxt(fl.pace)}${need}</small></span>
      <span class="gain ${fl.status === 'free' ? '' : fl.status === 'tight' ? 'warn' : 'neg'}">+1 pt</span></div>`;
  }
  box.innerHTML = `<h3>${esc(d.tla)}<small>${selected ? '' : 'leader · '}${w.laps_left} laps to go</small></h3>
    <div class="now"><span class="tyre t-${w.compound}">${w.compound[0]}</span> ${w.compound} · ${w.age} laps old</div>
    ${opts}${flHtml}${w.wet ? '<p class="fine">Wet race: options cover a fresh set of rain tyres. '
      + 'The switch to slicks depends on the weather and is not modelled.</p>' : ''}`;
}

// ---------- плавное движение: интерполяция с задержкой ----------
//
// Сервер шлёт все сэмплы координат с метками времени фида. Рисуем картинку
// с небольшой задержкой D и интерполируем между двумя известными точками —
// движение равномерное, без рывков. D ≈ 0.5 с реального времени и сама
// подстраивается, если данные приходят пачками (live-фид F1 — раз в ~1 с).

const tracks = {};      // num → [[t, x, y, on], ...] по возрастанию t
let feedEst = null;     // оценка текущего времени фида, мс
let lagMax = 0;         // наблюдаемое запаздывание данных, мс фида

function playSpeed() { return pb.mode === 'live' ? 1 : pb.speed || 1; }
function isPlaying() { return pb.mode === 'live' || (pb.loaded && pb.playing); }
function renderDelay() {
  const sp = playSpeed();
  return Math.max((0.3 + 0.2 * sp) * 1000, lagMax + 150 * sp);
}

function onPos(m) {
  if (m.reset) {
    for (const k of Object.keys(tracks)) delete tracks[k];
    lagMax = 0;
  }
  if (m.now != null) {
    const sp = playSpeed();
    if (feedEst == null || m.reset || Math.abs(m.now - feedEst) > 2000 * Math.max(1, sp)) {
      feedEst = m.now;                         // перемотка / старт — прыжок
    } else {
      feedEst += (m.now - feedEst) * 0.15;     // мягкая подстройка часов
    }
  }
  let newest = -Infinity;
  for (const [t, entries] of m.frames) {
    newest = Math.max(newest, t);
    for (const [num, v] of Object.entries(entries)) {
      const arr = tracks[num] || (tracks[num] = []);
      if (arr.length && arr.at(-1)[0] >= t) continue;
      arr.push([t, v[0], v[1], v[2]]);
      if (arr.length > 400) arr.splice(0, arr.length - 400);
    }
  }
  if (feedEst != null && newest > -Infinity) {
    lagMax = Math.max(lagMax * 0.98, feedEst - newest);
  }
}

// Кубическая интерполяция Эрмита: скорость в узлах оценивается по соседям,
// поэтому и положение, и скорость меняются плавно (без изломов в точках).
function velocity(arr, i) {
  const a = arr[Math.max(i - 1, 0)], b = arr[Math.min(i + 1, arr.length - 1)];
  const dt = b[0] - a[0];
  return dt > 0 ? [(b[1] - a[1]) / dt, (b[2] - a[2]) / dt] : [0, 0];
}

function sampleAt(arr, t) {
  if (!arr || !arr.length) return null;
  if (t <= arr[0][0]) return arr[0];
  const last = arr.at(-1);
  if (t >= last[0]) return last;
  let i = arr.length - 2;
  while (i > 0 && arr[i][0] > t) i--;
  const a = arr[i], b = arr[i + 1];
  const h = b[0] - a[0] || 1;
  const s = (t - a[0]) / h;
  // Большой разрыв (пит-лейн, потеря данных) — без «петель», просто линейно.
  if (h > 2000) return [t, a[1] + (b[1] - a[1]) * s, a[2] + (b[2] - a[2]) * s, b[3]];
  const va = velocity(arr, i), vb = velocity(arr, i + 1);
  const s2 = s * s, s3 = s2 * s;
  const h00 = 2 * s3 - 3 * s2 + 1, h10 = s3 - 2 * s2 + s, h01 = -2 * s3 + 3 * s2, h11 = s3 - s2;
  return [t,
    h00 * a[1] + h10 * h * va[0] + h01 * b[1] + h11 * h * vb[0],
    h00 * a[2] + h10 * h * va[1] + h01 * b[2] + h11 * h * vb[1],
    b[3]];
}

// ---------- карта трассы ----------

const canvas = $('map');
const ctx = canvas.getContext('2d');
let bounds = null;
let rot = 0;

function rotate(x, y) {
  const c = Math.cos(rot), s = Math.sin(rot);
  return [x * c - y * s, x * s + y * c];
}

function fitMap() {
  rot = (outline.rotation || 0) * Math.PI / 180;
  let pts = outline.points;
  if (!outline.done && state) {
    pts = pts.concat(Object.values(state.cars).filter((c) => c.x || c.y).map((c) => [c.x, c.y]));
  }
  if (pts.length < 2) { bounds = null; return; }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) {
    const [rx, ry] = rotate(x, y);
    x0 = Math.min(x0, rx); x1 = Math.max(x1, rx);
    y0 = Math.min(y0, ry); y1 = Math.max(y1, ry);
  }
  bounds = { x0, y0, x1, y1 };
  $('mapnote').textContent = outline.source === 'multiviewer' ? 'outline: MultiViewer'
    : outline.source === 'fastest-lap' ? 'outline: session fastest lap'
    : outline.done ? 'outline: recorded lap'
    : outline.points.length ? 'recording outline…' : 'waiting for position data';
}

// Вид карты: зум z вокруг точки (cx, cy) в «базовых» экранных координатах.
const view = { z: 1, cx: null, cy: null, follow: false };

function baseProject(x, y, w, h) {
  const pad = 34;
  const [rx, ry] = rotate(x, y);
  const b = bounds;
  const k = Math.min((w - 2 * pad) / (b.x1 - b.x0 || 1), (h - 2 * pad) / (b.y1 - b.y0 || 1));
  const ox = (w - (b.x1 - b.x0) * k) / 2;
  const oy = (h - (b.y1 - b.y0) * k) / 2;
  return [ox + (rx - b.x0) * k, h - (oy + (ry - b.y0) * k)];   // Y вверх
}

function project(x, y, w, h) {
  const [bx, by] = baseProject(x, y, w, h);
  const cx = view.cx ?? w / 2, cy = view.cy ?? h / 2;
  return [(bx - cx) * view.z + w / 2, (by - cy) * view.z + h / 2];
}

function resetView() { view.z = 1; view.cx = view.cy = null; }

function zoomAt(factor, sx, sy) {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  const cx = view.cx ?? w / 2, cy = view.cy ?? h / 2;
  const z = Math.min(12, Math.max(1, view.z * factor));
  // Точка под курсором остаётся на месте.
  const bx = (sx - w / 2) / view.z + cx, by = (sy - h / 2) / view.z + cy;
  view.cx = bx - (sx - w / 2) / z; view.cy = by - (sy - h / 2) / z;
  view.z = z;
  if (z === 1) resetView();
}

function syncFollowBtn() {
  $('m-follow').classList.toggle('on', view.follow && !!selected);
  $('m-follow').disabled = !selected;
}

canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const r = canvas.getBoundingClientRect();
  if (!view.follow) zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
  else view.z = Math.min(12, Math.max(1, view.z * Math.exp(-e.deltaY * 0.0015)));
}, { passive: false });

let drag = null;
canvas.addEventListener('pointerdown', (e) => {
  drag = { x: e.clientX, y: e.clientY };
  canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointermove', (e) => {
  if (!drag || view.z === 1) return;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  view.cx = (view.cx ?? w / 2) - (e.clientX - drag.x) / view.z;
  view.cy = (view.cy ?? h / 2) - (e.clientY - drag.y) / view.z;
  drag = { x: e.clientX, y: e.clientY };
  if (view.follow) { view.follow = false; syncFollowBtn(); }
});
canvas.addEventListener('pointerup', () => { drag = null; });

$('m-in').onclick = () => zoomAt(1.6, canvas.clientWidth / 2, canvas.clientHeight / 2);
$('m-out').onclick = () => zoomAt(1 / 1.6, canvas.clientWidth / 2, canvas.clientHeight / 2);
$('m-reset').onclick = () => { view.follow = false; resetView(); syncFollowBtn(); };
$('m-follow').onclick = () => {
  view.follow = !view.follow && !!selected;
  if (view.follow && view.z < 2.5) view.z = 2.5;
  if (!view.follow) resetView();
  syncFollowBtn();
};

function trackPath(pp, closed) {
  const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  ctx.beginPath();
  if (closed) {
    const n = pp.length;
    ctx.moveTo(...mid(pp[n - 1], pp[0]));
    for (let i = 0; i < n; i++) ctx.quadraticCurveTo(...pp[i], ...mid(pp[i], pp[(i + 1) % n]));
    ctx.closePath();
  } else {
    pp.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
  }
}

function drawTrack(w, h) {
  const pts = outline.points;
  if (pts.length < 2) return;
  const pp = pts.map(([x, y]) => project(x, y, w, h));
  const closed = outline.done && pp.length > 2;
  ctx.lineJoin = ctx.lineCap = 'round';
  trackPath(pp, closed);
  ctx.strokeStyle = '#0a0b0e'; ctx.lineWidth = 16; ctx.stroke();      // тень
  ctx.strokeStyle = '#3b404c'; ctx.lineWidth = 11; ctx.stroke();      // бордюр
  ctx.strokeStyle = '#262a33'; ctx.lineWidth = 8; ctx.stroke();       // асфальт
  ctx.setLineDash([2, 7]);
  ctx.strokeStyle = '#ffffff22'; ctx.lineWidth = 1; ctx.stroke();     // осевая
  ctx.setLineDash([]);
  drawSectors(pp, closed);

  // Линия старт-финиша — начало контура по лучшему кругу.
  if (outline.source === 'fastest-lap' && pp.length > 3) {
    const [ax, ay] = pp[0], [bx, by] = pp[2];
    const len = Math.hypot(bx - ax, by - ay) || 1;
    const nx = -(by - ay) / len, ny = (bx - ax) / len;
    for (let i = -3; i < 3; i++) {
      ctx.fillStyle = i % 2 ? '#fff' : '#111';
      ctx.fillRect(ax + nx * i * 2.2 - 1.5, ay + ny * i * 2.2 - 1.5, 3, 3);
    }
  }

  ctx.font = '600 10px Titillium Web, system-ui';
  ctx.fillStyle = '#6b7282';
  for (const c of outline.corners || []) {
    const [px, py] = project(c.x, c.y, w, h);
    ctx.fillText(c.n, px + 8, py - 8);
  }
}

const SECTOR_COLORS = ['#ff5d73', '#4dabf7', '#ffd43b'];

function drawSectors(pp, closed) {
  const sec = outline.sectors || [];
  if (!closed || sec.length !== 2) return;
  const n = pp.length;
  const bounds3 = [[0, sec[0]], [sec[0], sec[1]], [sec[1], n]];
  ctx.lineWidth = 2.5;
  bounds3.forEach(([a, b], k) => {
    ctx.beginPath();
    for (let i = a; i <= b; i++) {
      const [px, py] = pp[i % n];
      i === a ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    }
    ctx.strokeStyle = SECTOR_COLORS[k] + '99';
    ctx.stroke();
  });
  // Границы секторов и подписи снаружи трассы.
  const normal = (i) => {
    const [ax, ay] = pp[(i - 1 + n) % n], [bx, by] = pp[(i + 1) % n];
    const len = Math.hypot(bx - ax, by - ay) || 1;
    return [-(by - ay) / len, (bx - ax) / len];
  };
  ctx.font = '700 10px Titillium Web, system-ui';
  ctx.textAlign = 'center';
  bounds3.forEach(([a, b], k) => {
    const [nx, ny] = normal(a);
    const [px, py] = pp[a % n];
    ctx.strokeStyle = '#ffffffcc'; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(px - nx * 8, py - ny * 8); ctx.lineTo(px + nx * 8, py + ny * 8); ctx.stroke();
    const m = Math.floor((a + b) / 2) % n;
    const [mx, my] = pp[m], [mnx, mny] = normal(m);
    ctx.fillStyle = SECTOR_COLORS[k];
    ctx.fillText(`SECTOR ${k + 1}`, mx + mnx * 22, my + mny * 22 + 3);
  });
  ctx.textAlign = 'start';
}

function roundRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawCars(w, h) {
  if (!state) return;
  const tRender = feedEst != null ? feedEst - renderDelay() : null;
  const order = {};
  state.rows.forEach((r) => { order[r.num] = r.pos; });
  // Лидер и выбранный пилот — поверх остальных.
  const nums = Object.keys(state.cars).sort((a, b) =>
    (a === selected) - (b === selected) || (order[b] ?? 99) - (order[a] ?? 99));

  for (const num of nums) {
    const c = state.cars[num];
    const s = tRender != null ? sampleAt(tracks[num], tRender) : null;
    const x = s ? s[1] : c.x, y = s ? s[2] : c.y, on = s ? s[3] : c.on;
    if (!x && !y) continue;
    const [px, py] = project(x, y, w, h);
    const sel = num === selected;
    const lead = order[num] === 1;
    ctx.globalAlpha = on ? 1 : 0.35;

    if (sel) {
      ctx.beginPath(); ctx.arc(px, py, 14, 0, Math.PI * 2);
      ctx.fillStyle = c.color + '44'; ctx.fill();
    }
    ctx.beginPath(); ctx.arc(px, py, sel ? 8 : 6.5, 0, Math.PI * 2);
    ctx.fillStyle = c.color; ctx.fill();
    ctx.lineWidth = lead || sel ? 2.5 : 1.5;
    ctx.strokeStyle = lead ? '#ffd23f' : sel ? '#fff' : '#0b0c10';
    ctx.stroke();

    if (sel || nums.length <= 22) {
      ctx.font = `${sel ? 900 : 700} ${sel ? 12 : 10}px Titillium Web, system-ui`;
      const tw = ctx.measureText(c.tla).width;
      const lx = px + 10, ly = py - 8;
      roundRect(lx - 3, ly, tw + 6, sel ? 16 : 14, 3);
      ctx.fillStyle = sel ? '#ffffff' : '#0b0c10cc'; ctx.fill();
      ctx.fillStyle = sel ? '#0b0c10' : '#e9ebf0';
      ctx.fillText(c.tla, lx, ly + (sel ? 12 : 10.5));
    }
    ctx.globalAlpha = 1;
  }
}

function carPos(num) {
  const tRender = feedEst != null ? feedEst - renderDelay() : null;
  const s = tRender != null ? sampleAt(tracks[num], tRender) : null;
  const c = state && state.cars[num];
  return s ? [s[1], s[2]] : c ? [c.x, c.y] : null;
}

function followSelected(w, h) {
  if (!view.follow || !selected || view.z <= 1) return;
  const p = carPos(selected);
  if (!p || (!p[0] && !p[1])) return;
  const [bx, by] = baseProject(p[0], p[1], w, h);
  if (view.cx == null) { view.cx = bx; view.cy = by; }
  view.cx += (bx - view.cx) * 0.2;      // мягкое слежение камеры
  view.cy += (by - view.cy) * 0.2;
}

// ---------- спидометр (SVG, без картинок) ----------

let telBuf = [];          // [[t, speed, rpm, gear, throttle, brake, drs]]
let telState = null;      // loading | ok | unavailable

function onTel(m) {
  if (m.num !== selected) return;
  if (m.unavailable) { telState = 'unavailable'; return; }
  if (m.reset) telBuf = [];
  for (const f of m.frames) if (!telBuf.length || f[0] > telBuf.at(-1)[0]) telBuf.push(f);
  if (telBuf.length > 800) telBuf.splice(0, telBuf.length - 800);
  if (telBuf.length) telState = 'ok';
}

function sampleTel(t) {
  const a = telBuf;
  if (!a.length || t < a[0][0] - 1000 || t > a.at(-1)[0] + 3000) return null;
  let i = a.length - 1;
  while (i > 0 && a[i][0] > t) i--;
  const p = a[i], q = a[Math.min(i + 1, a.length - 1)];
  const k = q[0] > p[0] ? Math.min(1, Math.max(0, (t - p[0]) / (q[0] - p[0]))) : 0;
  const lerp = (j) => p[j] + (q[j] - p[j]) * k;
  return { speed: lerp(1), rpm: lerp(2), gear: p[3], thr: lerp(4), brk: p[5], drs: p[6] };
}

const G = { cx: 120, cy: 118 };
const svgNS = 'http://www.w3.org/2000/svg';
function polar(r, a) {
  const rad = a * Math.PI / 180;
  return [G.cx + r * Math.sin(rad), G.cy - r * Math.cos(rad)];
}
function arcD(r, a0, a1) {
  if (a1 - a0 < 0.01) return '';
  const [x0, y0] = polar(r, a0), [x1, y1] = polar(r, a1);
  return `M${x0.toFixed(1)} ${y0.toFixed(1)}A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}
function el(tag, attrs, text) {
  const e = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  $('gauge').appendChild(e);
  return e;
}

// Внешнее кольцо — скорость (0–360 км/ч), внутреннее — газ (слева) и тормоз (справа).
const R_SPEED = 102, R_PEDAL = 76;
const gauge = (() => {
  const track = { fill: 'none', 'stroke-linecap': 'butt' };
  el('path', { ...track, d: arcD(R_SPEED, -135, 135), stroke: '#262a33', 'stroke-width': 10 });
  el('path', { ...track, d: arcD(R_PEDAL, -135, -4), stroke: '#1d2230', 'stroke-width': 9 });
  el('path', { ...track, d: arcD(R_PEDAL, 4, 135), stroke: '#2a1a1f', 'stroke-width': 9 });
  for (let v = 0; v <= 350; v += 50) {
    const a = -135 + 270 * v / 360;
    const [x0, y0] = polar(R_SPEED + 5, a), [x1, y1] = polar(R_SPEED - 5, a);
    el('line', { x1: x0, y1: y0, x2: x1, y2: y1, stroke: '#0b0c10', 'stroke-width': 1.5 });
    const [x, y] = polar(R_SPEED - 15, a);
    el('text', { x, y: y + 3, 'text-anchor': 'middle', class: 'g-tick' }, v);
  }
  const g = {
    spd: el('path', { ...track, stroke: '#eef0f4', 'stroke-width': 10 }),
    thr: el('path', { ...track, stroke: '#3b82f6', 'stroke-width': 9 }),
    brk: el('path', { ...track, stroke: '#ff3b3b', 'stroke-width': 9 }),
    speed: el('text', { x: 120, y: 122, 'text-anchor': 'middle', class: 'g-speed' }, '–'),
    unit: el('text', { x: 120, y: 137, 'text-anchor': 'middle', class: 'g-unit' }, 'KM/H'),
    rpm: el('text', { x: 120, y: 156, 'text-anchor': 'middle', class: 'g-rpm' }, ''),
    gear: el('text', { x: 120, y: 175, 'text-anchor': 'middle', class: 'g-gear' }, ''),
    mode: el('text', { x: 120, y: 214, 'text-anchor': 'middle', class: 'g-mode' }, ''),
  };
  el('text', { x: 76, y: 198, 'text-anchor': 'middle', class: 'g-lbl' }, 'THROTTLE');
  el('text', { x: 164, y: 198, 'text-anchor': 'middle', class: 'g-lbl' }, 'BRAKE');
  return g;
})();

function updateGauge() {
  const tRender = feedEst != null ? feedEst - renderDelay() : null;
  const v = tRender != null ? sampleTel(tRender) : null;
  const note = telState === 'loading' ? 'loading telemetry…'
    : telState === 'unavailable' ? 'telemetry is available for OpenF1 sessions only'
    : !v ? 'no telemetry at this moment' : '';
  $('tel-note').textContent = note;
  const thr = v ? Math.min(100, Math.max(0, v.thr)) / 100 : 0;
  const brk = v ? (v.brk > 0 ? 1 : 0) : 0;
  const spd = v ? Math.min(360, v.speed) / 360 : 0;
  gauge.spd.setAttribute('d', arcD(R_SPEED, -135, -135 + 270 * spd));
  gauge.thr.setAttribute('d', arcD(R_PEDAL, -135, -135 + 131 * thr));
  gauge.brk.setAttribute('d', arcD(R_PEDAL, 135 - 131 * brk, 135));
  gauge.speed.textContent = v ? Math.round(v.speed) : '–';
  gauge.rpm.textContent = v ? `${Math.round(v.rpm).toLocaleString(LOCALE)} RPM` : '';
  gauge.gear.textContent = v ? `GEAR ${v.gear || 'N'}` : '';
  // DRS машины — если есть в телеметрии (до 2026); иначе режим сессии по Race Control
  // (в 2026 — Overtake: включает ли его конкретный пилот, в открытых данных нет).
  let mode = '', on = false;
  if (v && v.drs >= 0) { mode = 'DRS'; on = v.drs >= 10; }
  else if (state && state.overtake) {
    mode = `${state.overtake.name.toUpperCase()} ${state.overtake.on ? 'ENABLED' : 'DISABLED'}`;
    on = state.overtake.on;
  }
  gauge.mode.textContent = mode;
  gauge.mode.classList.toggle('on', on);
}

let lastFrame = performance.now();
function frame(now) {
  const dt = Math.min(250, now - lastFrame);
  lastFrame = now;
  if (feedEst != null && isPlaying()) feedEst += dt * playSpeed();

  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (bounds) {
    followSelected(w, h);
    drawTrack(w, h);
    drawCars(w, h);
  }
  if (!$('drv').hidden && !$('view-tele').hidden) updateGauge();
  requestAnimationFrame(frame);
}

let initialTab = 'timing';
try { initialTab = localStorage.getItem('f1tab') || 'timing'; } catch (e) { /* нет storage */ }
setTab(['tele', 'strat'].includes(initialTab) ? initialTab : 'timing');
window.addEventListener('resize', () => drawStrategy());
requestAnimationFrame(frame);
connect();
