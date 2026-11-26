/* Hinge Autopilot UI: plain JS, no build step. */
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pc = v => v == null ? '–' : `${Math.round(v * 100)}%`;
let S = { state: null, profiles: null, chats: null, insights: null, page: null, timers: [] };

async function api(path, body) {
  const r = await fetch('/api/' + path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, err) {
  const t = document.createElement('div');
  t.className = 'toast' + (err ? ' err' : '');
  t.textContent = msg;
  document.body.append(t);
  setTimeout(() => t.remove(), err ? 6000 : 3000);
}
async function act(fn, ok) { try { const r = await fn(); if (ok) toast(ok); return r; } catch (e) { toast(e.message, true); } }
const priv = () => S.state?.settings?.privacy;
const rd = (t, n) => { t = t ?? ''; if (!priv() || !n || n.length < 2 || n === 'unknown') return t; return String(t).replace(new RegExp('\\b' + n.replace(/[^\w]/g, '') + '\\b', 'gi'), n[0].toUpperCase() + '.'); };
const e2 = (t, n) => esc(rd(t, n));
const nm = n => { n = (n || 'unknown'); n = n[0].toUpperCase() + n.slice(1); return priv() ? n[0] + '.' : n; };
const frame = (folder, i = 0, w = 360) => `/api/frame?folder=${encodeURIComponent(folder)}&i=${i}&w=${w}&p=${priv() ? 1 : 0}`;
const when = iso => { if (!iso) return ''; const d = new Date(iso); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); };
const STRATEGY = { contradiction: 'Contradiction', rhyme: 'Rhyme', claim: 'Claim', blueprint: 'Blueprint', none: 'No hook' };
const STRATEGY_HELP = { contradiction: 'two things on the profile fight each other, named playfully', rhyme: 'a hidden pattern across prompts, treated as a discovery', claim: 'they claimed a skill, the comment contests it', blueprint: 'they wrote instructions, the comment accepts them literally' };
const statusPill = s => s ? `<span class="pill ${s.replace(' ', '')}">${{ pending: '⏳ pending', matched: '✓ matched', replied: '💬 replied', date: '🥂 date', 'no match': 'no match', unmatched: 'unmatched' }[s] || s}</span>` : '';

/* ------------------------------------------------------------------ routing */
const PAGES = { run: pageRun, profiles: pageProfiles, chats: pageChats, insights: pageInsights, schedule: pageSchedule, setup: () => openWizard(0, true) };
function go() {
  const page = (location.hash || '#run').slice(1);
  if (page === 'setup') { openWizard(0, true); history.replaceState(null, '', '#' + (S.page || 'run')); return; }
  S.timers.forEach(clearInterval); S.timers = [];
  S.page = page;
  document.querySelectorAll('nav.side a').forEach(a => a.classList.toggle('on', a.dataset.page === page));
  (PAGES[page] || pageRun)();
}
window.addEventListener('hashchange', go);

async function refreshState() {
  try { S.state = await api('state'); } catch { return; }
  const st = S.state, run = st.run, live = st.live;
  const busy = run?.state === 'running' || st.chats?.state === 'reading';
  $('#sideFoot').innerHTML = `<div><span class="dot ${busy ? 'busy' : 'on'}"></span>${run?.state === 'running' ? 'Swiping…' : st.chats?.state === 'reading' ? 'Reading chats…' : 'Idle'}</div>
    ${st.schedule?.next ? `<div>Next run ${when(st.schedule.next)}</div>` : ''}
    ${st.settings?.privacy ? '<div>🙈 Privacy mode on</div>' : ''}${st.settings?.dryRun ? '<div>🧪 Dry run on</div>' : ''}`;
  document.body.classList.toggle('privacy-on', !!st.settings?.privacy);
  return st;
}

/* ------------------------------------------------------------------ RUN */
async function pageRun() {
  const st = await refreshState();
  const likes = st?.settings?.runLikes || 8;
  $('#main').innerHTML = `
    <h1>Run</h1><div class="sub">The bot scrolls each profile, the AI decides, and every like goes out with a comment written for that person.</div>
    <div class="run-grid">
      <div class="cards">
        <div class="card">
          <div class="row">
            <div><h3>Likes this run</h3><input id="likes" class="big-num" type="number" min="1" max="50" value="${likes}"></div>
            <div style="flex:1;min-width:240px">
              <div class="status-line" id="runStatus">…</div>
              <div class="hint" id="runNote"></div>
            </div>
            <div class="row"><button class="primary" id="startBtn">▶ Start swiping</button><button class="danger" id="stopBtn">■ Stop</button></div>
          </div>
        </div>
        <div class="kpis" style="grid-template-columns:repeat(4,1fr);margin:0" id="runKpis"></div>
        <div class="card"><div class="row"><h2 style="margin:0">Latest decisions</h2><span class="spacer"></span><a href="#profiles">All profiles →</a></div><div class="mini-list" id="latest" style="margin-top:12px"></div></div>
        <div class="card"><h2>Run log</h2><pre class="log" id="log">…</pre></div>
      </div>
      <div><div class="phone"><img id="screen" alt=""><div class="cap" id="screenCap">Phone screen</div></div></div>
    </div>`;
  $('#startBtn').onclick = () => act(async () => { await api('run', { likes: +$('#likes').value }); tickRun(); }, 'Run started');
  $('#stopBtn').onclick = () => act(async () => { await api('stop', {}); tickRun(); }, 'Stopping');
  const tickRun = async () => {
    const st = await refreshState(); if (!st || S.page !== 'run') return;
    const r = st.run, live = st.live;
    const running = r?.state === 'running';
    $('#startBtn').disabled = running; $('#stopBtn').disabled = !running;
    $('#runStatus').innerHTML = running ? `<span class="dot busy"></span>${live?.state ? live.state[0].toUpperCase() + live.state.slice(1) : 'Starting'}${live?.profile_idx ? ` profile ${live.profile_idx}` : ''}`
      : r ? `<span class="dot on"></span>Last run ${r.state} ${when(r.finishedAt)}` : '<span class="dot on"></span>Ready';
    $('#runNote').textContent = running ? `Goal: ${r.likesWanted} likes${r.dryRun ? ' (dry run: nothing is sent)' : ''}` : (r?.note || 'Open Hinge on the phone, then press Start.');
    $('#runKpis').innerHTML = [['Profiles seen', r?.profiles ?? 0], ['Likes sent', r?.likes ?? 0], ['AI cost', '$' + (r?.cost ?? 0).toFixed(2)], ['Chats waiting', st.chats?.count ?? '–']]
      .map(([l, v]) => `<div class="kpi"><div class="v">${v}</div><div class="l">${l}${running ? ' (this run)' : ''}</div></div>`).join('');
    api('runlog?lines=60').then(j => { const el = $('#log'); if (el) { el.textContent = j.lines.join('\n') || 'No run yet.'; el.scrollTop = el.scrollHeight; } });
  };
  const tickScreen = () => { const img = $('#screen'); if (!img) return; const n = new Image(); n.onload = () => { img.src = n.src; $('#screenCap').textContent = 'Live · ' + new Date().toLocaleTimeString(); }; n.onerror = () => { $('#screenCap').textContent = 'Phone not connected'; }; n.src = '/api/screen?w=480&t=' + Date.now(); };
  const loadLatest = async () => {
    const { records } = await api('profiles'); S.profiles = records;
    const el = $('#latest'); if (!el) return;
    const last = records.filter(r => r.folder).slice(-6).reverse();
    el.innerHTML = last.map(r => `<div class="mini" data-seq="${r.seq}"><img src="${frame(r.folder, 0, 120)}" loading="lazy"><div class="t"><div class="row" style="gap:8px"><b>${esc(nm(r.name))}</b>${r.decision === 'like' ? '<span class="pill like">♥ liked</span>' : '<span class="pill skip">skipped</span>'}${r.score != null ? `<span class="pill">${r.score}</span>` : ''}</div><div class="c">${e2(r.message || r.rationale || '', r.name)}</div></div></div>`).join('') || '<div class="empty">No profiles yet. Press Start.</div>';
    el.querySelectorAll('.mini').forEach(m => m.onclick = () => openProfile(records.find(r => r.seq == m.dataset.seq)));
  };
  tickRun(); tickScreen(); loadLatest();
  S.timers.push(setInterval(tickRun, 3000), setInterval(tickScreen, 2500), setInterval(loadLatest, 15000));
}

/* ------------------------------------------------------------------ PROFILES */
let PF = { filter: 'all', sort: 'new', q: '', shown: 60 };
async function pageProfiles() {
  await refreshState();
  $('#main').innerHTML = `<h1>Profiles</h1><div class="sub">Every profile the AI judged. Click one to see how its comment was made, and how it went.</div>
    <div class="toolbar">
      <div class="seg" id="filters">${[['all', 'All'], ['like', 'Liked'], ['skip', 'Skipped'], ['matched', 'Matched'], ['replied', 'Replied']].map(([k, l]) => `<button data-f="${k}" class="${PF.filter === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <select id="sort"><option value="new">Newest first</option><option value="old">Oldest first</option><option value="score">Highest score</option><option value="looks">Best looks rating</option><option value="outcome">Best outcome</option></select>
      <select id="strat"><option value="">Any strategy</option>${Object.entries(STRATEGY).filter(([k]) => k !== 'none').map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>
      <input id="q" placeholder="Search name or comment" style="width:220px">
      <span class="spacer"></span><span class="hint" id="count"></span>
    </div>
    <div class="tiles" id="tiles"><div class="empty">Loading…</div></div>
    <div style="text-align:center;margin-top:20px"><button id="more">Show more</button></div>`;
  $('#sort').value = PF.sort; $('#strat').value = PF.strat || '';
  $('#filters').onclick = e => { const f = e.target.dataset.f; if (!f) return; PF.filter = f; PF.shown = 60; document.querySelectorAll('#filters button').forEach(b => b.classList.toggle('on', b.dataset.f === f)); renderTiles(); };
  $('#sort').onchange = e => { PF.sort = e.target.value; PF.shown = 60; renderTiles(); };
  $('#strat').onchange = e => { PF.strat = e.target.value; PF.shown = 60; renderTiles(); };
  $('#q').oninput = e => { PF.q = e.target.value.toLowerCase(); PF.shown = 60; renderTiles(); };
  $('#more').onclick = () => { PF.shown += 60; renderTiles(); };
  const { records } = await api('profiles'); S.profiles = records.filter(r => r.folder);
  renderTiles();
}
const RANK = { date: 5, replied: 4, matched: 3, pending: 1, 'no match': 0, unmatched: 0 };
function profileList() {
  let l = S.profiles || [];
  const f = PF.filter;
  if (f === 'like') l = l.filter(r => r.decision === 'like');
  if (f === 'skip') l = l.filter(r => r.decision !== 'like');
  if (f === 'matched') l = l.filter(r => ['matched', 'replied', 'date', 'unmatched'].includes(r.status));
  if (f === 'replied') l = l.filter(r => ['replied', 'date'].includes(r.status));
  if (PF.strat) l = l.filter(r => r.machine === PF.strat && r.decision === 'like');
  if (PF.q) l = l.filter(r => (r.name || '').includes(PF.q) || (r.message || '').toLowerCase().includes(PF.q));
  const by = { new: (a, b) => b.seq - a.seq, old: (a, b) => a.seq - b.seq, score: (a, b) => (b.score ?? -99) - (a.score ?? -99),
    looks: (a, b) => (b.looks ?? 0) - (a.looks ?? 0) || b.seq - a.seq, outcome: (a, b) => (RANK[b.status] ?? -1) - (RANK[a.status] ?? -1) || b.seq - a.seq }[PF.sort];
  return [...l].sort(by);
}
function renderTiles() {
  const l = profileList();
  $('#count').textContent = `${l.length} profiles`;
  $('#more').style.display = l.length > PF.shown ? '' : 'none';
  $('#tiles').innerHTML = l.slice(0, PF.shown).map(r => `
    <div class="tile" data-seq="${r.seq}">
      <div class="img"><img src="${frame(r.folder, 0, 320)}" loading="lazy">
        <div class="badge">${r.decision === 'like' ? (r.status && r.status !== 'pending' ? statusPill(r.status) : '<span class="pill like">♥ liked</span>') : '<span class="pill skip">skipped</span>'}</div>
        ${r.score != null ? `<div class="score">${r.score > 0 ? '+' : ''}${r.score}</div>` : ''}</div>
      <div class="body"><div class="nm"><span class="pname">${esc(nm(r.name))}${r.age ? `, ${r.age}` : ''}</span>${r.machine && r.machine !== 'none' && r.decision === 'like' ? `<span class="pill strategy">${STRATEGY[r.machine]}</span>` : ''}</div>
        <div class="msg">${r.decision === 'like' ? (r.message ? '“' + e2(r.message, r.name) + '”' : '') : e2((r.rationale || '').slice(0, 140), r.name)}</div></div>
    </div>`).join('') || '<div class="empty">Nothing here yet.</div>';
  document.querySelectorAll('.tile').forEach(t => t.onclick = () => openProfile(S.profiles.find(r => r.seq == t.dataset.seq)));
}

function openProfile(r) {
  if (!r) return;
  const o = r.outcome;
  const cands = (r.candidates || []).map(c => { const sel = (c.verdict || '').startsWith('selected'); return `<div class="cand ${sel ? 'sel' : ''}"><div class="mk">${sel ? '✅' : '✖️'}</div><div><div>${e2(c.comment, r.name)}</div><div class="why">${esc(STRATEGY[c.machine] || c.machine || '')} · ${e2(c.verdict, r.name)}</div></div></div>`; }).join('');
  $('#modal').innerHTML = `<div class="modal-bg"><button class="close">✕</button><div class="modal">
    <div class="frames">${Array.from({ length: r.frames }, (_, i) => `<img src="${frame(r.folder, i, 400)}" loading="lazy">`).join('')}</div>
    <div class="info">
      <div class="row"><h1 style="margin:0" class="pname">${esc(nm(r.name))}${r.age ? `, ${r.age}` : ''}</h1>
        ${r.decision === 'like' ? '<span class="pill like">♥ liked</span>' : '<span class="pill skip">skipped</span>'}${statusPill(r.status)}
        ${r.score != null ? `<span class="pill">score ${r.score > 0 ? '+' : ''}${r.score}</span>` : ''}${r.looks ? `<span class="pill">looks ${r.looks}/5</span>` : ''}</div>
      <div class="hint" style="margin-top:6px">${when(r.at)}${r.model ? ' · ' + esc(r.model) : ''}${r.cost ? ` · $${r.cost.toFixed(3)}` : ''}${r.dryRun ? ' · dry run' : ''}</div>
      <div class="flow">
        <div class="step"><div class="n">1</div><div class="h">What the AI saw</div><div class="b">${e2(r.rationale, r.name)}</div></div>
        ${r.decision === 'like' ? `
        <div class="step"><div class="n">2</div><div class="h">The hook it picked</div><div class="b">${r.target ? `<div class="quote">${e2(r.target, r.name)}</div>` : ''}<div style="margin-top:8px">${e2(r.hook, r.name)}</div>
          ${r.machine && r.machine !== 'none' ? `<div style="margin-top:8px"><span class="pill strategy">${STRATEGY[r.machine]}</span> <span class="hint">${STRATEGY_HELP[r.machine] || ''}${r.variation ? ' · ' + e2(r.variation, r.name) : ''}</span></div>` : ''}</div></div>
        ${cands ? `<div class="step"><div class="n">3</div><div class="h">Three drafts, two killed</div><div class="b">${cands}</div></div>` : ''}
        <div class="step"><div class="n">${cands ? 4 : 3}</div><div class="h">Sent${r.likedOn ? ` (liked their ${esc(r.likedOn)})` : ''}</div><div class="bubbles"><div class="bub like">${e2(r.message, r.name)}</div></div></div>
        <div class="step"><div class="n">${cands ? 5 : 4}</div><div class="h">What happened</div><div class="b">${o?.firstMessage ? `<div class="bubbles"><div class="bub them">${e2(o.firstMessage, r.name)}</div></div>` : (statusPill(r.status) || 'Nothing yet')}${o?.note ? `<div class="hint" style="margin-top:6px">Your note: ${e2(o.note, r.name)}</div>` : ''}</div></div>` : ''}
      </div>
      ${r.flags ? `<div class="hint">Notes: ${e2(r.flags, r.name)}</div>` : ''}
      <div class="card" style="margin-top:18px">
        <h3>Teach it your taste</h3>
        <div class="row" id="rate">${['no', 'maybe', 'yes', 'hot'].map(x => `<button data-r="${x}" class="${r.rating === x ? 'primary' : ''}">${{ no: '👎 No', maybe: '🤔 Maybe', yes: '👍 Yes', hot: '🔥 Hot' }[x]}</button>`).join('')}</div>
        <label>Tell the AI what it got wrong (or mark an outcome: m = matched, r: their reply, d = date, x = unmatched)</label>
        <div class="row"><input id="note" placeholder="e.g. too strict, I'd have liked her  ·  r: haha ok you got me"><button id="saveNote">Save</button></div>
      </div>
    </div></div></div>`;
  const close = () => $('#modal').innerHTML = '';
  $('.modal-bg').onclick = e => { if (e.target.classList.contains('modal-bg') || e.target.classList.contains('close')) close(); };
  $('#rate').onclick = e => { const x = e.target.dataset.r; if (!x) return; act(async () => { await api('taste/rate', { folder: r.folder, rating: x }); r.rating = x; document.querySelectorAll('#rate button').forEach(b => b.classList.toggle('primary', b.dataset.r === x)); }, 'Rated'); };
  $('#saveNote').onclick = () => act(async () => { const j = await api('outcome', { folder: r.folder, text: $('#note').value }); $('#note').value = ''; toast(j.ack); });
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('#modal').innerHTML = ''; });

/* ------------------------------------------------------------------ CHATS */
let CH = { key: null };
async function pageChats() {
  await refreshState();
  $('#main').innerHTML = `<div class="row" style="margin-bottom:16px"><div><h1>Chats</h1><div class="sub" style="margin:0" id="chatSub"></div></div><span class="spacer"></span>
    <button id="readQuick">↻ Read new chats</button><button id="readAll" title="Also opens Their turn and Hidden">Read everything</button></div>
    <div class="chats-grid"><div class="chat-list" id="chatList"></div><div class="card thread" id="thread"></div><div class="card sugg" id="sugg"></div></div>`;
  $('#readQuick').onclick = () => act(() => api('chats/sync', {}), 'Reading chats off the phone…');
  $('#readAll').onclick = () => act(() => api('chats/sync', { extended: true }), 'Reading every chat (takes a while)…');
  const load = async () => {
    const st = await refreshState(); if (S.page !== 'chats') return;
    const c = st.chats;
    $('#chatSub').textContent = c.state === 'reading' ? `Reading: ${c.progress || '…'}` : c.finishedAt ? `${c.count} chats · read ${when(c.finishedAt)}${st.suggest.current ? ' · drafting a reply…' : ''}` : 'Not read yet. Press Read new chats.';
    S.chats = (await api('chats')).chats || [];
    renderChatList();
  };
  load(); S.timers.push(setInterval(load, 8000));
}
function renderChatList() {
  const secs = {};
  S.chats.forEach(c => (secs[c.section || 'Your turn'] ||= []).push(c));
  $('#chatList').innerHTML = Object.entries(secs).map(([s, l]) => `<div class="sec">${esc(s)} (${l.length})</div>` + l.map(c => `
    <div class="chat-item ${CH.key === c.key ? 'on' : ''}" data-k="${esc(c.key)}"><div class="avatar">${esc((c.name || '?')[0])}</div>
      <div class="t"><div class="row" style="gap:6px"><b class="pname">${esc(nm(c.name))}</b>${c.suggestion && !c.suggestion.stale ? '<span class="pill strategy">✨</span>' : ''}</div><div class="p">${e2(c.preview || '', c.name)}</div></div></div>`).join('')).join('') || '<div class="empty">No chats yet.</div>';
  document.querySelectorAll('.chat-item').forEach(el => el.onclick = () => { CH.key = el.dataset.k; renderChatList(); });
  if (!CH.key && S.chats[0]) CH.key = S.chats[0].key;
  renderThread();
}
function renderThread() {
  const c = S.chats.find(x => x.key === CH.key);
  if (!c) { $('#thread').innerHTML = '<div class="empty">Pick a chat</div>'; $('#sugg').innerHTML = ''; return; }
  let last = null;
  const msgs = (c.messages || []).map(m => {
    let h = '';
    if (m.stamp && m.stamp !== last) { last = m.stamp; h += `<div class="stamp">${esc(m.stamp)}</div>`; }
    if (m.kind === 'like') h += `<div class="bub like ${m.from}"><span class="liked">♥ ${m.from === 'me' ? 'You liked' : 'Liked'} ${e2(m.liked || '', c.name)}</span>${e2(m.text || '', c.name)}</div>`;
    else h += `<div class="bub ${m.from}">${e2(m.text, c.name)}${m.reaction ? `<div class="liked" style="margin:4px 0 0">${e2(m.reaction, c.name)}</div>` : ''}</div>`;
    return h;
  }).join('');
  const op = c.opener;
  $('#thread').innerHTML = `<div class="row"><div class="avatar">${esc((c.name || '?')[0])}</div><h2 style="margin:0" class="pname">${esc(nm(c.name))}</h2><span class="pill">${esc(c.section || '')}</span>
      ${op?.folder ? `<span class="spacer"></span><button class="ghost" id="seeProfile">See profile</button>` : ''}</div>
    ${op ? `<div class="card" style="background:var(--panel2);margin:14px 0"><h3>How the bot opened</h3>${op.target ? `<div class="quote">${e2(op.target, c.name)}</div>` : ''}<div class="hint" style="margin-top:8px">${op.machine ? `<span class="pill strategy">${STRATEGY[op.machine] || op.machine}</span> ` : ''}${e2(op.hook || '', c.name)}</div></div>` : ''}
    <div class="bubbles" style="margin-top:12px">${msgs || '<div class="empty">No messages read</div>'}</div>`;
  if (op?.folder) $('#seeProfile').onclick = async () => { if (!S.profiles) S.profiles = (await api('profiles')).records; openProfile(S.profiles.find(r => (r.folder || '').endsWith(op.folder))); };
  const s = c.suggestion;
  $('#sugg').innerHTML = `<h2>✨ Next move</h2>
    ${s ? `<div class="hint">${s.stale ? '⚠️ The chat moved on since this was drafted' : 'Drafted ' + when(s.at)}</div>
      <div class="kv"><b>Read</b>${e2(s.read, c.name)}</div>
      <div class="kv"><b>Move</b><span class="pill strategy">${esc((s.action || '').replace('_', ' '))}</span></div>
      ${s.message ? `<div class="msg">${e2(s.message, c.name)}</div><button id="copy">Copy</button>` : ''}
      ${s.bold_variant ? `<div class="kv"><b>Bolder</b>${e2(s.bold_variant, c.name)}</div>` : ''}
      ${s.analysis ? `<div class="kv"><b>Analysis</b>${e2(s.analysis, c.name)}</div>` : ''}
      ${(s.signals || []).length ? `<div class="kv"><b>Signals</b>${s.signals.map(x => e2(x, c.name)).join('<br>')}</div>` : ''}
      ${s.why ? `<div class="kv"><b>Why</b>${e2(s.why, c.name)}</div>` : ''}
      <div class="kv"><b>When</b>${e2(s.send_when, c.name)}</div><div class="kv"><b>Then</b>${e2(s.next_move, c.name)}</div>` : '<div class="hint">No suggestion yet.</div>'}
    <label>Anything the AI should know?</label><textarea id="sNote" placeholder="optional: e.g. she's in town next week"></textarea>
    <button class="primary" id="suggest" style="margin-top:10px;width:100%">${s ? 'Draft again' : 'Draft my next message'}</button>`;
  $('#suggest').onclick = () => act(() => api('suggest', { key: c.key, note: $('#sNote').value }), 'Drafting… it shows here in ~30 s');
  if ($('#copy')) $('#copy').onclick = () => { navigator.clipboard.writeText(s.message); toast('Copied'); };
}

/* ------------------------------------------------------------------ INSIGHTS */
let IN = { dim: 'strategy' };
async function pageInsights() {
  await refreshState();
  $('#main').innerHTML = '<h1>Insights</h1><div class="sub">What works, from your own results. A like counts once its outcome is known.</div><div id="ins"><div class="empty">Crunching…</div></div>';
  const d = S.insights = await api('insights');
  const t = d.totals;
  const dims = Object.entries(d.breakdowns).filter(([, b]) => b.rows.length);
  $('#ins').innerHTML = `
    <div class="kpis">
      <div class="kpi"><div class="v">${d.judged.toLocaleString()}</div><div class="l">Profiles judged</div><div class="s">since ${d.since ? new Date(d.since).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '–'}</div></div>
      <div class="kpi"><div class="v">${t.sent.toLocaleString()}</div><div class="l">Likes with a comment</div><div class="s">${pc(t.sent / Math.max(1, d.judged))} of profiles</div></div>
      <div class="kpi hl"><div class="v">${pc(t.matchRate)}</div><div class="l">Match rate</div><div class="s">${t.matched} of ${t.resolved} known</div></div>
      <div class="kpi hl"><div class="v">${pc(t.replyRate)}</div><div class="l">They replied</div><div class="s">${t.replied} of ${t.matched} matches</div></div>
      <div class="kpi"><div class="v">${t.dates}</div><div class="l">Dates</div><div class="s">${t.pending} likes still pending</div></div>
    </div>
    <div class="cards" style="grid-template-columns:1.6fr 1fr">
      <div class="card">
        <div class="row"><h2 style="margin:0">Break it down by</h2><select id="dim" style="width:auto">${dims.map(([k, b]) => `<option value="${k}">${b.label}</option>`).join('')}</select><span class="spacer"></span>
          <div class="legend"><span><i style="background:var(--accent)"></i>Match rate</span><span><i style="background:var(--accent2)"></i>Reply rate (of matches)</span></div></div>
        <div class="bars" id="bars" style="margin-top:16px"></div>
      </div>
      <div class="cards">
        <div class="card"><h2>Funnel</h2><div class="funnel">${d.funnel.map(([l, v]) => `<div class="f"><div class="hint">${l}</div><div class="fb" style="width:${Math.max(1, v / Math.max(1, d.funnel[0][1]) * 100)}%"></div><b>${v.toLocaleString()}</b></div>`).join('')}</div></div>
        <div class="card"><h2>Double texting</h2>${d.doubles.total ? `<div class="kpi" style="border:0;padding:0;background:none"><div class="v">${pc(d.doubles.revived / d.doubles.total)}</div><div class="l">of ${d.doubles.total} double texts got an answer</div></div>` : '<div class="hint">No double texts in the chats yet.</div>'}</div>
      </div>
    </div>
    <div class="card" style="margin-top:16px"><h2>Week by week</h2><div id="trend"></div></div>
    <div class="card" style="margin-top:16px"><div class="toggle" style="padding-top:0"><div class="t"><b>Feed what's working back to the AI</b><span class="hint">Only patterns that survive a false discovery correction (enough data, a real gap) are added to the judge's prompt.</span></div>
      <label class="switch"><input type="checkbox" id="useLessons" ${d.lessons.enabled ? 'checked' : ''}><span></span></label></div>
      <pre class="log" style="max-height:220px">${esc(d.lessons.judge || 'Needs at least 30 likes with a known outcome.')}</pre></div>`;
  $('#dim').value = IN.dim in d.breakdowns ? IN.dim : dims[0]?.[0];
  $('#dim').onchange = e => { IN.dim = e.target.value; bars(); };
  $('#useLessons').onchange = e => act(() => api('settings', { useLessons: e.target.checked }), e.target.checked ? 'On' : 'Off');
  const bars = () => {
    const rows = d.breakdowns[$('#dim').value].rows;
    const max = Math.max(0.05, ...rows.map(r => Math.max(r.matchRate || 0, r.replyRate || 0)));
    $('#bars').innerHTML = rows.map(r => `<div class="bar-row"><div class="lab">${esc(r.label)}<small>${r.sent} likes · ${r.resolved} known</small></div>
      <div class="bar-pair"><div class="bar-track"><div class="bar-fill" style="width:${(r.matchRate || 0) / max * 100}%"></div></div><div class="bar-track"><div class="bar-fill b" style="width:${(r.replyRate || 0) / max * 100}%"></div></div></div>
      <div class="val">${pc(r.matchRate)}<small>${pc(r.replyRate)} reply</small></div></div>`).join('');
  };
  bars();
  trend(d.breakdowns.week?.rows || []);
}
function trend(rows) {
  rows = rows.map(r => ({ ...r, matchRate: r.resolved >= 10 ? r.matchRate : null }));
  if (rows.length < 2) { $('#trend').innerHTML = '<div class="hint">Needs two weeks of likes.</div>'; return; }
  const W = 1000, H = 220, P = 36, n = rows.length;
  const x = i => P + i * (W - 2 * P) / (n - 1);
  const maxSent = Math.max(...rows.map(r => r.sent));
  const maxRate = Math.max(0.1, ...rows.map(r => r.matchRate || 0));
  const y = v => H - P - v * (H - 2 * P);
  const pts = rows.map((r, i) => [r, i]).filter(([r]) => r.matchRate != null);
  const line = pts.map(([r, i], k) => `${k ? 'L' : 'M'}${x(i)},${y(r.matchRate / maxRate)}`).join(' ');
  const bw = Math.min(40, (W - 2 * P) / n * 0.6);
  $('#trend').innerHTML = `<div class="legend"><span><i style="background:#3a3a48"></i>Likes sent</span><span><i style="background:var(--accent)"></i>Match rate</span></div>
  <svg class="chart" viewBox="0 0 ${W} ${H}" width="100%">
    ${rows.map((r, i) => `<rect x="${x(i) - bw / 2}" y="${y(r.sent / maxSent)}" width="${bw}" height="${H - P - y(r.sent / maxSent)}" rx="4" fill="#2a2a35"/><text x="${x(i)}" y="${H - 12}" text-anchor="middle">${esc(r.label.replace('week of ', ''))}</text>`).join('')}
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="3" stroke-linejoin="round"/>
    ${pts.map(([r, i]) => `<circle cx="${x(i)}" cy="${y(r.matchRate / maxRate)}" r="4.5" fill="var(--accent)"/><text x="${x(i)}" y="${y(r.matchRate / maxRate) - 10}" text-anchor="middle" style="fill:var(--text)">${pc(r.matchRate)}</text>`).join('')}
  </svg><div class="hint">Match rate shown once a week has 10+ likes with a known outcome; newer likes are still pending.</div>`;
}

/* ------------------------------------------------------------------ SCHEDULE */
async function pageSchedule() {
  const st = await refreshState(); const s = st.settings;
  const sw = (k, t, h) => `<div class="toggle"><div class="t"><b>${t}</b><span class="hint">${h}</span></div><label class="switch"><input type="checkbox" data-k="${k}" ${s[k] ? 'checked' : ''}><span></span></label></div>`;
  $('#main').innerHTML = `<h1>Schedule & settings</h1><div class="sub">Let it run by itself every day, at a slightly different time each day.</div>
    <div class="cards" style="grid-template-columns:1fr 1fr">
      <div class="card">
        <h2>Daily swiping</h2>
        ${sw('autoRun', 'Swipe every day', 'Starts on its own while this app is open and the phone is plugged in.')}
        <div class="grid2"><div><label>Start around</label><input type="time" data-k="runAt" value="${s.runAt}"></div><div><label>Random delay, up to (min)</label><input type="number" data-k="runRandomMin" value="${s.runRandomMin}" min="0" max="240"></div>
        <div><label>Sessions a day</label><select data-k="runSessions">${[1, 2, 3].map(n => `<option ${s.runSessions == n ? 'selected' : ''}>${n}</option>`).join('')}</select></div><div><label>Gap between sessions (min)</label><input type="number" data-k="runGapMin" value="${s.runGapMin}" min="45" max="300"></div>
        <div><label>Likes per session</label><input type="number" data-k="runLikes" value="${s.runLikes}" min="1" max="50"></div><div><label>Minimum score to like</label><input type="number" data-k="minLikeScore" value="${s.minLikeScore}" min="1" max="15"></div></div>
        <div class="hint" style="margin-top:12px">${st.schedule.slots?.length ? 'Today: ' + st.schedule.slots.map(x => `${new Date(x.at).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })} (${x.state})`).join(', ') : ''}</div>
      </div>
      <div class="card">
        <h2>Chats</h2>
        ${sw('readAfterRun', 'Read chats after every run', 'Opens Matches and reads what came in.')}
        <div><label>Also read chats every N hours (0 = off)</label><input type="number" data-k="readEveryHours" value="${s.readEveryHours}" min="0" max="48"></div>
        ${sw('autoSuggest', 'Draft replies automatically', 'Every chat waiting on you gets a suggested next message. Nothing is ever sent in chats: you copy it.')}
        <h2 style="margin-top:22px">Safety & sharing</h2>
        ${sw('dryRun', 'Dry run', 'Judge and log every profile but never like. Good for tuning your answers first.')}
        ${sw('privacy', 'Privacy mode', 'Blurs photos and shortens names everywhere in this app. Turn it on before taking screenshots.')}
        <div><label>AI effort (Claude)</label><select data-k="effort">${['low', 'medium', 'high'].map(n => `<option ${s.effort == n ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
      </div>
    </div>`;
  document.querySelectorAll('[data-k]').forEach(el => el.onchange = () => {
    const k = el.dataset.k; let v = el.type === 'checkbox' ? el.checked : el.value;
    if (el.type === 'number' || k === 'runSessions') v = parseInt(v || '0', 10);
    act(async () => { await api('settings', { [k]: v }); await refreshState(); }, 'Saved');
  });
}

/* ------------------------------------------------------------------ SETUP WIZARD */
let W = { step: 0, profile: {}, ai: { provider: 'anthropic' }, poll: null, fromMenu: false };
const WSTEPS = ['Phone tools', 'Connect phone', 'AI', 'About you', 'Your type', 'Your voice'];
async function openWizard(step = 0, fromMenu = false) {
  W.step = step; W.fromMenu = fromMenu;
  W.profile = await api('profile').catch(() => ({}));
  renderWizard();
}
function closeWizard() { clearInterval(W.poll); $('#wizard').innerHTML = ''; go(); }
async function renderWizard() {
  clearInterval(W.poll);
  const st = await api('setup');
  if (st.ai.provider) W.ai.provider = W.ai.provider || st.ai.provider;
  const steps = `<div class="steps">${WSTEPS.map((_, i) => `<div class="${i <= W.step ? 'done' : ''}"></div>`).join('')}</div>`;
  const body = [wizAdb, wizPhone, wizAi, wizAbout, wizType, wizVoice][W.step](st);
  $('#wizard').innerHTML = `<div class="wizard"><div class="wiz">
    <div class="row"><div class="brand" style="padding:0"><div class="logo">💘</div>Hinge Autopilot</div><span class="spacer"></span>${W.fromMenu ? '<button class="ghost" id="wClose">Close</button>' : ''}</div>
    ${steps}<div class="hint">Step ${W.step + 1} of ${WSTEPS.length} · ${WSTEPS[W.step]}</div>
    ${body}
    <div class="wiz-foot"><button class="ghost" id="wBack" ${W.step ? '' : 'style="visibility:hidden"'}>← Back</button><button class="primary" id="wNext">${W.step === WSTEPS.length - 1 ? 'Finish ✓' : 'Continue →'}</button></div>
  </div></div>`;
  if ($('#wClose')) $('#wClose').onclick = closeWizard;
  $('#wBack').onclick = () => { saveStepFields(); W.step--; renderWizard(); };
  $('#wNext').onclick = async () => {
    saveStepFields();
    if (W.step >= 3) { const ok = await act(() => api('profile', W.profile)); if (!ok) return; }
    if (W.step === 3 && !W.profile.name) return toast('Add your first name', true);
    if (W.step === WSTEPS.length - 1) { toast('All set. Open Hinge on the phone and press Start.'); location.hash = '#run'; return closeWizard(); }
    W.step++; renderWizard();
  };
  wireStep(st);
}
function wizAdb(st) {
  const a = st.adb, inst = st.adbInstall;
  return `<h1>Let's connect your phone</h1><div class="sub">The bot drives Hinge on a real Android phone over USB, using Google's free Android tool <b>adb</b>.</div>
  <div class="card">
    <div class="status-box ${a.found ? 'ok' : 'wait'}"><div class="big">${a.found ? '✅' : '📦'}</div><div><b>${a.found ? 'Android tools are installed' : 'Android tools are not installed yet'}</b><div class="hint">${a.found ? esc(a.version) : 'About 7 MB, downloaded from Google into this folder. Nothing is installed system wide.'}</div></div></div>
    ${a.found ? '' : `<button class="primary" id="getAdb" ${inst.state === 'downloading' ? 'disabled' : ''}>${inst.state === 'downloading' ? `Downloading… ${inst.progress || 0}%` : '⬇ Download Android platform tools'}</button>
      ${inst.state === 'failed' ? `<div class="hint" style="color:var(--bad);margin-top:10px">${esc(inst.error)}</div>` : ''}
      <div class="hint" style="margin-top:12px">Already have adb? Make sure it's on your PATH and press Continue.</div>`}
  </div>`;
}
function wizPhone(st) {
  const p = st.phone;
  const box = p.state === 'connected' ? `<div class="status-box ok"><div class="big">📱</div><div><b>${esc(p.model || 'Phone')} connected</b><div class="hint">${p.screen ? p.screen.join(' × ') + ' screen' : ''}${p.hinge === false ? ' · ⚠️ Hinge is not installed on this phone' : p.hinge ? ' · Hinge found ✓' : ''}</div></div></div>`
    : p.state === 'unauthorized' ? `<div class="status-box wait"><div class="big">🔐</div><div><b>Phone found, waiting for you to allow it</b><div class="hint">Unlock the phone and tap <b>Allow</b> on "Allow USB debugging?". Tick "Always allow from this computer".</div></div></div>`
    : `<div class="status-box wait"><div class="big">🔌</div><div><b>Waiting for a phone…</b><div class="hint">${p.state === 'no_adb' ? 'Android tools missing: go back one step.' : 'Plug it in with a USB cable that carries data.'}</div></div></div>`;
  return `<h1>Plug in your Android phone</h1><div class="sub">One time setup on the phone. This page updates by itself.</div>
  <div class="card">${box}
    <ol class="howto"><li>Open <b>Settings → About phone</b> and tap <b>Build number</b> 7 times (you'll see "You are now a developer").</li>
      <li>Go to <b>Settings → System → Developer options</b> and turn on <b>USB debugging</b>.</li>
      <li>Plug the phone into this computer and tap <b>Allow</b> on the prompt.</li>
      <li>Open <b>Hinge</b>, log in, and leave it on the Discover tab.</li></ol>
    <div class="hint">iPhone isn't supported (iOS doesn't allow this kind of control). An Android emulator works too.</div></div>`;
}
const PROVIDERS = [['anthropic', 'Claude (Anthropic)', 'Best comments and judgement. Get a key at console.anthropic.com.'], ['openai', 'OpenAI', 'GPT models. Key from platform.openai.com.'],
  ['gemini', 'Google Gemini', 'Cheapest. Key from aistudio.google.com.'], ['claude_code', 'Claude Code (no key)', 'Uses your logged in `claude` CLI and its subscription.']];
function wizAi(st) {
  const p = W.ai.provider;
  const same = st.ai.provider === p;
  return `<h1>Pick the AI brain</h1><div class="sub">It looks at each profile's screenshots, decides, and writes the comment. You pay the provider directly; a profile costs about $0.01-0.05.</div>
  <div class="card"><div class="choices" id="prov">${PROVIDERS.map(([k, t, d]) => `<div class="choice ${p === k ? 'on' : ''}" data-p="${k}"><b>${t}</b><span>${d}</span></div>`).join('')}</div>
    ${p !== 'claude_code' ? `<label>API key ${same && st.ai.keyHint ? `(saved: ${esc(st.ai.keyHint)})` : ''}</label><input id="aiKey" type="password" placeholder="${same && st.ai.keyHint ? 'Leave empty to keep the saved key' : 'Paste your key'}" autocomplete="off">` : ''}
    <label>Model</label><input id="aiModel" value="${esc(same ? st.ai.model : st.defaults[p])}">
    <div class="row" style="margin-top:14px"><button class="primary" id="aiTest">Test & save</button><span class="hint" id="aiRes">${same && st.ai.tested ? '✅ Working, tested ' + when(st.ai.tested) : ''}</span></div>
    <div class="hint" style="margin-top:12px">The key is stored only in data/settings.json on this computer.</div></div>`;
}
const F = (k, label, ph = '', type = 'text') => `<div><label>${label}</label><input data-f="${k}" type="${type}" value="${esc(W.profile[k] ?? '')}" placeholder="${esc(ph)}"></div>`;
const T = (k, label, ph = '', hint = '') => `<div><label>${label}</label><textarea data-f="${k}" placeholder="${esc(ph)}">${esc(Array.isArray(W.profile[k]) ? W.profile[k].join('\n') : W.profile[k] || '')}</textarea>${hint ? `<div class="hint">${hint}</div>` : ''}</div>`;
const C = (k, opts) => `<div class="choices" data-c="${k}">${opts.map(([v, t, d]) => `<div class="choice ${W.profile[k] === v ? 'on' : ''}" data-v="${v}"><b>${t}</b><span>${d}</span></div>`).join('')}</div>`;
function wizAbout() {
  return `<h1>A bit about you</h1><div class="sub">So the comments sound like you and never claim something that isn't true.</div>
  <div class="card"><div class="grid3">${F('name', 'First name', 'Alex')}${F('age', 'Age', '29', 'number')}${F('city', 'City', 'Toronto')}</div>
    ${F('job', 'What you do', 'Software engineer')}
    ${T('about', 'In two lines, who are you?', 'Outdoorsy, always planning the next trip, cooks a lot, terrible at karaoke but goes anyway.')}
    ${T('facts', 'Facts the AI may use in a comment (one per line)', 'Makes a proper carbonara\nRan a half marathon last spring\nHas a very opinionated cat named Miso', 'Real, specific, a bit unusual. It uses at most one, in about 1 comment in 5.')}
    ${T('never_mention', 'Never mention (one per line)', 'my job title\nthat I just moved')}
    <label>Who are you looking for?</label>${C('interested_in', [['women', 'Women', ''], ['men', 'Men', ''], ['everyone', 'Everyone', '']])}
    <label>What are you looking for right now?</label>${C('looking_for', [['casual', 'Something casual', 'fun first, see where it goes'], ['open', 'Open to either', 'casual that could turn serious'], ['serious', 'A relationship', 'values and plans matter most']])}
  </div>`;
}
function wizType() {
  return `<h1>Your type</h1><div class="sub">The AI scores every profile against this. Be honest: nobody sees it but the AI.</div>
  <div class="card"><div class="grid2">${F('age_min', 'Youngest', '24', 'number')}${F('age_max', 'Oldest', '33', 'number')}</div>
    ${T('looks', 'What you find attractive', 'Athletic or slim, natural look, great smile, dresses well. Not a fan of heavy filters.')}
    ${T('green_flags', 'What makes you want to talk to someone (one per line)', 'Funny, specific prompts\nOutdoorsy, travels\nHas a job or studies something\nLikes cooking or food')}
    ${T('dealbreakers', 'Dealbreakers: instant skip (one per line)', 'Smokers\nWants kids right now\nOnly mirror selfies\nOver 2 hours away')}
    <div class="hint">You can retrain later: rate past profiles 👎 🤔 👍 🔥 on the Profiles page and the AI learns your taste.</div></div>`;
}
function wizVoice() {
  return `<h1>How you text</h1><div class="sub">Every comment is written in this voice, then checked against a list of things that make messages sound like a bot.</div>
  <div class="card"><label>Your tone</label>${C('tone', [['playful', 'Playful', 'light teasing, a grin in every line'], ['dry', 'Dry', 'deadpan, understated wit'], ['warm', 'Warm', 'kind, curious, gentle humor'], ['bold', 'Bold', 'direct, confident, a little cheeky']])}
    ${T('sample_messages', 'Optional: 2-5 openers you sent that worked (one per line)', 'ok but who taught you to rank pasta shapes\nyou had me at "will fight for the window seat"')}
    <div class="grid2">${F('languages', 'Other languages you text in', 'Spanish, French')}${F('max_words', 'Max words per comment', '18', 'number')}</div>
    <div class="toggle"><div class="t"><b>Emoji</b><span class="hint">At most one, when it helps.</span></div><label class="switch"><input type="checkbox" data-f="emoji" ${W.profile.emoji !== false ? 'checked' : ''}><span></span></label></div></div>`;
}
function saveStepFields() {
  document.querySelectorAll('#wizard [data-f]').forEach(el => {
    const k = el.dataset.f;
    W.profile[k] = el.type === 'checkbox' ? el.checked : el.value;
  });
  if (typeof W.profile.languages === 'string') W.profile.languages = W.profile.languages.split(',').map(x => x.trim()).filter(Boolean);
}
function wireStep(st) {
  document.querySelectorAll('#wizard [data-c]').forEach(g => g.onclick = e => { const c = e.target.closest('.choice'); if (!c) return; W.profile[g.dataset.c] = c.dataset.v; g.querySelectorAll('.choice').forEach(x => x.classList.toggle('on', x === c)); });
  if (W.step === 0) {
    if ($('#getAdb')) $('#getAdb').onclick = () => act(async () => { await api('setup/adb', {}); renderWizard(); });
    if (st.adbInstall.state === 'downloading') W.poll = setInterval(renderWizard, 1200);
  }
  if (W.step === 1) W.poll = setInterval(async () => { const s = await api('setup'); if (s.phone.state !== st.phone.state || s.phone.hinge !== st.phone.hinge) renderWizard(); }, 2000);
  if (W.step === 2) {
    $('#prov').onclick = e => { const c = e.target.closest('.choice'); if (!c) return; W.ai.provider = c.dataset.p; renderWizard(); };
    $('#aiTest').onclick = async () => {
      $('#aiRes').textContent = 'Testing…';
      try { await api('setup/ai', { provider: W.ai.provider, key: $('#aiKey')?.value || '', model: $('#aiModel').value }); $('#aiRes').textContent = '✅ Working'; }
      catch (e) { $('#aiRes').textContent = '❌ ' + e.message; }
    };
  }
}

/* ------------------------------------------------------------------ boot */
(async () => {
  await refreshState();
  const setup = await api('setup');
  go();
  if (!setup.profile.done || !setup.ai.hasKey) {
    const step = !setup.adb.found ? 0 : setup.phone.state !== 'connected' ? 1 : !setup.ai.hasKey ? 2 : 3;
    openWizard(step);
  }
  setInterval(() => { if (S.page !== 'run' && S.page !== 'chats') refreshState(); }, 10000);
})();
