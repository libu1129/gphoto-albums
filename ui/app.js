'use strict';
// 화면 동작: 프로그램 안 로컬 서버(/api/*)를 0.7~2초마다 읽어, 왼쪽 단계 목록과 지금 단계 화면을 그린다.
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

  // timeoutMs: 주기적으로 읽는 요청에만 준다(폴더·파일 고르기 창을 띄우는 요청은 사람이 고를 때까지 기다린다)
  async function api(path, body, timeoutMs) {
    const opt = { method: body === undefined ? 'GET' : 'POST', headers: { 'X-App-Token': TOKEN } };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const ctrl = timeoutMs ? new AbortController() : null;
    const tm = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
    if (ctrl) opt.signal = ctrl.signal;
    try {
      let r;
      try { r = await fetch('/api/' + path, opt); } catch (e) { throw new Error('프로그램과 연결이 끊겼습니다'); }
      let j;
      try { j = await r.json(); } catch (e) { throw new Error('응답을 읽지 못했습니다'); }
      if (!j.ok) throw new Error(j.error || '실패했습니다');
      return j.data;
    } finally {
      if (tm) clearTimeout(tm);
    }
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

  // ── 단계 ──
  const STEPS = {
    1: ['계정 연결', '사진을 올릴 구글 계정을 연결합니다. 처음 한 번만 하면 됩니다.'],
    2: ['사진 폴더', '사진이 든 최상위 폴더를 고릅니다. 그 아래 폴더마다 앨범이 하나씩 생깁니다.'],
    3: ['앨범 고르기', '만들 앨범을 살펴보고 올릴 것을 고릅니다. 이 단계까지는 아무것도 올라가지 않습니다.'],
    4: ['올리기', '올리는 동안 다른 단계를 봐도 됩니다. 창을 닫으면 멈추고, 같은 앨범을 다시 올리면 이어서 올립니다.'],
  };
  const ACTIVE = new Set(['running', 'waiting', 'stopping']);
  let step = 0;   // 0: 첫 상태를 받기 전

  let tableScroll = 0;   // 3단계를 떠날 때 표 스크롤 위치(돌아오면 되살린다)
  function go(n) {
    n = Math.max(1, Math.min(4, Number(n) || 1));
    if (n === step) return;
    if (step === 3) tableScroll = $('tableWrap').scrollTop;
    step = n;
    try { history.replaceState(null, '', location.pathname + location.search + '#step=' + n); } catch (e) { /* 주소를 못 바꿔도 화면은 넘긴다 */ }
    document.querySelectorAll('section.page').forEach((p) => { p.hidden = Number(p.dataset.step) !== n; });
    if (n === 3) $('tableWrap').scrollTop = tableScroll;
    $('stepCrumb').textContent = `단계 ${n} / 4`;
    $('stepTitle').textContent = STEPS[n][0];
    $('stepDesc').textContent = STEPS[n][1];
    if (n === 4) requestAnimationFrame(() => { const b = $('logBox'); b.scrollTop = b.scrollHeight; });
    if (S) render();
  }
  const hashStep = () => { const m = /step=([1-4])/.exec(location.hash); return m ? Number(m[1]) : 0; };
  function stepAfterAccount() {
    if (!S.settings.root) return 2;
    return S.plan && !S.plan.stale ? 3 : 2;
  }
  function firstStep() {
    if (ACTIVE.has(S.upload.phase)) return 4;
    if (!S.client.ok || !S.account.logged_in) return 1;
    return stepAfterAccount();
  }

  // ── 상태 읽기 ──
  let S = null;
  let A = { version: -1, items: [] };
  let tableKey = '';
  let timer = null;
  let inflight = false;
  let logSeq = -1;
  let prevLogin = null;
  let reqSeq = 0;            // 상태 읽기 차례 번호
  let kicked = false;        // 읽는 도중에 동작이 있었다: 끝나는 대로 바로 다시 읽는다
  let renderFailed = false;
  let scanWatch = null;      // 이 화면에서 시작한 훑기: 끝나면 3단계로 넘긴다
  let guideOpen = null;      // 열쇠 안내를 펴 둘지(사용자가 고름). null이면 열쇠·로그인이 다 될 때까지 저절로 편다
  let gView = 0;             // 안내에서 보고 있는 단계(1~8)
  let gFarPrev = 0;          // 지난번 그릴 때의 차례 단계(바뀌면 그 단계를 보여 준다)
  let gHelpOn = false;       // '막히면' 펼침
  const sticky = new Set();  // '선택한 것' 보기에서 방금 선택을 끈 행(보기를 바꿀 때까지 남긴다)

  async function refresh() {
    if (inflight) return;
    inflight = true;
    const seq = ++reqSeq;
    let s, k, a = null;
    try {
      s = await api('state', undefined, 15000);
      // 앨범 표는 이 값이 바뀔 때만 다시 받는다(선택은 개수·파일 수·용량, 올리는 중이면 체크 칸을 잠그므로 그 여부도)
      k = [s.plan ? s.plan.version : 0, s.upload.status_version, s.account.email || '', s.selection.count,
        s.selection.files, s.selection.bytes, s.uploading ? 1 : 0].join(':');
      if (k !== tableKey) a = await api('albums', undefined, 15000);
    } catch (e) {
      show($('offline'), true);
      return;
    } finally {
      inflight = false;
    }
    show($('offline'), false);
    S = s;
    if (a) A = a;
    try {
      transitions(seq);
      if (!step) go(hashStep() || firstStep());
      render();
      if (a) { renderTable(); renderAlbumProg(); renderFooter(); tableKey = k; }
      renderFailed = false;
    } catch (e) {   // 화면 그리기 오류는 연결 끊김과 따로 알린다(한 번만)
      console.error(e);
      if (!renderFailed) toast(`화면을 그리다 오류가 났습니다: ${e.message}`, true);
      renderFailed = true;
    }
  }
  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      kicked = false;
      await refresh();
      schedule(kicked ? 40 : (S && S.busy ? 700 : 2000));
    }, ms);
  }
  function kick() {
    kicked = true;
    if (!inflight) schedule(40);
  }

  // 로그인이 끝나거나 이 화면에서 시작한 훑기가 끝나면 다음 단계로 넘긴다
  function transitions(seq) {
    const lg = S.login.phase;
    if (prevLogin === 'waiting' && lg !== 'waiting' && S.account.logged_in) {
      toast(`로그인했습니다: ${S.account.email || ''}`);
      if (step === 1) go(stepAfterAccount());
    }
    prevLogin = lg;
    // 훑기 요청이 받아들여진 뒤에 시작한 읽기만 본다(그 전에 나간 읽기는 훑기 전 상태라 판단에서 뺀다)
    if (scanWatch && seq > scanWatch.after && S.scan.phase !== 'scanning') {
      const fresh = S.plan && S.plan.version !== scanWatch.from && S.scan.phase === 'done';
      scanWatch = null;
      if (fresh) {
        if (step === 2) go(3);
        else toast(`미리보기를 만들었습니다: 앨범 ${num(S.plan.folders)}개`);
      }
    }
  }

  // ── 그리기 ──
  // 보낸 바이트 기준 진행률. 보내던 파일이 기다림(429 등)에 걸리면 받아들여지기 전 바이트도 들어 있어,
  // 다 끝나기 전에는 100%로 보이지 않게 99%에서 멈춘다.
  function upPct(u) {
    if (u.phase === 'done') return 100;
    const handled = u.files_done + u.files_failed + u.files_skipped;
    const v = u.bytes_total > 0 ? 100 * u.bytes_done / u.bytes_total : (u.files_total ? 100 * handled / u.files_total : 0);
    return Math.max(0, Math.min(99, v));
  }

  function render() {
    renderNav();
    renderAccount();
    renderFolder();
    renderPreview();
    renderProgress();
    renderLog();
    renderFooter();
    $('footVersion').textContent = `버전 ${S.version}`;
    $('footVersion').title = `자료 폴더: ${S.data_dir}`;
  }

  function renderNav() {
    const c = S.client, a = S.account, p = S.plan, u = S.upload, st = S.settings;
    const active = ACTIVE.has(u.phase);
    const pct = upPct(u);
    const states = {
      1: c.ok && a.logged_in ? 'done' : (a.problem || S.login.phase === 'error') ? 'warn' : '',
      2: S.scan.phase === 'scanning' ? 'run' : (st.root && p && !p.stale) ? 'done'
        : (S.scan.phase === 'error' || (p && p.stale)) ? 'warn' : '',
      3: p && !p.stale && S.selection.count ? 'done' : '',
      4: active ? 'run' : (u.phase === 'done' && !u.files_failed) ? 'done'
        : (u.phase === 'error' || u.phase === 'stopped' || (u.phase === 'done' && u.files_failed)) ? 'warn' : '',
    };
    const subs = {
      1: a.logged_in ? (a.email || '로그인됨') : a.problem ? '다시 로그인하세요' : !c.ok ? '열쇠 파일 필요' : '로그인하세요',
      2: S.scan.phase === 'scanning' ? `훑는 중… 파일 ${num(S.scan.scanned)}개` : st.root ? (st.root_label || st.root) : '폴더를 고르세요',
      3: !p ? '미리보기 없음' : p.stale ? '미리보기를 다시 만드세요' : `앨범 ${num(p.folders)}개 · 선택 ${num(S.selection.count)}개`,
      4: ({ idle: '시작 전', running: `올리는 중 ${Math.floor(pct)}%`, waiting: `기다리는 중 ${Math.floor(pct)}%`, stopping: '멈추는 중…',
        stopped: `멈춤 · ${Math.floor(pct)}%`, done: u.files_failed ? `끝 · 실패 ${num(u.files_failed)}개` : '다 올렸습니다', error: '오류로 멈춤' })[u.phase] || '',
    };
    document.querySelectorAll('.nav-item').forEach((b) => {
      const n = Number(b.dataset.step);
      b.classList.toggle('on', n === step);
      if (n === step) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
      ['done', 'warn', 'run'].forEach((k) => b.classList.toggle(k, states[n] === k));
      $('navSub' + n).textContent = subs[n];
      b.title = `${n}. ${STEPS[n][0]} — ${subs[n]}`;
    });
    $('navSub2').title = st.root || '';
    show($('navProg'), active);
    $('navFill').style.width = pct.toFixed(1) + '%';
  }

  function renderAccount() {
    const c = S.client, a = S.account, lg = S.login;
    const waiting = lg.phase === 'waiting';
    $('tileClient').className = 'tile' + (c.ok ? ' ok' : c.error ? ' bad' : '');
    $('clientText').innerHTML = c.ok
      ? `등록됨 <span class="muted">· 프로젝트 ${esc(c.project || '?')}</span>`
      : (c.error ? `<span class="muted">${esc(c.error)}</span>` : '아직 없음 <span class="muted">— 아래 순서대로 만든 뒤 고르세요</span>');
    $('btnClient').textContent = c.ok ? '다른 열쇠 파일로' : '열쇠 파일 선택';
    $('btnClient').disabled = S.uploading;
    const guideOn = !c.ok || (guideOpen === null ? !a.logged_in : guideOpen);
    show($('guide'), guideOn);
    show($('btnGuide'), c.ok);
    $('btnGuide').textContent = guideOn ? '만드는 법 접기' : '만드는 법 보기';
    $('btnGuide').setAttribute('aria-expanded', String(guideOn));

    $('tileLogin').className = 'tile' + (waiting ? ' wait' : a.logged_in ? ' ok' : a.problem ? ' bad' : '');
    if (waiting) $('loginText').innerHTML = '브라우저에서 구글 로그인을 마쳐 주세요… <span class="muted">(5분 안에)</span>';
    else if (a.logged_in) $('loginText').innerHTML = `<b>${esc(a.email || '(이메일 모름)')}</b> <span class="muted">에 올립니다</span>`;
    else $('loginText').textContent = c.ok ? '로그인 안 됨' : '열쇠 파일을 먼저 고르세요';
    show($('btnLogin'), !a.logged_in && !waiting);
    $('btnLogin').disabled = !c.ok || S.uploading;
    show($('btnLoginCancel'), waiting);
    show($('btnLogout'), a.logged_in && !waiting);
    $('btnLogout').disabled = S.uploading;
    const note = $('loginNote');
    if (lg.phase === 'error' && lg.error) { note.className = 'note err'; note.textContent = lg.error; }
    else if (a.problem) { note.className = 'note warn'; note.textContent = a.problem; }
    else if (a.logged_in && !a.read_scope) { note.className = 'note warn'; note.textContent = "'앱이 만든 앨범 보기' 권한을 받지 못했습니다. 올리기는 되지만, 구글 포토에서 지운 앨범을 알아채지 못할 수 있습니다."; }
    else note.className = 'note hidden';
    $('accountChip').classList.toggle('on', a.logged_in);
    $('accountText').textContent = a.logged_in ? (a.email || '로그인됨') : '로그인 안 됨';
    $('accountChip').title = a.logged_in ? `${a.email || ''} 계정에 올립니다` : '1단계에서 로그인하세요';
    if (guideOn) renderGuide();
  }

  // ── 열쇠 만드는 법 ──
  // 차례(가장 먼 단계)는 앱이 아는 상태가 먼저다: 로그인했으면 다 끝남(9), 열쇠가 있으면 로그인 차례(8),
  // 그 밖에는 사용자가 '마쳤어요'로 간 단계(settings.guide_step, 열쇠 고르기 앞인 7까지).
  function guideFar() {
    if (S.account.logged_in) return 9;
    if (S.client.ok) return 8;
    return Math.max(1, Math.min(7, Number(S.settings.guide_step) || 1));
  }
  function renderGuide() {
    const far = guideFar();
    if (far !== gFarPrev || !gView) { gView = Math.min(far, 8); gFarPrev = far; gHelpOn = false; }
    document.querySelectorAll('#guideList .g-item').forEach((b) => {
      const n = Number(b.dataset.n);
      b.classList.toggle('done', n < far);
      b.classList.toggle('cur', n === far);
      b.classList.toggle('view', n === gView);
      if (n === gView) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
      // 화면 읽기 프로그램에도 끝남·차례가 들리게(체크 표시는 색과 모양뿐이라)
      b.setAttribute('aria-label', `${n}. ${b.querySelector('.g-name').textContent} — ${n < far ? '끝남' : n === far ? '지금 차례' : '남음'}`);
    });
    document.querySelectorAll('#guide .g-step').forEach((el) => {
      const on = Number(el.dataset.n) === gView;
      el.hidden = !on;
      const help = el.querySelector('.g-help');
      if (help) help.hidden = !(on && gHelpOn);
    });
    $('gHelp').textContent = gHelpOn ? '도움말 접기' : '막히면';
    $('gHelp').setAttribute('aria-expanded', String(gHelpOn));
    $('guideCount').textContent = far > 8 ? '8 / 8 · 다 했습니다' : `${far} / 8 단계 차례`;
    $('gPrev').disabled = gView <= 1;
    show($('gNext'), gView <= 6);   // 7·8은 앱이 열쇠·로그인을 알아채 저절로 넘어간다
    $('gNext').textContent = gView === 6 ? '마쳤어요, 열쇠 고르기 →' : '마쳤어요, 다음 →';
    document.querySelectorAll('#guide [data-proxy]').forEach((b) => {
      const src = $(b.dataset.proxy);
      b.disabled = src.disabled || src.classList.contains('hidden');
    });
  }
  function guideGo(n) {
    gView = Math.max(1, Math.min(8, n));
    gHelpOn = false;
    renderGuide();
    $('guideScroll').scrollTop = 0;
  }

  // 복사: 클립보드 API → 안 되면 숨은 칸 + execCommand → 그래도 안 되면 글자를 골라 두고 Ctrl+C 안내
  async function copyText(text, el) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { /* 아래 방법으로 */ }
    if (!ok) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      ta.remove();
    }
    if (ok) { toast('복사했습니다'); return; }
    if (!el) { toast(`복사하지 못했습니다. 직접 입력하세요: ${text}`, true); return; }
    const r = document.createRange();
    r.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    toast('복사하지 못했습니다. 골라 둔 글자를 Ctrl+C로 복사하세요', true);
  }

  function renderFolder() {
    const st = S.settings;
    const inp = $('rootInput');
    if (document.activeElement !== inp) inp.value = st.root || '';
    $('optRoot').checked = !!st.include_root;
    $('optVideo').checked = !!st.include_videos;
    const scanning = S.scan.phase === 'scanning';
    const lockIt = S.uploading || scanning;
    inp.disabled = lockIt; $('btnRoot').disabled = lockIt; $('optRoot').disabled = lockIt; $('optVideo').disabled = lockIt;
    const lbl = st.root_label || '사진';
    const ex = st.include_root ? `${lbl}_2023_제주여행` : '2023_제주여행';
    $('example').innerHTML = `<code>${esc(lbl)}\\2023\\제주여행</code> 폴더 → 앨범 <b>${esc(ex)}</b><br>` +
      `<code>${esc(lbl)}</code> 바로 아래 파일 → 앨범 <b>${esc(lbl)}</b>`;
    let cls = '', text;
    if (scanning) { cls = 'run'; text = `훑는 중… 파일 ${num(S.scan.scanned)}개 · ${S.scan.current || ''}`; }
    else if (S.scan.phase === 'error') { cls = 'err'; text = S.scan.error || '폴더를 훑지 못했습니다.'; }
    else if (S.plan && S.plan.stale) { cls = 'warn'; text = '설정이 바뀌었습니다. 미리보기를 다시 만드세요.'; }
    else if (S.plan) { cls = 'ok'; text = `미리보기를 만들었습니다: 앨범 ${num(S.plan.folders)}개 · 파일 ${num(S.plan.files)}개 · ${bytes(S.plan.bytes)} (${S.plan.elapsed}초 걸림)`; }
    else text = '미리보기는 PC 안에서만 폴더를 훑습니다. 아무것도 올라가지 않습니다.';
    $('scanBox').className = 'callout' + (cls ? ' ' + cls : '');
    show($('scanSpin'), scanning);
    $('scanText').textContent = text;
  }

  function renderPreview() {
    const p = S.plan;
    show($('previewEmpty'), !p);
    show($('previewBody'), !!p);
    if (!p) return;
    show($('staleBox'), !!p.stale);
    $('btnStaleRescan').disabled = S.uploading || S.scan.phase === 'scanning' || !S.settings.root;
    const stat = (v, k, tip) => `<div class="stat"${tip ? ` title="${esc(tip)}"` : ''}><b>${v}</b><small>${k}</small></div>`;
    $('stats').innerHTML = [
      stat(num(p.folders), '앨범'), stat(num(p.photos), '사진'), stat(num(p.videos), '영상'),
      stat(bytes(p.bytes), '용량', '원본 화질로 올라가 구글 저장 공간을 이만큼 씁니다'),
      stat(`${num(p.est_requests)}건`, `예상 호출 · 약 ${p.est_days}일`, '구글 프로젝트당 하루 10,000건 한도'),
    ].join('');
    const nSkip = Object.values(p.skipped || {}).reduce((x, y) => x + y, 0);
    const sk = $('btnSkipped');
    sk.textContent = (nSkip || p.n_errors)
      ? `올리지 않는 파일 ${num(nSkip)}개${p.n_errors ? ` · 읽지 못한 곳 ${num(p.n_errors)}개` : ''} 보기` : '올리지 않는 파일 없음';
    sk.disabled = !(nSkip || p.n_errors);
    sk.classList.toggle('warn', !!p.n_errors);
    $('albumFilter').disabled = $('albumView').disabled = false;
    renderSelection();
  }

  // ── 앨범 표 ──
  const itemPath = (it, lbl) => (it.key ? `${lbl}\\${it.key.split('/').join('\\')}` : `${lbl} (바로 아래 파일)`);
  const isFiltered = () => $('albumFilter').value.trim() !== '' || $('albumView').value !== 'all';
  function viewMatch(it, v) {
    const failed = (it.failed || 0) + (it.failed_perm || 0);
    switch (v) {
      case 'selected': return it.selected;
      case 'new': return !it.done && !(it.album_titles && it.album_titles.length);
      case 'partial': return it.done > 0 && it.done < it.files;
      case 'done': return it.files > 0 && it.done >= it.files;
      case 'failed': return failed > 0;
      case 'warn': return !!((it.warnings && it.warnings.length) || it.missing);
      default: return true;
    }
  }
  function visibleItems() {
    const q = $('albumFilter').value.trim().toLowerCase();
    const v = $('albumView').value;
    const lbl = (S && S.settings.root_label) || '';
    return A.items.filter((it) => {
      if (q && !(it.title.toLowerCase().includes(q) || itemPath(it, lbl).toLowerCase().includes(q))) return false;
      return viewMatch(it, v) || sticky.has(it.key);
    });
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

  // resetScroll: 거르기·보기를 바꿨을 때 맨 위부터 보인다. 다시 그려도 체크 칸의 키보드 초점은 지킨다.
  function renderTable(resetScroll) {
    const body = $('albumBody');
    const wrap = $('tableWrap');
    const top = resetScroll ? 0 : wrap.scrollTop;
    const ae = document.activeElement;
    const focusKey = ae && ae.matches && ae.matches('#albumBody input[data-key]') ? ae.dataset.key : null;
    const lbl = (S && S.settings.root_label) || '';
    const lock = S && S.uploading;
    const rows = visibleItems();
    body.innerHTML = rows.map((it) => {
      const extra = [];
      if (it.warnings && it.warnings.length) extra.push(`<div class="w">⚠ ${it.warnings.map(esc).join(' · ')}</div>`);
      if (it.album_titles && it.album_titles.length && it.album_titles[0] !== it.title) extra.push(`<div class="m">구글 앨범: ${it.album_titles.map(esc).join(', ')}</div>`);
      return `<tr class="${it.selected ? 'sel' : ''}">` +
        `<td class="c-chk"><input type="checkbox" aria-label="${esc(it.title)} 선택" data-key="${esc(it.key)}" ${it.selected ? 'checked' : ''} ${lock ? 'disabled' : ''}></td>` +
        `<td><div class="t">${esc(it.title)}</div><div class="p">${esc(itemPath(it, lbl))}</div>${extra.join('')}</td>` +
        `<td class="n">${num(it.photos)}</td><td class="n">${num(it.videos)}</td><td class="n">${bytes(it.bytes)}</td>` +
        `<td>${statusHtml(it)}</td></tr>`;
    }).join('');
    show($('tableEmpty'), A.items.length > 0 && rows.length === 0);
    wrap.scrollTop = top;
    if (focusKey !== null) {
      const el = body.querySelector(`input[data-key="${CSS.escape(focusKey)}"]`);
      if (el) el.focus({ preventScroll: true });
    }
    renderSelection(rows);
  }

  function renderSelection(rows) {
    if (!S || !S.plan) return;
    rows = rows || visibleItems();
    const nVisSel = rows.filter((it) => it.selected).length;
    const all = $('chkAll');
    all.checked = rows.length > 0 && nVisSel === rows.length;
    all.indeterminate = nVisSel > 0 && nVisSel < rows.length;
    all.disabled = S.uploading || rows.length === 0;
    $('chkAllText').textContent = isFiltered() ? `보이는 ${num(rows.length)}개 모두 선택` : '모두 선택';
    const sel = S.selection;
    const hidden = Math.max(0, A.items.filter((it) => it.selected).length - nVisSel);
    $('selText').textContent = sel.count
      ? `선택 ${num(sel.count)}개${hidden ? `(안 보이는 ${num(hidden)}개 포함)` : ''} · 파일 ${num(sel.files)}개 · ${bytes(sel.bytes)}`
      : '선택 없음';
  }

  // ── 올리기 ──
  function renderProgress() {
    const u = S.upload;
    show($('uploadEmpty'), u.phase === 'idle');
    show($('uploadBody'), u.phase !== 'idle');
    if (u.phase === 'idle') return;
    const titles = { running: '올리는 중', waiting: '기다리는 중', stopping: '멈추는 중', stopped: '멈춤', done: '다 올렸습니다', error: '오류로 멈춤' };
    $('progTitle').textContent = titles[u.phase] || '올리기';
    const pct = upPct(u);
    $('progPct').textContent = `${Math.floor(pct)}%`;
    const fill = $('progFill');
    fill.style.width = pct.toFixed(1) + '%';
    fill.className = 'fill' + (u.phase === 'done' && !u.files_failed ? ' ok' : '');
    const active = ACTIVE.has(u.phase);
    const going = u.phase === 'running' || u.phase === 'waiting';
    const stat = (v, k, cls) => `<div class="stat"><b>${v}</b><small${cls ? ` class="${cls}"` : ''}>${k}</small></div>`;
    const extra = [u.files_failed ? `실패 ${num(u.files_failed)}` : '', u.files_skipped ? `건너뜀 ${num(u.files_skipped)}` : ''].filter(Boolean);
    const speed = u.speed ? ` · ${bytes(u.speed)}/초` : '';
    $('progGrid').innerHTML = [
      stat(`${num(u.files_done)} / ${num(u.files_total)}`, ['올린 파일', ...extra].join(' · '), u.files_failed ? 'w' : ''),
      stat(bytes(u.bytes_done), `보낸 용량 / ${bytes(u.bytes_total)}`),
      going ? stat(u.eta != null ? `약 ${dur(u.eta)}` : '—', `남은 시간${speed}`)
        : stat(u.elapsed ? dur(u.elapsed) : '—', `걸린 시간${speed}`),
      stat(num(u.requests), '이번 호출 수'),
    ].join('');
    show($('progCurrent'), active);
    $('progCurrent').textContent = active && u.current ? `${u.album} › ${u.current}` : '';
    const wb = $('waitBox');
    if (u.wait_reason && u.wait_until) {
      wb.textContent = `${u.wait_reason} · 다시 시도 ${hhmm(u.wait_until)} (${dur(u.wait_until - Date.now() / 1000)} 남음)`;
      show(wb, true);
    } else show(wb, false);
    const msg = $('progMsg');
    if (u.message && (!active || u.phase === 'stopping')) {
      msg.textContent = u.message;
      msg.className = 'msg' + (u.phase === 'done' && !u.files_failed ? ' ok' : u.phase === 'error' ? ' err' : '');
    } else msg.className = 'msg hidden';
  }

  // 4단계 앨범별 진행: 이번 올리기에 든 앨범(live 상태가 있는 것)
  const LIVE = {
    queued: ['', '대기'], uploading: ['run', '올리는 중'], done: ['ok', '완료'], partial: ['part', '일부 실패'],
    stopped: ['', '멈춤'], missing: ['err', '구글에서 지워짐'],
  };
  function renderAlbumProg() {
    const items = A.items.filter((it) => it.live != null);
    const box = $('albumProg');
    if (!items.length) {
      box.innerHTML = '<p class="muted">이번에 올리는 앨범이 여기에 보입니다.</p>';
      $('albumProgCount').textContent = '';
      return;
    }
    const top = box.scrollTop;
    const finished = items.filter((it) => ['done', 'partial', 'missing'].includes(it.live)).length;
    $('albumProgCount').textContent = `${num(items.length)}개 중 ${num(finished)}개 끝`;
    box.innerHTML = items.map((it) => {
      const [cls, label] = LIVE[it.live] || ['', it.live];
      const ratio = it.files ? Math.min(100, 100 * it.done / it.files) : 0;
      const failed = (it.failed || 0) + (it.failed_perm || 0);
      const open = it.album_url && (it.live === 'done' || it.live === 'partial')
        ? `<button class="link" data-act="open" data-key="${esc(it.key)}">열기</button>` : '';
      return `<div class="row" data-key="${esc(it.key)}"><span class="t" title="${esc(it.title)}">${esc(it.title)}</span>` +
        `<span class="n">${num(it.done)}/${num(it.files)}${failed ? ` · 실패 ${num(failed)}` : ''} <span class="pill ${cls}">${esc(label)}</span>${open}</span>` +
        `<span class="bar"><span class="fill${it.live === 'done' ? ' ok' : ''}" style="width:${ratio.toFixed(1)}%"></span></span></div>`;
    }).join('');
    box.scrollTop = top;
  }

  function renderLog() {
    if (S.log.seq === logSeq) return;
    logSeq = S.log.seq;
    const box = $('logBox');
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
    box.textContent = S.log.lines.join('\n');
    $('logCount').textContent = S.log.lines.length ? `${num(S.log.lines.length)}줄` : '';
    if (atEnd) box.scrollTop = box.scrollHeight;
  }

  // ── 아래 막대: 단계마다 주 버튼과 못 누르는 까닭 ──
  const FOOT_BUTTONS = ['btnRescan', 'btnFailures', 'btnStop', 'btnResume', 'btnOpenPhotos', 'btnScan', 'btnUpload', 'btnNext'];
  function retryableFailed() {
    return A.items.filter((it) => it.live != null).reduce((n, it) => n + (it.failed || 0), 0);
  }
  function renderFooter() {
    if (!S) return;
    const u = S.upload, p = S.plan, st = S.settings, a = S.account;
    const active = ACTIVE.has(u.phase);
    const scanning = S.scan.phase === 'scanning';
    const on = {};
    let hint = '', warn = false, showSel = false;
    $('btnPrev').style.visibility = step > 1 ? 'visible' : 'hidden';
    $('btnNext').classList.remove('primary');
    if (step === 1) {
      on.btnNext = true;
      if (S.client.ok && a.logged_in) $('btnNext').classList.add('primary');
      hint = !S.client.ok ? '열쇠 파일이 있어야 로그인할 수 있습니다. 미리보기는 로그인 없이도 만들 수 있습니다.'
        : !a.logged_in ? '로그인은 올리기 전에만 있으면 됩니다. 미리보기는 로그인 없이도 만들 수 있습니다.' : '';
    } else if (step === 2) {
      const fresh = p && !p.stale;
      if (scanning || !fresh) {
        on.btnScan = true;
        $('btnScan').textContent = scanning ? '훑는 중…' : '미리보기 만들기 →';
        $('btnScan').disabled = !st.root || scanning || S.uploading;
      } else {
        on.btnRescan = true;
        $('btnRescan').disabled = S.uploading;
        on.btnNext = true;
        $('btnNext').classList.add('primary');
      }
      hint = S.uploading ? '올리는 중에는 폴더와 설정을 바꿀 수 없습니다.' : !st.root ? '사진이 든 최상위 폴더를 고르세요.' : '';
    } else if (step === 3) {
      if (p && !active) {
        on.btnUpload = true;
        const why = !a.logged_in ? (a.problem || '올리려면 1단계에서 구글에 로그인하세요.')
          : p.stale ? '설정이 바뀌었습니다. 미리보기를 다시 만드세요.'
          : !S.selection.count ? '올릴 앨범을 고르세요. 처음에는 1~2개로 시험해 보길 권합니다.' : '';
        $('btnUpload').disabled = !!why || S.uploading;
        hint = why;
        warn = !!why && (!a.logged_in || p.stale);
      } else if (active) hint = '올리는 중에는 선택을 바꿀 수 없습니다.';
      showSel = !!p;
    } else if (step === 4) {
      const failed = u.files_failed > 0;
      if (u.phase === 'running' || u.phase === 'waiting' || u.phase === 'stopping') {
        on.btnStop = true;
        $('btnStop').disabled = u.phase === 'stopping';
        $('btnStop').textContent = u.phase === 'stopping' ? '멈추는 중…' : '멈추기';
        hint = '창을 닫으면 멈춥니다.';
      }
      if (!active && failed) on.btnFailures = true;
      const retry = u.phase === 'done' ? retryableFailed() : 0;
      if (u.phase === 'stopped' || u.phase === 'error' || retry > 0) {
        on.btnResume = true;
        $('btnResume').textContent = u.phase === 'done' ? `실패한 ${num(retry)}개 다시 올리기` : '이어 올리기';
        const why = !a.logged_in ? (a.problem || '다시 올리려면 1단계에서 로그인하세요.')
          : (!p || p.stale) ? '미리보기를 다시 만든 뒤 앨범을 고르세요.'
          : !S.selection.count ? '3단계에서 올릴 앨범을 다시 고르세요.' : '';
        $('btnResume').disabled = !!why;
        hint = why; warn = !!why;
      }
      if (u.phase === 'done') {
        on.btnOpenPhotos = true;
        $('btnOpenPhotos').classList.toggle('primary', !on.btnResume);   // 다시 올릴 실패가 있으면 그쪽이 주 버튼
        if (failed && !on.btnResume) hint = '남은 실패는 다시 해도 안 될 것으로 본 것입니다. 실패 목록에서 까닭을 보세요.';
      }
    }
    FOOT_BUTTONS.forEach((id) => show($(id), !!on[id]));
    const mini = $('miniProg');
    const showMini = active && step !== 4;
    show(mini, showMini);
    if (showMini) {
      const pct = upPct(u);
      mini.innerHTML = `<span>${u.phase === 'waiting' ? '기다리는 중' : u.phase === 'stopping' ? '멈추는 중' : '올리는 중'} ${Math.floor(pct)}%</span>` +
        `<span class="bar"><span class="fill" style="width:${pct.toFixed(1)}%"></span></span>`;
    }
    show($('selText'), showSel);
    const h = $('footHint');
    h.textContent = hint;
    h.title = hint;
    h.classList.toggle('warn', warn);
  }

  // ── 창(모달) ──
  const MODALS = ['confirmModal', 'failModal', 'skipModal', 'closeModal'];
  let lastFocus = null;
  const modalOpen = () => MODALS.some((id) => !$(id).classList.contains('hidden'));
  function openModal(id, focusId) {
    if (!modalOpen()) lastFocus = document.activeElement;
    show($(id), true);
    const f = $(focusId);
    if (f) f.focus();
  }
  function closeModal(id) {
    show($(id), false);
    if (!modalOpen() && lastFocus && document.contains(lastFocus) && !lastFocus.disabled) lastFocus.focus();
  }

  async function openConfirm(mode) {
    let c;
    try { c = await api('confirm'); } catch (e) { toast(e.message, true); return; }
    const resume = mode === 'resume';
    $('cfTitle').textContent = resume ? '이어서 올릴까요?' : '구글 포토에 올릴까요?';
    $('cfStart').textContent = resume ? '이어 올리기' : '올리기 시작';
    const rows = [
      ['계정', esc(c.email || '(이메일 모름)')],
      ['앨범', `${num(c.folders)}개 <span class="muted">(새로 만들 앨범 ${num(c.new_albums)}개)</span>`],
      [resume ? '남은 파일' : '파일', `${num(c.files)}개 · ${esc(c.bytes_text)}`],
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
    // 올린 것은 이 프로그램으로 못 지우니, Enter를 누르고 있다가 바로 시작되지 않게 초점은 '취소'에 둔다
    openModal('confirmModal', 'cfCancel');
  }

  function openSkipped() {
    const p = S && S.plan;
    if (!p) return;
    const parts = Object.entries(p.skipped || {}).map(([k, v]) => {
      if (k === '지원하지 않는 형식' && p.skipped_ext.length) {
        return `<li>${esc(k)} ${num(v)}개 <span class="muted">(${p.skipped_ext.map(([e, n]) => `${esc(e || '확장자 없음')} ${num(n)}`).join(', ')})</span></li>`;
      }
      return `<li>${esc(k)} ${num(v)}개</li>`;
    });
    let html = '';
    if (parts.length) html += `<h4>까닭별</h4><ul>${parts.join('')}</ul>`;
    if (p.examples && p.examples.length) {
      html += `<h4>올리지 않는 파일 (앞 ${num(p.examples.length)}개)</h4><ul>${p.examples.map(([r, why]) => `<li>${esc(r)} <span class="muted">— ${esc(why)}</span></li>`).join('')}</ul>`;
    }
    if (p.n_errors) html += `<h4 class="w">읽지 못한 곳 ${num(p.n_errors)}개</h4><ul>${p.errors.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>`;
    $('skBody').innerHTML = html || '<p class="muted">올리지 않는 파일이 없습니다.</p>';
    openModal('skipModal', 'skClose');
  }

  async function startScan() {
    const w = { from: S && S.plan ? S.plan.version : -1, after: Infinity };
    scanWatch = w;
    if (await act('scan', {}) === undefined) {
      if (scanWatch === w) scanWatch = null;
      return;
    }
    w.after = reqSeq;   // 서버가 훑기를 '훑는 중'으로 바꾼 뒤다. 이 뒤에 시작하는 읽기부터 본다
  }

  // ── 동작 ──
  document.querySelectorAll('.nav-item').forEach((b) => b.addEventListener('click', () => go(b.dataset.step)));
  document.querySelectorAll('[data-go]').forEach((b) => b.addEventListener('click', () => go(b.dataset.go)));
  $('btnPrev').addEventListener('click', () => go(step - 1));
  $('btnNext').addEventListener('click', () => go(step + 1));
  $('miniProg').addEventListener('click', () => go(4));

  $('btnClient').addEventListener('click', () => act('client', {}));
  $('btnGuide').addEventListener('click', () => {
    guideOpen = $('guide').classList.contains('hidden');
    if (S) renderAccount();
  });
  $('guide').addEventListener('click', (e) => {
    const g = e.target.closest('button[data-guide]');
    if (g) { act('guide', { page: g.dataset.guide }); return; }
    const p = e.target.closest('button[data-proxy]');
    if (p) { $(p.dataset.proxy).click(); return; }
    const c = e.target.closest('button[data-copy], button[data-copy-text]');
    if (c) {
      const src = c.dataset.copy ? $(c.dataset.copy) : null;
      copyText(src ? src.textContent : c.dataset.copyText, src);
    }
  });
  $('guideList').addEventListener('click', (e) => {
    const b = e.target.closest('.g-item');
    if (b) guideGo(Number(b.dataset.n));
  });
  $('gPrev').addEventListener('click', () => guideGo(gView - 1));
  $('gNext').addEventListener('click', () => {
    const next = gView + 1;
    const save = next > guideFar();
    guideGo(next);
    // 7단계부터는 이 버튼이 숨으므로, 키보드로 누른 사람의 초점을 지금 단계 목록 줄로 옮긴다
    if ($('gNext').classList.contains('hidden')) document.querySelector('#guideList .g-item.view').focus();
    if (save) act('guide_step', { step: next });   // 차례를 앞으로 옮겨 다시 열어도 여기서 시작
  });
  $('gHelp').addEventListener('click', () => { gHelpOn = !gHelpOn; renderGuide(); });
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
  $('btnScan').addEventListener('click', startScan);
  $('btnRescan').addEventListener('click', startScan);
  $('btnStaleRescan').addEventListener('click', startScan);

  $('chkAll').addEventListener('change', (e) => {
    if (!isFiltered()) act('select', { all: e.target.checked });
    else act('select', { keys: visibleItems().map((it) => it.key), on: e.target.checked });
  });
  $('albumFilter').addEventListener('input', () => { sticky.clear(); renderTable(true); });
  $('albumView').addEventListener('change', () => { sticky.clear(); renderTable(true); });
  $('albumBody').addEventListener('change', (e) => {
    const cb = e.target.closest('input[type=checkbox][data-key]');
    if (!cb) return;
    if ($('albumView').value === 'selected' && !cb.checked) sticky.add(cb.dataset.key);
    act('select', { keys: [cb.dataset.key], on: cb.checked });
  });
  function rowAction(e) {
    const b = e.target.closest('button[data-act]');
    if (!b) return false;
    if (b.dataset.act === 'open') act('open', { what: 'album', key: b.dataset.key });
    if (b.dataset.act === 'forget') act('forget_album', { key: b.dataset.key }, '다음에 올릴 때 앨범을 새로 만듭니다');
    return true;
  }
  $('albumBody').addEventListener('click', (e) => {
    if (rowAction(e) || e.target.closest('input, button, a, label')) return;
    if (String(window.getSelection() || '')) return;   // 글자를 고르는 중이면 선택을 바꾸지 않는다
    const cb = e.target.closest('tr') && e.target.closest('tr').querySelector('input[type=checkbox][data-key]');
    if (cb && !cb.disabled) cb.click();
  });
  $('albumProg').addEventListener('click', rowAction);
  $('btnSkipped').addEventListener('click', openSkipped);
  $('skClose').addEventListener('click', () => closeModal('skipModal'));

  $('btnUpload').addEventListener('click', () => openConfirm('start'));
  $('btnResume').addEventListener('click', () => openConfirm('resume'));
  $('cfCancel').addEventListener('click', () => closeModal('confirmModal'));
  $('cfStart').addEventListener('click', async () => {
    closeModal('confirmModal');
    if (await act('start', {}) !== undefined) go(4);
  });
  $('btnStop').addEventListener('click', () => act('stop', {}));
  $('btnOpenPhotos').addEventListener('click', () => act('open', { what: 'photos' }));
  $('btnData').addEventListener('click', () => act('open', { what: 'data' }));
  $('btnPhotos').addEventListener('click', () => act('open', { what: 'photos' }));

  // 실패 목록
  $('btnFailures').addEventListener('click', async () => {
    let list;
    try { list = await api('failures'); } catch (e) { toast(e.message, true); return; }
    $('fmBody').innerHTML = list.length ? list.map((f) => `<div>${esc(f.path)}<br><span class="e">${esc(f.error)}</span></div>`).join('') : '<p class="muted">실패한 파일이 없습니다.</p>';
    openModal('failModal', 'fmClose');
  });
  $('fmClose').addEventListener('click', () => closeModal('failModal'));
  $('fmRetry').addEventListener('click', async () => {
    closeModal('failModal');
    await act('retry_failed', {}, '다음 올리기 때 다시 시도합니다');
  });

  // 창 닫기 확인(프로그램이 부름)
  $('clKeep').addEventListener('click', () => closeModal('closeModal'));
  $('clQuit').addEventListener('click', () => act('quit', {}));
  window.app = { askClose: () => openModal('closeModal', 'clKeep') };

  // 단축키: Esc(창 닫기), Alt+←/→(이전·다음 단계), 3단계 Ctrl+F(앨범 찾기)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const open = MODALS.filter((id) => !$(id).classList.contains('hidden'));
      if (open.length) { e.preventDefault(); closeModal(open[open.length - 1]); }
      return;
    }
    if (modalOpen() || !step) return;
    const el = document.activeElement;
    const typing = el && (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || (el.tagName === 'INPUT' && el.type !== 'checkbox'));
    if (e.altKey && !e.ctrlKey && !e.shiftKey && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) {
      if (typing) return;
      e.preventDefault();
      go(step + (e.key === 'ArrowLeft' ? -1 : 1));
      return;
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'f' && step === 3 && S && S.plan) {
      e.preventDefault();
      $('albumFilter').focus();
      $('albumFilter').select();
    }
  });
  window.addEventListener('hashchange', () => { const n = hashStep(); if (n && S) go(n); });

  kick();
})();
