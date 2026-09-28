/* Locius web UI — no build step. Talks to Sentinel (/sentinel/api) and, through it, the Runtime (/api). */
(() => {
'use strict';

// ------------------------------------------------------------------ i18n
// UI strings are written in Chinese in the source and wrapped in T()/Tf(); English comes from i18n.js.
const LANG = (() => {
  try { const v = localStorage.getItem('locius_lang'); if (v === 'zh' || v === 'en') return v; } catch (e) { /* storage blocked */ }
  return /^zh/i.test(navigator.language || '') ? 'zh' : 'en';
})();
const EN = window.LOCIUS_EN || {};
const CJK_RE = /[一-鿿]/;
// Fallback for dynamic bilingual text from the server, e.g. "发送邮件 Send email" or "已拒绝 (user denied)".
function biEn(s) {
  if (typeof s !== 'string' || !CJK_RE.test(s)) return s;
  const parts = s.split(/[；;]\s*/);
  if (parts.length > 1) return parts.map(biEn).join('; ');
  // "中文说明 (english explanation)" -> the part in brackets
  let m = s.match(/^(.*[\u4e00-\u9fff][^()（）]*?)\s*[(（]([^()（）\u4e00-\u9fff]*[A-Za-z][^()（）\u4e00-\u9fff]*)[)）]\s*([^\u4e00-\u9fff]*)$/);
  if (m) { const t = m[2].charAt(0).toUpperCase() + m[2].slice(1); return m[3] ? `${t} ${m[3]}` : t; }
  // "中文标签 English label" -> the trailing English
  m = s.match(/^([^\w\u4e00-\u9fff]*)\S.*[\u4e00-\u9fff](?:[）)」》]\s*|\s+)([A-Za-z][^\u4e00-\u9fff]*)$/);
  if (m) return (m[1] || '') + m[2];
  return s;
}
function T(s) { return LANG === 'en' ? (EN[s] ?? biEn(s)) : s; }
function Tf(s, ...a) { return (LANG === 'en' ? (EN[s] ?? s) : s).replace(/\{(\d+)\}/g, (_, i) => (a[i] ?? '')); }
const B = s => (LANG === 'en' ? biEn(String(s ?? '')) : s);   // server-provided bilingual text
async function setLang(l) {
  try { localStorage.setItem('locius_lang', l); } catch (e) { /* ignore */ }
  try { await fetch('api/settings', { method: 'PUT', headers: { 'X-Persona-UI': '1', 'Content-Type': 'application/json' }, body: JSON.stringify({ language: l }) }); } catch (e) { /* ignore */ }
  location.reload();
}
document.documentElement.lang = LANG === 'en' ? 'en' : 'zh-CN';
function i18nStatic() {
  const lt = h('button', { class: 'chip lang-toggle',  // i18n-ok: shows the other language
    title: LANG === 'en' ? '切换到中文' : 'Switch to English',  // i18n-ok
    onclick: () => setLang(LANG === 'en' ? 'zh' : 'en') }, LANG === 'en' ? '中文' : 'EN');  // i18n-ok
  const tr = document.querySelector('.top-right');
  if (tr) tr.prepend(lt);
  if (LANG !== 'en') return;
  const set = (sel, text, attr) => { const el = document.querySelector(sel); if (el) { if (attr) el.setAttribute(attr, text); else el.textContent = text; } };
  set('.brand small', 'Olares One · Local Agent');
  set('#menuBtn', 'Menu', 'aria-label');
  set('#viewTitle', 'Chat');
  set('#modelChip', 'Model', 'title'); set('#modelChip', 'Model …');
  set('#sentinelChip', 'Sentinel (guardian)', 'title');
  set('#approvalBell', 'Approvals', 'aria-label'); set('#approvalBell span', 'Approvals');
  set('.drawer-head h2', 'Pending approvals'); set('#drawerClose', 'Close', 'aria-label');
  set('#drawer > p', 'Raised independently by Sentinel, separate from the chat. The Agent pauses until you decide.');
}

// ------------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.setAttribute('style', v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function md(text) {
  try { return DOMPurify.sanitize(marked.parse(String(text || ''), { gfm: true, breaks: true }), { ADD_ATTR: ['target'] }); }
  catch (e) { return esc(text); }
}
function mdEl(text) { const d = h('div', { class: 'md' }); d.innerHTML = md(text); d.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noopener noreferrer'; }); return d; }
const fmtTime = ts => { if (!ts) return ''; const d = new Date(ts * 1000); return d.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); };
const fmtClock = ts => ts ? new Date(ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '';
const STATUS_ZH = { CREATED: T('已创建'), PLANNING: T('规划中'), RUNNING: T('执行中'), WAITING_APPROVAL: T('等待审批'), WAITING_EXTERNAL: T('等待外部'),
  PAUSED: T('已暂停'), FAILED: T('失败'), COMPLETED: T('已完成'), CANCELLED: T('已取消') };
const pill = st => h('span', { class: 'pill st-' + st, title: st }, LANG === 'en' ? (STATUS_ZH[st] || st) : `${STATUS_ZH[st] || st} ${st}`);
const riskPill = r => r ? h('span', { class: 'pill risk-' + r }, { low: T('低 low'), medium: T('中 medium'), high: T('高 high'), critical: T('极高 critical') }[r] || r) : '';

async function call(url, opts = {}) {
  const o = { method: opts.method || 'GET', headers: { 'X-Persona-UI': '1' } };
  if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(url, o);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}
const api = (p, o) => call('api/' + p, o);
const sapi = (p, o) => call('sentinel/api/' + p, o);
function toast(msg, err = false, onclick) {
  const t = h('div', { class: 'toast' + (err ? ' err' : ''), role: 'status' }, typeof msg === 'string' ? B(msg) : msg);
  t.onclick = () => { t.remove(); onclick && onclick(); };
  $('#toasts').append(t);
  setTimeout(() => t.remove(), err ? 9000 : 6000);
}
const isNarrow = () => window.matchMedia('(max-width: 760px)').matches;
const isTouch = () => window.matchMedia('(pointer: coarse)').matches;
const safe = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

// ------------------------------------------------------------------ state
const S = {
  view: 'chat', conv: null, convs: [], convData: null, tasks: {}, events: {}, approvals: [], selTask: null,
  settings: null, browserTimer: null, taskFilter: '', auditTask: '', seenApprovals: new Set(), sending: false,
};

const VIEWS = [
  ['chat', '💬', T('对话'), 'Chat'], ['tasks', '🗂', T('任务'), 'Tasks'], ['browser', '🌐', T('浏览器'), 'Browser'],
  ['schedules', '⚡', T('自动化'), 'Automations'], ['connections', '🔌', T('连接'), 'Connections'], ['memory', '🧠', T('记忆'), 'Memory'],
  ['activity', '📜', T('活动审计'), 'Audit'], ['settings', '⚙️', T('设置'), 'Settings'],
];

function renderNav() {
  const nav = $('#navlinks'); nav.innerHTML = '';
  for (const [id, ic, zh, en] of VIEWS) {
    const a = h('a', { href: '#' + id, class: S.view === id ? 'active' : '' }, h('span', { class: 'ic' }, ic), LANG === 'en' ? en : zh,
      id === 'tasks' && waitingCount() ? h('span', { class: 'badge' }, waitingCount()) : null, LANG === 'en' ? null : h('span', { class: 'en' }, en));
    nav.append(a);
  }
}
const waitingCount = () => Object.values(S.tasks).filter(t => t.status === 'WAITING_APPROVAL' || t.status === 'WAITING_EXTERNAL').length;

function route() {
  const v = (location.hash || '#chat').slice(1).split('/')[0];
  S.view = VIEWS.some(x => x[0] === v) ? v : 'chat';
  const arg = (location.hash || '').split('/')[1];
  if (S.view === 'tasks' && arg) S.selTask = arg;
  if (S.view === 'chat') { const c = arg || null; if (c !== S.conv) { S.conv = c; S.convData = null; } }
  if (S.browserTimer) { clearInterval(S.browserTimer); S.browserTimer = null; }
  $('#nav').classList.remove('open'); if ($('#navBack')) $('#navBack').hidden = true;
  const meta = VIEWS.find(x => x[0] === S.view);
  $('#viewTitle').textContent = LANG === 'en' ? meta[3] : `${meta[2]} ${meta[3]}`;
  renderNav();
  const view = $('#view'); view.innerHTML = '';
  ({ chat: viewChat, tasks: viewTasks, browser: viewBrowser, schedules: viewSchedules, connections: viewConnections,
     memory: viewMemory, activity: viewActivity, settings: viewSettings })[S.view](view);
}

// ================================================================== CHAT
const SUGGESTIONS = [
  T('看看这周有哪些重要邮件需要我回复'),
  T('给 John 回复：周二下午 3 点可以'),
  T('帮我研究东京三家酒店，并做一个比较'),
  T('每天早上 8 点给我总结重要邮件'),
  T('记住：我喜欢直飞航班和安静的精品酒店'),
];

async function viewChat(root) {
  const wrap = h('div', { class: 'chat' });
  const list = h('div', { class: 'convlist' });
  const thread = h('div', { class: 'thread' });
  wrap.append(list, thread); root.append(wrap);
  await loadConvs();
  if (S.conv && !S.convs.some(c => c.id === S.conv)) { S.conv = null; S.convData = null; }
  renderConvList(list);
  if (S.conv && !S.convData) await openConv(S.conv);
  else renderThread(thread);
  if (S.gmailReady === undefined) {  // only show the "connect Gmail" hint when Gmail really isn't connected
    sapi('connections').then(r => {
      const gm = (r.connections || []).find(c => c.name === 'gmail');
      S.gmailReady = !!(gm && gm.has_credential);
      if (!S.gmailReady && !S.conv) renderThread();
    }).catch(() => {});
  }
}

// start a fresh conversation: its own context, its own tasks
function newChat() {
  S.conv = null; S.convData = null; S.draft = '';
  closeHistory();
  if (location.hash !== '#chat') { history.replaceState(null, '', '#chat'); }
  renderConvList(); renderThread();
  const ta = $('#chatInput'); if (ta) { ta.value = ''; ta.dispatchEvent(new Event('input')); if (!isTouch()) ta.focus(); }
}

async function deleteConv(id) {
  const c = S.convs.find(x => x.id === id);
  if (!c) return;
  if (!confirmInline(Tf("删除对话「{0}」？任务记录仍保留在「任务」和「审计」里。", (c.title || T('无标题'))))) return;
  await api('conversations/' + id, { method: 'DELETE' });
  if (S.conv === id) newChat();
  await loadConvs(); renderConvList();
  toast(T('已删除 Deleted'));
}
// window.confirm is fine inside the Olares app window
function confirmInline(msg) { try { return window.confirm(msg); } catch (e) { return true; } }

function toggleHistory() {
  const pop = $('#histPop');
  if (pop) { closeHistory(); return; }
  const box = h('div', { class: 'hist-pop', id: 'histPop' });
  const listEl = h('div', { class: 'convlist in-pop' });
  box.append(listEl);
  $('.thread-head').append(box);
  renderConvList(listEl);
}
function closeHistory() { const p = $('#histPop'); if (p) p.remove(); }

async function loadConvs() {
  const r = await api('conversations');
  S.convs = r.conversations;
}

function renderConvList(list) {
  const lists = list ? [list] : Array.from(document.querySelectorAll('.convlist'));
  for (const el of lists) {
    el.innerHTML = '';
    el.append(h('button', { class: 'btn primary newchat', onclick: newChat, title: T('开始一个新对话（新的上下文）') }, T('＋ 新对话 New chat')));
    const chats = S.convs.filter(c => c.kind === 'chat'), scheds = S.convs.filter(c => c.kind === 'schedule');
    const mk = c => h('div', { class: 'conv' + (S.conv === c.id ? ' active' : ''), title: c.title, role: 'button', tabindex: '0',
        onclick: () => { closeHistory(); openConv(c.id); },
        onkeydown: e => { if (e.key === 'Enter') { closeHistory(); openConv(c.id); } } },
      h('span', { class: 'ct' }, c.title || T('(无标题)')),
      h('span', { class: 'cd faint' }, fmtTime(c.updated_at)),
      h('button', { class: 'cx', title: T('删除对话 Delete'), 'aria-label': T('删除对话'), onclick: e => { e.stopPropagation(); safe(deleteConv)(c.id); } }, '×'));
    if (chats.length) el.append(h('h4', null, T('对话 Chats')), ...chats.map(mk));
    else el.append(h('p', { class: 'small muted', style: 'padding:4px 8px' }, T('还没有对话')));
    if (scheds.length) el.append(h('h4', null, T('自动化 Automations')), ...scheds.map(mk));
  }
}

async function openConv(id) {
  if (id !== S.conv) S.draft = '';
  S.conv = id;
  if (location.hash !== '#chat/' + id) history.replaceState(null, '', '#chat/' + id);
  let r;
  try { r = await api('conversations/' + id); }
  catch (e) { toast(T('对话不存在或已删除'), true); newChat(); return; }
  if (S.conv !== id) return;  // user switched away meanwhile
  S.convData = r;
  Object.assign(S.tasks, r.tasks);
  renderConvList(); renderThread();
}

function renderThread(thread) {
  thread = thread || $('.thread'); if (!thread) return;
  const keepScroll = $('.msgs', thread);
  const oldComp = $('.composer', thread);
  // when a conversation is (re)opened, stick to the latest message for a moment even if other re-renders follow quickly
  if (S._renderedConv !== S.conv || !keepScroll) { S._renderedConv = S.conv; S._stickUntil = Date.now() + 2000; }
  const atBottom = Date.now() < (S._stickUntil || 0) || keepScroll.scrollHeight - keepScroll.scrollTop - keepScroll.clientHeight < 80;
  [...thread.children].forEach(c => { if (c !== oldComp) c.remove(); });
  const d = S.convData;
  const cur = S.convs.find(c => c.id === S.conv);
  const head = h('div', { class: 'thread-head' },
    h('button', { class: 'btn small hist-btn', onclick: toggleHistory, title: T('历史对话 History') }, T('☰ 历史')),
    h('div', { class: 'th-title', title: cur ? cur.title : '' }, cur ? (cur.kind === 'schedule' ? cur.title : cur.title || T('(无标题)')) : T('新对话 New chat')),
    d && d.messages.length ? h('span', { class: 'small faint th-meta' }, Tf("{0} 个任务 tasks", (Object.keys(d.tasks || {}).length))) : null,
    h('button', { class: 'btn small primary', onclick: newChat, title: T('开始新对话（新的上下文）') }, T('＋ 新对话')),
    S.conv ? h('button', { class: 'btn small', onclick: () => safe(deleteConv)(S.conv), title: T('删除当前对话') }, '🗑') : null);
  thread.insertBefore(head, oldComp || null);
  const msgs = h('div', { class: 'msgs', 'aria-live': 'polite' });
  if (!d || !d.messages.length) {
    msgs.append(h('div', { class: 'msg' }, h('div', { class: 'card' },
      h('h3', null, T('你好，我是 Locius 👋')),
      h('p', { class: 'sub' }, T('运行在你的 Olares One 上的私人 Agent。我可以读写 Gmail、操作浏览器、管理文件、记住你的偏好、定时执行任务。')
        + T('发送邮件、提交表单、付款、删除等高风险动作，都会由独立的 Sentinel（哨兵）弹出审批，你批准后才会执行。')),
      S.gmailReady === false ? h('p', { class: 'small muted' }, T('提示：先到「连接 Connections」配置 Gmail 应用专用密码 (App Password)。')) : null)));
  } else {
    const shown = new Set();
    for (const m of d.messages) {
      if (m.role === 'user') {
        msgs.append(h('div', { class: 'msg user' }, h('div', { class: 'bubble' }, m.content)));
        if (m.task_id && S.tasks[m.task_id]) { msgs.append(taskCard(S.tasks[m.task_id])); shown.add(m.task_id); }
      } else if (m.role === 'assistant') {
        if (m.task_id && !shown.has(m.task_id) && S.tasks[m.task_id]) { msgs.append(taskCard(S.tasks[m.task_id])); shown.add(m.task_id); }
        msgs.append(h('div', { class: 'msg assistant' }, h('div', { class: 'bubble' }, mdEl(m.content))));
      } else if (m.role === 'system') {
        let j = {}; try { j = JSON.parse(m.content); } catch (e) {}
        const t = S.tasks[j.task_id];
        if (j.type === 'approval') {
          const pending = S.approvals.some(a => a.id === j.approval_id);
          msgs.append(h('div', { class: 'msg' }, h('div', { class: 'sys-card' }, '🛡',
            h('span', null, Tf("Sentinel 请求审批：{0}", (j.title || ''))),
            pending ? h('button', { class: 'btn approve small', onclick: () => openApproval(j.approval_id) }, T('去审批 Review')) :
              h('span', { class: 'small muted' }, T('已处理 resolved')))));
        } else if (j.type === 'takeover') {
          const active = t && t.status === 'WAITING_EXTERNAL';
          msgs.append(h('div', { class: 'msg' }, h('div', { class: 'sys-card takeover' }, '🖐',
            h('span', null, Tf("Agent 请你接管浏览器：{0}", (j.reason || ''))),
            active ? h('a', { class: 'btn small', href: '#browser' }, T('打开浏览器 Open browser')) : h('span', { class: 'small muted' }, T('已结束 done')))));
        }
      }
    }
  }
  thread.insertBefore(msgs, oldComp || null);
  const compKey = `${S.conv || ''}|${d && d.messages.length ? 1 : 0}`;
  if (oldComp && oldComp.dataset.key === compKey) {
    if (atBottom) { msgs.scrollTop = msgs.scrollHeight; requestAnimationFrame(() => { msgs.scrollTop = msgs.scrollHeight; }); }
    else if (keepScroll) msgs.scrollTop = keepScroll.scrollTop;
    return;  // same conversation state: keep the existing input (focus, keyboard, draft) untouched
  }
  if (oldComp) oldComp.remove();
  const comp = h('div', { class: 'composer' }); comp.dataset.key = compKey;
  if (!d || !d.messages.length) comp.append(h('div', { class: 'suggest' }, SUGGESTIONS.map(s => h('button', { onclick: () => { ta.value = s; S.draft = s; grow(); ta.focus(); } }, s))));
  const hadFocus = document.activeElement && document.activeElement.id === 'chatInput';
  const narrow = isNarrow();
  const ph = S.conv ? (narrow ? T('继续这个对话…') : T('继续这个对话… (Enter 发送，Shift+Enter 换行)'))
    : (narrow ? T('告诉 Locius 要做什么…') : T('开始新对话：告诉 Locius 要做什么… (Enter 发送)'));
  const ta = h('textarea', { id: 'chatInput', rows: 1, placeholder: ph, enterkeyhint: isTouch() ? 'enter' : 'send', 'aria-label': T('消息 Message') });
  ta.value = S.draft || '';
  const grow = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight + 2, narrow ? 140 : 200) + 'px'; };
  ta.addEventListener('input', () => { S.draft = ta.value; grow(); });
  // desktop: Enter sends, Shift+Enter = newline. Phones: Enter = newline, tap the send button.
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !isTouch()) { e.preventDefault(); send(); } });
  if (hadFocus) requestAnimationFrame(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
  const btn = h('button', { class: 'btn primary send', onclick: () => send(), 'aria-label': T('发送 Send'), title: T('发送 Send') },
    h('span', { class: 'lbl' }, T('发送 Send')), h('span', { class: 'ico', 'aria-hidden': 'true' }, '↑'));
  comp.append(h('div', { class: 'box' }, ta, btn));
  thread.append(comp);
  requestAnimationFrame(grow);
  async function send() {
    const text = ta.value.trim(); if (!text || S.sending) return;
    S.sending = true; btn.disabled = true;
    try {
      const r = await api('chat', { method: 'POST', body: { message: text, conversation_id: S.conv } });
      ta.value = ''; S.draft = ''; grow();
      S.conv = r.conversation_id;
      await loadConvs(); await openConv(r.conversation_id);
    } catch (e) { toast(e.message, true); }
    finally { S.sending = false; btn.disabled = false; }
  }
  if (atBottom) { msgs.scrollTop = msgs.scrollHeight; requestAnimationFrame(() => { msgs.scrollTop = msgs.scrollHeight; }); }
  else if (keepScroll) msgs.scrollTop = keepScroll.scrollTop;
}

function planList(plan) {
  if (!plan || !plan.steps || !plan.steps.length) return null;
  const mk = { pending: '○', running: '▸', done: '✓', failed: '✕', skipped: '–' };
  return h('ol', { class: 'plan' }, plan.steps.map(s => h('li', { class: s.status || 'pending' },
    h('span', { class: 'mk' }, mk[s.status || 'pending'] || '○'), h('span', { class: 'tx' }, s.description))));
}

function taskCard(t) {
  const active = !['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.status);
  const card = h('div', { class: 'msg' }, h('div', { class: 'taskcard', id: 'tc-' + t.id },
    h('div', { class: 'head' }, pill(t.status),
      h('span', { class: 'goal', title: t.goal }, t.plan && t.plan.objective ? t.plan.objective : t.goal),
      active && t.status !== 'WAITING_APPROVAL' ? h('span', { class: 'typing dots' }, t.status === 'PLANNING' ? T('规划中') : T('执行中')) : null,
      h('a', { class: 'small', href: '#tasks/' + t.id }, T('详情 Details'))),
    planList(t.plan),
    t.status === 'WAITING_APPROVAL' && t.waiting ? h('div', { class: 'row', style: 'margin-top:8px' },
      h('button', { class: 'btn approve small', onclick: () => openApproval(t.waiting.approval_id) }, T('🛡 审批：') + ((t.waiting.summary || {}).title || '')))
      : null,
    t.status === 'WAITING_EXTERNAL' ? h('div', { class: 'row', style: 'margin-top:8px' },
      h('a', { class: 'btn small', href: '#browser' }, T('🖐 去浏览器接管 Take over'))) : null,
    t.status === 'FAILED' && t.error ? h('p', { class: 'small', style: 'color:var(--danger);margin:8px 0 0' }, t.error) : null,
    traceBox(t)));
  return card;
}

function traceBox(t) {
  const det = h('details', { class: 'trace' }, h('summary', null, T('执行过程 Activity')));
  const tl = h('div', { class: 'timeline', id: 'tl-' + t.id }, h('span', { class: 'small muted' }, T('加载中…')));
  det.append(tl);
  det.addEventListener('toggle', async () => {
    if (!det.open) return;
    const r = await api('tasks/' + t.id);
    S.events[t.id] = r.events;
    fillTimeline(tl, r.events);
  });
  return det;
}

function evLine(e) {
  const d = e.data || {};
  let body;
  switch (e.type) {
    case 'plan': body = h('span', null, Tf("📋 计划 Plan v{0}：{1} 步", (d.version || 1), ((d.steps || []).length))); break;
    case 'thinking': body = h('span', { class: 'muted' }, Tf("🤔 思考 step {0}", (d.step))); break;
    case 'reasoning': body = h('details', null, h('summary', { class: 'muted' }, T('💭 推理 reasoning')), h('pre', null, d.text)); break;
    case 'message': body = h('span', null, '💬 ', d.text); break;
    case 'tool_call': body = h('div', null, d.sub ? '↳ ' : '🔧 ', h('span', { class: 'tool' }, d.name), h('pre', null, JSON.stringify(d.args, null, 1))); break;
    case 'tool_result': body = h('details', null, h('summary', null, (d.ok === false ? '⚠️ ' : '✅ ') + d.name + T(' 结果 result')), h('pre', null, d.preview)); break;
    case 'waiting': body = h('span', { style: 'color:var(--approve)' }, d.type === 'approval' ? Tf("🛡 等待审批：{0}", B((d.summary || {}).title || d.tool)) : Tf("🖐 等待接管：{0}", (d.reason || ''))); break;
    case 'approval_resolved': body = h('span', null, Tf("🛡 审批结果：{0}", (d.decision))); break;
    case 'subagent_start': body = h('span', null, Tf("🧩 子 Agent ({0})：{1}", (d.role), (d.task))); break;
    case 'subagent_done': body = h('details', null, h('summary', null, Tf("🧩 子 Agent 完成 ({0})", (d.role))), h('pre', null, d.report)); break;
    case 'final': body = h('span', null, T('🏁 完成 Final')); break;
    case 'memory_saved': body = h('span', null, T('🧠 记住了：') + (d.facts || []).join(T('；'))); break;
    case 'replanning': body = h('span', { style: 'color:var(--warn)' }, T('🔄 重新规划 Re-plan')); break;
    case 'error': case 'planner_error': body = h('span', { style: 'color:var(--danger)' }, '❌ ' + (d.message || '')); break;
    default: body = h('span', { class: 'muted' }, e.type + ' ' + JSON.stringify(d).slice(0, 200));
  }
  return h('div', { class: 'ev' }, h('span', { class: 't' }, fmtClock(e.ts)), h('div', { class: 'b' }, body));
}
function fillTimeline(tl, events) {
  tl.innerHTML = '';
  if (!events.length) tl.append(h('span', { class: 'small muted' }, T('暂无 none')));
  events.filter(e => e.type !== 'thinking').forEach(e => tl.append(evLine(e)));
}

// ================================================================== TASKS
async function viewTasks(root) {
  const r = await api('tasks' + (S.taskFilter ? '?status=' + S.taskFilter : ''));
  r.tasks.forEach(t => { S.tasks[t.id] = { ...(S.tasks[t.id] || {}), ...t }; });
  const filters = [['', T('全部 All')], ['RUNNING,PLANNING,CREATED', T('执行中 Running')], ['WAITING_APPROVAL,WAITING_EXTERNAL,PAUSED', T('等待中 Waiting')],
    ['COMPLETED', T('已完成 Done')], ['FAILED,CANCELLED', T('失败 Failed')]];
  const left = h('div', null, h('div', { class: 'filters' }, filters.map(([v, l]) => h('button', { class: S.taskFilter === v ? 'on' : '', onclick: () => { S.taskFilter = v; route(); } }, l))));
  const list = h('div', { class: 'list' });
  if (!r.tasks.length) list.append(h('div', { class: 'empty' }, T('还没有任务。去「对话」里给 Locius 布置一个吧。')));
  for (const t of r.tasks) list.append(h('button', { class: 'item' + (S.selTask === t.id ? ' active' : ''), onclick: () => { location.hash = 'tasks/' + t.id; } },
    h('div', { class: 'top' }, pill(t.status), h('span', { class: 'title' }, t.goal)),
    h('div', { class: 'small muted' }, Tf("{0} · {1} · {2} 步", (fmtTime(t.created_at)), (t.source === 'schedule' ? T('⏰ 定时') : T('💬 对话')), (t.steps || 0)))));
  left.append(list);
  const right = h('div', { id: 'taskDetail' });
  root.append(h('div', { class: 'split' + (S.selTask ? ' has-sel' : '') }, left, right));
  if (S.selTask) renderTaskDetail(right, S.selTask);
  else right.append(h('div', { class: 'card empty' }, T('选择左侧任务查看计划、每一步工具调用和审批记录。')));
}

async function renderTaskDetail(box, id) {
  const t = await api('tasks/' + id);
  S.tasks[id] = { ...(S.tasks[id] || {}), ...t };
  S.events[id] = t.events;
  box.innerHTML = '';
  const active = !['COMPLETED', 'FAILED', 'CANCELLED'].includes(t.status);
  const act = (a) => safe(async () => { await api(`tasks/${id}/${a}`, { method: 'POST', body: {} }); toast(T('已提交 ') + a); setTimeout(() => renderTaskDetail(box, id), 500); });
  const tl = h('div', { class: 'timeline' }); fillTimeline(tl, t.events);
  box.append(h('div', { class: 'card stack' },
    h('button', { class: 'btn small only-narrow', onclick: () => { S.selTask = null; location.hash = 'tasks'; } }, T('← 任务列表 All tasks')),
    h('div', { class: 'row' }, pill(t.status), h('b', { style: 'flex:1' }, t.goal)),
    h('dl', { class: 'kv' },
      h('dt', null, T('任务 ID')), h('dd', { class: 'mono' }, t.id),
      h('dt', null, T('来源 Source')), h('dd', null, t.source === 'schedule' ? T('⏰ 定时任务 schedule') : T('💬 对话 chat')),
      h('dt', null, T('创建 Created')), h('dd', null, fmtTime(t.created_at)),
      h('dt', null, T('步数 Steps')), h('dd', null, t.steps || 0),
      t.waiting ? [h('dt', null, T('等待 Waiting')), h('dd', null, t.waiting.type === 'approval' ? '🛡 ' + B((t.waiting.summary || {}).title || T('审批')) : '🖐 ' + (t.waiting.reason || t.waiting.type))] : null),
    h('div', { class: 'row' },
      t.status === 'WAITING_APPROVAL' && t.waiting ? h('button', { class: 'btn approve small', onclick: () => openApproval(t.waiting.approval_id) }, T('🛡 去审批')) : null,
      t.status === 'WAITING_EXTERNAL' ? h('a', { class: 'btn small', href: '#browser' }, T('🖐 接管浏览器')) : null,
      ['RUNNING', 'PLANNING'].includes(t.status) ? h('button', { class: 'btn small', onclick: act('pause') }, T('⏸ 暂停 Pause')) : null,
      t.status === 'PAUSED' ? h('button', { class: 'btn small', onclick: act('resume') }, T('▶ 继续 Resume')) : null,
      active ? h('button', { class: 'btn danger small', onclick: act('cancel') }, T('■ 取消 Cancel')) : h('button', { class: 'btn small', onclick: act('retry') }, T('↻ 重新执行 Retry')),
      h('a', { class: 'btn small', href: '#activity', onclick: () => { S.auditTask = id; } }, T('📜 审计记录'))),
    t.plan && t.plan.steps && t.plan.steps.length ? h('div', null, h('b', null, T('计划 Plan')), planList(t.plan)) : null,
    t.result ? h('div', null, h('b', null, T('结果 Result')), mdEl(t.result)) : null,
    t.error ? h('p', { style: 'color:var(--danger)' }, t.error) : null,
    h('div', null, h('b', null, T('执行过程 Timeline')), tl)));
}

// ================================================================== APPROVALS
async function loadApprovals() {
  try {
    const r = await sapi('approvals?status=pending');
    S.approvals = r.approvals;
  } catch (e) { return; }
  const n = S.approvals.length;
  $('#approvalCount').textContent = n; $('#approvalCount').hidden = !n;
  $('#approvalBell').classList.toggle('hot', n > 0);
  if (!$('#drawer').hidden) renderDrawer();
  const fresh = S.approvals.filter(a => !S.seenApprovals.has(a.id));
  fresh.forEach(a => S.seenApprovals.add(a.id));
  if (fresh.length && !$('.modal-back')) openApproval(fresh[0].id);
  document.title = n ? `(${n}) Locius` : 'Locius';
}

function approvalForm(a, onDone) {
  const s = a.summary || {};
  const editable = new Set(s.editable || []);
  const inputs = {};
  const box = h('div', { class: 'approval' });
  box.append(h('div', { class: 'ttl' }, h('b', null, '🛡 ' + B(s.title || a.tool)), riskPill(a.risk), h('span', { class: 'small muted mono' }, a.tool)));
  box.append(h('div', { class: 'small muted' }, Tf("任务 {0} · {1}", (a.task_id), (fmtTime(a.created_at)))));
  if (a.reason) box.append(h('div', { class: 'why' }, T('为什么需要审批：') + B(a.reason)));
  if (s.warning) box.append(h('div', { class: 'why' }, B(s.warning)));
  const kv = h('dl', { class: 'kv' });
  for (const [k, v] of s.fields || []) {
    const key = { '收件人 To': 'to', '抄送 Cc': 'cc', '主题 Subject': 'subject', '转发给 To': 'to' }[k];  // i18n-ok: server field ids
    kv.append(h('dt', null, B(k)));
    if (key && editable.has(key)) { const inp = h('input', { type: 'text', value: v || '' }); inputs[key] = inp; kv.append(h('dd', null, inp)); }
    else kv.append(h('dd', null, v || '—'));
  }
  box.append(kv);
  if (Array.isArray(s.items)) {  // e.g. unsubscribe: one checkbox per email, untick to keep
    const METHOD = { 'one-click': T('一键退订 One-click'), email: T('发退订邮件 Email'), link: T('打开退订链接 Link') };
    const checks = [];
    const list = h('div', { class: 'itemlist' });
    for (const it of s.items) {
      const ok = !!it.method;
      const cb = h('input', { type: 'checkbox', checked: ok, disabled: !ok });
      cb.dataset.id = it.id; checks.push(cb);
      list.append(h('label', { class: 'item' + (ok ? '' : ' off') }, cb,
        h('span', { class: 'it-main' }, h('b', null, (it.from || '').replace(/<[^>]*>/, '').replace(/"/g, '').trim() || it.id), h('span', { class: 'muted' }, ' · ' + (it.subject || ''))),
        h('span', { class: 'small faint it-m' }, ok ? METHOD[it.method] || it.method : (B(it.error) || T('无退订信息 N/A')))));
    }
    const count = h('span', { class: 'small muted' });
    const upd = () => { count.textContent = Tf("已选 {0} / {1}（取消勾选 = 保留，不退订）", (checks.filter(c => c.checked).length), (s.items.length)); };
    checks.forEach(c => c.onchange = upd); upd();
    const all = (v) => () => { checks.forEach(c => { if (!c.disabled) c.checked = v; }); upd(); };
    box.append(h('div', { class: 'row' }, h('b', { class: 'small' }, T('要退订的邮件 Emails')), count,
      h('button', { class: 'btn small', onclick: all(true) }, T('全选')), h('button', { class: 'btn small', onclick: all(false) }, T('全不选'))), list);
    if (editable.has('message_ids')) inputs.message_ids = { get value() { return checks.filter(c => c.checked).map(c => c.dataset.id); } };
  }
  if (s.body !== undefined && s.body !== null) {
    const bodyKey = a.tool === 'gmail_forward' ? 'note' : a.tool === 'browser_type' ? 'text' : 'body';
    if (editable.has(bodyKey)) { const ta = h('textarea', { rows: 8 }); ta.value = s.body || ''; inputs[bodyKey] = ta; box.append(h('label', { class: 'field' }, h('span', null, T('内容（可修改后批准）Content — editable')), ta)); }
    else box.append(h('pre', { class: 'md', style: 'white-space:pre-wrap' }, s.body));
  }
  if (s.screenshot) box.append(h('img', { class: 'shot', src: `sentinel/api/approvals/${a.id}/screenshot`, alt: T('操作时的页面截图 page screenshot'), onerror: e => e.target.remove() }));
  const scope = h('select', { 'aria-label': T('授权范围 scope') },
    h('option', { value: 'ONCE' }, T('仅这一次 Approve once')),
    h('option', { value: 'TASK' }, T('本任务内同类操作 For this task')),
    h('option', { value: 'SESSION' }, T('8 小时内 For this session (8h)')),
    h('option', { value: 'TIME_BOUND' }, T('24 小时内 For 24 hours')),
    h('option', { value: 'PERMANENT' }, T('以后总是允许（同一目标）Always allow')));
  const note = h('input', { type: 'text', placeholder: T('拒绝原因（可选，会告诉 Agent）Reason for denying (optional)') });
  const approve = h('button', { class: 'btn approve' }, T('✓ 批准 Approve'));
  const deny = h('button', { class: 'btn danger' }, T('✕ 拒绝 Deny'));
  const resolve = async (decision) => {
    approve.disabled = deny.disabled = true;
    const args = {}; for (const [k, el] of Object.entries(inputs)) args[k] = el.value;
    try {
      const r = await sapi(`approvals/${a.id}/resolve`, { method: 'POST', body: { decision, scope: scope.value, ttl_hours: 24, args, note: note.value } });
      if (decision === 'approve') {
        const st = r.result && r.result.status;
        const res = (r.result && r.result.result) || {};
        const extra = res.results ? Tf("：退订成功 {0}，已打开页面 {1}，需手动/失败 {2}", (res.done || 0), (res.link_opened || 0), (res.manual_or_failed || 0)) : '';
        if (r.status === 'denied') toast(T('没有勾选任何邮件，已取消 Nothing selected'));
        else toast(st === 'ok' ? T('已批准并执行 ✓ Approved & executed') + extra : Tf("已批准，但执行结果：{0}", ((r.result && (r.result.error || st)) || r.reason || '')), st !== 'ok');
      } else toast(T('已拒绝 Denied'));
      onDone && onDone();
      loadApprovals();
    } catch (e) { toast(e.message, true); approve.disabled = deny.disabled = false; }
  };
  approve.onclick = () => resolve('approve');
  deny.onclick = () => resolve('deny');
  box.append(h('div', { class: 'ap-actions' }, h('div', { class: 'actions' }, approve, scope), h('div', { class: 'actions' }, deny, note)));
  return box;
}

function openApproval(id) {
  const a = S.approvals.find(x => x.id === id);
  if (!a) { toast(T('这个审批已处理或已过期 already resolved')); loadApprovals(); return; }
  closeModal();
  const back = h('div', { class: 'modal-back', onclick: e => { if (e.target === back) closeModal(); } });
  const m = h('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': T('审批 Approval') },
    h('div', { class: 'row' }, h('h2', { style: 'flex:1' }, T('Locius 请求执行操作')), h('button', { class: 'icon-btn', onclick: closeModal, 'aria-label': T('稍后 later') }, '✕')),
    h('p', { class: 'small muted', style: 'margin:0' }, T('此请求来自 Sentinel（独立于 Agent）。你决定前任务会暂停。')),
    approvalForm(a, closeModal));
  back.append(m); $('#modalRoot').append(back);
}
function closeModal() { $('#modalRoot').innerHTML = ''; }

const AP_DONE = { approved: [T('✅ 已批准'), 'st-COMPLETED'], denied: [T('❌ 已拒绝'), 'st-FAILED'], expired: [T('⌛ 已过期'), 'st-CANCELLED'] };
function renderDrawer() {
  const b = $('#drawerBody'); b.innerHTML = '';
  if (!S.approvals.length) b.append(h('div', { class: 'empty' }, T('没有待审批的操作 ✓')));
  S.approvals.forEach(a => b.append(approvalForm(a)));
  const hist = h('div', { class: 'stack', style: 'gap:6px' });
  b.append(h('details', { class: 'ap-history', style: 'margin-top:16px' },
    h('summary', { class: 'small' }, h('b', null, T('最近已处理 Recent decisions'))), hist));
  sapi('approvals?status=resolved&limit=20').then(r => {
    if (!r.approvals.length) { hist.append(h('div', { class: 'small muted' }, T('还没有处理过的审批'))); return; }
    r.approvals.forEach(a => {
      const [lbl, cls] = AP_DONE[a.status] || [a.status, ''];
      const res = a.result || {};
      const why = a.status === 'expired' ? (res.reason || '') : a.decided_by === 'user' && a.scope && a.scope !== 'ONCE' ? Tf("范围 {0}", (a.scope)) : '';
      hist.append(h('div', { class: 'card', style: 'padding:8px 10px' },
        h('div', { class: 'row' }, h('span', { style: 'flex:1' }, B((a.summary || {}).title || a.tool)), h('span', { class: 'pill ' + cls }, lbl)),
        h('div', { class: 'small muted' }, `${fmtTime(a.resolved_at || a.created_at)} · ${a.tool}` + (why ? ' · ' + why : ''))));
    });
  }).catch(() => {});
}

// ================================================================== BROWSER
async function viewBrowser(root) {
  const banner = h('div', { class: 'banner agent' });
  const url = h('input', { type: 'text', class: 'url', placeholder: T('接管时可输入网址 URL (takeover only)'), 'aria-label': 'URL' });
  const img = h('img', { alt: T('Agent 浏览器实时画面 live view'), draggable: 'false' });
  const trap = h('textarea', { class: 'keytrap', 'aria-label': T('键盘输入 keyboard input') });
  const kbd = h('div', { class: 'kbd-hint' });
  const wrap = h('div', { class: 'screen-wrap' }, img, trap, kbd);
  const takeBtn = h('button', { class: 'btn' }, T('🖐 接管 Take over'));
  const relBtn = h('button', { class: 'btn primary' }, T('↩ 交还给 Agent Hand back'));
  const tabsSel = h('select', { style: 'width:auto', 'aria-label': T('任务标签页 task tab') });
  const bar = h('div', { class: 'bbar' },
    h('button', { class: 'btn small', title: T('后退 back'), onclick: () => input({ type: 'back' }) }, '←'),
    h('button', { class: 'btn small', title: T('刷新 reload'), onclick: () => input({ type: 'reload' }) }, '⟳'),
    url, h('button', { class: 'btn small', onclick: () => go() }, T('前往 Go')), tabsSel);
  let urlDirty = false;
  const go = () => { urlDirty = false; input({ type: 'navigate', url: url.value }); };
  url.addEventListener('input', () => { urlDirty = true; });
  url.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); go(); } });
  const typeBox = h('input', { type: 'text', placeholder: T('接管时在此输入文字后回车发送到页面（密码不会被记录或给 Agent）') });
  const sendText = h('div', { class: 'row' }, typeBox, h('button', { class: 'btn small', onclick: () => { input({ type: 'text', text: typeBox.value }); typeBox.value = ''; } }, T('输入 Type')),
    ['Enter', 'Tab', 'Backspace', 'Escape'].map(k => h('button', { class: 'btn small', onclick: () => input({ type: 'key', key: k }) }, k)));
  typeBox.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); input({ type: 'text', text: typeBox.value }); typeBox.value = ''; } });
  root.append(h('div', { class: 'bview' }, banner, h('div', { class: 'row' }, takeBtn, relBtn), bar, sendText, wrap,
    h('p', { class: 'small muted' }, T('Agent 只能通过受限 API（打开网址 / 读取无障碍快照 / 点击 / 输入）操作这个浏览器，不能执行任意 JavaScript。接管期间 Agent 完全暂停，你的输入不会进入 Agent 上下文。'))));
  let st = {};
  // Inputs are sent strictly one after another (typing fast used to arrive out of order: "test" -> "tste").
  // Characters typed while a request is in flight are merged into the next "text" event.
  let queue = Promise.resolve(), buf = null, refreshTimer = null;
  const send = async ev => {
    try { await sapi('browser/input', { method: 'POST', body: ev }); } catch (e) { toast(e.message, true); }
    clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh(true), 250);
  };
  function input(ev) {
    if (st.mode !== 'user') { toast(T('请先点击「接管 Take over」')); return queue; }
    if (ev.type === 'text') {
      if (!ev.text) return queue;
      if (!buf) {   // open a text batch at this point of the queue; later characters join it until it is sent
        const mine = buf = { text: '' };
        queue = queue.then(() => { if (buf === mine) buf = null; return mine.text ? send({ type: 'text', text: mine.text }) : null; });
      }
      buf.text += ev.text;
      return queue;
    }
    buf = null;     // characters typed after this key must be sent after it
    queue = queue.then(() => send(ev));
    return queue;
  }
  takeBtn.onclick = safe(async () => { st = await sapi('browser/takeover', { method: 'POST', body: { task_id: tabsSel.value || null } }); toast(T('你已接管浏览器，Agent 已暂停')); refresh(true); });
  relBtn.onclick = safe(async () => { await sapi('browser/release', { method: 'POST', body: {} }); toast(T('已交还，Agent 继续执行')); refresh(true); });
  tabsSel.onchange = safe(async () => { await sapi('browser/view', { method: 'POST', body: { task_id: tabsSel.value } }); refresh(true); });
  const kbdState = () => {
    const on = document.activeElement === trap;
    wrap.classList.toggle('kbd-on', on && st.mode === 'user');
    kbd.textContent = st.mode !== 'user' ? '' : on ? T('⌨️ 键盘已连接：直接输入（可粘贴 Ctrl/⌘+V）Keyboard connected') : T('👆 先点一下画面里的输入框，再用键盘输入 Click a field first');
  };
  trap.addEventListener('focus', kbdState); trap.addEventListener('blur', kbdState);
  img.addEventListener('click', e => {
    trap.focus({ preventScroll: true });
    if (st.mode !== 'user') { toast(T('请先点击「接管 Take over」')); return; }
    const r = img.getBoundingClientRect(), vw = img.naturalWidth || (st.viewport || {}).width || 1280, vh = img.naturalHeight || (st.viewport || {}).height || 800;
    input({ type: 'click', x: (e.clientX - r.left) / r.width * vw, y: (e.clientY - r.top) / r.height * vh });
  });
  img.addEventListener('wheel', e => {
    if (st.mode !== 'user') return; e.preventDefault();
    const r = img.getBoundingClientRect(), vw = img.naturalWidth || (st.viewport || {}).width || 1280, vh = img.naturalHeight || (st.viewport || {}).height || 800;
    input({ type: 'wheel', x: (e.clientX - r.left) / r.width * vw, y: (e.clientY - r.top) / r.height * vh, dx: e.deltaX, dy: e.deltaY });
  }, { passive: false });
  trap.addEventListener('keydown', e => {
    if (st.mode !== 'user' || e.isComposing) return;
    const special = ['Enter', 'Tab', 'Backspace', 'Delete', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'];
    if (special.includes(e.key)) { e.preventDefault(); input({ type: 'key', key: e.key }); return; }
    // shortcuts: select all / undo / redo (Mac ⌘ is sent as Ctrl; paste is handled natively via the input event)
    if ((e.ctrlKey || e.metaKey) && /^[azy]$/i.test(e.key)) { e.preventDefault(); input({ type: 'key', key: 'Control+' + e.key.toLowerCase() }); }
  });
  // typing while nothing is focused (e.g. right after a click elsewhere) still goes to the remote page
  const docKeys = e => {
    if (S.view !== 'browser') { document.removeEventListener('keydown', docKeys); return; }
    if (st.mode !== 'user' || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
    const ae = document.activeElement;
    if (ae && ae !== document.body && ae !== trap) return;   // user is typing in the URL bar / text box etc.
    if (ae === trap) return;
    trap.focus({ preventScroll: true });
    if (e.key.length === 1) { e.preventDefault(); input({ type: 'text', text: e.key }); }
    else if (['Enter', 'Tab', 'Backspace', 'Delete', 'Escape'].includes(e.key)) { e.preventDefault(); input({ type: 'key', key: e.key }); }
  };
  if (S.browserKeys) document.removeEventListener('keydown', S.browserKeys);
  S.browserKeys = docKeys; document.addEventListener('keydown', docKeys);
  trap.addEventListener('input', () => { if (st.mode === 'user' && trap.value) { input({ type: 'text', text: trap.value }); trap.value = ''; } });
  let busy = false;
  async function refresh(force) {
    if (busy && !force) return; busy = true;
    try {
      st = await sapi('browser/state');
      const req = st.requested;
      banner.className = 'banner ' + (st.mode === 'user' ? 'user' : req ? 'req' : 'agent');
      banner.textContent = st.mode === 'offline' ? T('⚠️ 浏览器服务未就绪 browser offline: ') + (st.error || '') :
        st.mode === 'user' ? T('🖐 你正在控制浏览器（Agent 已暂停）。完成登录/验证后点击「交还给 Agent」。') :
        req ? Tf("🙋 Agent 请求你接管：{0}（点击「接管」）", (req.reason || '')) : T('🤖 Agent 控制中 — 实时画面 Live view');
      if (st.popup && st.mode !== 'offline') banner.textContent += T(' ｜ 🪟 正在显示弹出窗口（如 Google 登录）；它关闭后会自动回到原页面 Popup window shown — returns to the page automatically when it closes.');
      wrap.classList.toggle('user', st.mode === 'user'); kbdState();
      takeBtn.disabled = st.mode === 'user'; relBtn.disabled = st.mode !== 'user';
      if (document.activeElement !== url && !urlDirty) url.value = st.url || '';
      const cur = tabsSel.value;
      tabsSel.innerHTML = '';
      (st.tasks || []).forEach(t => tabsSel.append(h('option', { value: t.task_id, selected: t.task_id === st.view_task }, `${t.task_id.slice(0, 14)} · ${(t.url || '').slice(0, 40)}`)));
      if (!st.tasks || !st.tasks.length) tabsSel.append(h('option', { value: '' }, T('（无标签页 no tabs）')));
      const r = await fetch('sentinel/api/browser/screenshot?ts=' + Date.now());
      if (r.status === 200) { const b = await r.blob(); const u = URL.createObjectURL(b); const old = img.src; img.src = u; if (old.startsWith('blob:')) URL.revokeObjectURL(old); }
    } catch (e) { banner.textContent = '⚠️ ' + e.message; }
    finally { busy = false; }
  }
  refresh(true);
  S.browserTimer = setInterval(() => { if (!document.hidden) refresh(); }, 1200);
}

// Human description of a schedule / trigger / goal check (server sends Chinese; build English here).
function cronText(spec) {
  const p = String(spec || '').trim().split(/\s+/);
  if (p.length !== 5 || !/^\d+$/.test(p[0]) || !/^\d+$/.test(p[1]) || p[2] !== '*' || p[3] !== '*') return null;
  const t = `${p[1]}:${p[0].padStart(2, '0')}`;
  const en = LANG === 'en';
  const DAYS = en ? ['Sundays', 'Mondays', 'Tuesdays', 'Wednesdays', 'Thursdays', 'Fridays', 'Saturdays', 'Sundays']
    : ['每周日', '每周一', '每周二', '每周三', '每周四', '每周五', '每周六', '每周日'];
  if (p[4] === '*') return en ? `Daily at ${t}` : `每天 ${t}`;
  if (p[4] === '1-5') return en ? `Weekdays at ${t}` : `工作日 ${t}`;
  if (/^[0-7]$/.test(p[4])) return en ? `${DAYS[+p[4]]} at ${t}` : `${DAYS[+p[4]]} ${t}`;
  return null;
}
// Human description of a schedule / trigger / goal check (server sends Chinese; build English here).
function schedDesc(x) {
  if (x.kind === 'cron' && cronText(x.spec)) return cronText(x.spec);
  if (LANG !== 'en') return x.describe || x.check || '';
  if (x.kind === 'event') {
    let d = {}; try { d = JSON.parse(x.spec || '{}'); } catch (e) { /* ignore */ }
    const src = { 'gmail.new_email': 'New email', 'slack.new_message': 'New Slack message', 'notion.db_changed': 'Notion database changed' }[d.source] || d.source;
    const p = Object.entries(d.params || {}).filter(([, v]) => v).map(([k, v]) => `${k}=${v}`).join(', ');
    return `${src}${p ? ' · ' + p : ''} · checked every ${d.every || 3} min`;
  }
  if (x.kind === 'interval') return `every ${x.spec} min`;
  return `cron ${x.spec || ''}`;
}

// ================================================================== AUTOMATIONS (goals, triggers, schedules)
const GOAL_ST = { active: [T('进行中 Active'), 'st-RUNNING'], paused: [T('已暂停 Paused'), 'st-PAUSED'], achieved: [T('已达成 Achieved'), 'st-COMPLETED'],
  failed: [T('未达成 Failed'), 'st-FAILED'], expired: [T('已过期 Expired'), 'st-CANCELLED'], cancelled: [T('已取消 Cancelled'), 'st-CANCELLED'],
  blocked: [T('需要你帮忙 Blocked'), 'st-WAITING_EXTERNAL'] };
const SRC_FIELDS = {
  'gmail.new_email': [['query', T('Gmail 搜索条件（可选）'), T('例如 from:boss@acme.com 或 is:important；留空 = 所有新邮件')], ['account', T('只看某个邮箱（可选）'), 'you@gmail.com']],
  'slack.new_message': [['channel', T('Slack 频道'), '#general'], ['keyword', T('包含关键词才触发（可选）'), T('例如 urgent')], ['mentions_only', T('只在 @我 时触发（填 yes）'), 'yes']],
  'notion.db_changed': [['database_id', T('Notion 数据库 ID 或链接'), 'https://www.notion.so/…']],
};
function checkPicker(defKind = 'interval') {
  const kind = h('select', null, h('option', { value: 'interval' }, T('每隔 N 分钟 Interval')), h('option', { value: 'cron' }, T('按时间 Cron')),
    h('option', { value: 'event' }, T('有新事件时 Event')));
  kind.value = defKind;
  const spec = h('input', { type: 'text', value: '60' });
  const src = h('select', null, Object.keys(SRC_FIELDS).map(k => h('option', { value: k }, { 'gmail.new_email': T('📧 收到新邮件'), 'slack.new_message': T('💬 Slack 新消息'), 'notion.db_changed': T('📝 Notion 数据库变化') }[k])));
  const every = h('input', { type: 'number', min: '1', value: '3', style: 'width:80px' });
  const pbox = h('div', { class: 'stack' });
  const inputs = {};
  const renderParams = () => { pbox.innerHTML = ''; for (const k in inputs) delete inputs[k];
    SRC_FIELDS[src.value].forEach(([k, l, ph]) => { inputs[k] = h('input', { type: 'text', placeholder: ph }); pbox.append(h('label', { class: 'field' }, h('span', null, l), inputs[k])); }); };
  src.onchange = renderParams; renderParams();
  const specField = h('label', { class: 'field', style: 'flex:1' }, h('span', null, T('规则 Spec')), spec);
  const evBox = h('div', { class: 'stack' }, h('div', { class: 'row' }, h('label', { class: 'field', style: 'flex:2' }, h('span', null, T('事件来源 Source')), src),
    h('label', { class: 'field', style: 'flex:1' }, h('span', null, T('每几分钟检查')), every)), pbox);
  const sync = () => { const ev = kind.value === 'event'; evBox.style.display = ev ? '' : 'none'; specField.style.display = ev ? 'none' : '';
    if (kind.value === 'cron' && /^\d+$/.test(spec.value)) spec.value = '0 9 * * *'; if (kind.value === 'interval' && !/^\d+$/.test(spec.value)) spec.value = '60'; };
  kind.onchange = sync;
  const el = h('div', { class: 'stack' }, h('div', { class: 'row' }, h('label', { class: 'field', style: 'flex:1' }, h('span', null, T('检查方式 How often')), kind), specField), evBox);
  const onlyEvent = () => { kind.value = 'event'; kind.closest('label').style.display = 'none'; sync(); };
  sync();
  return { el, onlyEvent, value: () => kind.value === 'event'
    ? { kind: 'event', spec: { source: src.value, every: Number(every.value) || 3, params: Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value.trim()]).filter(([, v]) => v)) } }
    : { kind: kind.value, spec: spec.value } };
}

// Why a schedule did not produce a result: its last run is still waiting on you, or runs were skipped / superseded.
const LAST_RUN = { WAITING_APPROVAL: T('⏳ 上次运行在等你审批'), WAITING_EXTERNAL: T('⏳ 上次运行在等你接管浏览器'), PAUSED: T('⏸ 上次运行已暂停'),
  CREATED: T('▶ 正在运行'), PLANNING: T('▶ 正在运行'), RUNNING: T('▶ 正在运行'), COMPLETED: T('✅ 上次运行完成'), FAILED: T('❌ 上次运行失败'),
  CANCELLED: T('⛔ 上次运行已取消') };
function schedNotice(s) {
  const b = s.blocked || {}, out = [];
  if (s.last_status && LAST_RUN[s.last_status]) {
    const waitingYou = s.last_status === 'WAITING_APPROVAL';
    out.push(h('div', { class: 'row', style: 'gap:8px' }, h('span', { class: 'pill st-' + s.last_status }, LAST_RUN[s.last_status]),
      waitingYou ? h('button', { class: 'btn small', onclick: () => { $('#drawer').hidden = false; renderDrawer(); } }, T('去审批 Review')) : null));
  }
  if (b.skipped) out.push(h('div', { class: 'small', style: 'color:var(--warn)' },
    Tf("⚠️ {0} 到点时上一次运行还没结束，这次被跳过了（共 {1} 次）", (fmtTime(b.skipped.ts)), (b.skipped.count || 1))));
  if (b.superseded) out.push(h('div', { class: 'small muted' },
    Tf("ℹ️ {0} 上一次运行一直没完成，已自动取消并重新运行", (fmtTime(b.superseded.ts)))));
  return out.length ? h('div', { class: 'stack', style: 'gap:4px' }, out) : null;
}

async function viewSchedules(root) {
  const [r, gr] = await Promise.all([api('schedules'), api('goals')]);
  const scheds = r.schedules.filter(s => s.kind !== 'event'), triggers = r.schedules.filter(s => s.kind === 'event');
  // ---- goals
  const gTitle = h('input', { type: 'text', placeholder: T('例如：拿到 John 对合同的确认') });
  const gObj = h('textarea', { rows: 3, placeholder: T('要达成什么？背景是什么？Locius 每次检查时会读这段话。例如：John 还没确认合同条款。每天检查他有没有回信；3 天没回就起草一封礼貌的跟进邮件（发送需要我批准）。') });
  const gCrit = h('input', { type: 'text', placeholder: T('怎样算完成？例如：John 回信确认同意') });
  const gDl = h('input', { type: 'date' });
  const gCheck = checkPicker('interval');
  const goalForm = h('details', { open: !gr.goals.length }, h('summary', null, h('b', null, T('＋ 新目标 New goal'))),
    h('div', { class: 'stack', style: 'margin-top:10px' },
      h('label', { class: 'field' }, h('span', null, T('标题 Title')), gTitle),
      h('label', { class: 'field' }, h('span', null, T('目标描述 Objective')), gObj),
      h('label', { class: 'field' }, h('span', null, T('完成标准 Success criteria')), gCrit),
      gCheck.el,
      h('label', { class: 'field' }, h('span', null, T('截止日期 Deadline（可选）')), gDl),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: safe(async () => {
        await api('goals', { method: 'POST', body: { title: gTitle.value, objective: gObj.value, criteria: gCrit.value, deadline: gDl.value, run_now: true, ...gCheck.value() } });
        toast(T('已创建目标，正在进行第一次检查')); route();
      }) }, T('创建并开始 Create & start')))));
  const goalCard = g => {
    const [stl, stc] = GOAL_ST[g.status] || [g.status, ''];
    const last = (g.progress || []).slice(-1)[0];
    const blocked = last && last.status === 'blocked' && g.status === 'active';
    return h('div', { class: 'card stack goal-card' },
      h('div', { class: 'row' }, h('b', { style: 'flex:1' }, '🎯 ' + g.title), blocked ? h('span', { class: 'pill st-WAITING_EXTERNAL' }, T('🙋 需要你帮忙')) : null, h('span', { class: 'pill ' + stc }, stl)),
      h('div', { class: 'small' }, g.objective),
      g.criteria ? h('div', { class: 'small muted' }, T('✅ 完成标准：') + g.criteria) : null,
      h('div', { class: 'small muted' }, `🔁 ${schedDesc(g)}` + (g.deadline ? Tf(" · ⏳ 截止 {0}", (fmtTime(g.deadline))) : '') + (g.status === 'active' && g.next_run ? Tf(" · 下次检查 {0}", (fmtTime(g.next_run))) : '')),
      g.state_error ? h('div', { class: 'small', style: 'color:var(--danger)' }, '⚠️ ' + B(g.state_error)) : null,
      (g.progress || []).length ? h('details', { open: g.status === 'active' }, h('summary', { class: 'small' }, Tf("进展记录 Progress（{0}）", (g.progress.length))),
        h('ol', { class: 'goal-log' }, g.progress.slice().reverse().slice(0, 12).map(p => h('li', null,
          h('span', { class: 'muted' }, `${p.at} `), h('span', { class: 'pill ' + ((GOAL_ST[p.status] || [])[1] || '') }, (GOAL_ST[p.status] || [p.status])[0].split(' ')[0]), ' ', p.note)))) : h('div', { class: 'small muted' }, T('还没有进展记录')),
      h('div', { class: 'row' },
        g.status === 'active' ? h('button', { class: 'btn small', onclick: safe(async () => { await api(`goals/${g.id}/run`, { method: 'POST', body: {} }); toast(T('已开始检查')); }) }, T('▶ 立即检查 Check now')) : null,
        g.status === 'active' ? h('button', { class: 'btn small', onclick: safe(async () => { await api('goals/' + g.id, { method: 'PUT', body: { status: 'paused' } }); route(); }) }, T('⏸ 暂停')) : null,
        g.status === 'paused' ? h('button', { class: 'btn small', onclick: safe(async () => { await api('goals/' + g.id, { method: 'PUT', body: { status: 'active' } }); route(); }) }, T('▶ 继续')) : null,
        h('button', { class: 'btn small', onclick: () => { location.hash = 'chat'; setTimeout(() => openConv(g.conv_id), 50); } }, T('查看运行记录')),
        h('button', { class: 'btn danger small', onclick: safe(async () => { if (!confirmInline(Tf("删除目标「{0}」？", (g.title)))) return; await api('goals/' + g.id, { method: 'DELETE' }); route(); }) }, T('删除'))));
  };
  const goals = h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h3', { style: 'flex:1' }, T('🎯 场景目标 Goals')), h('span', { class: 'chip' }, Tf("{0} 个进行中", (gr.goals.filter(g => g.status === 'active').length)))),
    h('p', { class: 'sub' }, T('交给 Locius 一个需要几天才能完成的目标，它会按你设定的频率（或有新事件时）检查进展、推进下一步，直到达成或到截止时间。发送、提交等操作照常需要你审批；达成、失败或需要你帮忙时会通知你（包括 Telegram）。也可以直接在对话里说「帮我盯着……直到……」。')),
    goalForm, gr.goals.length ? gr.goals.map(goalCard) : null);
  // ---- triggers
  const tName = h('input', { type: 'text', placeholder: T('例如：老板来信提醒') });
  const tGoal = h('textarea', { rows: 3, placeholder: T('有新事件时要做什么（完整指令）。例如：总结这封邮件的要点和需要我做的事，用 notify_user 发给我；如果是会议邀请，查一下我那天的安排。') });
  const tCheck = checkPicker('event'); tCheck.onlyEvent();
  const trigForm = h('details', { open: !triggers.length }, h('summary', null, h('b', null, T('＋ 新触发器 New trigger'))),
    h('div', { class: 'stack', style: 'margin-top:10px' },
      h('label', { class: 'field' }, h('span', null, T('名称 Name')), tName), tCheck.el,
      h('label', { class: 'field' }, h('span', null, T('要做什么 Then do')), tGoal),
      h('p', { class: 'small muted' }, T('创建后第一次检查只记录现状，之后出现的新邮件/消息/修改才会触发。Locius 自己发的消息、自己改的页面不会触发，避免循环。')),
      h('div', null, h('button', { class: 'btn primary', onclick: safe(async () => {
        const v = tCheck.value();
        const s = await api('schedules', { method: 'POST', body: { name: tName.value, goal: tGoal.value, kind: 'event', spec: v.spec } });
        await api(`schedules/${s.id}/poll`, { method: 'POST', body: {} }).catch(() => {});
        toast(T('已创建触发器 Created')); route();
      }) }, T('创建 Create')))));
  const schedRow = (s, isTrig) => h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('b', { style: 'flex:1' }, (isTrig ? '⚡ ' : '⏰ ') + s.name),
      h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: !!s.enabled, onchange: safe(async e => { await api('schedules/' + s.id, { method: 'PUT', body: { enabled: e.target.checked } }); }) }), T('启用'))),
    h('div', { class: 'small' }, s.goal),
    h('div', { class: 'small muted' }, isTrig ? `${schedDesc(s)}` + (s.trigger._checked ? Tf(" · 上次检查 {0}", (s.trigger._checked)) : '') + (s.last_run ? Tf(" · 上次触发 {0}", (fmtTime(s.last_run))) : '')
      : Tf("{0} · {1} · 下次 next: {2} · 上次 last: {3}", (schedDesc(s)), (s.tz), (fmtTime(s.next_run)), (fmtTime(s.last_run) || '—'))),
    isTrig && s.trigger._error ? h('div', { class: 'small', style: 'color:var(--danger)' }, '⚠️ ' + B(s.trigger._error)) : null,
    schedNotice(s),
    Object.keys(s.state || {}).length ? h('pre', { class: 'small', style: 'white-space:pre-wrap;margin:0' }, JSON.stringify(s.state, null, 1)) : null,
    h('div', { class: 'row' },
      isTrig ? h('button', { class: 'btn small', onclick: safe(async () => { const p = await api(`schedules/${s.id}/poll`, { method: 'POST', body: {} });
        toast(p.error ? '⚠️ ' + p.error : p.fired ? T('发现新事件，已开始运行') : T('没有新事件 Nothing new'), !!p.error); route(); }) }, T('🔍 立即检查 Check now'))
        : h('button', { class: 'btn small', onclick: safe(async () => { await api(`schedules/${s.id}/run`, { method: 'POST', body: {} }); toast(T('已开始运行')); }) }, T('▶ 立即运行 Run now')),
      h('button', { class: 'btn small', onclick: () => { location.hash = 'chat'; setTimeout(() => openConv(s.conv_id), 50); } }, T('查看结果 Results')),
      h('button', { class: 'btn danger small', onclick: safe(async () => { await api('schedules/' + s.id, { method: 'DELETE' }); route(); }) }, T('删除 Delete'))));
  const trig = h('div', { class: 'card stack' }, h('h3', null, T('⚡ 事件触发 Triggers')),
    h('p', { class: 'sub' }, T('「当……发生时，自动做……」。例如：收到老板的邮件就总结给我；Slack #support 有人提到 urgent 就整理问题；Notion 任务表有新任务就排进计划。')),
    trigForm, triggers.map(s => schedRow(s, true)));
  // ---- schedules
  const name = h('input', { type: 'text', placeholder: T('例如：每日邮件简报') });
  const goal = h('textarea', { rows: 3, placeholder: T('每次运行时 Agent 要做什么（完整指令）。例如：总结过去 24 小时的重要邮件，如有需要我回复的，用 notify_user 通知我。') });
  const kind = h('select', null, h('option', { value: 'cron' }, T('Cron 表达式')), h('option', { value: 'interval' }, T('间隔（分钟）Interval')));
  const spec = h('input', { type: 'text', value: '0 8 * * *' });
  const presets = [[T('每天 8:00'), 'cron', '0 8 * * *'], [T('工作日 9:00'), 'cron', '0 9 * * 1-5'], [T('每周五 17:00'), 'cron', '0 17 * * 5'], [T('每小时'), 'interval', '60']];
  const sch = h('div', { class: 'card stack' }, h('h3', null, T('⏰ 定时任务 Schedules')),
    h('details', { open: !scheds.length }, h('summary', null, h('b', null, T('＋ 新定时任务 New schedule'))),
      h('div', { class: 'stack', style: 'margin-top:10px' },
        h('p', { class: 'sub' }, T('也可以直接在对话里说「每天早上 8 点……」，Agent 会自动创建。')),
        h('label', { class: 'field' }, h('span', null, T('名称 Name')), name),
        h('label', { class: 'field' }, h('span', null, T('任务 Goal')), goal),
        h('div', { class: 'row' }, h('label', { class: 'field', style: 'flex:1' }, h('span', null, T('类型 Kind')), kind), h('label', { class: 'field', style: 'flex:1' }, h('span', null, T('规则 Spec')), spec)),
        h('div', { class: 'row' }, presets.map(([l, k, v]) => h('button', { class: 'btn small', onclick: () => { kind.value = k; spec.value = v; } }, l))),
        h('div', null, h('button', { class: 'btn primary', onclick: safe(async () => {
          await api('schedules', { method: 'POST', body: { name: name.value, goal: goal.value, kind: kind.value, spec: spec.value } });
          toast(T('已创建 Created')); route();
        }) }, T('创建 Create'))))),
    scheds.map(s => schedRow(s, false)));
  root.append(goals, h('div', { style: 'height:16px' }), h('div', { class: 'grid2' }, trig, sch));
}

// ================================================================== CONNECTIONS
async function viewConnections(root) {
  const [r, g] = await Promise.all([sapi('connections'), sapi('grants')]);
  const byName = Object.fromEntries(r.connections.map(c => [c.name, c]));
  const gm = byName.gmail, br = byName.browser, tg = byName.telegram;
  const permRow = (conn, key, zh, en, risk) => h('div', { class: 'perm' },
    h('div', null, h('div', null, zh + ' ', LANG === 'en' ? null : h('span', { class: 'muted small' }, en)), h('div', { class: 'small muted' }, riskPill(risk))),
    h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: !!conn.permissions[key], onchange: safe(async e => {
      await sapi('connections/' + conn.name, { method: 'PUT', body: { permissions: { [key]: e.target.checked } } }); toast(T('已保存 Saved'));
    }) })));

  // --- Gmail (multiple mailboxes)
  const accs = gm.accounts || [];
  const email = h('input', { type: 'email', value: '', placeholder: 'you@gmail.com', autocomplete: 'off' });
  const pw = h('input', { type: 'password', placeholder: 'xxxx xxxx xxxx xxxx', autocomplete: 'new-password' });
  const dname = h('input', { type: 'text', value: '', placeholder: T('发件人显示名 (可选) e.g. Alex Chen') });
  const gmStatus = h('span', { class: 'chip ' + (accs.length ? 'ok' : '') }, accs.length ? Tf("已连接 {0} 个邮箱", (accs.length)) : T('未连接 Not connected'));
  const accRows = accs.map(a => h('div', { class: 'perm' },
    h('div', { style: 'min-width:0' }, h('b', null, a.email), a.id === gm.default ? h('span', { class: 'chip ok', style: 'margin-left:6px' }, T('默认 Default')) : null,
      a.display_name ? h('div', { class: 'small muted' }, a.display_name) : null,
      a.ready ? null : h('div', { class: 'small', style: 'color:var(--danger)' }, T('缺少密码，请重新添加 (password missing)'))),
    h('div', { class: 'row', style: 'flex-wrap:nowrap' },
      h('button', { class: 'btn small', onclick: safe(async () => { const t = await sapi('connections/gmail/test', { method: 'POST', body: { account: a.id } }); toast(t.ok ? Tf("{0} 正常 ✓ 未读 {1}", (a.email), (t.test.unread_inbox)) : t.error, !t.ok); }) }, T('测试')),
      a.id !== gm.default ? h('button', { class: 'btn small', onclick: safe(async () => { await sapi('connections/gmail/default', { method: 'POST', body: { account: a.id } }); toast(T('已设为默认发件邮箱')); route(); }) }, T('设为默认')) : null,
      h('button', { class: 'btn danger small', onclick: safe(async () => { if (!confirmInline(Tf("断开 {0}？", (a.email)))) return; await sapi('connections/gmail/accounts/' + a.id, { method: 'DELETE' }); route(); }) }, T('断开')))));
  const gmail = h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h3', { style: 'flex:1' }, '📧 Gmail'), gmStatus),
    h('p', { class: 'sub' }, T('可以连接多个 Gmail 邮箱：搜索时一起查，发信时默认用「默认邮箱」，回复总是用原邮件所在的邮箱。每个邮箱用各自的应用专用密码 (App Password)，加密保存在 Sentinel 保险箱 (Vault) 里，Agent 和模型永远看不到。')),
    accs.length ? h('div', null, ...accRows) : null,
    h('details', { open: !accs.length },
      h('summary', null, h('b', null, accs.length ? T('＋ 添加另一个邮箱 Add another mailbox') : T('连接邮箱 Connect a mailbox'))),
      h('div', { class: 'stack', style: 'margin-top:10px' },
        h('ol', { class: 'steps-help' },
          h('li', null, T('确认该 Google 账号已开启两步验证 (2-Step Verification)。')),
          h('li', null, T('打开 '), h('a', { href: 'https://myaccount.google.com/apppasswords', target: '_blank', rel: 'noopener' }, 'myaccount.google.com/apppasswords'), T('，新建一个应用专用密码。')),
          h('li', null, T('把 16 位密码粘贴到下方，点击「连接并测试」。同一邮箱再添加一次 = 更新密码。'))),
        h('label', { class: 'field' }, h('span', null, T('邮箱 Email')), email),
        h('label', { class: 'field' }, h('span', null, T('应用专用密码 App Password')), pw),
        h('label', { class: 'field' }, h('span', null, T('显示名 Display name')), dname),
        h('div', { class: 'row' },
          h('button', { class: 'btn primary', onclick: safe(async e => {
            e.target.disabled = true; e.target.textContent = T('连接中…');
            try {
              const res = await sapi('connections/gmail/credential', { method: 'POST', body: { email: email.value, app_password: pw.value, display_name: dname.value } });
              toast(Tf("{0} 已连接 ✓ 收件箱未读 {1} 封", (email.value), (res.test.unread_inbox ?? '?'))); route();
            } finally { e.target.disabled = false; e.target.textContent = T('连接并测试 Connect & test'); }
          }) }, T('连接并测试 Connect & test'))))),
    h('div', null, h('b', null, T('权限 Permissions（读写分离）')),
      permRow(gm, 'read', T('读取与搜索'), 'read / search', 'low'),
      permRow(gm, 'organize', T('整理（归档、标签、已读）'), 'organize', 'medium'),
      permRow(gm, 'draft', T('创建草稿'), 'draft', 'medium'),
      permRow(gm, 'send', T('发送 / 回复 / 转发（每次需审批）'), 'send — approval', 'high')));

  // --- Browser
  const blocked = h('textarea', { rows: 2, placeholder: T('每行一个域名，如 example.com') }); blocked.value = (br.config.blocked_domains || []).join('\n');
  const allowed = h('textarea', { rows: 2, placeholder: T('信任的域名：即使任务读过机密数据也可直接访问') }); allowed.value = (br.config.allowed_domains || []).join('\n');
  const browser = h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h3', { style: 'flex:1' }, T('🌐 浏览器 Browser')), h('span', { class: 'chip ok' }, 'Chromium')),
    h('p', { class: 'sub' }, T('独立的 Chromium，登录状态保存在本机。Agent 只能用受限 API，不能执行 JS；无法访问内网/集群地址。')),
    h('div', null, permRow(br, 'browse', T('浏览与读取'), 'browse', 'low'), permRow(br, 'interact', T('点击与输入（提交/购买/删除类需审批）'), 'interact', 'medium'),
      permRow(br, 'upload', T('上传文件（需审批）'), 'upload', 'high'), permRow(br, 'download', T('下载（隔离扫描）'), 'download', 'low')),
    h('label', { class: 'field' }, h('span', null, T('禁止访问的域名 Blocked domains')), blocked),
    h('label', { class: 'field' }, h('span', null, T('信任的域名 Trusted domains')), allowed),
    h('div', null, h('button', { class: 'btn small', onclick: safe(async () => { await sapi('connections/browser', { method: 'PUT', body: { config: { blocked_domains: blocked.value, allowed_domains: allowed.value } } }); toast(T('已保存 Saved')); }) }, T('保存 Save'))));

  // --- Telegram (two-way control)
  const tok = h('input', { type: 'password', placeholder: tg.has_credential ? T('已保存 saved（不改可留空）') : '123456:AA…', autocomplete: 'new-password' });
  const chat = h('input', { type: 'text', value: tg.config.chat_id || '', placeholder: 'chat id' });
  const tgState = h('span', { class: 'small muted' });
  const detect = safe(async () => {
    const r = await sapi('connections/telegram/detect', { method: 'POST', body: { bot_token: tok.value } });
    if (!r.chats.length) { toast(T('没找到：请先在 Telegram 里给你的机器人发一条 /start，再点检测 (send /start to your bot first)'), true); return; }
    chat.value = r.chats[r.chats.length - 1].chat_id; toast(Tf("找到 {0}：{1}", (r.chats[r.chats.length - 1].name), (chat.value)));
  });
  const telegram = h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h3', { style: 'flex:1' }, T('📱 Telegram 遥控 Remote control')), h('span', { class: 'chip ' + (tg.has_credential && tg.enabled ? 'ok' : '') }, tg.has_credential && tg.enabled ? T('已连接') : T('未配置'))),
    h('p', { class: 'sub' }, T('在 Telegram 里直接给 Locius 发消息布置任务、看进度和结果；需要审批时点 ✅ 批准 / ❌ 拒绝；需要登录时发来浏览器接管链接。只响应下面这个 chat id（你本人），其他人发消息一律忽略。')),
    h('ol', { class: 'steps-help' },
      h('li', null, T('在 Telegram 找 @BotFather，发 /newbot 创建机器人，复制它给的 Bot Token')),
      h('li', null, T('给你的新机器人发一条 /start')),
      h('li', null, T('在下面填 Token，点「检测 Chat ID」，再点「保存并连接」')),
      h('li', null, T('建议在 Telegram 设置里开启「两步验证 Two-Step Verification」'))),
    h('label', { class: 'field' }, h('span', null, 'Bot Token'), tok),
    h('label', { class: 'field' }, h('span', null, T('Chat ID（你本人）')), h('div', { class: 'row', style: 'flex-wrap:nowrap' }, chat, h('button', { class: 'btn small', onclick: detect }, T('检测 Chat ID')))),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: safe(async () => { await sapi('connections/telegram/credential', { method: 'POST', body: { bot_token: tok.value, chat_id: chat.value } }); toast(T('Telegram 已连接，已发送测试消息')); route(); }) }, T('保存并连接 Save & connect')),
      tg.has_credential ? h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: tg.enabled, onchange: safe(async e => { await sapi('connections/telegram', { method: 'PUT', body: { enabled: e.target.checked } }); }) }), T('启用')) : null,
      tgState));
  if (tg.has_credential) sapi('telegram/status').then(st => {
    tgState.textContent = st.running ? Tf("🟢 在线 @{0}", (st.username || '')) : st.last_error ? `🔴 ${B(st.last_error)}` : T('⏳ 连接中…');
  }).catch(() => {});

  // --- Grants
  const grants = h('div', { class: 'card stack' }, h('h3', null, T('🛡 已授权规则 Standing approvals')),
    h('p', { class: 'sub' }, T('你在审批时选择「本任务 / 8 小时 / 24 小时 / 总是允许」后生成的规则。疑似提示注入的任务会忽略这些规则。')),
    g.grants.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, [T('操作 Tool'), T('范围 Scope'), T('目标 Destination'), T('到期 Expires'), ''].map(x => h('th', null, x)))),
      h('tbody', null, g.grants.map(x => h('tr', null, h('td', { class: 'mono' }, x.tool), h('td', null, x.scope), h('td', null, (x.match || {}).destination || T('任意 any')),
        h('td', null, x.expires_at ? fmtTime(x.expires_at) : (x.scope === 'TASK' ? T('任务结束') : T('永久'))),
        h('td', null, h('button', { class: 'btn danger small', onclick: safe(async () => { await sapi('grants/' + x.id, { method: 'DELETE' }); route(); }) }, T('撤销 Revoke'))))))))
      : h('div', { class: 'muted small' }, T('暂无。所有高风险操作都会逐次询问你。')));
  // --- Notion
  const nt = byName.notion, sl = byName.slack;
  const tokenCard = (conn, o) => {
    const inp = h('input', { type: 'password', placeholder: conn.has_credential ? T('已保存 saved — 粘贴新令牌可替换') : o.ph, autocomplete: 'new-password' });
    const connected = conn.has_credential && conn.enabled;
    return h('div', { class: 'card stack' },
      h('div', { class: 'row' }, h('h3', { style: 'flex:1' }, o.title), h('span', { class: 'chip ' + (connected ? 'ok' : '') }, connected ? Tf("已连接 {0}", (o.who(conn.config))) : T('未连接 Not connected'))),
      h('p', { class: 'sub' }, o.desc),
      h('details', { open: !conn.has_credential }, h('summary', null, h('b', null, conn.has_credential ? T('更换令牌 Change token') : T('连接 Connect'))),
        h('div', { class: 'stack', style: 'margin-top:10px' }, h('ol', { class: 'steps-help' }, o.steps.map(x => h('li', null, x))),
          h('label', { class: 'field' }, h('span', null, o.label), inp),
          h('div', null, h('button', { class: 'btn primary', onclick: safe(async e => {
            e.target.disabled = true;
            try { const r = await sapi(`connections/${conn.name}/credential`, { method: 'POST', body: { token: inp.value } }); toast(o.ok(r)); route(); }
            finally { e.target.disabled = false; }
          }) }, T('连接并测试 Connect & test'))))),
      h('div', null, h('b', null, T('权限 Permissions')), o.perms.map(([k, zh, en, risk]) => permRow(conn, k, zh, en, risk))),
      conn.has_credential ? h('div', { class: 'row' },
        h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: conn.enabled, onchange: safe(async e => { await sapi('connections/' + conn.name, { method: 'PUT', body: { enabled: e.target.checked } }); }) }), T('启用')),
        h('button', { class: 'btn danger small', onclick: safe(async () => { if (!confirmInline(Tf("断开 {0}？令牌会被删除。", (o.title)))) return; await sapi(`connections/${conn.name}/credential`, { method: 'DELETE' }); route(); }) }, T('断开 Disconnect'))) : null);
  };
  const notion = tokenCard(nt, {
    title: '📝 Notion', ph: 'ntn_…', label: T('集成令牌 Internal integration secret'), who: c => c.workspace || '',
    desc: T('读写你的 Notion 页面和数据库：查资料、把报告写进 Notion、更新任务表。Locius 只能看到你主动共享给它的页面。'),
    steps: [h('span', null, T('打开 '), h('a', { href: 'https://www.notion.so/my-integrations', target: '_blank', rel: 'noopener' }, 'notion.so/my-integrations'), T('，新建一个「内部集成 Internal integration」，名字填 Locius')),
      T('复制它的「密钥 Internal Integration Secret」（ntn_ 开头），粘贴到下面'),
      T('在要给 Locius 用的页面或数据库右上角 ••• → 连接 Connections → 选择 Locius（子页面会自动继承）')],
    ok: r => Tf("Notion 已连接 ✓ {0}，能看到 {1} 个页面", (r.workspace), (r.visible.length)) + (r.visible.length ? '' : T('（记得把页面共享给集成）')),
    perms: [['read', T('读取与搜索'), 'read', 'low'], ['write', T('新建 / 追加 / 修改页面（归档需审批）'), 'write', 'medium']] });
  const slack = tokenCard(sl, {
    title: '💬 Slack', ph: T('xoxb-… 或 xoxp-…'), label: T('Slack 令牌 Token'), who: c => `${c.team || ''}${c.token_type === 'user' ? T(' · 用户令牌') : T(' · 机器人')}`,
    desc: T('读取频道和讨论串、在你批准后发消息；配合「自动化」可以做到「#support 有人提到 urgent 就整理给我」。'),
    steps: [h('span', null, T('打开 '), h('a', { href: 'https://api.slack.com/apps', target: '_blank', rel: 'noopener' }, 'api.slack.com/apps'), ' → Create New App → From scratch'),
      T('OAuth & Permissions 里添加 Bot Token Scopes：channels:history, channels:read, groups:history, groups:read, im:history, im:read, users:read, chat:write（想用搜索：再加 User Token Scope search:read，并使用 xoxp- 用户令牌）'),
      T('Install to Workspace，复制 Bot User OAuth Token（xoxb- 开头）粘贴到下面'),
      T('在要让 Locius 读取的频道里输入 /invite @你的应用名')],
    ok: r => Tf("Slack 已连接 ✓ {0}，已加入 {1} 个频道", (r.team), (r.member_of.length)),
    perms: [['read', T('读取频道 / 讨论串 / 搜索'), 'read', 'low'], ['send', T('发送消息（每次需审批）'), 'send — approval', 'high']] });
  const mcp = await mcpCard();
  root.append(h('div', { class: 'grid2' }, gmail, h('div', { class: 'stack' }, browser, telegram)), h('div', { style: 'height:16px' }),
    h('div', { class: 'grid2' }, notion, slack), h('div', { style: 'height:16px' }), mcp,
    h('div', { style: 'height:16px' }), grants);
}

// --- MCP connectors (Model Context Protocol)
const MCP_KIND = { read: [T('只读 read'), 'risk-low'], write: [T('写入 write'), 'risk-medium'], destructive: [T('可能删除/覆盖 destructive'), 'risk-high'] };
const MCP_DC = [['CONFIDENTIAL', T('机密 Confidential（私人文档、工作数据）')], ['PERSONAL', T('个人 Personal')], ['PUBLIC', T('公开 Public（天气、公开网页等）')]];
async function mcpCard() {
  const r = await sapi('mcp/servers');
  const list = r.servers || [];
  const pending = list.reduce((n, s) => n + s.tools.filter(t => t.status !== 'ok').length, 0);
  const authSel = () => h('select', null, h('option', { value: 'none' }, T('无 None')), h('option', { value: 'bearer' }, T('Bearer 令牌 Token')),
    h('option', { value: 'header' }, T('自定义请求头 Custom header')));
  // add form
  const name = h('input', { type: 'text', placeholder: T('例如 GitHub、公司知识库') });
  const url = h('input', { type: 'url', placeholder: 'https://…/mcp', autocomplete: 'off' });
  const auth = authSel();
  const hname = h('input', { type: 'text', placeholder: 'X-API-Key' });
  const token = h('input', { type: 'password', placeholder: T('令牌 token（加密保存，不会显示）'), autocomplete: 'new-password' });
  const hnameField = h('label', { class: 'field', style: 'display:none' }, h('span', null, T('请求头名称 Header name')), hname);
  const tokenField = h('label', { class: 'field', style: 'display:none' }, h('span', null, T('令牌 Token')), token);
  auth.onchange = () => { hnameField.style.display = auth.value === 'header' ? '' : 'none'; tokenField.style.display = auth.value === 'none' ? 'none' : ''; };
  const dc = h('select', null, MCP_DC.map(([v, l]) => h('option', { value: v }, l)));
  const addBtn = h('button', { class: 'btn primary', onclick: safe(async e => {
    e.target.disabled = true; e.target.textContent = T('连接中…');
    try {
      const s = await sapi('mcp/servers', { method: 'POST', body: { name: name.value, url: url.value, auth_type: auth.value, token: token.value, header_name: hname.value, data_class: dc.value } });
      const off = s.tools.filter(t => t.flags && t.flags.length).length;
      toast(Tf("「{0}」已连接 ✓ 读到 {1} 个工具", (s.name), (s.tools.length)) + (off ? Tf("，其中 {0} 个疑似有注入内容，已关闭", (off)) : ''));
      route();
    } finally { e.target.disabled = false; e.target.textContent = T('连接并读取工具 Connect'); }
  }) }, T('连接并读取工具 Connect'));
  const addForm = h('details', { open: !list.length },
    h('summary', null, h('b', null, list.length ? T('＋ 添加 MCP 服务器 Add server') : T('添加第一个 MCP 服务器 Add your first server'))),
    h('div', { class: 'stack', style: 'margin-top:10px' },
      h('ol', { class: 'steps-help' },
        h('li', null, T('准备一个支持 HTTP 的 MCP 服务器地址（Streamable HTTP 或旧版 SSE 都可以）。公网地址必须是 https://；局域网 / Olares 上的服务可以用 http://。')),
        h('li', null, T('如果服务需要令牌（API Key / Personal Access Token），在「认证」里选择方式并粘贴。令牌加密存进 Sentinel 保险箱 (Vault)，模型看不到。')),
        h('li', null, T('连接后会列出它提供的工具：只读工具默认「自动」，会改数据的工具默认「每次审批」，你可以逐个调整。')),
        h('li', null, T('例：GitHub 官方 MCP https://api.githubcopilot.com/mcp/ ，认证选 Bearer，填 GitHub 个人访问令牌 (Personal Access Token)。'))),
      h('div', { class: 'row' }, h('label', { class: 'field', style: 'flex:1;min-width:160px' }, h('span', null, T('名称 Name')), name),
        h('label', { class: 'field', style: 'flex:2;min-width:220px' }, h('span', null, T('服务器地址 Server URL')), url)),
      h('div', { class: 'row' }, h('label', { class: 'field', style: 'flex:1;min-width:160px' }, h('span', null, T('认证 Auth')), auth),
        h('label', { class: 'field', style: 'flex:2;min-width:220px' }, h('span', null, T('数据敏感度 Data class')), dc)),
      hnameField, tokenField, h('div', null, addBtn)));

  const serverBox = s => {
    const setMode = (t, mode, accept) => safe(async () => {
      await sapi(`mcp/servers/${s.id}/tools/${encodeURIComponent(t.name)}`, { method: 'PUT', body: accept ? { accept: true } : { mode } });
      toast(accept ? Tf("已接受「{0}」的新定义", (t.name)) : T('已保存 Saved')); route();
    });
    const toolRow = t => {
      const [kl, kc] = MCP_KIND[t.kind] || [t.kind, ''];
      const sel = h('select', { class: 'small', onchange: e => setMode(t, e.target.value)() },
        [['auto', T('自动 Auto')], ['ask', T('每次审批 Ask')], ['off', T('关闭 Off')]].map(([v, l]) => h('option', { value: v, selected: t.mode === v }, l)));
      const badges = [];
      if (t.status === 'new') badges.push(h('span', { class: 'pill risk-medium' }, T('新工具 New — 选择模式后启用')));
      if (t.status === 'changed') badges.push(h('span', { class: 'pill risk-high' }, T('定义已变化 Changed — 检查后接受')));
      if (t.flags && t.flags.length) badges.push(h('span', { class: 'pill risk-high', title: t.flags.join(', ') }, T('⚠️ 描述疑似含注入指令 injection')));
      return h('div', { class: 'mcp-tool' },
        h('div', { style: 'min-width:0;flex:1' },
          h('div', null, h('b', { class: 'mono' }, t.name), t.title ? h('span', { class: 'muted small' }, ' · ' + t.title) : null, ' ',
            h('span', { class: 'pill ' + kc, title: t.guessed ? T('服务器没有标注，按名称推测 (guessed from the name)') : '' }, kl + (t.guessed ? T('（推测）') : '')), ' ', badges),
          t.description ? h('div', { class: 'small muted mcp-desc' }, t.description) : null,
          t.status === 'changed' && t.previous_description !== undefined ? h('div', { class: 'small', style: 'margin-top:4px' },
            h('div', { class: 'muted' }, T('原来 Before：'), t.previous_description || T('（空）')), h('div', null, T('现在 Now：'), t.description || T('（空）'))) : null),
        h('div', { class: 'row', style: 'flex-wrap:nowrap' }, sel,
          t.status === 'changed' ? h('button', { class: 'btn small', onclick: setMode(t, null, true) }, T('接受 Accept')) : null));
    };
    const tok = h('input', { type: 'password', placeholder: s.has_credential ? T('已保存 saved — 填新令牌可替换') : T('令牌 token'), autocomplete: 'new-password' });
    const a2 = authSel(); a2.value = s.auth || 'none';
    const hn = h('input', { type: 'text', value: s.header_name || '', placeholder: 'X-API-Key' });
    const dcs = h('select', { onchange: safe(async e => { await sapi('mcp/servers/' + s.id, { method: 'PUT', body: { data_class: e.target.value } }); toast(T('已保存 Saved')); }) },
      MCP_DC.map(([v, l]) => h('option', { value: v, selected: s.data_class === v }, l)));
    const active = s.tools.filter(t => t.status === 'ok' && t.mode !== 'off').length;
    return h('details', { class: 'mcp-server', open: s.tools.some(t => t.status !== 'ok') },
      h('summary', null, h('span', { class: 'row', style: 'display:inline-flex;gap:8px;align-items:center' },
        h('b', null, '🧩 ' + s.name), h('span', { class: 'chip ' + (s.enabled && !s.last_error ? 'ok' : s.last_error ? 'bad' : '') },
          !s.enabled ? T('已停用 Disabled') : s.last_error ? T('连接异常') : Tf("{0}/{1} 个工具启用", (active), (s.tools.length))),
        h('span', { class: 'chip' }, s.transport === 'sse' ? T('SSE (旧版)') : 'Streamable HTTP'))),
      h('div', { class: 'stack', style: 'margin-top:8px' },
        h('div', { class: 'small muted mono', style: 'word-break:break-all' }, s.url, s.server_info && s.server_info.name ? `  ·  ${s.server_info.name} ${s.server_info.version || ''}` : ''),
        s.last_error ? h('div', { class: 'small', style: 'color:var(--danger)' }, '⚠️ ' + B(s.last_error)) : null,
        h('div', { class: 'row' },
          h('label', { class: 'toggle' }, h('input', { type: 'checkbox', checked: s.enabled, onchange: safe(async e => { await sapi('mcp/servers/' + s.id, { method: 'PUT', body: { enabled: e.target.checked } }); route(); }) }), T('启用')),
          h('button', { class: 'btn small', onclick: safe(async () => { const t = await sapi(`mcp/servers/${s.id}/test`, { method: 'POST' }); toast(Tf("连接正常 ✓ {0}", (t.transport))); route(); }) }, T('测试 Test')),
          h('button', { class: 'btn small', onclick: safe(async () => {
            const x = await sapi(`mcp/servers/${s.id}/refresh`, { method: 'POST' });
            const d = x.diff || {};
            toast(Tf("已刷新：新增 {0}，变化 {1}，移除 {2}", (d.added.length), (d.changed.length), (d.removed.length)) + (d.added.length + d.changed.length ? T(' — 请检查标黄/标红的工具') : ''));
            route();
          }) }, T('刷新工具 Refresh')),
          h('button', { class: 'btn danger small', onclick: safe(async () => { if (!confirmInline(Tf("删除 MCP 服务器「{0}」？令牌也会一起删除。", (s.name)))) return; await sapi('mcp/servers/' + s.id, { method: 'DELETE' }); route(); }) }, T('删除 Remove'))),
        h('label', { class: 'field' }, h('span', null, T('数据敏感度 Data class（决定外泄检查的严格程度）')), dcs),
        h('details', null, h('summary', { class: 'small' }, T('更换令牌 / 认证方式 Change token')),
          h('div', { class: 'row', style: 'margin-top:8px' }, a2, hn, tok,
            h('button', { class: 'btn small', onclick: safe(async () => { await sapi('mcp/servers/' + s.id, { method: 'PUT', body: { auth_type: a2.value, token: tok.value, header_name: hn.value } }); toast(T('已更新认证 Updated')); route(); }) }, T('保存')))),
        h('div', null, h('b', null, Tf("工具 Tools（{0}）", (s.tools.length))), h('div', { class: 'small muted' },
          T('自动 = 直接执行；每次审批 = 每次弹出审批（手机 Telegram 也能点 ✅）；关闭 = Agent 看不到。工具定义被服务器悄悄修改时会自动停用，等你确认。')),
          s.tools.length ? s.tools.map(toolRow) : h('div', { class: 'muted small' }, T('这个服务器没有提供工具。')))));
  };

  return h('div', { class: 'card stack' },
    h('div', { class: 'row' }, h('h3', { style: 'flex:1' }, T('🧩 MCP 连接器 MCP connectors')),
      pending ? h('span', { class: 'chip bad' }, Tf("{0} 个工具待检查", (pending))) : null,
      h('span', { class: 'chip ' + (list.length ? 'ok' : '') }, list.length ? Tf("{0} 个服务器", (list.length)) : T('未添加'))),
    h('p', { class: 'sub' }, T('MCP（Model Context Protocol，模型上下文协议）是让 AI 连接外部工具的通用标准。添加一个 MCP 服务器后，它提供的工具（查文档、建任务、查代码…）就能被 Locius 使用。所有调用都经过 Sentinel：按你的设置自动执行或弹出审批，并写入活动审计。返回的内容一律当作不可信数据处理。')),
    list.map(serverBox), addForm);
}

// ================================================================== MEMORY
async function viewMemory(root) {
  const r = await api('memory');
  const inp = h('input', { type: 'text', placeholder: T('例如：我偏好直飞航班；John Smith 是 Acme 的 CFO') });
  root.append(h('div', { class: 'grid2' },
    h('div', { class: 'card stack' }, h('h3', null, T('我知道的关于你的事 What I know about you')),
      h('p', { class: 'sub' }, T('长期事实 (Semantic memory)。Agent 只从你自己说的话里提取，不会从邮件或网页里学习。你可以随时删除。')),
      h('div', { class: 'row' }, inp, h('button', { class: 'btn primary', onclick: safe(async () => { await api('memory', { method: 'POST', body: { fact: inp.value } }); route(); }) }, T('记住 Remember'))),
      r.facts.length ? h('div', { class: 'tablewrap' }, h('table', { class: 'data memtable' },
        h('thead', null, h('tr', null, [T('事实 Fact'), T('类别'), T('来源 Source'), T('可信度'), ''].map(x => h('th', null, x)))),
        h('tbody', null, r.facts.map(f => h('tr', null, h('td', null, f.fact), h('td', null, f.category), h('td', { class: 'small muted' }, f.source), h('td', null, Math.round((f.confidence || 0) * 100) + '%'),
          h('td', null, h('button', { class: 'btn danger small', onclick: safe(async () => { await api('memory/' + f.id, { method: 'DELETE' }); route(); }) }, T('忘记 Forget'))))))))
        : h('div', { class: 'muted small' }, T('还没有记忆。'))),
    h('div', { class: 'card stack' }, h('h3', null, T('经历 Episodes')), h('p', { class: 'sub' }, T('已完成任务的摘要 (Episodic memory)，用于回溯。')),
      r.episodes.length ? r.episodes.map(e => h('div', { class: 'small', style: 'border-bottom:1px solid var(--line-2);padding-bottom:8px' },
        h('div', { class: 'muted' }, fmtTime(e.ts)), e.summary)) : h('div', { class: 'muted small' }, T('暂无')))));
}

// ================================================================== ACTIVITY / AUDIT
async function viewActivity(root) {
  const q = S.auditTask ? `audit?task_id=${encodeURIComponent(S.auditTask)}&limit=300` : 'audit?limit=300';
  const [r, v] = await Promise.all([sapi(q), sapi('audit/verify')]);
  const taskInp = h('input', { type: 'text', value: S.auditTask, placeholder: T('按任务 ID 过滤 filter by task id'), style: 'max-width:280px' });
  root.append(h('div', { class: 'stack' },
    h('div', { class: 'row' },
      h('span', { class: 'chip ' + (v.ok ? 'ok' : 'bad') }, v.ok ? Tf("✓ 审计链完整 hash chain intact · {0} 条", (v.checked)) : Tf("✕ 审计链在 #{0} 处损坏", (v.broken_at))),
      taskInp, h('button', { class: 'btn small', onclick: () => { S.auditTask = taskInp.value.trim(); route(); } }, T('过滤 Filter')),
      S.auditTask ? h('button', { class: 'btn small', onclick: () => { S.auditTask = ''; route(); } }, T('清除 Clear')) : null),
    h('p', { class: 'small muted', style: 'margin:0' }, T('只追加 (append-only)：每条记录都包含上一条的 SHA-256 哈希，任何篡改都会被检测到。包括模型调用、工具调用、权限决策、审批、浏览器操作。')),
    h('div', { class: 'tablewrap' }, h('table', { class: 'data' },
      h('thead', null, h('tr', null, ['#', T('时间 Time'), T('主体 Actor'), T('动作 Action'), T('资源 Resource'), T('风险 Risk'), T('决策 Decision'), T('结果 Result'), T('任务 Task')].map(x => h('th', null, x)))),
      h('tbody', null, r.events.map(e => {
        const tr = h('tr', { class: 'clickable', title: T('点击查看详情') },
          h('td', { class: 'mono' }, e.seq), h('td', { class: 'mono' }, fmtTime(e.ts)), h('td', null, e.actor), h('td', { class: 'mono' }, e.action),
          h('td', null, (e.resource || '').slice(0, 40)), h('td', null, riskPill(e.risk)), h('td', { class: 'dec-' + e.decision }, e.decision),
          h('td', null, e.result), h('td', { class: 'mono' }, e.task_id ? h('a', { href: '#tasks/' + e.task_id }, e.task_id.slice(0, 13)) : ''));
        tr.onclick = ev => { if (ev.target.tagName === 'A') return; const nx = tr.nextSibling; if (nx && nx.classList && nx.classList.contains('detail')) { nx.remove(); return; }
          tr.after(h('tr', { class: 'detail' }, h('td', { colspan: 9 }, h('pre', { style: 'white-space:pre-wrap;margin:0' }, JSON.stringify(e.detail, null, 2)), h('div', { class: 'small faint mono' }, 'hash ' + e.hash)))); };
        return tr;
      }))))));
}

// ================================================================== SETTINGS
async function viewSettings(root) {
  const r = await api('settings');
  const s = r.settings;
  const f = {};
  const field = (k, zh, en, type = 'text', help) => { const i = h('input', { type, value: s[k] ?? '' }); f[k] = i;
    return h('label', { class: 'field' }, h('span', null, `${zh} `, LANG === 'en' ? null : h('span', { class: 'muted' }, en)), i, help ? h('span', null, help) : null); };
  const tog = (k, zh) => { const i = h('input', { type: 'checkbox', checked: !!s[k] }); f[k] = i; return h('label', { class: 'toggle' }, i, zh); };
  const testOut = h('span', { class: 'small muted' });
  root.append(h('div', { class: 'grid2' },
    h('div', { class: 'card stack' }, h('h3', null, T('🧠 模型 Model（通过 Olares Router）')),
      h('p', { class: 'sub' }, T('默认全部使用本机 Qwen3.8-27B，数据不出 Olares One。任何 OpenAI 兼容接口都可替换。')),
      field('model_base_url', T('接口地址'), 'Base URL'), field('model_name', T('执行模型'), 'Model'),
      field('planner_model', T('规划模型（留空=同上）'), 'Planner model'),
      field('temperature', T('温度'), 'Temperature', 'number'), field('max_tokens', T('单次最大输出'), 'Max tokens', 'number'),
      field('llm_timeout', T('超时（秒）'), 'Timeout s', 'number'),
      tog('disable_thinking', T('关闭思考模式（更快，复杂任务效果可能下降）Disable thinking')),
      field('extra_body', T('额外请求参数 JSON'), 'Extra body'),
      h('div', { class: 'row' }, h('button', { class: 'btn small', onclick: safe(async () => { testOut.textContent = T('测试中…'); const t = await api('settings/test-model', { method: 'POST', body: {} });
        testOut.textContent = t.ok ? Tf("✓ {0}s：{1}", (t.latency_s), (t.reply)) : '✕ ' + t.error; }) }, T('测试模型 Test model')), testOut)),
    h('div', { class: 'card stack' }, h('h3', null, '🤖 Agent'),
      field('user_name', T('你的名字'), 'Your name'), field('timezone', T('时区（定时任务）'), 'Timezone'),
      field('max_steps', T('每个任务最多步数'), 'Max steps', 'number'),
      tog('memory_extraction', T('任务结束后自动提取长期记忆 Auto memory extraction')),
      h('div', null, h('b', null, T('技能 Skills')), h('ul', { class: 'small' }, r.skills.map(k => h('li', null, h('span', { class: 'mono' }, k.name), ' — ', B(k.description)))))),
  ), h('div', { style: 'margin-top:16px' }, h('button', { class: 'btn primary', onclick: safe(async () => {
    const body = {};
    for (const [k, el] of Object.entries(f)) body[k] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;
    await api('settings', { method: 'PUT', body }); toast(T('已保存 Saved')); refreshModelChip();
  }) }, T('保存设置 Save settings'))));
}

// ================================================================== live updates
function onEvent(ev) {
  if (ev.kind === 'task_update') {
    const t = ev.task; S.tasks[t.id] = { ...(S.tasks[t.id] || {}), ...t };
    renderNav();
    const card = document.getElementById('tc-' + t.id);
    if (card && S.view === 'chat') { const fresh = taskCard(S.tasks[t.id]); const det = card.querySelector('details'); const wasOpen = det && det.open;
      card.parentElement.replaceWith(fresh); if (wasOpen) { const d2 = fresh.querySelector('details'); d2.open = true; } }
    if (S.view === 'tasks' && S.selTask === t.id) { const box = $('#taskDetail'); if (box) renderTaskDetail(box, t.id); }
    if (t.status === 'COMPLETED' && t.source === 'schedule') toast(Tf("⏰ 定时任务完成：{0}", (t.goal.slice(0, 40))));
  } else if (ev.kind === 'task_event') {
    const tl = document.getElementById('tl-' + ev.task_id);
    if (tl && tl.closest('details').open && ev.type !== 'thinking') { if (tl.querySelector('.muted.small')) tl.innerHTML = ''; tl.append(evLine(ev)); }
    if (ev.type === 'plan' && S.tasks[ev.task_id]) { S.tasks[ev.task_id].plan = ev.data; const card = document.getElementById('tc-' + ev.task_id);
      if (card && S.view === 'chat') card.parentElement.replaceWith(taskCard(S.tasks[ev.task_id])); }
  } else if (ev.kind === 'conv_update') {
    if (S.view === 'chat') { loadConvs().then(() => renderConvList()); if (ev.conv_id === S.conv) openConv(S.conv); }
  } else if (ev.kind === 'approval_requested') {
    loadApprovals();
  } else if (ev.kind === 'takeover_requested') {
    toast(T('🖐 Agent 请求你接管浏览器：') + (ev.reason || ''), false, () => { location.hash = 'browser'; });
  } else if (ev.kind === 'notification') {
    toast('🔔 ' + ev.notification.title + T('：') + ev.notification.body);
  } else if (ev.kind === 'memory_update' && S.view === 'memory') route();
  else if ((ev.kind === 'schedule_update' || ev.kind === 'goal_update') && S.view === 'schedules') route();
}

function connectStream() {
  const es = new EventSource('api/stream');
  es.onmessage = m => { try { onEvent(JSON.parse(m.data)); } catch (e) { console.error(e); } };
  es.onerror = () => { $('#sentinelChip').className = 'chip bad'; };
  es.onopen = () => { $('#sentinelChip').className = 'chip ok'; };
}

async function refreshModelChip() {
  try {
    const r = await api('settings');
    const name = String(r.settings.model_name || '').split('/').pop().replace(/-GGUF.*$/i, '');
    $('#modelChip').textContent = '🧠 ' + name;
    $('#modelChip').title = r.settings.model_base_url;
  } catch (e) { $('#modelChip').textContent = T('模型 ?'); }
}

// ------------------------------------------------------------------ boot
$('#approvalBell').onclick = () => { $('#drawer').hidden = !$('#drawer').hidden; renderDrawer(); };
$('#drawerClose').onclick = () => { $('#drawer').hidden = true; };
const setNav = open => { $('#nav').classList.toggle('open', open); $('#navBack').hidden = !open; };
$('#menuBtn').onclick = () => setNav(!$('#nav').classList.contains('open'));
$('#navBack').onclick = () => setNav(false);
$('#navlinks').addEventListener('click', e => { if (e.target.closest('a')) setNav(false); });
// keep the layout exactly as tall as the visible area (iOS/Android keyboards and toolbars)
if (window.visualViewport) {
  const vv = window.visualViewport;
  const fit = () => {
    document.documentElement.style.setProperty('--app-h', vv.height + 'px');
    if (vv.height < window.innerHeight) window.scrollTo(0, 0);
    const m = $('.msgs'); const ta = $('#chatInput');
    if (m && ta && document.activeElement === ta) m.scrollTop = m.scrollHeight;
  };
  vv.addEventListener('resize', fit); fit();
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeModal(); closeHistory(); $('#drawer').hidden = true; }
  if (e.altKey && (e.key === 'n' || e.key === 'N') && S.view === 'chat') { e.preventDefault(); newChat(); }
});
document.addEventListener('click', e => { const p = $('#histPop'); if (p && !p.contains(e.target) && !e.target.closest('.hist-btn')) closeHistory(); });
window.addEventListener('hashchange', () => { route(); });
i18nStatic();
(async () => {
  try { const hs = await sapi('health'); $('#navfoot').textContent = Tf("v{0} · 数据保存在本机 local-first", (hs.version)); } catch (e) {}
  // seed "seen" so old pending approvals don't all pop at once; open the newest one
  try { const r = await sapi('approvals?status=pending'); S.approvals = r.approvals; } catch (e) {}
  route();
  refreshModelChip();
  connectStream();
  loadApprovals();
  setInterval(loadApprovals, 4000);
})();
})();
