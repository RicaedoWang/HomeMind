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
    valve: 'i-valve' };
  const CATEGORY_ICON = { plant: 'i-plant', laundry: 'i-laundry', pet: 'i-pet',
    security: 'i-security', cleaning: 'i-robot', maintenance: 'i-security', other: 'i-task' };
  const SCENE_ICON = { home: 'i-home', away: 'i-exit', movie: 'i-movie', sleep: 'i-moon', wake: 'i-sun' };
  const SRC_TEXT = { automation: '自动化', 'device-event': '设备触发', manual: '手动', ai: 'AI 建议' };

  function icon(id, cls = 'hm-ico') {
    return `<svg class="${cls}" aria-hidden="true"><use href="#${id}"/></svg>`;
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

  async function api(path, body) {
    const options = body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : { method: 'GET' };
    const res = await fetch(path, options);
    if (!res.ok) throw new Error(`${res.status} ${path}`);
    return res.json();
  }

  // ---------------------------------------------------------- 全局状态
  const store = {
    data: null,
    page: 'home',
    hubCollapsed: false,
    activeScene: null,
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
    const card = $(`.hm-taskcard[data-id="${id}"]`);
    if (!card || card.classList.contains('is-completing')) return;
    card.classList.add('is-completing', 'is-done');
    burstBubbles(card);

    const title = store.data.tasks.find((t) => t.id === id)?.title || '任务';
    try {
      const res = await api(`/api/console/tasks/${id}/complete`, { member_id: 'member_001' });
      const linked = (res.linked || []).filter((l) => l.ok).map((l) => l.name);
      setTimeout(() => {
        flip($('#taskhub-list'), () => card.remove());
        renderTaskHubCounters();
      }, 420);
      showToast(`已完成「${title}」${linked.length ? ` · 已联动${linked.join('、')}` : ''}`,
        () => undoTask(id, 'reopen', `已撤销「${title}」`));
      await refresh();
    } catch (err) {
      card.classList.remove('is-completing', 'is-done');
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
        <div class="hm-room-title">${esc(room)} <span>${onCount} / ${devices.length} 开启</span></div>
        <div class="hm-devicegrid"></div>`;
      const grid = $('.hm-devicegrid', section);

      devices.forEach((d) => {
        const card = document.createElement('article');
        card.className = 'hm-devicecard' + (d.power ? ' is-on' : '')
          + (d.online === false ? ' is-offline' : '') + (d.alert ? ' has-alert' : '');
        card.dataset.id = d.id;
        card.innerHTML = deviceCardHtml(d);

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
      if (!res.ok) { showToast(res.error || '设备没响应，稍后再试'); return; }
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
    $('#summary').innerHTML = `
      <div>${icon('i-bolt', 'hm-ico hm-ico--sm')} 今日用电 <b>${energy.today} ${energy.unit}</b>
        · 较昨日 <span class="${down ? 'is-down' : ''}">${down ? '↓' : '↑'}${Math.abs(energy.delta)}%</span></div>
      <div style="display:flex;align-items:center;gap:6px">
        ${icon('i-bell', 'hm-ico hm-ico--sm')}
        ${unread ? `<b>${unread} 条新消息</b>` : '暂无新消息'}
      </div>`;
  }

  function renderAside() {
    const { energy, messages, family } = store.data;
    $('#aside-energy').innerHTML = `
      <div class="hm-card">
        <div class="hm-card__title">用电趋势（近 7 天）</div>
        <div class="hm-spark">${[3.1, 3.6, 2.9, 4.2, 3.4, 3.0, energy.today]
          .map((v) => `<span style="height:${Math.round((v / 4.5) * 100)}%"></span>`).join('')}</div>
        <div class="hm-kv"><span>今日</span><b>${energy.today} kWh</b></div>
        <div class="hm-kv"><span>本周</span><b>${energy.week} kWh</b></div>
        <div class="hm-kv"><span>本月</span><b>${energy.month} kWh</b></div>
      </div>`;

    const msgIcon = { device: 'i-washer', security: 'i-lock', system: 'i-bell' };
    $('#aside-messages').innerHTML = `<div class="hm-card"><div class="hm-msgitems">
      ${messages.map((m) => `
        <div class="hm-msgitem">
          <span class="hm-msgitem__icon">${icon(msgIcon[m.type] || 'i-bell', 'hm-ico hm-ico--sm')}</span>
          <div class="hm-msgitem__body">
            <div class="hm-msgitem__title">${esc(m.title)}${m.unread ? '<i class="hm-dot-unread"></i>' : ''}</div>
            <div class="hm-msgitem__desc">${esc(m.body)}</div>
          </div>
          <span class="hm-msgitem__time">${esc(m.time)}</span>
        </div>`).join('')}
    </div></div>`;

    $('#aside-family').innerHTML = `<div class="hm-card">
      ${family.map((m) => `
        <div class="hm-member" style="padding:6px 0">
          <span class="hm-avatar" style="background:${m.colorTag}">${esc(m.avatar)}</span>
          <div class="hm-listitem__body">
            <div class="hm-listitem__title">${esc(m.name)}</div>
            <div class="hm-listitem__desc">${esc({ owner: '管理员', admin: '共同管理', member: '成员' }[m.role] || m.role)}</div>
          </div>
        </div>`).join('')}
    </div>`;

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
        ${res.habits.map((h) => `<div class="hm-kv"><span>${esc(h.key.replace(/^preference[:：]/, ''))}</span>
          <b>${esc(h.value)}</b></div>`).join('')}
      </div>`;
    } catch {
      host.innerHTML = '<div class="hm-card"><div class="hm-card__title">习惯读取失败</div></div>';
    }
  }

  function renderPages() {
    // 任务页：全部任务（含已完成 / 已取消），可恢复
    const all = store.data.tasks;
    $('#tasks-sub').textContent = `${all.filter((t) => t.status === 'pending').length} 项待办 · ${all.length} 条记录`;
    $('#page-tasks-list').innerHTML = all.length ? all.map((t) => {
      const statePill = t.status === 'done' ? '<span class="hm-state-pill is-done">已完成</span>'
        : t.status === 'cancelled' ? '<span class="hm-state-pill is-cancelled">已取消</span>' : '';
      return `
      <div class="hm-listitem" data-id="${t.id}">
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
    }).join('') : '<div class="hm-empty">还没有任务</div>';

    $$('[data-undo]', $('#page-tasks-list')).forEach((b) => {
      b.addEventListener('click', () => undoTask(b.dataset.undo, 'reopen', '已恢复为待办'));
    });

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
    $('#page-me-body').innerHTML = `
      <div class="hm-card">
        <div class="hm-card__title">外观</div>
        <div class="hm-theme-switch">
          <button data-theme="dark" class="is-active">深色（默认）</button>
          <button data-theme="light">浅色</button>
        </div>
      </div>
      <div class="hm-card">
        <div class="hm-card__title">家庭成员</div>
        ${store.data.family.map((m) => `
          <div class="hm-member" style="padding:6px 0">
            <span class="hm-avatar" style="background:${m.colorTag}">${esc(m.avatar)}</span>
            <div class="hm-listitem__body">
              <div class="hm-listitem__title">${esc(m.name)}</div>
              <div class="hm-listitem__desc">${esc({ owner: '管理员', admin: '共同管理', member: '成员' }[m.role] || m.role)}</div>
            </div>
          </div>`).join('')}
      </div>
      <div class="hm-card">
        <div class="hm-card__title">已设提醒（${schedules.length}）</div>
        ${schedules.length
          ? schedules.map((s) => `<div class="hm-kv"><span>${esc(s.content || '提醒')}</span>
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
    const saved = (() => { try { return localStorage.getItem('hm-theme'); } catch { return null; } })();
    if (saved && saved !== 'dark') $('#page-me-body [data-theme="light"]')?.click();

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
    if (store.page === 'tasks' || store.page === 'scenes') renderPages();
  }

  async function boot() {
    const saved = (() => { try { return localStorage.getItem('hm-theme'); } catch { return null; } })();
    if (saved) document.documentElement.dataset.theme = saved;

    renderSuggestions();
    $('#taskhub-list').innerHTML = '<div class="hm-skeleton"></div><div class="hm-skeleton"></div>';
    try {
      await refresh();
      pushMsg('ai', '我是家里的 AI 管家。可以直接说「客厅有点暗」，也可以让我记住你的习惯。');
    } catch {
      pushMsg('ai', '拿不到后端状态。先跑起来：<code>uvicorn homemind.api.main:app --reload</code>');
    }
  }

  boot();
})();
