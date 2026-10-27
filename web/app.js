const $ = (id) => document.getElementById(id);

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
};

/* ---------------- 基础 ---------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
    + (r.archived ? ' archived' : '') + (isDm ? ' dm' : '');

  if (isDm) {
    const who = (r.members || [])[0] || '';
    const m = who ? memberOf(who) : null;
    const av = document.createElement('span');
    av.className = 'mini-av';
    av.style.background = m?.color || '#71717a';
    av.textContent = initials(m?.persona || who || '?');
    el.appendChild(av);
  }

  const nm = document.createElement('span');
  nm.textContent = r.name;
  nm.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1';
  el.appendChild(nm);

  const c = document.createElement('span');
  c.className = 'count';
  c.textContent = (r.members || []).length;
  el.appendChild(c);

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
    rl.textContent = `${m.role} · @${m.name}`;
    info.append(nm, rl);

    card.dataset.name = name;
    card.append(av, info);
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

  $('room-meta').textContent = `${names.length} 位成员`;
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

function msgEl(sender, content, mine) {
  const wrap = document.createElement('div');
  wrap.className = 'msg' + (mine ? ' me' : '');

  const av = document.createElement('div');
  av.className = 'avatar';
  if (!mine) av.style.background = colorOf(sender);
  av.textContent = mine ? '我' : initials(memberOf(sender)?.persona || sender);

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = mine ? '你' : personaOf(sender);
  const text = document.createElement('div');
  text.className = 'text';
  text.textContent = content;
  bubble.append(who, text);

  wrap.append(av, bubble);
  return wrap;
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
    box.appendChild(msgEl(m.sender, m.content, m.kind === 'say'));
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
      if (ev.kind === 'say') {
        box.appendChild(msgEl(ev.sender, ev.content, true));
      } else {
        box.appendChild(msgEl(ev.sender, ev.content, false));
      }
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
      $('btn-send').disabled = state.busy;
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
  state.working.clear();        // 切房间时清掉上一个房间的工作状态
  renderWorking();
  const room = state.config.rooms.find((r) => r.id === id);
  $('room-title').textContent = room?.name || id;
  renderRooms();
  renderRoomSelect();
  renderMembers();
  await loadMessages();
  connectSSE();
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
  $('modal-project').classList.remove('hidden');
}

async function saveProject() {
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

/* ---------------- 发送 ---------------- */

async function send() {
  const input = $('input');
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = '';
  input.style.height = 'auto';
  try {
    await api(`/api/rooms/${state.roomId}/say`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  } catch (e) {
    toast('发送失败：' + e.message);
  }
}

/* ---------------- 启动 ---------------- */

async function boot() {
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
}

$('btn-send').onclick = send;
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

boot().catch((e) => {
  document.body.innerHTML = `<pre style="padding:24px">启动失败：${e.message}</pre>`;
});
