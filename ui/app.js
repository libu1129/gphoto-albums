'use strict';
// 화면 동작: 프로그램 안 로컬 서버(/api/*)를 0.7~2초마다 읽어 그린다.
(function () {
  const TOKEN = new URLSearchParams(location.search).get('t') || '';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (n) => Number(n || 0).toLocaleString('ko-KR');
  function bytes(n) {
    n = Number(n || 0);
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + u[i];
  }
  function dur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (d) return `${d}일 ${h}시간`;
    if (h) return `${h}시간 ${m}분`;
    if (m) return `${m}분 ${s}초`;
    return `${s}초`;
  }
  const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });

  async function api(path, body) {
    const opt = { method: body === undefined ? 'GET' : 'POST', headers: { 'X-App-Token': TOKEN } };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    let r;
    try { r = await fetch('/api/' + path, opt); } catch (e) { throw new Error('프로그램과 연결이 끊겼습니다'); }
    let j;
    try { j = await r.json(); } catch (e) { throw new Error('응답을 읽지 못했습니다'); }
    if (!j.ok) throw new Error(j.error || '실패했습니다');
    return j.data;
  }
  async function act(path, body, okMsg) {
    try {
      const d = await api(path, body || {});
      if (okMsg) toast(okMsg);
      kick();
      return d;
    } catch (e) {
      toast(e.message, true);
      kick();
      return undefined;
    }
  }

  let toastTimer = null;
  function toast(msg, isErr) {
    const t = $('toast');
    t.textContent = msg;
    t.className = 'toast' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), isErr ? 6000 : 3000);
  }
  const show = (el, on) => el.classList.toggle('hidden', !on);

  // ── 상태 읽기 ──
  let S = null;
  let A = { version: -1, items: [] };
  let tableKey = '';
  let timer = null;
  let inflight = false;
  let logSeq = -1;

  async function refresh() {
    if (inflight) return;
    inflight = true;
    try {
      S = await api('state');
      show($('offline'), false);
      render();
      const k = [S.plan ? S.plan.version : 0, S.upload.status_version, S.account.email || '', S.selection.count].join(':');
      if (k !== tableKey) {
        A = await api('albums');
        tableKey = k;
        renderTable();
      }
    } catch (e) {
      show($('offline'), true);
    } finally {
      inflight = false;
    }
  }
  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(async () => { await refresh(); schedule(S && S.busy ? 700 : 2000); }, ms);
  }
  function kick() { schedule(40); }

  // ── 그리기 ──
  function render() {
    renderAccount();
    renderFolder();
    renderPreview();
    renderProgress();
    renderLog();
    $('footVersion').textContent = `버전 ${S.version} · 자료: ${S.data_dir}`;
  }

  function renderAccount() {
    const c = S.client, a = S.account, lg = S.login;
    $('clientText').innerHTML = c.ok
      ? `등록됨 <span class="muted">· 프로젝트 ${esc(c.project || '?')}</span>`
      : (c.error ? `<span class="muted">${esc(c.error)}</span>` : '아직 없음 <span class="muted">— 「만드는 법」을 따라 만든 뒤 고르세요</span>');
    const waiting = lg.phase === 'waiting';
    if (waiting) $('loginText').innerHTML = '브라우저에서 구글 로그인을 마쳐 주세요… <span class="muted">(5분 안에)</span>';
    else if (a.logged_in) $('loginText').innerHTML = `<b>${esc(a.email || '(이메일 모름)')}</b> <span class="muted">에 올립니다</span>`;
    else $('loginText').textContent = '로그인 안 됨';
    show($('btnLogin'), !a.logged_in && !waiting);
    $('btnLogin').disabled = !c.ok || S.uploading;
    show($('btnLoginCancel'), waiting);
    show($('btnLogout'), a.logged_in && !waiting);
    $('btnLogout').disabled = S.uploading;
    $('btnClient').disabled = S.uploading;
    const note = $('loginNote');
    if (lg.phase === 'error' && lg.error) { note.className = 'note err'; note.textContent = lg.error; }
    else if (a.problem) { note.className = 'note warn'; note.textContent = a.problem; }
    else if (a.logged_in && !a.read_scope) { note.className = 'note warn'; note.textContent = "'앱이 만든 앨범 보기' 권한을 받지 못했습니다. 올리기는 되지만, 구글 포토에서 지운 앨범을 알아채지 못할 수 있습니다."; }
    else note.className = 'note hidden';
    show($('accountBadge'), a.logged_in);
    $('accountChip').classList.toggle('on', a.logged_in);
    $('accountText').textContent = a.logged_in ? (a.email || '로그인됨') : '로그인 안 됨';
  }

  function renderFolder() {
    const st = S.settings;
    const inp = $('rootInput');
    if (document.activeElement !== inp) inp.value = st.root || '';
    $('optRoot').checked = !!st.include_root;
    $('optVideo').checked = !!st.include_videos;
    const lockIt = S.uploading || S.scan.phase === 'scanning';
    inp.disabled = lockIt; $('btnRoot').disabled = lockIt; $('optRoot').disabled = lockIt; $('optVideo').disabled = lockIt;
    const lbl = st.root_label || '사진';
    const ex = st.include_root ? `${lbl}_2023_제주여행` : '2023_제주여행';
    $('example').innerHTML = `예: <code>${esc(lbl)}\\2023\\제주여행</code> 폴더 → 앨범 <b>${esc(ex)}</b>` +
      ` <span class="muted">· <code>${esc(lbl)}</code> 바로 아래 파일 → 앨범 <b>${esc(lbl)}</b></span>`;
    const scanning = S.scan.phase === 'scanning';
    $('btnScan').disabled = !st.root || scanning || S.uploading;
    $('btnScan').textContent = S.plan ? '미리보기 다시 만들기' : '미리보기 만들기';
    const t = $('scanText');
    if (scanning) t.textContent = `훑는 중… 파일 ${num(S.scan.scanned)}개 · ${S.scan.current || ''}`;
    else if (S.scan.phase === 'error') t.textContent = S.scan.error || '';
    else if (S.plan && S.plan.stale) t.textContent = '설정이 바뀌었습니다. 미리보기를 다시 만드세요.';
    else t.textContent = '';
    t.style.color = (S.scan.phase === 'error' || (S.plan && S.plan.stale)) ? 'var(--warn)' : '';
  }

  function renderPreview() {
    const p = S.plan;
    show($('previewEmpty'), !p);
    show($('previewBody'), !!p);
    if (!p) return;
    const stat = (v, k) => `<div class="stat"><b>${v}</b><small>${k}</small></div>`;
    $('stats').innerHTML = [
      stat(num(p.folders), '앨범'), stat(num(p.photos), '사진'), stat(num(p.videos), '영상'),
      stat(bytes(p.bytes), '용량'), stat(`${num(p.est_requests)}건`, `예상 호출 · 약 ${p.est_days}일`),
    ].join('');
    const parts = Object.entries(p.skipped || {}).map(([k, v]) => {
      if (k === '지원하지 않는 형식' && p.skipped_ext.length) {
        return `${esc(k)} ${num(v)}개(${p.skipped_ext.map(([e, n]) => `${esc(e || '확장자 없음')} ${num(n)}`).join(', ')})`;
      }
      return `${esc(k)} ${num(v)}개`;
    });
    let html = parts.length ? `올리지 않는 파일: ${parts.join(' · ')}` : '올리지 않는 파일 없음';
    if (p.examples && p.examples.length) {
      html += `<details><summary>제외한 파일 보기(앞 ${num(p.examples.length)}개)</summary><ul>${p.examples.map(([r, why]) => `<li>${esc(r)} — ${esc(why)}</li>`).join('')}</ul></details>`;
    }
    if (p.n_errors) {
      html += `<details><summary style="color:var(--warn)">읽지 못한 곳 ${num(p.n_errors)}개</summary><ul>${p.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul></details>`;
    }
    $('skipped').innerHTML = html;
    const sel = S.selection;
    $('selText').textContent = sel.count ? `선택 ${num(sel.count)}개 · 파일 ${num(sel.files)}개 · ${bytes(sel.bytes)}` : '선택 없음';
    const all = $('chkAll');
    const n = p.folders;
    all.checked = n > 0 && sel.count === n;
    all.indeterminate = sel.count > 0 && sel.count < n;
    all.disabled = S.uploading;
    const btn = $('btnUpload');
    const why = !S.account.logged_in ? '올리려면 1단계에서 구글에 로그인하세요.'
      : p.stale ? '설정이 바뀌었습니다. 미리보기를 다시 만드세요.'
      : !sel.count ? '올릴 앨범을 고르세요. 처음에는 1~2개로 시험해 보길 권합니다.'
      : '';
    btn.disabled = !!why || S.uploading;
    btn.textContent = S.uploading ? '올리는 중…' : '선택한 앨범 올리기';
    const hint = $('uploadHint');
    hint.textContent = S.uploading ? '' : why;
    hint.className = 'hint' + (why && S.account.logged_in ? ' warn' : '');
  }

  function statusHtml(it) {
    const out = [];
    if (it.live === 'uploading') out.push(`<span class="pill run">올리는 중</span><span class="muted">${num(it.done)}/${num(it.files)}</span>`);
    else if (it.live === 'queued') out.push('<span class="pill">대기</span>');
    else if (it.missing) out.push(`<span class="pill err">구글에서 지워짐</span><button class="link" data-act="forget" data-key="${esc(it.key)}">다시 만들기</button>`);
    else if (it.done >= it.files && it.files > 0) out.push(`<span class="pill ok">올림 완료</span>${it.album_url ? `<button class="link" data-act="open" data-key="${esc(it.key)}">앨범 열기</button>` : ''}`);
    else if (it.done > 0) out.push(`<span class="pill part">일부 ${num(it.done)}/${num(it.files)}</span>${it.album_url ? `<button class="link" data-act="open" data-key="${esc(it.key)}">앨범 열기</button>` : ''}`);
    else out.push('<span class="muted">새 앨범</span>');
    const failed = (it.failed || 0) + (it.failed_perm || 0);
    if (failed && !it.missing) out.push(`<span class="pill err">실패 ${num(failed)}</span>`);
    return `<div class="st">${out.join('')}</div>`;
  }

  function renderTable() {
    const body = $('albumBody');
    const wrap = $('tableWrap');
    const top = wrap.scrollTop;
    const lbl = (S && S.settings.root_label) || '';
    const lock = S && S.uploading;
    body.innerHTML = A.items.map((it) => {
      const path = it.key ? `${lbl}\\${it.key.split('/').join('\\')}` : `${lbl} (바로 아래 파일)`;
      const extra = [];
      if (it.warnings && it.warnings.length) extra.push(`<div class="w">⚠ ${it.warnings.map(esc).join(' · ')}</div>`);
      if (it.album_titles && it.album_titles.length && it.album_titles[0] !== it.title) extra.push(`<div class="m">구글 앨범: ${it.album_titles.map(esc).join(', ')}</div>`);
      return `<tr class="${it.selected ? 'sel' : ''}">` +
        `<td class="c-chk"><input type="checkbox" aria-label="${esc(it.title)} 선택" data-key="${esc(it.key)}" ${it.selected ? 'checked' : ''} ${lock ? 'disabled' : ''}></td>` +
        `<td><div class="t">${esc(it.title)}</div><div class="p">${esc(path)}</div>${extra.join('')}</td>` +
        `<td class="n">${num(it.photos)}</td><td class="n">${num(it.videos)}</td><td class="n">${bytes(it.bytes)}</td>` +
        `<td>${statusHtml(it)}</td></tr>`;
    }).join('');
    wrap.scrollTop = top;
  }

  function renderProgress() {
    const u = S.upload;
    const card = $('progressCard');
    show(card, u.phase !== 'idle');
    if (u.phase === 'idle') return;
    const titles = { running: '올리는 중', waiting: '기다리는 중', stopping: '멈추는 중', stopped: '멈춤', done: '다 올렸습니다', error: '오류로 멈춤' };
    $('progTitle').textContent = titles[u.phase] || '올리기';
    const handled = u.files_done + u.files_failed + u.files_skipped;
    const pct = u.bytes_total > 0 ? Math.min(100, 100 * u.bytes_done / u.bytes_total) : (u.files_total ? 100 * handled / u.files_total : (u.phase === 'done' ? 100 : 0));
    $('progFill').style.width = pct.toFixed(1) + '%';
    const stat = (v, k) => `<div class="stat"><b>${v}</b><small>${k}</small></div>`;
    const cells = [
      stat(`${num(u.files_done)} / ${num(u.files_total)}`, '올린 파일'),
      stat(`${bytes(u.bytes_done)} / ${bytes(u.bytes_total)}`, '보낸 용량'),
      stat(u.speed ? `${bytes(u.speed)}/초` : '—', '속도'),
      stat(u.eta != null && (u.phase === 'running' || u.phase === 'waiting') ? `약 ${dur(u.eta)}` : '—', '남은 시간(용량 기준)'),
      stat(num(u.requests), '이번 호출 수'),
    ];
    if (u.files_failed) cells.push(stat(num(u.files_failed), '실패'));
    if (u.files_skipped) cells.push(stat(num(u.files_skipped), '건너뜀'));
    $('progGrid').innerHTML = cells.join('');
    const active = u.phase === 'running' || u.phase === 'waiting' || u.phase === 'stopping';
    $('progCurrent').textContent = active && u.current ? `${u.album} › ${u.current}` : '';
    const wb = $('waitBox');
    if (u.wait_reason && u.wait_until) {
      wb.textContent = `${u.wait_reason} · 다시 시도 ${hhmm(u.wait_until)} (${dur(u.wait_until - Date.now() / 1000)} 남음)`;
      show(wb, true);
    } else show(wb, false);
    const msg = $('progMsg');
    if (u.message && !active) {
      msg.textContent = u.message;
      msg.className = 'msg' + (u.phase === 'done' ? ' ok' : u.phase === 'error' ? ' err' : '');
    } else if (u.message && u.phase === 'stopping') {
      msg.textContent = u.message; msg.className = 'msg';
    } else msg.className = 'msg hidden';
    show($('btnStop'), u.phase === 'running' || u.phase === 'waiting');
    show($('btnFailures'), u.files_failed > 0 && !active);
  }

  function renderLog() {
    if (S.log.seq === logSeq) return;
    logSeq = S.log.seq;
    const box = $('logBox');
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
    box.textContent = S.log.lines.join('\n');
    $('logCount').textContent = S.log.lines.length ? `(${num(S.log.lines.length)}줄)` : '';
    if (atEnd) box.scrollTop = box.scrollHeight;
  }

  // ── 동작 ──
  $('btnClient').addEventListener('click', () => act('client', {}));
  $('btnGuide').addEventListener('click', () => {
    const g = $('guide');
    const open = g.classList.contains('hidden');
    show(g, open);
    $('btnGuide').setAttribute('aria-expanded', String(open));
  });
  $('guide').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-guide]');
    if (b) act('guide', { page: b.dataset.guide });
  });
  $('btnLogin').addEventListener('click', () => act('login', {}));
  $('btnLoginCancel').addEventListener('click', () => act('login_cancel', {}));
  $('btnLogout').addEventListener('click', () => act('logout', {}, '로그아웃했습니다'));
  $('btnRoot').addEventListener('click', () => act('root', {}));
  const rootInput = $('rootInput');
  async function applyRoot() {
    const v = rootInput.value.trim();
    if (!v || (S && v === S.settings.root)) return;
    const r = await act('root', { path: v });
    if (r === undefined && S) rootInput.value = S.settings.root || '';
  }
  rootInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); rootInput.blur(); } });
  rootInput.addEventListener('change', applyRoot);
  $('optRoot').addEventListener('change', (e) => act('options', { include_root: e.target.checked }));
  $('optVideo').addEventListener('change', (e) => act('options', { include_videos: e.target.checked }));
  $('btnScan').addEventListener('click', () => act('scan', {}));
  $('chkAll').addEventListener('change', (e) => act('select', { all: e.target.checked }));
  $('albumBody').addEventListener('change', (e) => {
    const cb = e.target.closest('input[type=checkbox][data-key]');
    if (cb) act('select', { keys: [cb.dataset.key], on: cb.checked });
  });
  $('albumBody').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    if (b.dataset.act === 'open') act('open', { what: 'album', key: b.dataset.key });
    if (b.dataset.act === 'forget') act('forget_album', { key: b.dataset.key }, '다음에 올릴 때 앨범을 새로 만듭니다');
  });
  $('btnStop').addEventListener('click', () => act('stop', {}));
  $('btnData').addEventListener('click', () => act('open', { what: 'data' }));
  $('btnPhotos').addEventListener('click', () => act('open', { what: 'photos' }));

  // 올리기 확인 창
  $('btnUpload').addEventListener('click', async () => {
    let c;
    try { c = await api('confirm'); } catch (e) { toast(e.message, true); return; }
    const rows = [
      ['계정', esc(c.email || '(이메일 모름)')],
      ['앨범', `${num(c.folders)}개 <span class="muted">(새로 만들 앨범 ${num(c.new_albums)}개)</span>`],
      ['파일', `${num(c.files)}개 · ${esc(c.bytes_text)}`],
      ['예상 호출', c.files ? `${num(c.est_requests)}건 <span class="muted">· 하루 1만 건 한도라 약 ${num(c.est_days)}일</span>` : '—'],
    ];
    let html = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
    if (!c.files) html += '<p class="note warn">새로 올릴 파일이 없습니다. 이미 다 올렸거나, 다시 해도 안 될 실패로 빠진 파일만 남았습니다.</p>';
    html += '<ul class="caution">' +
      `<li>원본 화질로 올라가 구글 저장 공간을 <b>${esc(c.bytes_text)}</b>만큼 씁니다. 공간이 모자라면 Gmail·휴대폰 백업도 멈추니 남은 공간을 먼저 확인하세요.</li>` +
      '<li>이 프로그램으로는 올린 사진·앨범을 지울 수 없습니다. 잘못 올렸으면 구글 포토에서 직접 지워야 합니다.</li>' +
      '<li>올리는 동안 PC가 절전에 들지 않게 합니다. 창을 닫으면 멈추고, 다음에 이어서 올립니다.</li></ul>';
    if (c.skipped_perm) html += `<p class="note">다시 해도 안 될 실패로 본 ${num(c.skipped_perm)}개는 빼고 올립니다.</p>`;
    $('cfBody').innerHTML = html;
    $('cfStart').disabled = !c.files;
    show($('confirmModal'), true);
    $('cfStart').focus();
  });
  $('cfCancel').addEventListener('click', () => show($('confirmModal'), false));
  $('cfStart').addEventListener('click', async () => {
    show($('confirmModal'), false);
    await act('start', {});
  });

  // 실패 목록
  $('btnFailures').addEventListener('click', async () => {
    let list;
    try { list = await api('failures'); } catch (e) { toast(e.message, true); return; }
    $('fmBody').innerHTML = list.length ? list.map((f) => `<div>${esc(f.path)}<br><span class="e">${esc(f.error)}</span></div>`).join('') : '<p class="muted">실패한 파일이 없습니다.</p>';
    show($('failModal'), true);
  });
  $('fmClose').addEventListener('click', () => show($('failModal'), false));
  $('fmRetry').addEventListener('click', async () => {
    show($('failModal'), false);
    await act('retry_failed', {}, '다음 올리기 때 다시 시도합니다');
  });

  // 창 닫기 확인(프로그램이 부름)
  $('clKeep').addEventListener('click', () => show($('closeModal'), false));
  $('clQuit').addEventListener('click', () => act('quit', {}));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    ['confirmModal', 'failModal', 'closeModal'].forEach((id) => show($(id), false));
  });
  window.app = { askClose: () => { show($('closeModal'), true); $('clKeep').focus(); } };

  kick();
})();
