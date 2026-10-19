/* ============================================================
   HomeMind 控制台 · 前端无头冒烟（jsdom）
   ------------------------------------------------------------
   为什么要有这个脚本：
   前端是零构建原生 JS，改一行 app.js 不会有编译期报错兜底。
   只有真把它塞进 DOM 里跑一遍、把该点的都点一遍，
   才能确认"渲染出来了"和"点了有反应"是两件都成立的事。

   它验证什么：
   1. 首屏能在门禁态正常渲染，不抛运行时错误
   2. 登录 → 拉全量状态 → 四个页面都渲染得出来
   3. **每个可交互元素点下去都有真实动作**（点了必须有网络请求，
      没有请求 = 点了没反应 = 本次要抓的 bug）

   跑法（jsdom 装在托管 node 工作区，不污染本机）：
     NODE_PATH="C:/Users/13141/.workbuddy/binaries/node/workspace/node_modules" \
     node scripts/smoke_web.js
   ============================================================ */

const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');
const appSrc = fs.readFileSync(path.join(ROOT, 'web', 'assets', 'app.js'), 'utf8');

// ---------------------------------------------------------------- 假后端
// 只造形状，不跑真服务：这里要验的是前端有没有把动作发出去，不是后端算得对不对。
const state = {
  env: {
    date: new Date().toISOString().slice(0, 10),
    weekday: 'Wednesday', lunar: '九月初四',
    indoor: { temp: 24.5, humidity: 62, sensor: '客厅' },
    outdoor: { temp: 25.0, high: 28, low: 21, condition: 'sunny', conditionText: '晴' },
    aqi: { value: 43, level: '优' }, tempDiffWarn: false,
  },
  hint: {
    level: 'info', icon: 'thermometer', text: '客厅 24.5°C，要不把空调开上？',
    action: { label: '开客厅空调 26°C', deviceId: 'living_room_ac', action: 'set_value', value: 26 },
  },
  tasks: [
    {
      id: 't-plant', title: '给阳台的绿萝浇花', description: '土壤湿度 28%',
      iconKey: 'plant', category: 'plant', source: 'automation', creatorId: 'm2',
      assigneeIds: ['m1'], roomId: 'balcony',
      dueAt: new Date(Date.now() + 3600e3).toISOString(),
      priority: 'urgent', status: 'pending', linkedDeviceIds: ['balcony_valve'],
      completionAction: 'control-device', colorTag: '#34D399',
    },
    {
      id: 'sch_0001', title: '关灯睡觉', description: 'AI 助手创建',
      iconKey: 'bell', category: 'reminder', source: 'ai', creatorId: 'ai',
      assigneeIds: [], roomId: '',
      dueAt: new Date(Date.now() + 7200e3).toISOString(),
      priority: 'normal', status: 'pending', linkedDeviceIds: [],
      completionAction: 'notify-only', isSchedule: true, colorTag: '#A78BFA',
    },
    {
      id: 't-done', title: '晾晒衣服', description: '已完成',
      iconKey: 'laundry', category: 'laundry', source: 'device-event',
      assigneeIds: ['m1'], priority: 'normal', status: 'done',
      dueAt: new Date(Date.now() - 3600e3).toISOString(),
      linkedDeviceIds: [], completionAction: 'notify-only', colorTag: '#60A5FA',
    },
  ],
  pendingCount: 2,
  devices: [
    { id: 'living_room_light', name: '客厅灯', room: '客厅', type: 'light', power: true,
      value: 60, control: 'dimmer', statusText: '已开 60%', online: true, glow: '#FFB871' },
    { id: 'living_room_ac', name: '客厅空调', room: '客厅', type: 'ac', power: false,
      value: 26, control: 'temperature', statusText: '已关闭', online: true },
    { id: 'front_door_lock', name: '入户门锁', room: '玄关', type: 'lock', power: false,
      value: 0, control: 'switch', statusText: '已上锁', online: true, highRisk: true },
  ],
  rooms: ['客厅', '玄关'],
  scenes: [
    { id: 'home', name: '回家', icon: 'home', builtin: true, editable: false,
      actions: [{ device_id: 'living_room_light', action: 'turn_on', value: 60 }] },
    { id: 'away', name: '离家', icon: 'exit', builtin: true, editable: false,
      actions: [{ device_id: 'living_room_light', action: 'turn_off', value: null }] },
    // 自建场景：只有它才带编辑按钮，用来验证"内置不可改、自建可改"这条分界
    { id: 'scene_smoke01', name: '夜读', icon: 'moon', builtin: false, editable: true,
      actions: [{ device_id: 'living_room_light', action: 'set_value', value: 15 }] },
  ],
  schedules: [{ schedule_id: 'sch_0001', content: '关灯睡觉',
    scheduled_at: new Date(Date.now() + 7200e3).toISOString(), status: 'pending' }],
  energy: { today: 3.2, unit: 'kWh', delta: -12, week: 24.6, month: 108.4 },
  messages: [
    { id: 'evt_0001', type: 'device', title: '关闭了客厅灯', body: '操作人：我',
      time: new Date(Date.now() - 600e3).toISOString(), unread: true },
    { id: 'evt_0002', type: 'system', title: 'AI 添加了提醒：关灯睡觉', body: '由对话创建',
      time: new Date(Date.now() - 1200e3).toISOString(), unread: false },
  ],
  unreadCount: 1,
  family: [
    { id: 'm1', name: '我', colorTag: '#4F7BFF', role: 'owner', avatar: '我' },
    { id: 'm2', name: '妈妈', colorTag: '#F472B6', role: 'admin', avatar: '妈' },
  ],
  viewer: { name: '我', familyName: '深圳的家', city: '深圳', role: 'L4', member_id: 'member_001' },
};

const hits = [];          // 记录前端真的发出去的请求
const errors = [];        // 收集运行时错误

function jsonResponse(payload) {
  return {
    ok: true, status: 200,
    json: async () => payload,
  };
}

function fakeFetch(url, options) {
  const method = (options && options.method) || 'GET';
  const pathname = String(url).replace(/^https?:\/\/[^/]+/, '');
  hits.push(`${method} ${pathname}`);

  if (pathname === '/api/auth/accounts') {
    return Promise.resolve(jsonResponse({
      accounts: [{ username: 'wo', name: '我', role: 'L4', city: '深圳',
        family: { id: 'family_sz', name: '深圳的家' } }],
    }));
  }
  if (pathname === '/api/auth/login') {
    return Promise.resolve(jsonResponse({ token: 'fake-token', user: { name: '我' } }));
  }
  if (pathname === '/api/console/state') return Promise.resolve(jsonResponse(state));
  if (pathname === '/api/console/habits') {
    return Promise.resolve(jsonResponse({
      habits: [{ key: 'preference:空调温度', value: '24°C', type: 'preference', version: 1 }],
      count: 1,
    }));
  }
  if (pathname.startsWith('/api/console/messages/')) {
    return Promise.resolve(jsonResponse({ ok: true, unreadCount: 0 }));
  }
  if (pathname === '/api/console/messages/read-all') {
    return Promise.resolve(jsonResponse({ ok: true, marked: 1, unreadCount: 0 }));
  }
  if (pathname.startsWith('/api/console/schedules/')) {
    return Promise.resolve(jsonResponse({ ok: true, removed: 'sch_0001' }));
  }
  if (pathname === '/api/console/habits/forget') {
    return Promise.resolve(jsonResponse({ ok: true, forgot: 'preference:空调温度' }));
  }
  if (pathname.startsWith('/api/console/tasks/')) {
    return Promise.resolve(jsonResponse({ ok: true, task: {}, linked: [] }));
  }
  if (pathname.startsWith('/api/console/devices/')) {
    return Promise.resolve(jsonResponse({ ok: true, message: '已执行', device: state.devices[0] }));
  }
  if (pathname.startsWith('/api/console/scenes/')) {
    return Promise.resolve(jsonResponse({ ok: true, scene: '回家',
      affected: [{ deviceId: 'living_room_light', name: '客厅灯', ok: true }] }));
  }
  return Promise.resolve(jsonResponse({ ok: true }));
}

// ---------------------------------------------------------------- 环境补齐
// jsdom 不实现 Web Animations / scrollIntoView / 指针捕获，
// 缺哪个补哪个——缺了会在真实浏览器里没事、在这里却误报成前端 bug。
function patchWindow(win) {
  win.fetch = (url, options) => fakeFetch(url, options);
  win.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  win.Element.prototype.animate = () => ({
    finished: Promise.resolve(), cancel() {}, finish() {},
  });
  win.Element.prototype.scrollIntoView = function scrollIntoView() {};
  win.Element.prototype.setPointerCapture = function setPointerCapture() {};
  win.Element.prototype.releasePointerCapture = function releasePointerCapture() {};
  win.Element.prototype.hasPointerCapture = () => false;
  // jsdom 没实现滚动，不补的话每次切页都会刷一条假错误
  win.scrollTo = function scrollTo() {};
  win.Element.prototype.scrollTo = function scrollTo() {};
}

// ---------------------------------------------------------------- 跑
async function main() {
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', (e) => errors.push(`jsdomError: ${e.message}`));
  virtualConsole.on('error', (msg) => errors.push(`console.error: ${msg}`));

  const dom = new JSDOM(html.replace('<script src="/assets/app.js"></script>', ''), {
    url: 'http://localhost:8010/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,
    virtualConsole,
  });
  const { window } = dom;
  const doc = window.document;

  window.addEventListener('error', (e) => errors.push(`window.error: ${e.message}`));
  window.addEventListener('unhandledrejection', (e) => errors.push(`unhandledrejection: ${e.reason}`));
  patchWindow(window);

  window.eval(appSrc);
  const tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
  await tick(80);

  // ---- 1. 门禁态：没身份时必须是登录页，而不是白屏
  if (doc.querySelector('#login').hidden) errors.push('门禁失败：无 token 时登录层没显示');
  await tick(60);
  const acctBtn = doc.querySelector('.hm-login__acct');
  if (!acctBtn) errors.push('登录页没渲染出演示账号');

  // ---- 2. 登录 → 拉状态

  const click = (sel, root = doc) => {
    const el = root.querySelector(sel);
    if (!el) { errors.push(`找不到元素：${sel}`); return null; }
    el.click();
    return el;
  };

  // ---- 3. 逐个点可交互元素
  // 分两类，别混在一起判：
  //   - 要落库的动作 → 断言"发了请求"（没发就等于点了没反应）
  //   - 纯界面动作   → 断言"界面真的变了"（切换视图本来就不该发请求）
  const actions = [];
  async function expectRequest(label, selector, { confirm = false } = {}) {
    const before = hits.length;
    const el = click(selector);
    if (!el) { actions.push([label, '失败：元素不存在']); return; }
    await tick(60);
    if (confirm) {
      // 高危动作先弹层：点"确定"之后才该发请求
      const ok = doc.querySelector('#modal-ok');
      if (!ok) { actions.push([label, '失败：确认弹层没打开']); errors.push(`${label} 弹层没打开`); return; }
      ok.click();
      await tick(90);
    }
    const grew = hits.length > before;
    // 报告里要显示"这次动作的主请求"，而不是尾巴上那次 refresh 的 /habits
    const fresh = hits.slice(before);
    const main = fresh.find((h) => !h.includes('/console/state')
      && !h.includes('/console/habits')) || fresh[0] || '(无)';
    actions.push([label, grew ? `OK → ${main}` : '** 点了没发请求 **']);
    if (!grew) errors.push(`点了没反应：${label}（${selector}）`);
  }

  async function expectUi(label, selector, check, expectText) {
    const el = click(selector);
    if (!el) { actions.push([label, '失败：元素不存在']); return; }
    await tick(60);
    const ok = check(doc);
    actions.push([label, ok ? `OK → ${expectText}` : '** 点了界面没变 **']);
    if (!ok) errors.push(`点了界面没变化：${label}（${selector}）`);
  }

  // ---- 1.5 注册入口（必须赶在登录之前测：登录完门禁就关了）
  if (!doc.querySelector('#login-to-register')) {
    errors.push('登录页没有「创建一个家」入口');
  } else {
    await expectUi('注册·打开表单', '#login-to-register',
      (d) => !d.querySelector('#register-form').hidden, '注册表单出现');
    await expectUi('注册·切到加入家庭', '#reg-modes .hm-seg__item[data-regmode="join"]',
      (d) => !d.querySelector('#reg-join-fields').hidden, '邀请码输入框出现');
    await expectUi('注册·返回登录', '#register-back',
      (d) => d.querySelector('#register-form').hidden, '回到登录表单');
  }

  // ---- 2. 登录 → 拉状态
  acctBtn.click();
  await tick(150);
  if (!doc.querySelector('#login').hidden) errors.push('登录后门禁没关掉');
  if (!doc.querySelector('.hm-devicecard')) errors.push('设备卡没渲染');
  if (!doc.querySelector('.hm-taskcard')) errors.push('今日任务卡没渲染');

  // 首页
  await expectRequest('设备开关', '.hm-devicecard .hm-switch');
  await expectRequest('设备卡整卡（非开关区域）', '.hm-devicecard__name');
  await expectRequest('房间标题（整屋一键）', '.hm-room-title[data-room]');
  await expectRequest('场景胶囊', '.hm-scene');

  // 场景自定义：新建入口 → 编辑器 → 填名字保存必须真发请求 → 自建场景可编辑
  await expectUi('场景·新建（打开编辑器）', '#scene-new',
    (d) => d.querySelector('#modal-mask').classList.contains('is-open')
      && !!d.querySelector('#scene-name'), '编辑器打开');
  click('#modal-cancel');
  await tick(40);

  click('#scene-new');
  await tick(40);
  doc.querySelector('#scene-name').value = '冒烟新场景';
  const beforeSave = hits.length;
  click('#modal-ok');
  await tick(150);
  const posted = hits.slice(beforeSave).some((h) => h.startsWith('POST /api/console/scenes'));
  actions.push(['场景·保存新场景', posted ? 'OK → POST /api/console/scenes' : '** 没发请求 **']);
  if (!posted) errors.push('新建场景点了保存却没发请求');

  await expectUi('场景·编辑自建场景', '.hm-scene__edit',
    (d) => d.querySelector('#modal-mask').classList.contains('is-open')
      && d.querySelector('#scene-name').value === '夜读', '带出原场景名「夜读」');
  click('#modal-cancel');
  await tick(40);

  await expectRequest('舒适度提示按钮', '#hint-action');
  // 消息条目必须排在"一键已读"前面：全标已读之后，点单条本就不该再发请求
  await expectRequest('消息条目（标已读）', '.hm-msgitem[data-msg]');
  await expectUi('摘要条·用电切换', '[data-energy-view]',
    (d) => d.querySelector('#summary').textContent.includes('本周用电'), '今日 → 本周');
  await expectRequest('摘要条·消息（一键已读）', '[data-goto-messages]');
  await expectRequest('家庭成员（筛选任务）', '.hm-member[data-member]');
  await expectRequest('右栏用电行', '[data-energy-jump]');
  await tick(60);
  await expectRequest('习惯条目（遗忘 → 弹层确认）', '#aside-habits [data-habit]',
    { confirm: true });

  // ---- 4. 页面切换后继续点
  const goPage = (p) => {
    const btn = doc.querySelector(`.hm-tabbar__item[data-page="${p}"]`);
    if (btn) btn.click();
    else errors.push(`底部导航缺 ${p}`);
  };

  goPage('tasks'); await tick(80);
  await expectRequest('任务页·行点击（完成/恢复）', '#page-tasks-list .hm-listitem[data-id]');

  goPage('scenes'); await tick(80);
  await expectRequest('场景页·条目', '#page-scenes-list [data-scene]');

  goPage('me'); await tick(120);
  await expectRequest('我的页·家庭成员', '#page-me-body [data-member]');
  await expectRequest('我的页·提醒（删除 → 弹层确认）', '#page-me-body [data-schedule]',
    { confirm: true });
  await tick(150);
  await expectRequest('我的页·习惯条目（遗忘 → 弹层确认）', '#page-me-habits [data-habit]',
    { confirm: true });

  // ---- 5. AI 面板
  doc.querySelector('#fab').click();
  await tick(40);
  const input = doc.querySelector('#sheet-input');
  input.value = '客厅有点暗';
  doc.querySelector('#sheet-form').dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await tick(150);
  if (!doc.querySelector('.hm-msg.is-user')) errors.push('AI 面板没渲染用户消息');

  // ---- 6. 汇报
  console.log('=== 前端无头冒烟（jsdom）===');
  console.log(`点击动作 ${actions.length} 项：`);
  actions.forEach(([label, result]) => {
    console.log(`  ${result.startsWith('OK') ? '✓' : '✗'} ${label.padEnd(28, '　')} ${result}`);
  });
  console.log(`\n前端发起请求 ${hits.length} 次：`);
  [...new Set(hits)].forEach((h) => console.log(`  ${h}`));

  if (errors.length) {
    console.log(`\n发现 ${errors.length} 个问题：`);
    errors.forEach((e) => console.log(`  ✗ ${e}`));
    process.exit(1);
  }
  console.log('\n全部可点元素均有真实动作，无运行时错误。');
  window.close();
}

main().catch((e) => { console.error('冒烟脚本自身炸了：', e); process.exit(1); });
