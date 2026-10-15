/* ============================================================
   HomeMind 控制台 · 前端主逻辑（零构建，原生 ES2020）
   对齐《界面设计文档 v1.1》第 4-8 章
   ------------------------------------------------------------
   三条铁律：
   1. 设备状态只有一份真相 —— 永远来自后端 /api/console/state，
      界面上"点开关"和"跟 AI 说关灯"走的是同一套后端工具，
      所以不会出现「AI 说关了、卡片还亮着」的撕裂。
   2. 手势裁决优先 —— 点击 / 左滑 / 上滑三者共存，靠
      主轴判定（|dx| vs |dy|）+ 阈值裁决，不靠"猜"。
   3. 危险操作必须显式确认 —— 门锁之类的高危设备，
      前端不替用户做决定，一律弹层确认（与后端安全护栏一致）。
   ============================================================ */

(() => {
  'use strict';

  // ---------------------------------------------------------- 基础工具
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const WEEKDAY_CN = { Monday: '周一', Tuesday: '周二', Wednesday: '周三', Thursday: '周四',
    Friday: '周五', Saturday: '周六', Sunday: '周日' };

  const DEVICE_ICON = { light: 'i-light', ac: 'i-ac', curtain: 'i-curtain', lock: 'i-lock',
    washer: 'i-washer', robot: 'i-robot', dryer: 'i-laundry', humidifier: 'i-humidifier',
    valve: 'i-valve',
    // 青岛家的设备（地暖/新风/油烟机）也得有图标，不然卡片上是空的
    heater: 'i-thermo', fan: 'i-drop', hood: 'i-washer' };
  const CATEGORY_ICON = { plant: 'i-plant', laundry: 'i-laundry', pet: 'i-pet',
    security: 'i-security', cleaning: 'i-robot', maintenance: 'i-security', other: 'i-task',
    reminder: 'i-bell' };   // AI 创建的提醒
  const SCENE_ICON = { home: 'i-home', away: 'i-exit', movie: 'i-movie', sleep: 'i-moon',
    wake: 'i-sun', vent: 'i-drop' };
  const SRC_TEXT = { automation: '自动化', 'device-event': '设备触发', manual: '手动',
    ai: 'AI 创建' };

  function icon(id, cls = 'hm-ico') {
    return `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
  }

  // 消息中心以前的时间是写死的"12 分钟前""1 小时前"——现在消息是真的，
  // 时间也必须从事件的真实时间戳算出来，否则又是一处假数据。
  function relTime(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return iso;
    const diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
    if (diff < 172800) {
      return `昨天 ${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
    }
    return `${t.getMonth() + 1}月${t.getDate()}日`;
  }
  function hexA(hex, alpha) {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex || '');
    if (!m) return hex || 'transparent';
    return `rgba(${parseInt(m[1], 16)}, ${parseInt(m[2], 16)}, ${parseInt(m[3], 16)}, ${alpha})`;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function pad2(n) { return String(n).padStart(2, '0'); }

  function fmtDuration(min) {
    if (min < 60) return `${min} 分钟`;
    const h = Math.floor(min / 60);
    if (h < 24) return `${h} 小时${min % 60 ? ` ${min % 60} 分` : ''}`;
    return `${Math.floor(h / 24)} 天${h % 24 ? ` ${h % 24} 小时` : ''}`;
  }

  function fmtScheduleTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso || '');
    return `${d.getMonth() + 1}月${d.getDate()}日 ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  }

  /** 截止时间 → 展示文案（逾期 / 即将 / 今天 hh:mm）。 */
  function dueInfo(task) {
    const due = new Date(task.dueAt);
    const diffMin = Math.round((due - Date.now()) / 60000);
    if (diffMin < 0) return { text: `逾期 ${fmtDuration(-diffMin)}`, cls: 'is-overdue' };
    if (diffMin <= 90) return { text: `${Math.max(1, diffMin)} 分钟后到期`, cls: 'is-soon' };
    return { text: `今天 ${pad2(due.getHours())}:${pad2(due.getMinutes())}`, cls: '' };
  }

  // ---------------------------------------------------------- 登录态
  const TOKEN_KEY = 'hm_token';

  function getToken() { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } }
  function setToken(t) { try { localStorage.setItem(TOKEN_KEY, t); } catch { /* 隐私模式忽略 */ } }
  function clearToken() { try { localStorage.removeItem(TOKEN_KEY); } catch { /* 同上 */ } }

  async function api(path, body, method) {
    const headers = { 'Content-Type': 'application/json' };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const verb = method || (body ? 'POST' : 'GET');
    const options = body
      ? { method: verb, headers, body: JSON.stringify(body) }
      : { method: verb, headers };
    const res = await fetch(path, options);
    if (res.status === 401) { clearToken(); showLogin('登录态失效，请重新登录'); throw new Error('unauthorized'); }
    if (!res.ok) throw new Error(`${res.status} ${path}`);
    return res.json();
  }

  /** 显示登录门禁。没有有效身份时，控制台一个数据都拿不到。 */
  async function showLogin(errorText) {
    const box = $('#login');
    box.hidden = false;
    if (errorText) {
      let el = $('#login-error');
      if (!el) {
        el = document.createElement('p');
        el.id = 'login-error';
        el.className = 'hm-login__error';
        $('#login-form').after(el);
      }
      el.textContent = errorText;
    }
    try {
      const res = await fetch('/api/auth/accounts');
      const list = (await res.json()).accounts || [];
      $('#login-accounts').innerHTML = list.map((a) => `
        <button type="button" class="hm-login__acct" data-user="${esc(a.username)}">
          <span class="hm-avatar" style="background:${a.role === 'L4' ? 'var(--hm-color-brand)' : 'var(--hm-text-tertiary)'}">${esc(a.name.slice(0, 1))}</span>
          <div class="hm-listitem__body">
            <div class="hm-listitem__title">${esc(a.name)} · ${esc(a.family.name)}</div>
            <div class="hm-listitem__desc">${esc(a.username)} · ${esc(a.city)} · ${esc(a.role)}</div>
          </div>
          ${icon('i-chevron-right', 'hm-ico hm-ico--sm')}
        </button>`).join('');
      $$('.hm-login__acct').forEach((b) => {
        b.addEventListener('click', () => {
          $('#login-user').value = b.dataset.user;
          $('#login-pass').value = 'demo1234';
          submitLogin();     // 不用 requestSubmit：老 Safari 不支持，jsdom 也没实现
        });
      });
    } catch { /* 拿不到清单就手填，不阻塞登录 */ }
  }

  async function doLogin(username, password) {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      showLogin('用户名或密码错误');
      return false;
    }
    setToken((await res.json()).token);
    $('#login').hidden = true;
    // 换账号 = 新会话：清掉上一个人的对话痕迹，别让 AI 面板还残留旧消息
    $('#sheet-body').innerHTML = '';
    $('#sheet-sub').textContent = '意图识别 · 多智能体 · 安全护栏';
    return true;
  }

  // ---------------------------------------------------------- 全局状态
  const store = {
    data: null,
    page: 'home',
    hubCollapsed: false,
    activeScene: null,
    taskFilter: null,    // 只看某个成员的任务（null = 看全部）
    lastConfirm: null,   // 待确认的高危动作（AI 侧）
  };

  // ---------------------------------------------------------- Toast
  let toastTimer = null;
  let toastUndo = null;

  function showToast(text, undo, ms = 5000) {
    const el = $('#toast');
    $('#toast-text').textContent = text;
    const undoBtn = $('#toast-undo');
    undoBtn.hidden = !undo;
    toastUndo = undo || null;
    el.classList.add('is-visible');

    const bar = $('#toast-progress');
    bar.style.animation = 'none';
    void bar.offsetWidth;                       // 强制重排，重启进度动画
    bar.style.animation = `hm-toast-progress ${ms}ms linear forwards`;

    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, ms);
  }
  function hideToast() {
    clearTimeout(toastTimer);
    $('#toast').classList.remove('is-visible');
    toastUndo = null;
  }
  $('#toast-undo').addEventListener('click', () => {
    const fn = toastUndo;
    hideToast();
    if (fn) fn();
  });

  // ---------------------------------------------------------- 确认弹层
  function openModal({ title, desc, tone = '', chips = [], okText = '确定', onOk }) {
    $('#modal-title').textContent = title;
    $('#modal-title').className = 'hm-modal__title' + (tone ? ` is-${tone}` : '');
    $('#modal-desc').textContent = desc || '';
    $('#modal-ok').textContent = okText;
    $('#modal-ok').className = 'hm-btn ' + (tone === 'danger' ? 'hm-btn--danger'
      : tone === 'warning' ? 'hm-btn--warning' : 'hm-btn--primary');

    const chipBox = $('#modal-chips');
    chipBox.innerHTML = '';
    let selected = null;
    chipBox.hidden = chips.length === 0;
    chips.forEach((label) => {
      const b = document.createElement('button');
      b.className = 'hm-chip';
      b.type = 'button';
      b.textContent = label;
      b.addEventListener('click', () => {
        $$('.hm-chip', chipBox).forEach((c) => c.classList.remove('is-selected'));
        b.classList.add('is-selected');
        selected = label;
      });
      chipBox.appendChild(b);
    });

    $('#modal-mask').classList.add('is-open');
    const ok = $('#modal-ok');
    const cancel = $('#modal-cancel');
    const close = () => {
      $('#modal-mask').classList.remove('is-open');
      ok.onclick = null; cancel.onclick = null;
    };
    ok.onclick = () => { const v = selected; close(); onOk && onOk(v); };
    cancel.onclick = close;
  }
  $('#modal-mask').addEventListener('click', (e) => {
    if (e.target === $('#modal-mask')) $('#modal-cancel').click();
  });

  // ---------------------------------------------------------- 粒子 / FLIP
  function burstBubbles(card) {
    if (reduceMotion) return;
    const layer = document.createElement('div');
    layer.className = 'hm-bubble-layer';
    card.appendChild(layer);
    const rect = card.getBoundingClientRect();
    for (let i = 0; i < 16; i += 1) {
      const b = document.createElement('span');
      b.className = 'hm-bubble';
      const size = 6 + Math.random() * 14;
      b.style.width = `${size}px`;
      b.style.height = `${size}px`;
      b.style.left = `${16 + Math.random() * Math.max(20, rect.width - 32)}px`;
      b.style.bottom = '-12px';
      layer.appendChild(b);
      const rise = rect.height * (0.5 + Math.random() * 0.9);
      const drift = (Math.random() - 0.5) * 64;
      b.animate([
        { transform: 'translate(0,0) scale(0.35)', opacity: 0 },
        { transform: `translate(${drift * 0.4}px, ${-rise * 0.45}px) scale(1)`, opacity: 0.9, offset: 0.35 },
        { transform: `translate(${drift}px, ${-rise}px) scale(0.65)`, opacity: 0 },
      ], { duration: 620 + Math.random() * 280, delay: Math.random() * 160,
           easing: 'cubic-bezier(0.22,1,0.36,1)' });
    }
    setTimeout(() => layer.remove(), 1300);
  }

  /** FLIP：先量旧位置，改 DOM，再把差值反向动画回来。 */
  function flip(container, mutate) {
    if (reduceMotion) { mutate(); return; }
    const items = $$('.hm-taskcard, .hm-listitem', container);
    const firstTop = new Map(items.map((el) => [el, el.getBoundingClientRect().top]));
    mutate();
    $$('.hm-taskcard, .hm-listitem', container).forEach((el) => {
      const from = firstTop.get(el);
      if (from == null) return;
      const delta = from - el.getBoundingClientRect().top;
      if (Math.abs(delta) < 1) return;
      el.animate(
        [{ transform: `translateY(${delta}px)` }, { transform: 'translateY(0)' }],
        { duration: 320, easing: 'cubic-bezier(0.22,1,0.36,1)' },
      );
    });
  }

  // ---------------------------------------------------------- 环境栏
  function renderEnv(env) {
    const d = new Date(env.date + 'T00:00:00');
    $('#env-date').textContent = `${d.getMonth() + 1}月${d.getDate()}日 ${WEEKDAY_CN[env.weekday] || ''}`;
    $('#env-lunar').textContent = env.lunar ? `· ${env.lunar}` : '';
    $('#env-temp').innerHTML = `${env.indoor.temp}<sup>°C</sup>`;
    const diff = env.tempDiffWarn ? '<span class="hm-badge-diff">室内外温差大</span>' : '';
    $('#env-metrics').innerHTML =
      `湿度 <b>${env.indoor.humidity}%</b> ${env.indoor.sensor ? `（${esc(env.indoor.sensor)}）` : ''}`
      + ` · 室外 <b>${env.outdoor.temp}°C</b> ${esc(env.outdoor.conditionText)}`
      + ` · 空气 <b>${env.aqi.value}</b> ${esc(env.aqi.level)} ${diff}`;
  }

  function renderHint(hint) {
    const box = $('#hint');
    if (!hint) { box.hidden = true; return; }
    box.hidden = false;
    box.className = 'hm-hint' + (hint.level === 'warning' ? ' is-warning' : '');
    const HINT_ICON = { drop: 'i-drop', thermometer: 'i-thermo', plant: 'i-plant' };
    $('#hint-icon').innerHTML = `<use href="#${HINT_ICON[hint.icon] || 'i-thermo'}"/>`;
    $('#hint-text').textContent = hint.text;
    const btn = $('#hint-action');
    btn.textContent = hint.action.label;
    btn.onclick = () => {
      if (hint.action.deviceId) {
        controlDevice(hint.action.deviceId, hint.action.action, hint.action.value);
      } else if (hint.action.taskId) {
        go('home');
        $('#taskhub').scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
      }
    };
  }

  // ---------------------------------------------------------- 任务中心
  function taskCardHtml(t) {
    const due = dueInfo(t);
    const ic = CATEGORY_ICON[t.category] || 'i-task';
    const overdue = t.status === 'pending' && due.cls === 'is-overdue';
    return `
      <span class="hm-taskcard__bar"></span>
      <div class="hm-taskcard__icon" style="background:${hexA(t.colorTag, 0.16)};color:${t.colorTag}">
        ${icon(ic)}
      </div>
      <div class="hm-taskcard__body">
        <div class="hm-taskcard__meta">
          <span class="hm-taskcard__src">${esc(SRC_TEXT[t.source] || t.source)}</span>
          <span class="${due.cls}">${esc(due.text)}</span>
        </div>
        <div class="hm-taskcard__title">${esc(t.title)}</div>
        <div class="hm-taskcard__desc">${esc(t.description || '')}</div>
      </div>
      <button class="hm-taskcard__complete" aria-label="标记完成">${icon('i-check', 'hm-ico hm-ico--sm')}</button>
      <div class="hm-taskcard__actions">
        ${icon('i-close', 'hm-ico hm-ico--sm')}<span>取消</span>
      </div>`;
  }

  function renderTaskHub() {
    const pending = store.data.tasks.filter((t) => t.status === 'pending');
    const list = $('#taskhub-list');
    const hub = $('#taskhub');

    const hasOverdue = pending.some((t) => dueInfo(t).cls === 'is-overdue');
    hub.classList.toggle('is-overdue', hasOverdue);
    $('#taskhub-title').textContent = hasOverdue ? '今日任务 · 有逾期' : '今日任务';
    $('#taskhub-count').textContent = pending.length ? `${pending.length} 项待办` : '';
    $('#taskhub-summary').textContent = pending.length
      ? `已折叠：${pending.slice(0, 2).map((t) => t.title).join('、')}${pending.length > 2 ? ' 等' : ''}`
      : '全部完成，今天很清爽';

    list.innerHTML = '';
    if (!pending.length) {
      list.innerHTML = `
        <div class="hm-taskhub__empty">
          ${icon('i-task', 'hm-ico hm-ico--lg')}
          <h4>今天没有待办了</h4>
          <p style="margin:0;font-size:13px">设备有动静时我会自动生成任务，不用你盯着。</p>
        </div>`;
      return;
    }
    pending
      .sort((a, b) => ({ urgent: 0, normal: 1, low: 2 }[a.priority] - { urgent: 0, normal: 1, low: 2 }[b.priority])
        || new Date(a.dueAt) - new Date(b.dueAt))
      .forEach((t) => {
        const card = document.createElement('article');
        card.className = 'hm-taskcard';
        card.dataset.id = t.id;
        card.dataset.priority = t.priority;
        card.innerHTML = taskCardHtml(t);
        attachTaskGestures(card);
        list.appendChild(card);
      });
  }

  /** 点击 / 左滑 / 上滑 三手势共存：主轴 + 阈值裁决。 */
  function attachTaskGestures(card) {
    let sx = 0, sy = 0, st = 0, mode = null, dx = 0;
    const scroller = $('#taskhub-list');
    const lock = (on) => scroller.classList.toggle('is-locked', on);

    card.addEventListener('pointerdown', (e) => {
      if (card.classList.contains('is-completing')) return;
      sx = e.clientX; sy = e.clientY; st = Date.now(); mode = null; dx = 0;
      card.setPointerCapture(e.pointerId);
      card.classList.add('is-pressed');
    });

    card.addEventListener('pointermove', (e) => {
      const ddx = e.clientX - sx;
      const ddy = e.clientY - sy;
      if (!mode) {
        if (Math.abs(ddx) > 12 && Math.abs(ddx) > Math.abs(ddy) * 1.2) { mode = 'swipe'; lock(true); }
        else if (ddy < -16 && Math.abs(ddy) > Math.abs(ddx) * 1.2) { mode = 'up'; }
        else if (Math.abs(ddx) > 10 || Math.abs(ddy) > 10) { mode = 'scroll'; }
      }
      if (mode === 'swipe') {
        dx = Math.max(-96, Math.min(0, ddx));
        card.style.transform = `translateX(${dx}px)`;
        card.classList.toggle('is-swiping', dx < -6);
      }
    });

    const finish = (e) => {
      card.classList.remove('is-pressed');
      const ddx = (e.clientX || sx) - sx;
      const ddy = (e.clientY || sy) - sy;
      const dt = Date.now() - st;
      if (mode === 'swipe') {
        lock(false);
        card.classList.remove('is-swiping');
        card.style.transform = '';
        // 滑到底（-72 以上）= 明确的取消意图；否则回弹，避免误触
        if (dx < -72) askCancel(card.dataset.id);
        return;
      }
      if (mode === 'up' && ddy < -56) { setHubCollapsed(true); return; }
      if (Math.abs(ddx) < 10 && Math.abs(ddy) < 10 && dt < 500) completeTask(card.dataset.id);
    };
    card.addEventListener('pointerup', finish);
    card.addEventListener('pointercancel', () => {
      card.classList.remove('is-pressed', 'is-swiping');
      card.style.transform = '';
      lock(false);
    });
  }

  function setHubCollapsed(on) {
    store.hubCollapsed = on;
    $('#taskhub').classList.toggle('is-collapsed', on);
  }

  async function completeTask(id) {
    // 首页有卡片（走动画），任务页没有卡片（只走接口）。
    // 以前这里写死"找不到卡片就 return"，导致从任务页点完成**永远没反应**——
    // 一个只在首页存在的 DOM 依赖，不该决定任务能不能完成。
    const card = $(`.hm-taskcard[data-id="${id}"]`);
    if (card) {
      if (card.classList.contains('is-completing')) return;
      card.classList.add('is-completing', 'is-done');
      burstBubbles(card);
    }

    const title = store.data.tasks.find((t) => t.id === id)?.title || '任务';
    try {
      const res = await api(`/api/console/tasks/${id}/complete`, { member_id: 'member_001' });
      const linked = (res.linked || []).filter((l) => l.ok).map((l) => l.name);
      if (card) {
        setTimeout(() => {
          flip($('#taskhub-list'), () => card.remove());
          renderTaskHubCounters();
        }, 420);
      }
      showToast(`已完成「${title}」${linked.length ? ` · 已联动${linked.join('、')}` : ''}`,
        () => undoTask(id, 'reopen', `已撤销「${title}」`));
      await refresh();
    } catch (err) {
      if (card) card.classList.remove('is-completing', 'is-done');
      showToast('网络异常，任务没改成，再点一次试试');
    }
  }

  function askCancel(id) {
    const task = store.data.tasks.find((t) => t.id === id);
    if (!task) return;
    openModal({
      title: '取消这条任务？',
      desc: `「${task.title}」将从今日待办移除，记录会保留，可随时恢复。`,
      tone: 'danger',
      chips: ['已经做过了', '今天不用做', '信息有误', '改天再说'],
      okText: '取消任务',
      onOk: async (reason) => {
        try {
          await api(`/api/console/tasks/${id}/cancel`, { reason: reason || '未填写原因' });
          showToast(`已取消「${task.title}」`, () => undoTask(id, 'reopen', '已恢复任务'));
          await refresh();
        } catch {
          showToast('取消失败，请重试');
        }
      },
    });
  }

  /** 让 AI 忘掉一条习惯：长期记忆 + 语义索引双库同清（后端保证）。 */
  function askForgetHabit(key) {
    const label = key.replace(/^preference[:：]/, '');
    openModal({
      title: '让 AI 忘掉这条？',
      desc: `「${label}」会从长期记忆与语义索引里一起删掉。此后 AI 不再按它行事，除非你重新告诉它。`,
      tone: 'warning',
      okText: '忘掉它',
      onOk: async () => {
        try {
          await api('/api/console/habits/forget', { memory_key: key });
          showToast(`AI 已忘掉「${label}」`);
          await refresh();
        } catch {
          showToast('遗忘失败，请重试');
        }
      },
    });
  }

  /** 删除一条 AI 创建的提醒。高危操作走确认弹层——删了就找不回来。 */
  function askDeleteSchedule(id) {
    const s = (store.data.schedules || []).find((x) => x.schedule_id === id);
    if (!s) return;
    openModal({
      title: '删除这条提醒？',
      desc: `「${s.content}」（${fmtScheduleTime(s.scheduled_at)}）将从今日任务和提醒清单里移除，不可恢复。`,
      tone: 'danger',
      okText: '删除提醒',
      onOk: async () => {
        try {
          await api(`/api/console/schedules/${id}`, null, 'DELETE');
          showToast(`已删除提醒「${s.content}」`);
          await refresh();
        } catch {
          showToast('删除失败，请重试');
        }
      },
    });
  }

  async function undoTask(id, action, message) {
    try {
      await api(`/api/console/tasks/${id}/${action}`, {});
      showToast(message);
      await refresh();
    } catch {
      showToast('撤销失败，请重试');
    }
  }

  function renderTaskHubCounters() {
    // 乐观更新：不等网络回包就先把计数改掉，手感更快
    const pending = store.data.tasks.filter((t) => t.status === 'pending');
    $('#taskhub-count').textContent = pending.length ? `${pending.length} 项待办` : '';
  }

  // ---------------------------------------------------------- 场景
  function renderScenes() {
    const box = $('#scenes');
    box.innerHTML = '';
    store.data.scenes.forEach((s) => {
      const b = document.createElement('button');
      b.className = 'hm-scene' + (store.activeScene === s.id ? ' is-active' : '');
      b.type = 'button';
      b.innerHTML = `<span class="hm-scene__icon">${icon(SCENE_ICON[s.id] || 'i-scene', 'hm-ico hm-ico--lg')}</span>`
        + `<span>${esc(s.name)}</span>`;
      b.addEventListener('click', () => activateScene(s.id));
      box.appendChild(b);
    });
  }

  async function activateScene(id) {
    store.activeScene = id;
    renderScenes();
    try {
      const res = await api(`/api/console/scenes/${id}/activate`, {});
      const ok = (res.affected || []).filter((a) => a.ok).length;
      showToast(`已执行「${res.scene}」· 联动 ${ok} 个设备`);
      await refresh();
    } catch {
      showToast('场景执行失败，请重试');
    } finally {
      setTimeout(() => { store.activeScene = null; renderScenes(); }, 2000);
    }
  }

  // ---------------------------------------------------------- 设备
  function deviceCardHtml(d) {
    const on = !!d.power;
    const ic = DEVICE_ICON[d.type] || 'i-light';
    const slider = d.control === 'dimmer' || d.control === 'temperature';
    let range = { min: 1, max: 100, step: 1, unit: '%' };
    if (d.control === 'temperature') range = { min: 16, max: 30, step: 1, unit: '°C' };

    const sliderHtml = slider ? `
      <div class="hm-devicecard__slider">
        <input class="hm-slider" type="range" min="${range.min}" max="${range.max}" step="${range.step}"
               value="${Math.round(d.value ?? range.min)}" ${on ? '' : 'disabled'}
               aria-label="${esc(d.name)}调节" />
      </div>` : '';

    return `
      ${d.glow ? '<span class="hm-devicecard__glow" style="--glow-color:' + d.glow + '"></span>' : ''}
      <div class="hm-devicecard__top">
        <span class="hm-devicecard__icon">${icon(ic, 'hm-ico hm-ico--lg')}</span>
        <button class="hm-switch" role="switch" aria-checked="${on}"
                aria-label="${esc(d.name)}开关" ${d.online === false ? 'disabled' : ''}></button>
      </div>
      <div class="hm-devicecard__name">${esc(d.name)}</div>
      <div class="hm-devicecard__status">${esc(d.statusText || '')}</div>
      ${sliderHtml}`;
  }

  function renderDevices() {
    const host = $('#device-sections');
    host.innerHTML = '';
    const groups = new Map();
    store.data.devices.forEach((d) => {
      if (!groups.has(d.room)) groups.set(d.room, []);
      groups.get(d.room).push(d);
    });

    groups.forEach((devices, room) => {
      const section = document.createElement('section');
      section.className = 'hm-section';
      const onCount = devices.filter((d) => d.power).length;
      section.innerHTML = `
        <div class="hm-room-title is-clickable" data-room="${esc(room)}" role="button" tabindex="0"
             title="点击一键${onCount ? '关闭' : '打开'}本房间设备">${esc(room)}
          <span>${onCount} / ${devices.length} 开启</span></div>
        <div class="hm-devicegrid"></div>`;
      const grid = $('.hm-devicegrid', section);

      // 房间标题显示着"几台开着"，看着就该能点。点了 = 整屋一键开关。
      const roomBar = $('[data-room]', section);
      if (roomBar) {
        const fire = () => toggleRoom(room);
        roomBar.addEventListener('click', fire);
        roomBar.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
        });
      }

      devices.forEach((d) => {
        const card = document.createElement('article');
        card.className = 'hm-devicecard' + (d.power ? ' is-on' : '')
          + (d.online === false ? ' is-offline' : '') + (d.alert ? ' has-alert' : '');
        card.dataset.id = d.id;
        card.innerHTML = deviceCardHtml(d);

        // 整张卡都能点：手机上没人会去瞄右上角那个小开关。
        // 开关和滑杆自己有处理逻辑，点它们不算点卡片。
        card.addEventListener('click', (e) => {
          if (e.target.closest('.hm-switch, .hm-slider')) return;
          toggleDevice(d);
        });

        const sw = $('.hm-switch', card);
        sw.addEventListener('click', (e) => {
          e.stopPropagation();
          toggleDevice(d);
        });

        const slider = $('.hm-slider', card);
        if (slider) {
          slider.addEventListener('input', () => {
            $('.hm-devicecard__status', card).textContent =
              `${slider.value}${d.control === 'temperature' ? '°C' : '%'}`;
          });
          slider.addEventListener('change', () => {
            controlDevice(d.id, 'set_value', Number(slider.value));
          });
          slider.addEventListener('pointerdown', (e) => e.stopPropagation());
        }
        grid.appendChild(card);
      });
      host.appendChild(section);
    });
  }

  async function toggleDevice(d) {
    if (d.highRisk && d.power === false) {
      openModal({
        title: '开锁属于高危操作',
        desc: `确认要对「${d.name}」执行开锁？我会记录操作人，并在 30 秒内可撤销。`,
        tone: 'danger',
        okText: '确认开锁',
        onOk: () => controlDevice(d.id, 'turn_on', null, true),
      });
      return;
    }
    await controlDevice(d.id, d.power ? 'turn_off' : 'turn_on');
  }

  /** 房间标题「N / M 开启」→ 整屋一键开关。
   *
   * 高危设备（门锁）不进批量：整屋一键把门开了，这种自动化没人敢用。
   * 它们跳过并在提示里说清楚，用户想开还得自己到卡片上确认一次。
   */
  async function toggleRoom(room) {
    const inRoom = store.data.devices.filter((d) => d.room === room && d.online !== false);
    const risky = inRoom.filter((d) => d.highRisk);
    const plain = inRoom.filter((d) => !d.highRisk);
    if (!plain.length) {
      showToast(`「${room}」只有门锁这类高危设备，请在卡片上单独确认`);
      return;
    }
    const anyOn = plain.some((d) => d.power);
    const action = anyOn ? 'turn_off' : 'turn_on';
    let ok = 0;
    for (const d of plain) {
      try {
        const res = await api(`/api/console/devices/${d.id}/control`,
          { action, member_id: 'member_001' });
        if (res.ok) ok += 1;
      } catch { /* 单台失败不中断整屋，最后按成功台数汇报 */ }
    }
    const skip = risky.length ? ` · 已跳过 ${risky.map((d) => d.name).join('、')}（需单独确认）` : '';
    showToast(`已${anyOn ? '关闭' : '打开'}「${room}」${ok} 台设备${skip}`);
    await refresh();
  }

  async function controlDevice(id, action, value = null, confirmed = false) {
    const card = $(`.hm-devicecard[data-id="${id}"]`);
    const sw = card && $('.hm-switch', card);
    if (sw) sw.classList.add('is-loading');
    try {
      const res = await api(`/api/console/devices/${id}/control`,
        { action, value, confirmed, member_id: 'member_001' });
      if (res.requiresConfirm) {
        openModal({
          title: '需要二次确认',
          desc: res.message || '该操作属于高危动作，请确认后再执行。',
          tone: 'warning',
          okText: '确认执行',
          onOk: () => controlDevice(id, action, value, true),
        });
        return;
      }
      // 失败时优先说后端给的具体原因（"不支持的动作"），
      // 别拿"设备没响应"糊弄——用户看到含糊提示只会以为是自己点错了
      if (!res.ok) { showToast(res.message || res.error || '设备没响应，稍后再试'); return; }
      showToast(res.message || '已执行');
      await refresh();
    } catch {
      showToast('控制失败，请检查服务是否还在跑');
    } finally {
      if (sw) sw.classList.remove('is-loading');
    }
  }

  // ---------------------------------------------------------- 摘要 / 右栏 / 二级页
  function renderSummary() {
    const { energy, messages } = store.data;
    const unread = messages.filter((m) => m.unread).length;
    const down = energy.delta < 0;
    // 用电这块点了要真切换（今日 → 本周 → 本月），不能是个只会亮着的数字
    const views = [{ key: 'today', label: '今日用电' }, { key: 'week', label: '本周用电' },
      { key: 'month', label: '本月用电' }];
    const view = views[(store.energyView || 0) % views.length];
    $('#summary').innerHTML = `
      <div data-energy-view role="button" tabindex="0" class="hm-summary__btn"
           title="点击切换 今日 / 本周 / 本月">
        ${icon('i-bolt', 'hm-ico hm-ico--sm')} ${view.label}
        <b>${energy[view.key]} ${energy.unit}</b>
        ${view.key === 'today'
          ? `· 较昨日 <span class="${down ? 'is-down' : ''}">${down ? '↓' : '↑'}${Math.abs(energy.delta)}%</span>`
          : ''}
      </div>
      <div data-goto-messages role="button" tabindex="0" class="hm-summary__btn"
           title="点击查看消息">
        ${icon('i-bell', 'hm-ico hm-ico--sm')}
        ${unread ? `<b>${unread} 条新消息</b>` : '暂无新消息'}
        ${unread ? '<span class="hm-summary__hint">点击标记全部已读</span>' : ''}
      </div>`;

    const ev = $('[data-energy-view]', $('#summary'));
    if (ev) {
      const flip = () => {
        store.energyView = ((store.energyView || 0) + 1) % views.length;
        renderSummary();
      };
      ev.addEventListener('click', flip);
      ev.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); }
      });
    }

    const gm = $('[data-goto-messages]', $('#summary'));
    if (gm) {
      const jump = () => {
        const target = $('#aside-messages');
        if (target && target.offsetParent !== null) {
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else {
          markAllMessagesRead();   // 窄屏看不到右栏，就当"一键已读"用
        }
      };
      gm.addEventListener('click', jump);
      gm.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
      });
    }
  }

  async function markAllMessagesRead() {
    try {
      const res = await api('/api/console/messages/read-all', {});
      (store.data.messages || []).forEach((m) => { m.unread = false; });
      renderAside(); renderSummary();
      if (res.marked) showToast(`已把 ${res.marked} 条消息标为已读`);
    } catch {
      showToast('标记已读失败，请重试');
    }
  }

  // 点家庭成员 → 只看 TA 的任务；再点同一个人 / 点"清除筛选" → 恢复全部
  function filterTasksByMember(memberId) {
    store.taskFilter = (memberId && store.taskFilter === memberId) ? null : memberId;
    go('tasks');
    renderPages();
  }

  async function markMessageRead(id) {
    const m = (store.data.messages || []).find((x) => x.id === id);
    if (!m || !m.unread) return;
    m.unread = false;          // 先本地乐观更新：点了立刻有反馈，不等网络
    renderAside(); renderSummary();
    try {
      await api(`/api/console/messages/${id}/read`, {});
    } catch {
      m.unread = true;         // 失败了就回滚，不能假装已读
      renderAside(); renderSummary();
    }
  }

  function renderAside() {
    const { energy, messages, family } = store.data;
    $('#aside-energy').innerHTML = `
      <div class="hm-card">
        <div class="hm-card__title">用电趋势（近 7 天）· 点击切换摘要</div>
        <div class="hm-spark">${[3.1, 3.6, 2.9, 4.2, 3.4, 3.0, energy.today]
          .map((v) => `<span style="height:${Math.round((v / 4.5) * 100)}%"></span>`).join('')}</div>
        ${[{ k: 'today', label: '今日' }, { k: 'week', label: '本周' }, { k: 'month', label: '本月' }]
          .map((row, i) => `<div class="hm-kv is-clickable${(store.energyView || 0) === i ? ' is-selected' : ''}"
            data-energy-jump="${i}" role="button" tabindex="0" title="在摘要条里看${row.label}用电">
            <span>${row.label}</span><b>${energy[row.k]} kWh</b></div>`).join('')}
      </div>`;

    // 右栏这三个数字点了要真切换摘要条，不是摆着好看的统计
    $$('[data-energy-jump]', $('#aside-energy')).forEach((el) => {
      const fire = () => {
        store.energyView = Number(el.dataset.energyJump);
        renderSummary();
        renderAside();
        const target = $('#summary');
        if (target) target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' });
      };
      el.addEventListener('click', fire);
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
      });
    });

    // 消息图标按真实事件类型选：设备 / 场景 / 任务 / 安全 / 系统(AI 提醒)
    const msgIcon = { device: 'i-light', scene: 'i-scene', task: 'i-check',
      security: 'i-lock', system: 'i-sparkle' };
    const unreadN = messages.filter((m) => m.unread).length;
    $('#aside-messages').innerHTML = `<div class="hm-card">
      <div class="hm-card__title">家庭消息${unreadN ? ` · ${unreadN} 条未读` : ''}</div>
      <div class="hm-msgitems">
      ${messages.length ? messages.map((m) => `
        <div class="hm-msgitem${m.unread ? ' is-unread' : ''}" data-msg="${esc(m.id)}"
             role="button" tabindex="0" title="${m.unread ? '点击标为已读' : '已读'}">
          <span class="hm-msgitem__icon">${icon(msgIcon[m.type] || 'i-bell', 'hm-ico hm-ico--sm')}</span>
          <div class="hm-msgitem__body">
            <div class="hm-msgitem__title">${esc(m.title)}${m.unread ? '<i class="hm-dot-unread"></i>' : ''}</div>
            <div class="hm-msgitem__desc">${esc(m.body || '')}</div>
          </div>
          <span class="hm-msgitem__time">${relTime(m.time)}</span>
        </div>`).join('')
        : `<div class="hm-empty">还没有消息。开关设备、执行场景、完成任务，
           或让 AI 帮你加个提醒——每一次真实操作都会在这里留下记录。</div>`}
    </div></div>`;

    // 消息条目必须能点：点了标为已读。
    // 渲染出来却点不出任何反应的元素，比不做这个元素更让人恼火。
    $$('[data-msg]', $('#aside-messages')).forEach((el) => {
      const fire = () => markMessageRead(el.dataset.msg);
      el.addEventListener('click', fire);
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
      });
    });

    $('#aside-family').innerHTML = `<div class="hm-card">
      <div class="hm-card__title">家庭成员 · 点击查看 TA 的任务</div>
      ${family.map((m) => `
        <div class="hm-member" style="padding:6px 0" data-member="${esc(m.id)}"
             role="button" tabindex="0">
          <span class="hm-avatar" style="background:${m.colorTag}">${esc(m.avatar)}</span>
          <div class="hm-listitem__body">
            <div class="hm-listitem__title">${esc(m.name)}</div>
            <div class="hm-listitem__desc">${esc({ owner: '管理员', admin: '共同管理', member: '成员' }[m.role] || m.role)}</div>
          </div>
          ${icon('i-chevron-right', 'hm-ico hm-ico--sm')}
        </div>`).join('')}
    </div>`;

    // 点成员 = 筛选"指派给 TA 的任务"，不是摆着看的头像
    $$('[data-member]', $('#aside-family')).forEach((el) => {
      const fire = () => filterTasksByMember(el.dataset.member);
      el.addEventListener('click', fire);
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
      });
    });

    renderHabitsInto($('#aside-habits'));
  }

  async function renderHabitsInto(host) {
    host.innerHTML = '<div class="hm-card"><div class="hm-card__title">正在读取记忆库…</div></div>';
    try {
      const res = await api('/api/console/habits');
      if (!res.count) {
        host.innerHTML = `<div class="hm-card"><div class="hm-card__title">AI 记住的习惯</div>
          <div style="font-size:13px;color:var(--hm-text-tertiary)">
            还没有记录。试试跟 AI 说「我习惯空调开 24 度」。</div></div>`;
        return;
      }
      host.innerHTML = `<div class="hm-card">
        <div class="hm-card__title">AI 记住的习惯 · 点击可让 AI 忘掉</div>
        ${res.habits.map((h) => `<div class="hm-kv is-clickable" data-habit="${esc(h.key)}"
            role="button" tabindex="0" title="点击让 AI 忘掉这条">
            <span>${esc(h.key.replace(/^preference[:：]/, ''))}</span>
            <b>${esc(h.value)}</b></div>`).join('')}
      </div>`;

      // 记忆能存就得能删，否则"记住了错的偏好"这件事永远改不回来
      $$('[data-habit]', host).forEach((el) => {
        const fire = () => askForgetHabit(el.dataset.habit);
        el.addEventListener('click', fire);
        el.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
        });
      });
    } catch {
      host.innerHTML = '<div class="hm-card"><div class="hm-card__title">习惯读取失败</div></div>';
    }
  }

  function renderPages() {
    if (!store.data) return;   // 没登录时不渲染，别拿 null 去取 tasks
    // 任务页：全部任务（含已完成 / 已取消），可恢复
    // 任务页支持"看某个人的任务"（从家庭成员卡片点进来）
    const all = store.data.tasks;
    const filter = store.taskFilter;
    const shown = filter
      ? all.filter((t) => (t.assigneeIds || []).includes(filter))
      : all;
    const filterName = filter
      ? (store.data.family || []).find((m) => m.id === filter)
      : null;
    $('#tasks-sub').textContent = filter && filterName
      ? `只看「${filterName.name}」· ${shown.filter((t) => t.status === 'pending').length} 项待办`
      : `${all.filter((t) => t.status === 'pending').length} 项待办 · ${all.length} 条记录`;
    const filterBar = filter ? `
      <div class="hm-filterbar" data-clear-filter>
        ${icon('i-user', 'hm-ico hm-ico--sm')}
        <span>只看「${esc(filterName ? filterName.name : filter)}」的任务</span>
        <button class="hm-btn hm-btn--ghost" style="height:32px;padding:0 10px">清除筛选</button>
      </div>` : '';
    $('#page-tasks-list').innerHTML = filterBar + (shown.length ? shown.map((t) => {
      const statePill = t.status === 'done' ? '<span class="hm-state-pill is-done">已完成</span>'
        : t.status === 'cancelled' ? '<span class="hm-state-pill is-cancelled">已取消</span>' : '';
      return `
      <div class="hm-listitem is-clickable" data-id="${t.id}" role="button" tabindex="0"
           title="${t.status === 'pending' ? '点击标记完成' : '点击恢复为待办'}">
        <div class="hm-taskcard__icon" style="background:${hexA(t.colorTag, 0.16)};color:${t.colorTag}">
          ${icon(CATEGORY_ICON[t.category] || 'i-task')}
        </div>
        <div class="hm-listitem__body">
          <div class="hm-listitem__title">${esc(t.title)}</div>
          <div class="hm-listitem__desc">${esc(t.description || '')}${t.cancelReason ? ` · 原因：${esc(t.cancelReason)}` : ''}</div>
        </div>
        ${statePill}
        ${t.status !== 'pending'
          ? `<button class="hm-btn hm-btn--ghost" style="flex:none;height:36px;padding:0 12px"
               data-undo="${t.id}">恢复</button>` : ''}
      </div>`;
    }).join('') : '<div class="hm-empty">还没有任务</div>');

    $$('[data-undo]', $('#page-tasks-list')).forEach((b) => {
      b.addEventListener('click', (e) => {
        e.stopPropagation();     // 别让行级的"点击恢复"再触发一次
        undoTask(b.dataset.undo, 'reopen', '已恢复为待办');
      });
    });

    // 任务行本身可点：待办 → 标记完成；已完成 / 已取消 → 恢复。
    // 行上那个"恢复"按钮自己已处理，点它不算点行（上面 stopPropagation）。
    $$('[data-id]', $('#page-tasks-list')).forEach((row) => {
      const fire = () => {
        const t = store.data.tasks.find((x) => x.id === row.dataset.id);
        if (!t) return;
        if (t.status === 'pending') completeTask(t.id);
        else undoTask(t.id, 'reopen', `已恢复「${t.title}」`);
      };
      row.addEventListener('click', fire);
      row.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
      });
    });

    const clearBar = $('[data-clear-filter]', $('#page-tasks-list'));
    if (clearBar) clearBar.addEventListener('click', () => filterTasksByMember(null));

    // 场景页
    $('#page-scenes-list').innerHTML = store.data.scenes.map((s) => `
      <div class="hm-listitem" data-scene="${s.id}">
        <div class="hm-taskcard__icon" style="background:var(--hm-color-brand-soft);color:var(--hm-color-brand)">
          ${icon(SCENE_ICON[s.id] || 'i-scene')}
        </div>
        <div class="hm-listitem__body">
          <div class="hm-listitem__title">${esc(s.name)}</div>
          <div class="hm-listitem__desc">一键联动多个设备</div>
        </div>
        ${icon('i-chevron-right', 'hm-ico hm-ico--sm')}
      </div>`).join('');
    $$('[data-scene]', $('#page-scenes-list')).forEach((el) => {
      el.addEventListener('click', () => activateScene(el.dataset.scene));
    });

    // 我的
    const schedules = store.data.schedules || [];
    const v = store.data.viewer || {};
    $('#page-me-body').innerHTML = `
      <div class="hm-card">
        <div class="hm-card__title">当前身份</div>
        <div class="hm-member">
          <span class="hm-avatar" style="background:var(--hm-color-brand)">${esc((v.name || '?').slice(0, 1))}</span>
          <div class="hm-listitem__body">
            <div class="hm-listitem__title">${esc(v.name || '')} · ${esc(v.familyName || '')}</div>
            <div class="hm-listitem__desc">${esc(v.city || '')} · 权限 ${esc(v.role || '')} · ${esc(v.member_id || '')}</div>
          </div>
        </div>
        <button class="hm-btn hm-btn--ghost" id="btn-logout" style="margin-top:12px">退出登录</button>
      </div>
      <div class="hm-card">
        <div class="hm-card__title">外观</div>
        <div class="hm-theme-switch">
          <button data-theme="dark" class="is-active">深色（默认）</button>
          <button data-theme="light">浅色</button>
        </div>
      </div>
      <div class="hm-card">
        <div class="hm-card__title">家庭成员 · 点击查看 TA 的任务</div>
        ${store.data.family.map((m) => `
          <div class="hm-member" style="padding:6px 0" data-member="${esc(m.id)}"
               role="button" tabindex="0">
            <span class="hm-avatar" style="background:${m.colorTag}">${esc(m.avatar)}</span>
            <div class="hm-listitem__body">
              <div class="hm-listitem__title">${esc(m.name)}</div>
              <div class="hm-listitem__desc">${esc({ owner: '管理员', admin: '共同管理', member: '成员' }[m.role] || m.role)}</div>
            </div>
            ${icon('i-chevron-right', 'hm-ico hm-ico--sm')}
          </div>`).join('')}
      </div>
      <div class="hm-card">
        <div class="hm-card__title">已设提醒（${schedules.length}）· 点击可删除</div>
        ${schedules.length
          ? schedules.map((s) => `<div class="hm-kv is-clickable" data-schedule="${esc(s.schedule_id)}"
              role="button" tabindex="0" title="点击删除这条提醒">
              <span>${esc(s.content || '提醒')}</span>
              <b>${esc(fmtScheduleTime(s.scheduled_at))}</b></div>`).join('')
          : '<div style="font-size:13px;color:var(--hm-text-tertiary)">还没有提醒，跟 AI 说「明天 7 点叫我起床」即可创建。</div>'}
      </div>
      <div id="page-me-habits"></div>
      <div class="hm-card">
        <div class="hm-card__title">关于这套系统</div>
        <div class="hm-kv"><span>意图识别</span><b>规则 + LLM 双通道</b></div>
        <div class="hm-kv"><span>编排</span><b>LangGraph 多智能体</b></div>
        <div class="hm-kv"><span>工具治理</span><b>超时 / 重试 / 熔断 / 幂等</b></div>
        <div class="hm-kv"><span>记忆</span><b>短期 + 长期 + 语义三层</b></div>
        <div class="hm-kv"><span>安全</span><b>高危动作强制二次确认</b></div>
      </div>`;

    $$('#page-me-body [data-theme]').forEach((b) => {
      b.addEventListener('click', () => {
        document.documentElement.dataset.theme = b.dataset.theme;
        $$('#page-me-body [data-theme]').forEach((x) =>
          x.classList.toggle('is-active', x.dataset.theme === b.dataset.theme));
        try { localStorage.setItem('hm-theme', b.dataset.theme); } catch { /* 忽略隐私模式 */ }
      });
    });
    // 「我的」页的家庭成员与右栏一致：点了就去任务页看这个人被指派了什么
    $$('#page-me-body [data-member]').forEach((el) => {
      const fire = () => filterTasksByMember(el.dataset.member);
      el.addEventListener('click', fire);
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
      });
    });

    // 提醒能加也要能删：AI 建的提醒挂在今日任务框里，删不掉就是永久污染
    $$('#page-me-body [data-schedule]').forEach((el) => {
      const fire = () => askDeleteSchedule(el.dataset.schedule);
      el.addEventListener('click', fire);
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fire(); }
      });
    });

    const saved = (() => { try { return localStorage.getItem('hm-theme'); } catch { return null; } })();
    if (saved && saved !== 'dark') $('#page-me-body [data-theme="light"]')?.click();

    const logout = $('#btn-logout');
    if (logout) {
      logout.addEventListener('click', async () => {
        try { await api('/api/auth/logout', {}); } catch { /* 退出本身失败也要清本地 */ }
        clearToken();
        $('#sheet-body').innerHTML = '';   // 退出瞬间就清对话，别等下个人登录
        await boot();
      });
    }

    renderHabitsInto($('#page-me-habits'));
  }

  // ---------------------------------------------------------- 路由
  function go(page) {
    store.page = page;
    $$('.hm-page').forEach((p) => p.classList.toggle('is-active', p.id === `page-${page}`));
    $$('[data-page]').forEach((b) => b.classList.toggle('is-active', b.dataset.page === page));
    window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
    if (page === 'me') renderPages();
  }
  $$('[data-page]').forEach((b) => b.addEventListener('click', () => go(b.dataset.page)));

  // ---------------------------------------------------------- 滚动行为
  let lastY = 0;
  window.addEventListener('scroll', () => {
    const y = window.scrollY;
    const dy = y - lastY;
    if (Math.abs(dy) > 6 && y > 60) {
      $('#envbar').classList.toggle('is-collapsed', dy > 0);   // 下滚收起、上滚展开
      $('#fab').classList.toggle('is-hidden', dy > 0);         // 滚动时收起悬浮球
    }
    lastY = y;
  }, { passive: true });

  // 折叠把手：点击切换 + 上滑折叠（三条路径之一）
  const handle = $('#taskhub-handle');
  handle.addEventListener('click', () => setHubCollapsed(!store.hubCollapsed));
  handle.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setHubCollapsed(!store.hubCollapsed); }
  });
  let hy = 0;
  handle.addEventListener('pointerdown', (e) => { hy = e.clientY; handle.setPointerCapture(e.pointerId); });
  handle.addEventListener('pointerup', (e) => {
    if (e.clientY - hy < -40) setHubCollapsed(true);
    else if (e.clientY - hy > 40) setHubCollapsed(false);
  });

  // 环境栏「问一句」
  $('#btn-ask').addEventListener('click', () => {
    const bar = $('#envbar');
    const asking = bar.classList.toggle('is-asking');
    if (asking) $('#env-ask-input').focus();
  });
  $('#env-ask').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('#env-ask-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    $('#envbar').classList.remove('is-asking');
    openSheet();
    sendMessage(text);
  });

  // ---------------------------------------------------------- AI 助手面板
  const SUGGESTIONS = [
    '客厅有点暗',
    '我习惯空调开 24 度',
    '明天早上 7 点叫我起床',
    '空调怎么清洗滤网',
    '出门了',
    '记住我喜欢客厅灯暖一点',
  ];

  function openSheet() {
    $('#sheet').classList.add('is-open');
    $('#sheet-mask').classList.add('is-open');
    setTimeout(() => $('#sheet-input').focus(), 260);
  }
  function closeSheet() {
    $('#sheet').classList.remove('is-open');
    $('#sheet-mask').classList.remove('is-open');
  }
  $('#fab').addEventListener('click', openSheet);
  $('#sheet-close').addEventListener('click', closeSheet);
  $('#sheet-mask').addEventListener('click', closeSheet);

  function renderSuggestions() {
    $('#sheet-suggestions').innerHTML = SUGGESTIONS
      .map((s) => `<button class="hm-suggestion" type="button">${esc(s)}</button>`).join('');
    $$('.hm-suggestion').forEach((b) => {
      b.addEventListener('click', () => sendMessage(b.textContent));
    });
  }

  function pushMsg(role, html) {
    const el = document.createElement('div');
    el.className = `hm-msg is-${role}`;
    el.innerHTML = html;
    $('#sheet-body').appendChild(el);
    $('#sheet-body').scrollTop = $('#sheet-body').scrollHeight;
    return el;
  }

  function pushActionCard(action) {
    const el = document.createElement('div');
    el.className = 'hm-actioncard' + (action.ok ? '' : ' is-error');
    const canUndo = action.ok && action.deviceId;
    el.innerHTML = `
      <div class="hm-actioncard__row">
        ${icon(action.ok ? 'i-check' : 'i-close', 'hm-ico hm-ico--sm')}
        <span class="hm-actioncard__title">${esc(action.title)}</span>
        ${canUndo ? '<button class="hm-actioncard__undo">撤销</button>' : ''}
      </div>
      <div class="hm-actioncard__meta">工具 ${esc(action.tool || '-')} · ${action.ok ? '执行成功' : esc(action.error || '执行失败')}</div>`;
    $('#sheet-body').appendChild(el);
    $('#sheet-body').scrollTop = $('#sheet-body').scrollHeight;

    if (canUndo) {
      const btn = $('.hm-actioncard__undo', el);
      btn.addEventListener('click', async () => {
        const d = store.data.devices.find((x) => x.id === action.deviceId);
        await controlDevice(action.deviceId, d && d.power ? 'turn_off' : 'turn_on');
        btn.textContent = '已撤销';
        btn.disabled = true;
      });
      setTimeout(() => { btn.textContent = '已过期'; btn.disabled = true; }, 10000);
    }
  }

  let sending = false;
  async function sendMessage(text, confirmed = false) {
    if (sending || !text.trim()) return;
    sending = true;
    if (!confirmed) pushMsg('user', esc(text));
    const typing = pushMsg('ai', '<span style="color:var(--hm-text-tertiary)">正在理解意图…</span>');

    try {
      const res = await api('/api/console/assistant',
        { text, member_id: 'member_001', confirmed, session_id: 'console' });
      typing.remove();

      const intents = (res.intents || []).map((i) => i.type || i).join(' / ');
      pushMsg('ai', esc(res.answer || '（没有生成应答）'));
      if (intents) {
        const meta = document.createElement('div');
        meta.className = 'hm-msg is-ai';
        meta.style.fontSize = '11px';
        meta.style.color = 'var(--hm-text-tertiary)';
        meta.textContent = `意图：${intents}${res.degraded ? ' · 已降级' : ''}`;
        $('#sheet-body').appendChild(meta);
      }

      (res.actions || []).forEach(pushActionCard);

      if (res.requiresConfirm) {
        store.lastConfirm = { text };
        const card = document.createElement('div');
        card.className = 'hm-actioncard is-warning';
        card.innerHTML = `
          <div class="hm-actioncard__row">
            ${icon('i-security', 'hm-ico hm-ico--sm')}
            <span class="hm-actioncard__title">${esc(res.confirmPrompt || '该操作需要你确认')}</span>
            <button class="hm-actioncard__undo">确认执行</button>
          </div>`;
        $('.hm-actioncard__undo', card).addEventListener('click', () => {
          card.remove();
          sendMessage(text, true);
        });
        $('#sheet-body').appendChild(card);
      }
      $('#sheet-sub').textContent = `trace ${String(res.traceId || '').slice(0, 8)} · 多智能体已处理`;
      await refresh();
    } catch {
      typing.remove();
      pushMsg('ai', '后端没响应了。确认一下 `uvicorn homemind.api.main:app` 还活着？');
    } finally {
      sending = false;
    }
  }

  $('#sheet-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const input = $('#sheet-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    sendMessage(text);
  });

  // 桌面端快捷键：/ 或 Ctrl(Cmd)+K 唤起
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea';
    if ((e.key === '/' && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) {
      e.preventDefault();
      openSheet();
    }
    if (e.key === 'Escape') { closeSheet(); $('#modal-cancel').click(); }
  });

  // ---------------------------------------------------------- 数据装载
  async function refresh() {
    store.data = await api('/api/console/state');
    renderEnv(store.data.env);
    renderHint(store.data.hint);
    renderTaskHub();
    setHubCollapsed(store.hubCollapsed);
    renderScenes();
    renderDevices();
    renderSummary();
    renderAside();
    // 任何非首页都要重渲染：否则切换账号后"我的"页身份卡还是上一个人的渲染缓存
    if (store.page !== 'home') renderPages();
  }

  async function submitLogin() {
    const btn = $('#login-submit');
    btn.disabled = true;
    btn.textContent = '登录中…';
    const ok = await doLogin($('#login-user').value.trim(), $('#login-pass').value);
    btn.disabled = false;
    btn.textContent = '登录';
    if (ok) await boot();
  }
  $('#login-form').addEventListener('submit', (e) => { e.preventDefault(); submitLogin(); });

  async function boot() {
    const saved = (() => { try { return localStorage.getItem('hm-theme'); } catch { return null; } })();
    if (saved) document.documentElement.dataset.theme = saved;

    renderSuggestions();
    if (!getToken()) {
      // 没有身份就没有"家"——先把门禁摆出来，别让页面停在白屏
      pushMsg('ai', '先登录，我才知道该给你看哪个家。');
      await showLogin();
      return;
    }
    $('#login').hidden = true;
    $('#taskhub-list').innerHTML = '<div class="hm-skeleton"></div><div class="hm-skeleton"></div>';
    try {
      await refresh();
      pushMsg('ai', '我是家里的 AI 管家。可以直接说「客厅有点暗」，也可以让我记住你的习惯。');
    } catch {
      pushMsg('ai', '拿不到后端状态。先跑起来：<code>python scripts/console_server.py</code>');
    }
  }

  boot();
})();
