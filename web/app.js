const $ = (id) => document.getElementById(id);

// 脚本一旦报错，界面会静默变空——直接把错误摆到用户面前
window.addEventListener('error', (e) => {
  const el = document.getElementById('boot-error');
  if (!el) return;
  el.textContent = `脚本错误：${e.message}（${e.filename || ''}:${e.lineno || ''}）`;
  el.classList.remove('hidden');
});

const state = {
  config: null,
  templates: [],
  models: [],
  defaultModel: '',
  efforts: [],
  defaultEffort: '',
  roomId: null,
  es: null,
  busy: false,
  editing: null,
  working: new Map(),      // 成员名 -> 开始时间戳
  attachments: [],         // 待发送的附件 [{file, url}]
  files: [],               // 当前房间工作目录文件
  fileSet: new Set(),      // 上面的 path 集合，用来判断正文里的路径是否真实存在
  fileAbs: new Map(),      // path -> 本地绝对路径，悬停时显示
  memStats: {},            // 成员名 -> 本项目里的记忆条数
  uploading: false,
  previewing: null,        // 正在预览的相对路径
  previewAbs: '',          // 对应的本地绝对路径
  selecting: false,        // 多选删除模式
  selected: new Set(),     // 选中的消息 id
};

/* ---------------- 基础 ---------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    // 后端的错误说明比「400 Bad Request」有用得多
    let msg = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j && j.error) msg = j.error;
    } catch (e) { /* 不是 JSON 就用状态码 */ }
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), 2200);
}

function initials(name) {
  return (name || '?').slice(0, 1).toUpperCase();
}

function memberOf(name) {
  return (state.config?.members || []).find((m) => m.name === name);
}

function colorOf(name) {
  return memberOf(name)?.color || '#71717a';
}

function personaOf(name) {
  const m = memberOf(name);
  return m ? `${m.persona || m.name} · ${m.role}` : name;
}

/* ---------------- 文件与附件 ---------------- */

const IMG_RE = /\.(png|jpe?g|gif|webp|bmp|ico|svg|avif)$/i;
// 正文里认文件路径：不含空格，必须以已知后缀结尾
const FILE_TOKEN_RE = /[A-Za-z0-9_\u4e00-\u9fa5][\w\u4e00-\u9fa5.\-/\\]*\.(?:md|markdown|txt|log|csv|tsv|json|ya?ml|toml|ini|cfg|py|m?js|ts|jsx|tsx|css|html?|xml|sql|sh|bat|ps1|java|go|rs|cpp|rb|php|lua|png|jpe?g|gif|webp|bmp|svg|pdf|docx?|xlsx?|pptx?|zip|mp4|mov|mp3|wav)/gi;

function humanSize(n) {
  if (n == null || Number.isNaN(Number(n))) return '';
  let v = Number(n);
  const units = ['B', 'KB', 'MB', 'GB'];
  for (let i = 0; i < units.length; i++) {
    if (v < 1024 || i === units.length - 1) {
      return i === 0 ? `${Math.round(v)}B` : `${v.toFixed(1)}${units[i]}`;
    }
    v /= 1024;
  }
  return '';
}

function isImage(path, type) {
  if (type && String(type).startsWith('image/')) return true;
  return IMG_RE.test(path || '');
}

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] || '');
    r.onerror = () => reject(new Error('读取失败'));
    r.readAsDataURL(file);
  });
}

function rawUrl(path, download) {
  const base = `/api/rooms/${state.roomId}/raw?path=${encodeURIComponent(path)}`;
  return download ? `${base}&download=1` : base;
}

async function copyText(text) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    // 剪贴板权限被拒时退回老办法
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;opacity:0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e2) { /* 放弃 */ }
    ta.remove();
  }
  toast('路径已复制，粘到资源管理器地址栏就能打开');
}

// 交给系统默认程序打开，或在资源管理器里定位——不用再下载一遍
async function openLocal(path, mode = 'reveal') {
  if (!path) return;
  try {
    await api(`/api/rooms/${state.roomId}/reveal`, {
      method: 'POST',
      body: JSON.stringify({ path, mode }),
    });
  } catch (e) {
    toast((mode === 'open' ? '打开失败：' : '定位失败：') + e.message);
  }
}

/* ---------------- 极简 Markdown（先转义再套标签，避免注入） ---------------- */

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

function mdToHtml(src) {
  const lines = esc(src || '').split(/\r?\n/);
  const out = [];
  let para = [];
  let i = 0;

  const flush = () => {
    if (para.length) {
      out.push(`<p>${para.join('<br>')}</p>`);
      para = [];
    }
  };

  while (i < lines.length) {
    const ln = lines[i];

    // 代码块：内容不再做行内处理
    if (/^\s*```/.test(ln)) {
      flush();
      const buf = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre class="md-code"><code>${buf.join('\n')}</code></pre>`);
      continue;
    }

    // 分隔线
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(ln)) {
      flush(); out.push('<hr>'); i += 1; continue;
    }

    // 标题
    const h = ln.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flush();
      const lv = Math.min(h[1].length, 4);
      out.push(`<h${lv}>${mdInline(h[2])}</h${lv}>`);
      i += 1;
      continue;
    }

    // 引用
    if (/^\s*>\s?/.test(ln)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ''));
        i += 1;
      }
      out.push(`<blockquote>${mdInline(buf.join('<br>'))}</blockquote>`);
      continue;
    }

    // 无序列表
    if (/^\s*[-*+]\s+/.test(ln)) {
      flush();
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(`<li>${mdInline(lines[i].replace(/^\s*[-*+]\s+/, ''))}</li>`);
        i += 1;
      }
      out.push(`<ul>${items.join('')}</ul>`);
      continue;
    }

    // 有序列表
    if (/^\s*\d+[.)]\s+/.test(ln)) {
      flush();
      const items = [];
      while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
        items.push(`<li>${mdInline(lines[i].replace(/^\s*\d+[.)]\s+/, ''))}</li>`);
        i += 1;
      }
      out.push(`<ol>${items.join('')}</ol>`);
      continue;
    }

    if (!ln.trim()) { flush(); i += 1; continue; }

    // 连续非空行合成一段，段内保留换行
    para.push(mdInline(ln));
    i += 1;
  }
  flush();
  return out.join('\n');
}

/* ---------------- 工作目录文件 ---------------- */

async function refreshFiles() {
  if (!state.roomId) return;
  try {
    const { files } = await api(`/api/rooms/${state.roomId}/files`);
    state.files = files || [];
  } catch (e) {
    state.files = [];
  }
  state.fileSet = new Set(state.files.map((f) => f.path));
  state.fileAbs = new Map(state.files.map((f) => [f.path, f.abs || f.path]));
  renderFileList();
}

async function loadMemStats() {
  if (!state.roomId) return;
  try {
    state.memStats = await api(`/api/rooms/${state.roomId}/memory-stats`);
  } catch (e) {
    state.memStats = {};
  }
}

/* ---------------- 渲染 ---------------- */

function showCtxMenu(x, y, items) {
  const menu = $('ctx-menu');
  menu.innerHTML = '';
  items.forEach((it) => {
    const b = document.createElement('button');
    b.textContent = it.label;
    if (it.danger) b.className = 'danger';
    b.onclick = (ev) => {
      ev.stopPropagation();
      hideCtxMenu();
      it.action();
    };
    menu.appendChild(b);
  });
  menu.style.left = `${Math.min(x, window.innerWidth - 170)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 20 - items.length * 34)}px`;
  menu.classList.remove('hidden');
}

function hideCtxMenu() {
  $('ctx-menu').classList.add('hidden');
}

function roomItem(r) {
  const isDm = r.kind === 'direct';
  const el = document.createElement('div');
  el.className = 'room-item' + (r.id === state.roomId ? ' active' : '')
    + (r.archived ? ' archived' : '');

  const n = (r.members || []).length;
  let title = '';
  let sub = '';

  if (isDm) {
    const who = (r.members || [])[0] || '';
    const m = who ? memberOf(who) : null;
    title = m?.persona || who || r.name;
    sub = m ? `单聊 · ${m.role}` : '单聊';
    const av = document.createElement('span');
    av.className = 'mini-av';
    av.style.background = m?.color || 'var(--faint)';
    av.style.color = '#fff';
    av.textContent = initials(title);
    el.appendChild(av);
  } else {
    // 项目名是主标题，群聊名退到副标题——记忆按项目存，导航也该按项目看
    title = r.project || r.name;
    sub = r.project && r.project !== r.name
      ? `${r.name} · ${n} 人`
      : `${n} 个成员`;
    const av = document.createElement('span');
    av.className = 'mini-av';
    av.style.background = 'var(--primary-soft)';
    av.style.color = 'var(--primary)';
    av.textContent = (title || '?').slice(0, 1);
    el.appendChild(av);
  }

  const info = document.createElement('div');
  info.className = 'room-info';
  const nm = document.createElement('div');
  nm.className = 'room-name';
  nm.textContent = title;
  const sb = document.createElement('div');
  sb.className = 'room-sub';
  sb.textContent = sub;
  info.append(nm, sb);
  el.appendChild(info);

  el.title = `${title}\n${sub}`;
  el.onclick = () => selectRoom(r.id);
  el.oncontextmenu = (e) => {
    e.preventDefault();
    const items = [];
    if (r.archived) {
      items.push({ label: '恢复群聊', action: () => setArchived(r.id, false) });
    } else {
      items.push({ label: '归档（保留内容）', action: () => setArchived(r.id, true) });
    }
    items.push({ label: '删除群聊', danger: true, action: () => deleteRoom(r.id) });
    showCtxMenu(e.clientX, e.clientY, items);
  };
  return el;
}

function renderRooms() {
  const rooms = state.config.rooms || [];
  const active = rooms.filter((r) => !r.archived);
  const groups = active.filter((r) => r.kind !== 'direct');
  const dms = active.filter((r) => r.kind === 'direct');
  const arch = rooms.filter((r) => r.archived);

  const gbox = $('room-list');
  gbox.innerHTML = '';
  if (!groups.length) {
    gbox.innerHTML = '<div class="empty-hint">还没有群聊</div>';
  }
  groups.forEach((r) => gbox.appendChild(roomItem(r)));
  if (arch.length) {
    const h = document.createElement('div');
    h.className = 'section-title archived-head';
    h.textContent = `已归档 ${arch.length}`;
    gbox.appendChild(h);
    arch.forEach((r) => gbox.appendChild(roomItem(r)));
  }

  const dbox = $('dm-list');
  dbox.innerHTML = '';
  if (!dms.length) {
    dbox.innerHTML = '<div class="empty-hint">还没有单聊</div>';
  }
  dms.forEach((r) => dbox.appendChild(roomItem(r)));
}

async function setArchived(id, flag) {
  const res = await api(`/api/rooms/${id}/archive`, {
    method: 'POST',
    body: JSON.stringify({ archived: flag }),
  });
  state.config = res.config;
  renderRooms();
  toast(flag ? '已归档，内容都保留着' : '已恢复到列表');
  if (flag && state.roomId === id) {
    const first = state.config.rooms.find((r) => !r.archived);
    if (first) await selectRoom(first.id);
  }
}

function renderMembers() {
  const box = $('member-list');
  box.innerHTML = '';
  const room = state.config.rooms.find((r) => r.id === state.roomId);
  const names = room?.members || [];
  const leader = room?.leader || '';

  if (!names.length) {
    const d = document.createElement('div');
    d.className = 'tip';
    d.textContent = '这个群还没有成员。点上方「+ 添加」，从已有成员里挑，或者直接新建一个。';
    box.appendChild(d);
  }

  names.forEach((name) => {
    const m = memberOf(name);
    if (!m) return;
    const card = document.createElement('div');
    card.className = 'member-card';

    const av = document.createElement('div');
    av.className = 'avatar';
    av.style.background = m.color || '#71717a';
    av.textContent = initials(m.persona || m.name);

    const info = document.createElement('div');
    info.className = 'info';
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = m.persona || m.name;
    if (name === leader) {
      const badge = document.createElement('span');
      badge.className = 'leader-badge';
      badge.textContent = '负责人';
      nm.appendChild(badge);
    }
    const rl = document.createElement('div');
    rl.className = 'rl';
    const display = m.persona || m.name;
    rl.textContent = (m.role && m.role !== display)
      ? `${m.role} · @${m.name}`
      : `@${m.name}`;
    info.append(nm, rl);

    card.dataset.name = name;
    card.append(av, info);

    const mem = state.memStats[name] || 0;
    if (mem) {
      const cnt = document.createElement('span');
      cnt.className = 'mem-count';
      cnt.textContent = `${mem}`;
      cnt.title = `在这个项目里积累了 ${mem} 条记忆`;
      card.appendChild(cnt);
    }
    card.title = name === leader
      ? `负责人 · 点击插入 @${m.name}，右键更多`
      : `点击插入 @${m.name}，右键更多`;
    card.onclick = () => insertMention(m.name);
    card.oncontextmenu = (e) => {
      e.preventDefault();
      const items = [{ label: '编辑人格 / 记忆', action: () => openMember(m.name) }];
      if (name === leader) {
        items.push({ label: '取消负责人', action: () => setLeader('') });
      } else {
        items.push({ label: '设为本群负责人', action: () => setLeader(name) });
      }
      items.push({
        label: '从本群移除',
        danger: true,
        action: () => removeMemberDirect(name),
      });
      showCtxMenu(e.clientX, e.clientY, items);
    };
    box.appendChild(card);
  });

  $('room-meta').textContent = $('room-meta').textContent || `${names.length} 位成员`;
  renderMobileMembers();
}

async function setLeader(name) {
  const res = await api(`/api/rooms/${state.roomId}/leader`, {
    method: 'POST',
    body: JSON.stringify({ leader: name }),
  });
  state.config = res.config;
  renderMembers();
  toast(name ? `已设为负责人，它派的活会自动汇总回来验收` : '已取消负责人');
}

async function removeMemberDirect(name) {
  const res = await api(`/api/rooms/${state.roomId}/members/${name}`, { method: 'DELETE' });
  state.config = res.config;
  renderMembers();
  renderRooms();
  toast(`已从本群移除 ${name}（人格和记忆都还在）`);
}

function insertMention(name) {
  const input = $('input');
  const cur = input.value;
  const sep = cur && !cur.endsWith(' ') ? ' ' : '';
  input.value = `${cur}${sep}@${name} `;
  input.focus();
  input.selectionStart = input.selectionEnd = input.value.length;
  input.dispatchEvent(new Event('input'));
}

function renderMobileMembers() {
  const box = $('mobile-members');
  if (!box) return;
  box.innerHTML = '';
  const room = state.config.rooms.find((r) => r.id === state.roomId);
  (room?.members || []).forEach((name) => {
    const m = memberOf(name);
    if (!m) return;
    const av = document.createElement('div');
    av.className = 'avatar';
    av.style.background = m.color || '#71717a';
    av.textContent = initials(m.persona || m.name);
    av.title = `@${m.name} · ${m.role}`;
    av.onclick = () => insertMention(m.name);
    av.oncontextmenu = (e) => {
      e.preventDefault();
      openMember(m.name);
    };
    box.appendChild(av);
  });
}

function renderRoomSelect() {
  const sel = $('room-select');
  if (!sel) return;
  sel.innerHTML = '';
  (state.config.rooms || []).forEach((r) => {
    const o = document.createElement('option');
    o.value = r.id;
    o.textContent = r.name;
    if (r.id === state.roomId) o.selected = true;
    sel.appendChild(o);
  });
}

/* ---------------- 消息渲染 ---------------- */

function renderRich(el, content) {
  el.innerHTML = mdToHtml(content || '');
  linkifyFiles(el);
}

// 只把「工作目录里真实存在」的路径变成链接，避免满屏假链接
function linkifyFiles(root) {
  if (!state.fileSet.size) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
  const targets = [];
  let node = walker.nextNode();
  while (node) {
    const p = node.parentElement;
    // 代码块里通常是命令，不插链接；行内 code 里的文件名还是要能点
    if (p && !p.closest('a') && !p.closest('pre')) {
      FILE_TOKEN_RE.lastIndex = 0;
      if (FILE_TOKEN_RE.test(node.nodeValue)) targets.push(node);
    }
    node = walker.nextNode();
  }
  targets.forEach(linkifyNode);
}

function linkifyNode(node) {
  const s = node.nodeValue;
  const frag = document.createDocumentFragment();
  let last = 0;
  let m;
  FILE_TOKEN_RE.lastIndex = 0;
  while ((m = FILE_TOKEN_RE.exec(s))) {
    const clean = m[0].replace(/^\.\//, '').replace(/^\/+/, '').replace(/\\/g, '/');
    if (!state.fileSet.has(clean)) continue;
    if (m.index > last) frag.appendChild(document.createTextNode(s.slice(last, m.index)));
    const a = document.createElement('a');
    a.className = 'file-link';
    a.textContent = m[0];
    a.href = '#';
    const abs = state.fileAbs.get(clean);
    a.title = abs ? `${abs}\n点击预览` : `打开 ${clean}`;
    a.onclick = (e) => { e.preventDefault(); previewFile(clean); };
    frag.appendChild(a);
    last = m.index + m[0].length;
  }
  if (last === 0) return;                 // 一个都没匹配上就别动 DOM
  if (last < s.length) frag.appendChild(document.createTextNode(s.slice(last)));
  node.parentNode.replaceChild(frag, node);
}

function attachGrid(atts) {
  const box = document.createElement('div');
  box.className = 'attachments';
  atts.forEach((a) => {
    const path = a.path || a.name || '';
    const chip = document.createElement('a');
    chip.className = 'attach-item' + (isImage(path, a.type) ? ' image' : '');
    chip.href = '#';
    chip.title = [a.name || path, a.size ? humanSize(a.size) : '', a.abs || '']
      .filter(Boolean).join('\n');
    if (isImage(path, a.type)) {
      const img = document.createElement('img');
      img.src = rawUrl(path);
      img.alt = a.name || path;
      img.loading = 'lazy';
      chip.appendChild(img);
    } else {
      const ic = document.createElement('span');
      ic.className = 'attach-icon';
      const ext = (a.name || path).split('.').pop() || '';
      ic.textContent = ext.slice(0, 4).toUpperCase() || 'FILE';
      const nm = document.createElement('span');
      nm.className = 'attach-name';
      nm.textContent = a.name || path;
      const sz = document.createElement('span');
      sz.className = 'attach-size';
      sz.textContent = humanSize(a.size);
      chip.append(ic, nm, sz);
    }
    chip.onclick = (e) => { e.preventDefault(); previewFile(path); };
    box.appendChild(chip);
  });
  return box;
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return '';
  const hm = d.toLocaleTimeString('zh-CN',
    { hour: '2-digit', minute: '2-digit', hour12: false });
  if (d.toDateString() === new Date().toDateString()) return hm;
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hm}`;
}

function fmtFull(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false });
}

// 长消息先折叠，别让一轮输出占满整屏
function clipBar(el, content) {
  if (!content || content.length < 700) return null;
  el.classList.add('clipped');
  const bar = document.createElement('div');
  bar.className = 'expand-bar';
  const btn = document.createElement('button');
  btn.className = 'expand-btn';
  btn.textContent = `展开全文 · ${content.length} 字`;
  btn.onclick = () => { el.classList.remove('clipped'); bar.remove(); };
  bar.appendChild(btn);
  return bar;
}

function msgEl(sender, content, mine, meta, ts, msgId) {
  const wrap = document.createElement('div');
  wrap.className = 'msg' + (mine ? ' me' : '');
  if (msgId) wrap.dataset.id = msgId;

  if (!mine) {
    const av = document.createElement('div');
    av.className = 'avatar';
    av.style.background = colorOf(sender);
    av.textContent = initials(memberOf(sender)?.persona || sender);
    wrap.appendChild(av);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble';

  const who = document.createElement('div');
  who.className = 'who';
  if (mine) {
    const t = document.createElement('span');
    t.className = 'time';
    t.textContent = fmtTime(ts);
    t.title = fmtFull(ts);
    who.appendChild(t);
  } else {
    const m = memberOf(sender);
    const name = m?.persona || sender;
    const role = m?.role || '';
    const nm = document.createElement('span');
    nm.className = 'name';
    nm.textContent = name;
    who.appendChild(nm);
    // 昵称和角色一样时只显示一个，别出现「工程师 工程师」
    if (role && role !== name) {
      const rl = document.createElement('span');
      rl.className = 'role';
      rl.textContent = role;
      who.appendChild(rl);
    }
    const t = document.createElement('span');
    t.className = 'time';
    t.textContent = fmtTime(ts);
    t.title = fmtFull(ts);
    who.appendChild(t);
  }
  bubble.appendChild(who);

  const atts = (meta && meta.attachments) || [];
  // 只发附件时后端会补一句占位说明给成员看，这里不必再显示一遍
  const placeholder = !content || content.startsWith('（用户只上传了附件');
  if (content && !(atts.length && placeholder)) {
    const text = document.createElement('div');
    text.className = 'text';
    const inner = document.createElement('div');
    inner.className = 'md';
    renderRich(inner, content);
    text.appendChild(inner);
    bubble.appendChild(text);
    const bar = clipBar(text, content);
    if (bar) bubble.appendChild(bar);
  }
  if (atts.length) bubble.appendChild(attachGrid(atts));

  // 多选删除用的勾选点：平时不显示，悬停或多选模式下才出现
  if (msgId) {
    const check = document.createElement('button');
    check.type = 'button';
    check.className = 'msg-check';
    check.title = '选择这条';
    check.onclick = (e) => {
      e.stopPropagation();
      toggleSelect(msgId);
    };
    wrap.appendChild(check);

    wrap.addEventListener('click', (e) => {
      if (!state.selecting) return;
      if (e.target.closest('a, button')) return;   // 链接和按钮照常用
      toggleSelect(msgId);
    });
  }

  wrap.appendChild(bubble);
  return wrap;
}

/* ---------------- 多选删除 ---------------- */

function enterSelect() {
  state.selecting = true;
  $('messages').classList.add('selecting');
  $('select-bar').classList.remove('hidden');
  renderSelectBar();
}

function exitSelect() {
  state.selecting = false;
  state.selected.clear();
  $('messages').classList.remove('selecting');
  $('select-bar').classList.add('hidden');
  document.querySelectorAll('.msg.selected').forEach((el) => el.classList.remove('selected'));
  renderSelectBar();
}

function toggleSelect(id) {
  if (!id) return;
  if (!state.selecting) enterSelect();
  if (state.selected.has(id)) state.selected.delete(id);
  else state.selected.add(id);
  const el = document.querySelector(`.msg[data-id="${id}"]`);
  if (el) el.classList.toggle('selected', state.selected.has(id));
  renderSelectBar();
}

function renderSelectBar() {
  const n = state.selected.size;
  const count = $('select-count');
  if (count) count.textContent = n ? `已选 ${n} 条` : '点消息前面的圆圈选择';
  const del = $('btn-delete-msgs');
  if (del) del.disabled = !n;
}

function askConfirm(title, desc) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal';
    overlay.innerHTML = `
      <div class="modal-card" style="width:min(420px,100%)">
        <h2></h2>
        <p class="tip"></p>
        <div class="modal-actions">
          <span class="spacer"></span>
          <button class="btn-ghost" data-act="no">取消</button>
          <button class="btn-danger" data-act="yes">删除</button>
        </div>
      </div>`;
    overlay.querySelector('h2').textContent = title;
    overlay.querySelector('.tip').textContent = desc;
    document.body.appendChild(overlay);
    const done = (v) => { overlay.remove(); resolve(v); };
    overlay.querySelector('[data-act="yes"]').onclick = () => done(true);
    overlay.querySelector('[data-act="no"]').onclick = () => done(false);
    overlay.onclick = (e) => { if (e.target === overlay) done(false); };
  });
}

function selectAllMessages() {
  if (!state.selecting) enterSelect();
  const els = [...document.querySelectorAll('.msg[data-id]')];
  const all = els.map((el) => Number(el.dataset.id));
  const allSelected = all.length && all.every((id) => state.selected.has(id));
  state.selected = new Set(allSelected ? [] : all);
  els.forEach((el) => el.classList.toggle('selected', state.selected.has(Number(el.dataset.id))));
  renderSelectBar();
}

async function deleteSelected() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const ok = await askConfirm(
    `删除 ${ids.length} 条消息？`,
    '数据库和 Obsidian 里的对话记录都会删掉，成员记忆不受影响。'
      + '删除不可撤销，但会在项目的 .trash 目录里留一份纯文本备份。',
  );
  if (!ok) return;

  try {
    const res = await api(`/api/rooms/${state.roomId}/messages`, {
      method: 'DELETE',
      body: JSON.stringify({ ids }),
    });
    const sync = res.obsidian ? `，Obsidian 同步 ${res.obsidian} 条` : '';
    toast(`已删除 ${res.deleted} 条${sync}`);
    exitSelect();
    await loadMessages();
  } catch (e) {
    toast('删除失败：' + e.message);
  }
}

function addStatus(text, isError = false) {
  const el = document.createElement('div');
  el.className = 'status-line' + (isError ? ' error' : '');
  el.textContent = text;
  $('messages').appendChild(el);
  scrollBottom();
}

function scrollBottom() {
  const box = $('messages');
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 消息流 ---------------- */

async function loadMessages() {
  const box = $('messages');
  box.innerHTML = '';
  const { messages } = await api(`/api/rooms/${state.roomId}/messages`);
  if (!messages.length) {
    addStatus('房间已就绪。用 @名字 指定谁来做，比如「@researcher 看看工作目录里有什么」');
  }
  messages.forEach((m) => {
    box.appendChild(msgEl(m.sender, m.content, m.kind === 'say', m.meta, m.created_at, m.id));
  });
  scrollBottom();
}

function connectSSE() {
  if (state.es) state.es.close();
  const es = new EventSource(`/api/rooms/${state.roomId}/stream`);
  state.es = es;

  es.onopen = () => {
    $('conn-dot').className = 'dot on';
    $('conn-text').textContent = '已连接';
  };
  es.onerror = () => {
    $('conn-dot').className = 'dot off';
    $('conn-text').textContent = '重连中…';
  };
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleEvent(ev);
  };
}

function handleEvent(ev) {
  const box = $('messages');
  switch (ev.type) {
    case 'message':
      box.appendChild(msgEl(
        ev.sender, ev.content, ev.kind === 'say',
        ev.attachments && ev.attachments.length ? { attachments: ev.attachments } : null,
        ev.ts, ev.id,
      ));
      scrollBottom();
      break;
    case 'status':
      addStatus(ev.text);
      break;
    case 'working':
      updateWorking(ev.name, ev.on);
      break;
    case 'busy':
      state.busy = !!ev.on;
      // 不再禁用发送：中途想补一句是正常需求，交给后端排队
      $('input').placeholder = state.busy
        ? '正在处理… 现在发送会排队，这一轮跑完自动执行'
        : '在这里输入消息。打 @ 选人，也可以粘贴图片或拖文件进来（Enter 发送，Shift+Enter 换行）';
      if (ev.on) addStatus('处理中…');
      break;
    case 'error':
      addStatus(ev.text, true);
      break;
    case 'idle':
      state.busy = false;
      $('btn-send').disabled = false;
      state.working.clear();          // 兜底：万一 working 事件丢了
      renderWorking();
      refreshFiles();                 // 这一轮可能又生成了文件
      break;
  }
}

/* ---------------- 工作中状态 ---------------- */

let workingTimer = null;

function updateWorking(name, on) {
  if (!name) return;
  if (on) state.working.set(name, Date.now());
  else state.working.delete(name);
  renderWorking();
}

function renderWorking() {
  const bar = $('working-bar');
  const cards = document.querySelectorAll('.member-card');

  cards.forEach((c) => {
    c.classList.toggle('working', state.working.has(c.dataset.name));
  });

  if (!state.working.size) {
    bar.classList.add('hidden');
    if (workingTimer) {
      clearInterval(workingTimer);
      workingTimer = null;
    }
    return;
  }

  bar.classList.remove('hidden');

  const paint = () => {
    const names = [...state.working.keys()];
    const started = Math.min(...state.working.values());
    const secs = Math.max(0, Math.round((Date.now() - started) / 1000));

    bar.innerHTML = '';
    const sp = document.createElement('span');
    sp.className = 'spinner';
    const txt = document.createElement('span');
    txt.textContent = `${names.join('、')} 正在工作`;
    const el = document.createElement('span');
    el.className = 'elapsed';
    el.textContent = `${secs}s`;
    bar.append(sp, txt, el);
  };

  paint();
  if (!workingTimer) workingTimer = setInterval(paint, 1000);
}

/* ---------------- 房间 ---------------- */

async function selectRoom(id) {
  state.roomId = id;
  exitSelect();
  state.working.clear();        // 切房间时清掉上一个房间的工作状态
  state.attachments.forEach((a) => URL.revokeObjectURL(a.url));
  state.attachments = [];
  renderAttachStrip();
  renderWorking();
  const room = state.config.rooms.find((r) => r.id === id);
  // 标题用项目名，群聊名退到副标题
  $('room-title').textContent = room?.project || room?.name || id;
  const n = (room?.members || []).length;
  $('room-meta').textContent = (room?.project && room.project !== room.name)
    ? `${room.name} · ${n} 位成员`
    : `${n} 位成员`;
  renderRooms();
  renderRoomSelect();
  await refreshFiles();         // 先拿到文件表，正文里的路径才能点
  await loadMemStats();
  renderMembers();
  await loadMessages();
  // ?sse=0 跳过实时推送，截图工具才能等到页面静止（SSE 是长连接，会一直占着）
  if (new URLSearchParams(location.search).get('sse') !== '0') connectSSE();
}

function askText(title, placeholder = '') {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal';
    overlay.innerHTML = `
      <div class="modal-card" style="width:min(420px,100%)">
        <h2>${title}</h2>
        <div class="form"><input id="ask-input" placeholder="${placeholder}"></div>
        <div class="modal-actions">
          <span class="spacer"></span>
          <button class="btn-ghost" id="ask-cancel">取消</button>
          <button class="btn-primary" id="ask-ok">确定</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#ask-input');
    const done = (v) => { overlay.remove(); resolve(v); };
    input.focus();
    overlay.querySelector('#ask-ok').onclick = () => done(input.value.trim());
    overlay.querySelector('#ask-cancel').onclick = () => done('');
    overlay.onclick = (e) => { if (e.target === overlay) done(''); };
    input.onkeydown = (e) => {
      if (e.key === 'Enter') done(input.value.trim());
      if (e.key === 'Escape') done('');
    };
  });
}

async function newRoom(kind = 'group') {
  if (kind === 'direct') return newDirect();

  const name = await askText('新建群聊', '比如：投研小组');
  if (!name) return;
  const res = await api('/api/rooms', {
    method: 'POST',
    body: JSON.stringify({ name, kind: 'group' }),
  });
  state.config = res.config;
  renderRooms();
  await selectRoom(res.id);
  toast(`已创建「${name}」`);
}

async function newDirect() {
  const members = state.config.members || [];
  if (!members.length) {
    toast('还没有成员，先在右侧加一个');
    return;
  }
  const who = await pickMember('和谁单聊？');
  if (!who) return;

  const res = await api('/api/rooms', {
    method: 'POST',
    body: JSON.stringify({ kind: 'direct', member: who }),
  });
  state.config = res.config;
  renderRooms();
  await selectRoom(res.id);
  toast(res.existed ? '已经有和它的单聊，跳过去了' : `已和 ${who} 开始单聊`);
}

/* ---------------- 成员选择器 ---------------- */

let pickResolve = null;

function pickMember(title) {
  return new Promise((resolve) => {
    pickResolve = resolve;
    $('pick-title').textContent = title;
    const box = $('pick-list');
    box.innerHTML = '';

    (state.config.members || []).forEach((m) => {
      const row = document.createElement('div');
      row.className = 'tpl-item';

      const av = document.createElement('div');
      av.className = 'avatar';
      av.style.cssText = `width:26px;height:26px;font-size:11px;border-radius:8px;background:${m.color || '#71717a'}`;
      av.textContent = initials(m.persona || m.name);

      const info = document.createElement('div');
      info.className = 'info';
      const nm = document.createElement('div');
      nm.className = 'nm';
      nm.textContent = m.persona || m.name;
      const ds = document.createElement('div');
      ds.className = 'ds';
      ds.textContent = `${m.role} · @${m.name}`;
      info.append(nm, ds);

      row.append(av, info);
      row.onclick = () => closePick(m.name);
      box.appendChild(row);
    });

    $('modal-pick').classList.remove('hidden');
  });
}

function closePick(value) {
  $('modal-pick').classList.add('hidden');
  if (pickResolve) {
    pickResolve(value || '');
    pickResolve = null;
  }
}

async function deleteRoom(id) {
  const room = state.config.rooms.find((r) => r.id === id);
  const ans = await askText(`删除群聊「${room?.name || id}」？输入 delete 确认`, 'delete');
  if (ans !== 'delete') return;
  await api(`/api/rooms/${id}`, { method: 'DELETE' });
  state.config = await api('/api/config');
  if (state.roomId === id) {
    state.roomId = state.config.rooms[0]?.id || null;
    if (state.roomId) await selectRoom(state.roomId);
  }
  renderRooms();
  toast('群聊已删除');
}

/* ---------------- 成员 ---------------- */

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t) => {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  $('panel-persona').classList.toggle('hidden', name !== 'persona');
  $('panel-memory').classList.toggle('hidden', name !== 'memory');
}

async function openMember(name) {
  const m = name ? memberOf(name) : null;
  state.editing = name || null;
  $('modal-title').textContent = m ? `编辑 ${m.persona || m.name}` : '新建成员';
  $('f-name').value = m?.name || '';
  $('f-name').disabled = !!m;
  $('f-role').value = m?.role || '';
  $('f-persona').value = m?.persona || '';
  $('f-color').value = m?.color || '#4F46E5';
  $('f-prompt').value = m?.system_prompt || '';
  $('f-memory').value = '';

  const sel = $('f-model');
  sel.innerHTML = '';
  const cur = m?.model || state.defaultModel || '';
  const models = state.models || [];
  const options = [...models];
  if (cur && !options.includes(cur)) options.unshift(cur);
  options.forEach((x) => {
    const o = document.createElement('option');
    o.value = x;
    o.textContent = x;
    if (x === cur) o.selected = true;
    sel.appendChild(o);
  });
  if (!options.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '（默认）';
    sel.appendChild(o);
  }

  const effSel = $('f-effort');
  effSel.innerHTML = '';
  const efforts = state.efforts || ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultracode'];
  const curEff = m?.effort || state.defaultEffort || 'ultracode';
  const effOptions = [...efforts];
  if (curEff && !effOptions.includes(curEff)) effOptions.unshift(curEff);
  effOptions.forEach((x) => {
    const o = document.createElement('option');
    o.value = x;
    o.textContent = x === 'ultracode' ? 'ultracode（xhigh + workflows）' : x;
    if (x === curEff) o.selected = true;
    effSel.appendChild(o);
  });

  $('f-fallback').value = (m?.fallback_models || []).join(', ');
  $('btn-del').style.display = m ? '' : 'none';
  $('btn-remove-room').style.display = m ? '' : 'none';
  switchTab('persona');
  $('modal').classList.remove('hidden');

  if (m) {
    try {
      const r = await api(`/api/rooms/${state.roomId}/members/${name}/memory`);
      $('f-memory').value = r.content || '';
    } catch (e) {
      /* 没有记忆文件是正常的 */
    }
  }
}

async function saveMember() {
  const payload = {
    name: $('f-name').value.trim(),
    role: $('f-role').value.trim(),
    persona: $('f-persona').value.trim(),
    color: $('f-color').value,
    model: $('f-model').value,
    effort: $('f-effort').value,
    fallback_models: $('f-fallback').value
      .split(/[,，\s]+/)
      .map((s) => s.trim())
      .filter(Boolean),
    system_prompt: $('f-prompt').value,
  };
  if (!payload.name) return toast('名字不能为空');
  const res = await api('/api/members', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
  state.config = res.config;

  if (!state.editing) {
    // 新成员只加入当前群聊，不自动进别的群
    const r2 = await api(`/api/rooms/${state.roomId}/members`, {
      method: 'POST',
      body: JSON.stringify({ names: [payload.name] }),
    });
    state.config = r2.config;
  } else {
    // 保存这个成员在当前群聊里的记忆
    await api(`/api/rooms/${state.roomId}/members/${state.editing}/memory`, {
      method: 'POST',
      body: JSON.stringify({ content: $('f-memory').value }),
    });
  }

  $('modal').classList.add('hidden');
  renderMembers();
  renderRooms();
  toast('已保存');
}

async function removeFromRoom() {
  const name = state.editing;
  if (!name) return;
  const res = await api(`/api/rooms/${state.roomId}/members/${name}`, { method: 'DELETE' });
  state.config = res.config;
  $('modal').classList.add('hidden');
  renderMembers();
  renderRooms();
  toast(`已从本群移除 ${name}（人格和记忆都还在）`);
}

/* ---------------- 项目内容 ---------------- */

async function openProject() {
  const r = await api(`/api/rooms/${state.roomId}/project`);
  $('f-project').value = r.content || '';
  $('f-project-name').value = r.project || '';
  try {
    const v = await api('/api/vault');
    $('vault-tip').textContent =
      `这个文件同时是 Obsidian 仓库里的笔记：${v.path} —— 用 Obsidian「打开文件夹作为仓库」选这个目录，就能看到项目与成员记忆的双链图谱。`;
  } catch (e) {
    $('vault-tip').textContent = '';
  }
  $('modal-project').classList.remove('hidden');
}

async function saveProject() {
  const name = $('f-project-name').value.trim();
  const room = state.config.rooms.find((x) => x.id === state.roomId);
  const oldName = room?.project || '';

  // 先改名（会真的重命名目录），失败就别往下走，免得内容写进旧目录
  if (name && name !== oldName) {
    try {
      const res = await api(`/api/rooms/${state.roomId}/project-name`, {
        method: 'POST',
        body: JSON.stringify({ name }),
      });
      state.config = res.config;
      $('f-project-name').value = res.project;
      if (res.project !== name) toast(`目录重名，已改名为「${res.project}」`);
    } catch (e) {
      toast('改名失败：' + e.message);
      return;
    }
  }

  await api(`/api/rooms/${state.roomId}/project`, {
    method: 'POST',
    body: JSON.stringify({ content: $('f-project').value }),
  });
  $('modal-project').classList.add('hidden');
  toast('项目内容已保存');
}

/* ---------------- 添加成员到本群 ---------------- */

function switchAddTab(name) {
  document.querySelectorAll('[data-addtab]').forEach((t) => {
    t.classList.toggle('active', t.dataset.addtab === name);
  });
  $('add-pool').classList.toggle('hidden', name !== 'pool');
  $('add-tpl').classList.toggle('hidden', name !== 'tpl');
  $('add-tip').textContent = name === 'pool'
    ? '从已有成员里勾选加入。人格是全局的，加进来只是建立关联。'
    : '点一个模板，直接按它的设定创建成员并加入本群。名字重复会自动加序号。';
  $('btn-add-ok').style.display = name === 'pool' ? '' : 'none';
}

function openAddMember() {
  const room = state.config.rooms.find((r) => r.id === state.roomId);
  const inRoom = new Set(room?.members || []);

  const box = $('add-pool');
  box.innerHTML = '';
  const candidates = state.config.members.filter((m) => !inRoom.has(m.name));
  if (!candidates.length) {
    const d = document.createElement('div');
    d.className = 'tip';
    d.textContent = '已有成员都在这个群里了。切到「模板库」挑一个新的。';
    box.appendChild(d);
  }
  candidates.forEach((m) => {
    const row = document.createElement('label');
    row.className = 'add-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = m.name;
    const info = document.createElement('div');
    info.className = 'info';
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = m.persona || m.name;
    const rl = document.createElement('div');
    rl.className = 'rl';
    rl.textContent = `${m.role} · @${m.name}`;
    info.append(nm, rl);
    row.append(cb, info);
    box.appendChild(row);
  });

  const tplBox = $('add-tpl');
  tplBox.innerHTML = '';
  (state.templates || []).forEach((t) => {
    const row = document.createElement('div');
    row.className = 'tpl-item';
    const info = document.createElement('div');
    info.className = 'info';
    const nm = document.createElement('div');
    nm.className = 'nm';
    nm.textContent = t.name;
    const ds = document.createElement('div');
    ds.className = 'ds';
    ds.textContent = t.description;
    info.append(nm, ds);
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = t.group;
    row.append(info, tag);
    row.onclick = () => applyTemplate(t);
    tplBox.appendChild(row);
  });

  switchAddTab('pool');
  $('modal-add').classList.remove('hidden');
}

async function applyTemplate(tpl) {
  const existing = new Set(state.config.members.map((m) => m.name));
  let name = tpl.slug;
  let i = 2;
  while (existing.has(name)) name = `${tpl.slug}${i++}`;

  const res = await api('/api/members', {
    method: 'POST',
    body: JSON.stringify({
      name,
      role: tpl.role,
      persona: tpl.persona,
      color: tpl.color,
      system_prompt: tpl.system_prompt,
    }),
  });
  state.config = res.config;
  const r2 = await api(`/api/rooms/${state.roomId}/members`, {
    method: 'POST',
    body: JSON.stringify({ names: [name] }),
  });
  state.config = r2.config;
  $('modal-add').classList.add('hidden');
  renderMembers();
  renderRooms();
  toast(`已按模板创建「${tpl.name}」并加入本群`);
}

async function confirmAdd() {
  const names = [...document.querySelectorAll('#add-pool input:checked')].map((i) => i.value);
  if (!names.length) return toast('先勾选成员');
  const res = await api(`/api/rooms/${state.roomId}/members`, {
    method: 'POST',
    body: JSON.stringify({ names }),
  });
  state.config = res.config;
  $('modal-add').classList.add('hidden');
  renderMembers();
  renderRooms();
  toast(`已加入 ${names.length} 位成员`);
}

async function deleteMember() {
  const name = state.editing;
  if (!name) return;
  const ans = await askText(`彻底删除「${name}」？人格设定和它在所有群聊里的记忆都会清掉。输入 delete 确认`, 'delete');
  if (ans !== 'delete') return;
  const res = await api(`/api/members/${name}`, { method: 'DELETE' });
  state.config = res.config;
  $('modal').classList.add('hidden');
  renderMembers();
  renderRooms();
  toast('成员已删除');
}

/* ---------------- @ 自动补全 ---------------- */

const mention = { open: false, index: 0, items: [], start: 0 };

function roomMemberList() {
  const room = state.config.rooms.find((r) => r.id === state.roomId);
  return (room?.members || []).map((n) => memberOf(n)).filter(Boolean);
}

function detectMention() {
  const input = $('input');
  const pos = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, pos);
  const m = before.match(/@([\w\u4e00-\u9fa5-]*)$/);
  if (!m) return closeMention();

  const q = m[1].toLowerCase();
  const items = roomMemberList().filter((x) =>
    !q || x.name.toLowerCase().includes(q) || (x.persona || '').toLowerCase().includes(q));
  if (!items.length) return closeMention();

  mention.open = true;
  mention.index = 0;
  mention.items = items;
  mention.start = pos - m[0].length;
  renderMention();
}

function renderMention() {
  const pop = $('mention-pop');
  pop.innerHTML = '';
  mention.items.forEach((m, i) => {
    const el = document.createElement('div');
    el.className = 'mention-item' + (i === mention.index ? ' active' : '');
    const av = document.createElement('div');
    av.className = 'avatar';
    av.style.cssText = `width:22px;height:22px;font-size:10px;border-radius:6px;background:${m.color || '#71717a'}`;
    av.textContent = initials(m.persona || m.name);
    const nm = document.createElement('span');
    nm.className = 'nm';
    nm.textContent = m.persona || m.name;
    const rl = document.createElement('span');
    rl.className = 'rl';
    rl.textContent = `@${m.name}`;
    el.append(av, nm, rl);
    el.onmousedown = (e) => {
      e.preventDefault();
      pickMention(m);
    };
    pop.appendChild(el);
  });
  pop.classList.remove('hidden');
}

function pickMention(m) {
  const input = $('input');
  const pos = input.selectionStart ?? input.value.length;
  const before = input.value.slice(0, mention.start);
  const after = input.value.slice(pos);
  input.value = `${before}@${m.name} ${after}`;
  const np = mention.start + m.name.length + 2;
  input.focus();
  input.setSelectionRange(np, np);
  closeMention();
  input.dispatchEvent(new Event('input'));
}

function closeMention() {
  mention.open = false;
  $('mention-pop').classList.add('hidden');
}

/* ---------------- 剩余积分 ---------------- */

function renderCredits(c) {
  const el = $('credits-value');
  const has = c && c.value !== null && c.value !== undefined && c.value !== '';
  if (!has) {
    el.textContent = '点击设置';
    el.className = 'credits-value empty';
  } else {
    const n = Number(c.value);
    el.textContent = `${Number.isFinite(n) ? n.toLocaleString() : c.value} ${c.unit || ''}`.trim();
    el.className = 'credits-value';
  }
  $('credits').title = c?.updated
    ? `上次更新 ${c.updated}（点击修改）`
    : '点击设置剩余积分';
}

async function loadCredits() {
  try {
    renderCredits(await api('/api/credits'));
  } catch (e) {
    /* 忽略 */
  }
}

async function editCredits() {
  const cur = await api('/api/credits');
  const v = await askText('剩余积分（官网 → 头像 → 个人主页 → 用量）',
    cur.value != null ? String(cur.value) : '比如 1250');
  if (v === '') return;
  const num = Number(String(v).replace(/[^\d.]/g, ''));
  const c = await api('/api/credits', {
    method: 'POST',
    body: JSON.stringify({ value: Number.isFinite(num) && String(v).match(/\d/) ? num : v }),
  });
  renderCredits(c);
  toast('积分已更新');
}

/* ---------------- 待发送的附件 ---------------- */

const MAX_UPLOAD_MB = 25;

function addFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  let rejected = 0;
  files.forEach((f) => {
    if (f.size > MAX_UPLOAD_MB * 1024 * 1024) { rejected += 1; return; }
    state.attachments.push({ file: f, url: URL.createObjectURL(f) });
  });
  if (rejected) toast(`${rejected} 个文件超过 ${MAX_UPLOAD_MB}MB，已跳过`);
  renderAttachStrip();
}

function removeAttachment(i) {
  const [a] = state.attachments.splice(i, 1);
  if (a) URL.revokeObjectURL(a.url);
  renderAttachStrip();
}

function renderAttachStrip() {
  const strip = $('attach-strip');
  strip.innerHTML = '';
  if (!state.attachments.length) {
    strip.classList.add('hidden');
    return;
  }
  strip.classList.remove('hidden');
  state.attachments.forEach((a, i) => {
    const chip = document.createElement('div');
    chip.className = 'pending-item';
    if (isImage(a.file.name, a.file.type)) {
      const el = document.createElement('img');
      el.src = a.url;
      chip.appendChild(el);
    } else {
      const ic = document.createElement('span');
      ic.className = 'pending-icon';
      const ext = a.file.name.split('.').pop() || '';
      ic.textContent = ext.slice(0, 4).toUpperCase() || 'FILE';
      chip.appendChild(ic);
    }
    const nm = document.createElement('span');
    nm.className = 'pending-name';
    nm.textContent = a.file.name;
    nm.title = `${a.file.name} · ${humanSize(a.file.size)}`;
    chip.appendChild(nm);

    const x = document.createElement('button');
    x.className = 'pending-x';
    x.textContent = '×';
    x.title = '移除';
    x.onclick = (e) => { e.stopPropagation(); removeAttachment(i); };
    chip.appendChild(x);
    strip.appendChild(chip);
  });
}

async function uploadPending() {
  const payload = [];
  for (const a of state.attachments) {
    payload.push({ name: a.file.name, type: a.file.type, data: await fileToB64(a.file) });
  }
  const res = await api(`/api/rooms/${state.roomId}/upload`, {
    method: 'POST',
    body: JSON.stringify({ files: payload }),
  });
  if (res.failed && res.failed.length) {
    toast(`${res.failed.length} 个文件没传上去：${res.failed[0].why}`);
  }
  return res.files || [];
}

/* ---------------- 文件面板与预览 ---------------- */

function renderFileList() {
  paintFileList($('file-list'), false);        // 弹窗：完整信息
  paintFileList($('side-file-list'), true);    // 右栏：紧凑，路径放 title
}

function paintFileList(box, compact) {
  if (!box) return;
  box.innerHTML = '';
  if (!state.files.length) {
    box.innerHTML = '<div class="tip">工作目录还是空的。让成员写点东西，或者自己传一个上去。</div>';
    return;
  }
  state.files.forEach((f) => {
    const row = document.createElement('div');
    row.className = 'file-row';

    const ic = document.createElement('span');
    ic.className = 'file-row-icon' + (f.kind === 'image' ? ' img' : '');
    const ext = (f.name.split('.').pop() || '').toUpperCase();
    ic.textContent = ext.slice(0, 4) || 'FILE';

    const info = document.createElement('div');
    info.className = 'file-row-info';

    const nm = document.createElement('div');
    nm.className = 'file-row-name';
    nm.textContent = f.name;
    const size = document.createElement('span');
    size.className = 'file-row-size';
    size.textContent = humanSize(f.size);
    nm.appendChild(size);
    info.appendChild(nm);

    // 本地绝对路径：点一下复制，不用再走一遍下载
    if (!compact) {
      const meta = document.createElement('div');
      meta.className = 'file-row-meta';
      meta.textContent = f.abs || f.path;
      meta.title = `${f.abs || f.path}\n点击复制路径`;
      meta.onclick = (e) => {
        e.stopPropagation();
        copyText(f.abs || f.path);
      };
      info.appendChild(meta);
    }

    row.title = `${f.abs || f.path}\n点击预览`;
    row.append(ic, info);

    if (!compact) {
      const time = document.createElement('span');
      time.className = 'file-row-time';
      time.textContent = new Date((f.mtime || 0) * 1000)
        .toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
      row.appendChild(time);

      const acts = document.createElement('div');
      acts.className = 'file-row-acts';
      [['打开', 'open'], ['定位', 'reveal']].forEach(([label, mode]) => {
        const b = document.createElement('button');
        b.className = 'mini-btn';
        b.textContent = label;
        b.title = mode === 'open' ? '用系统默认程序打开' : '在资源管理器中显示';
        b.onclick = (e) => {
          e.stopPropagation();
          openLocal(f.path, mode);
        };
        acts.appendChild(b);
      });
      row.appendChild(acts);
    }

    row.onclick = () => previewFile(f.path);
    box.appendChild(row);
  });
}

async function openFiles() {
  await refreshFiles();
  renderFileList();
  $('modal-files').classList.remove('hidden');
}

async function previewFile(path) {
  if (!path || !state.roomId) return;
  state.previewing = path;
  state.previewAbs = '';
  const body = $('preview-body');
  $('preview-title').textContent = path.split('/').pop();
  $('preview-meta').textContent = '读取中…';
  $('preview-download').href = rawUrl(path, true);
  $('preview-download').setAttribute('download', path.split('/').pop());
  body.innerHTML = '';
  $('modal-preview').classList.remove('hidden');

  let d;
  try {
    d = await api(`/api/rooms/${state.roomId}/file?path=${encodeURIComponent(path)}`);
  } catch (e) {
    $('preview-meta').textContent = '';
    body.innerHTML = `<div class="preview-empty">打不开这个文件：${esc(e.message)}</div>`;
    return;
  }

  state.previewAbs = d.abs || path;
  $('preview-title').textContent = d.name || path;
  $('preview-meta').textContent = `${d.abs || d.path} · ${humanSize(d.size)}`;
  $('preview-meta').title = '点击复制本地路径';
  body.innerHTML = '';

  if (d.kind === 'image') {
    const img = document.createElement('img');
    img.className = 'preview-img';
    img.src = d.raw;
    img.alt = d.name || path;
    body.appendChild(img);
    return;
  }

  if (d.kind === 'text') {
    if (/\.(md|markdown)$/i.test(path)) {
      const div = document.createElement('div');
      div.className = 'md';
      div.innerHTML = mdToHtml(d.content || '');
      body.appendChild(div);
    } else if (/\.html?$/i.test(path)) {
      const frame = document.createElement('iframe');
      frame.className = 'preview-frame';
      frame.setAttribute('sandbox', 'allow-scripts allow-popups');
      frame.src = d.raw;
      body.appendChild(frame);
    } else {
      const pre = document.createElement('pre');
      pre.className = 'preview-pre';
      pre.textContent = d.content || '（空文件）';
      body.appendChild(pre);
    }
    if (d.truncated) {
      const tip = document.createElement('div');
      tip.className = 'preview-empty';
      tip.textContent = '文件较大，这里只显示前 40 万字符。要完整内容请点「下载」。';
      body.appendChild(tip);
    }
    return;
  }

  const box = document.createElement('div');
  box.className = 'preview-empty';
  const ext = (d.name || '').split('.').pop() || '未知';
  box.textContent = `这个类型（${ext}）不能直接预览。`;
  const a = document.createElement('a');
  a.href = rawUrl(path, true);
  a.className = 'btn-ghost';
  a.textContent = '下载文件';
  box.appendChild(document.createElement('br'));
  box.appendChild(a);
  body.appendChild(box);
}

/* ---------------- 发送 ---------------- */

async function send() {
  const input = $('input');
  const text = input.value.trim();
  const pending = state.attachments.slice();
  if (!text && !pending.length) return;
  if (state.uploading) return;

  // 先把附件传上去，拿到工作目录里的真实路径，再发消息
  let uploaded = [];
  if (pending.length) {
    const btn = $('btn-send');
    state.uploading = true;
    btn.disabled = true;
    btn.textContent = '上传中';
    try {
      uploaded = await uploadPending();
    } catch (e) {
      toast('上传失败：' + e.message);
      return;                       // 失败就什么都不清，用户可以直接重试
    } finally {
      state.uploading = false;
      btn.disabled = false;
      btn.textContent = '发送';
    }
  }

  input.value = '';
  input.style.height = 'auto';
  closeMention();
  pending.forEach((a) => URL.revokeObjectURL(a.url));
  state.attachments = [];
  renderAttachStrip();

  try {
    const res = await api(`/api/rooms/${state.roomId}/say`, {
      method: 'POST',
      body: JSON.stringify({ text, attachments: uploaded }),
    });
    if (res.queued > 0) {
      toast(`已排队（前面还有 ${res.queued} 条），轮到它时自动执行`);
    }
  } catch (e) {
    toast('发送失败：' + e.message);
  }
}

/* ---------------- 主题 ---------------- */

const THEMES = ['auto', 'light', 'dark'];
const THEME_LABEL = { auto: '跟随系统', light: '浅色', dark: '深色' };
const darkMedia = window.matchMedia('(prefers-color-scheme: dark)');

function themeMode() {
  const saved = localStorage.getItem('theme');
  return THEMES.includes(saved) ? saved : 'auto';
}

function applyTheme() {
  const mode = themeMode();
  const dark = mode === 'dark' || (mode === 'auto' && darkMedia.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const btn = $('btn-theme');
  if (btn) btn.textContent = THEME_LABEL[mode];
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(themeMode()) + 1) % THEMES.length];
  localStorage.setItem('theme', next);
  applyTheme();
  toast(`主题：${THEME_LABEL[next]}`);
}

/* ---------------- 右侧栏 ---------------- */

function switchRTab(name) {
  document.querySelectorAll('[data-rtab]').forEach((t) => {
    t.classList.toggle('active', t.dataset.rtab === name);
  });
  $('rtab-members').classList.toggle('hidden', name !== 'members');
  $('rtab-files').classList.toggle('hidden', name !== 'files');
  if (name === 'files') refreshFiles();
}

function setSideCollapsed(v) {
  $('app').classList.toggle('side-collapsed', v);
  $('btn-expand-side').classList.toggle('hidden', !v);
  localStorage.setItem('sideCollapsed', v ? '1' : '');
}

/* ---------------- 启动 ---------------- */

async function boot() {
  // 支持 ?theme=dark 直接指定，方便截图和分享
  const urlTheme = new URLSearchParams(location.search).get('theme');
  if (THEMES.includes(urlTheme)) localStorage.setItem('theme', urlTheme);
  applyTheme();
  if (localStorage.getItem('sideCollapsed') === '1') setSideCollapsed(true);
  state.config = await api('/api/config');
  try {
    state.templates = (await api('/api/templates')).templates || [];
  } catch (e) {
    state.templates = [];
  }
  try {
    const m = await api('/api/models');
    state.models = m.models || [];
    state.defaultModel = m.default || '';
    state.efforts = m.efforts || [];
    state.defaultEffort = m.default_effort || '';
  } catch (e) {
    state.models = [];
  }
  renderRooms();
  loadCredits();
  const first = state.config.rooms.find((r) => !r.archived) || state.config.rooms?.[0];
  if (first) await selectRoom(first.id);
  else $('messages').innerHTML = '<div class="status-line">还没有群聊，点左边「新建群聊」</div>';

  const urlTab = new URLSearchParams(location.search).get('rtab');
  if (urlTab === 'files' || urlTab === 'members') switchRTab(urlTab);

  // ?select=1 直接进多选模式并选中前两条，方便截图和演示
  if (new URLSearchParams(location.search).get('select') === '1') {
    enterSelect();
    [...document.querySelectorAll('.msg[data-id]')].slice(0, 2)
      .forEach((el) => toggleSelect(Number(el.dataset.id)));
  }
}

$('btn-send').onclick = send;
$('btn-delete-msgs').onclick = deleteSelected;
$('btn-cancel-select').onclick = exitSelect;
$('btn-select-all').onclick = selectAllMessages;
$('btn-attach').onclick = () => $('file-input').click();
$('file-input').onchange = (e) => {
  addFiles(e.target.files);
  e.target.value = '';            // 同一个文件连选两次也要能触发 change
};
$('btn-files').onclick = openFiles;
$('files-close').onclick = () => $('modal-files').classList.add('hidden');
$('files-refresh').onclick = async () => {
  await refreshFiles();
  renderFileList();
  toast(`已刷新，共 ${state.files.length} 个文件`);
};
$('preview-close').onclick = () => $('modal-preview').classList.add('hidden');
$('preview-open').onclick = () => {
  if (state.previewing) openLocal(state.previewing, 'open');
};
$('preview-reveal').onclick = () => {
  if (state.previewing) openLocal(state.previewing, 'reveal');
};
$('preview-copy').onclick = () => copyText(state.previewAbs || state.previewing);
$('preview-meta').onclick = () => {
  if (state.previewAbs) copyText(state.previewAbs);
};
$('modal-preview').onclick = (e) => {
  if (e.target.id === 'modal-preview') $('modal-preview').classList.add('hidden');
};
$('modal-files').onclick = (e) => {
  if (e.target.id === 'modal-files') $('modal-files').classList.add('hidden');
};
$('btn-new-room').onclick = () => newRoom('group');
$('btn-new-room-top').onclick = () => newRoom('group');
$('btn-new-dm').onclick = () => newRoom('direct');
$('pick-cancel').onclick = () => closePick('');
$('room-select').onchange = (e) => selectRoom(e.target.value);

$('btn-add-member').onclick = openAddMember;
$('btn-add-ok').onclick = confirmAdd;
$('btn-add-cancel').onclick = () => $('modal-add').classList.add('hidden');
$('btn-new-in-room').onclick = () => {
  $('modal-add').classList.add('hidden');
  openMember(null);
};

$('btn-save').onclick = saveMember;
$('btn-del').onclick = deleteMember;
$('btn-remove-room').onclick = removeFromRoom;
$('btn-cancel').onclick = () => $('modal').classList.add('hidden');

$('btn-project').onclick = openProject;
$('btn-theme').onclick = cycleTheme;
$('btn-collapse-side').onclick = () => setSideCollapsed(true);
$('btn-expand-side').onclick = () => setSideCollapsed(false);
document.querySelectorAll('[data-rtab]').forEach((t) => {
  t.onclick = () => switchRTab(t.dataset.rtab);
});
darkMedia.addEventListener('change', () => {
  if (themeMode() === 'auto') applyTheme();
});
$('btn-project-save').onclick = saveProject;
$('btn-project-cancel').onclick = () => $('modal-project').classList.add('hidden');
$('credits').onclick = editCredits;
$('credits-link').onclick = (e) => e.stopPropagation();

document.querySelectorAll('.tab').forEach((t) => {
  t.onclick = () => switchTab(t.dataset.tab);
});
document.querySelectorAll('[data-addtab]').forEach((t) => {
  t.onclick = () => switchAddTab(t.dataset.addtab);
});
document.addEventListener('click', hideCtxMenu);
window.addEventListener('resize', hideCtxMenu);

$('modal').onclick = (e) => {
  if (e.target.id === 'modal') $('modal').classList.add('hidden');
};
$('modal-project').onclick = (e) => {
  if (e.target.id === 'modal-project') $('modal-project').classList.add('hidden');
};
$('modal-add').onclick = (e) => {
  if (e.target.id === 'modal-add') $('modal-add').classList.add('hidden');
};

$('input').addEventListener('keydown', (e) => {
  if (mention.open) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      mention.index = (mention.index + 1) % mention.items.length;
      renderMention();
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      mention.index = (mention.index - 1 + mention.items.length) % mention.items.length;
      renderMention();
      return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      e.preventDefault();
      pickMention(mention.items[mention.index]);
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      closeMention();
      return;
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

$('input').addEventListener('input', (e) => {
  e.target.style.height = 'auto';
  e.target.style.height = `${Math.min(e.target.scrollHeight, 140)}px`;
  detectMention();
});
$('input').addEventListener('click', detectMention);
$('input').addEventListener('blur', () => setTimeout(closeMention, 120));

/* ---------------- 粘贴 / 拖拽上传 ---------------- */

$('input').addEventListener('paste', (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;        // 纯文本粘贴走默认行为
  e.preventDefault();
  addFiles(files);
});

const composerEl = document.querySelector('.composer');

composerEl.addEventListener('dragover', (e) => {
  const types = [...(e.dataTransfer?.types || [])];
  if (!types.includes('Files')) return;   // 拖文字进来还是正常插入
  e.preventDefault();
  composerEl.classList.add('dragging');
});
composerEl.addEventListener('dragleave', (e) => {
  if (composerEl.contains(e.relatedTarget)) return;
  composerEl.classList.remove('dragging');
});
composerEl.addEventListener('drop', (e) => {
  composerEl.classList.remove('dragging');
  const files = [...(e.dataTransfer?.files || [])];
  if (!files.length) return;
  e.preventDefault();
  addFiles(files);
});

boot().catch((e) => {
  document.body.innerHTML = `<pre style="padding:24px">启动失败：${e.message}</pre>`;
});
