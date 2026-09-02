/* 근무표 화면 그리기.
   관리자 페이지(schedule.html)와 직원용 열람 페이지(schedule_public.html)가 같이 쓴다.
   서버가 넘겨준 window.SCHED_BOOT 하나로 시작하고, 바뀐 건 fetch 로 되돌려 저장한다.

   주간 캘린더는 끌어서 옮기는 조작이 핵심이라, 이 화면만은 다른 화면들과 달리
   폼 제출 대신 자바스크립트로 그린다. 저장은 바뀔 때마다 자동으로 올라간다. */
(function () {
  'use strict';

  var B = window.SCHED_BOOT || {};
  var MODE = window.SCHED_MODE || 'admin';       // 'admin' | 'public'
  var CFG = B.cfg || {};
  var WEEKS = B.weeks || [];
  var HOL = B.holidays || {};
  var WX = B.weather || {};
  var SALES = B.sales || {};
  var DOW = B.dow || ['월', '화', '수', '목', '금', '토', '일'];
  var TODAY = B.todayIso;

  // API 주소 앞부분 — 집 PC 웹앱은 /schedule, 클라우드는 /<비밀주소>/schedule
  var API = B.api || '/schedule';

  var HH = 36;                    // 캘린더에서 1시간 = 36px
  var AXIS_START = 6, AXIS_END = 22;
  var wkIdx = 0, meName = null, dayGaps = [], bizDraft = null;
  var $ = function (id) { return document.getElementById(id); };

  // ── 유틸 ──────────────────────────────────────────────────
  function pad(n) { return String(n).padStart(2, '0'); }
  function hm(h) { return pad(Math.floor(h)) + ':' + pad(Math.round((h % 1) * 60)); }
  function toH(v) { var p = String(v).split(':'); return (+p[0] || 0) + (+p[1] || 0) / 60; }
  function span(sh) { return hm(sh.s) + '–' + hm(sh.e); }
  function dur(sh) { return sh.e - sh.s; }
  function hrs(n) { return String(Math.round(n * 10) / 10); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function mdOf(iso) { return (+iso.slice(5, 7)) + '/' + (+iso.slice(8, 10)); }
  function dowOf(iso) { return (new Date(iso + 'T00:00:00').getDay() + 6) % 7; }
  function staffOf(n) { return (CFG.staff || []).filter(function (s) { return s.name === n; })[0]; }
  function colorOf(n) { var s = staffOf(n); return (s && s.c) || '#8a897f'; }
  function holidayOf(iso) { return CFG.showHoliday ? (HOL[iso] || null) : null; }
  function weatherOf(iso) { return CFG.showWeather ? (WX[iso] || null) : null; }
  function isClosed(iso, di) {
    return (CFG.closedDates || []).indexOf(iso) >= 0 || (CFG.closedDows || []).indexOf(di) >= 0;
  }

  // 그 날짜에 적용되던 영업시간 — 바꿔도 과거는 그대로다
  function bizOf(iso) {
    var list = (CFG.bizHours || []).slice().sort(function (a, b) {
      return String(a.from).localeCompare(String(b.from));
    });
    var entry = list[0];
    for (var i = 0; i < list.length; i++) if (list[i].from <= iso) entry = list[i];
    var pair = (entry && entry.dows && entry.dows[dowOf(iso)]) || [7, 21];
    return { open: +pair[0], close: +pair[1] };
  }
  function bizToday() { return bizOf(TODAY); }

  function nowHour() {
    var d = new Date();
    return d.getHours() + Math.floor(d.getMinutes() / 30) * 0.5;
  }

  function recalcAxis() {
    var lo = [], hi = [];
    WEEKS.forEach(function (w) {
      w.days.forEach(function (day) {
        day.forEach(function (sh) { lo.push(sh.s); hi.push(sh.e); });
      });
      w.iso.forEach(function (iso) { var b = bizOf(iso); lo.push(b.open); hi.push(b.close); });
    });
    if (!lo.length) { lo = [7]; hi = [21]; }
    AXIS_START = Math.floor(Math.min.apply(null, lo));
    AXIS_END = Math.max(AXIS_START + 1, Math.ceil(Math.max.apply(null, hi)));
  }

  // ── 저장 ──────────────────────────────────────────────────
  var saveTimer = null, pendingWeeks = {};
  function flash(msg, bad) {
    var el = $('savenote'); if (!el) return;
    el.textContent = msg;
    el.className = 'note' + (bad ? ' warn' : '');
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.hidden = true; }, bad ? 8000 : 1600);
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }
  function saveWeek(i) {
    if (MODE !== 'admin') return;
    pendingWeeks[i] = true;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      var idxs = Object.keys(pendingWeeks); pendingWeeks = {};
      Promise.all(idxs.map(function (k) {
        var w = WEEKS[k];
        return post(API + '/api/week', { week_start: w.start, locked: w.locked, days: w.days });
      })).then(function () { flash('저장했어요'); })
        .catch(function () { flash('저장하지 못했어요 — 창을 닫지 말고 다시 시도해주세요.', true); });
    }, 400);
  }
  function saveConfig() {
    if (MODE !== 'admin') return;
    post(API + '/api/config', {
      bizHours: CFG.bizHours, closedDows: CFG.closedDows, closedDates: CFG.closedDates,
      presets: CFG.presets, staff: CFG.staff, salesPerHead: CFG.salesPerHead,
      showHoliday: CFG.showHoliday, showWeather: CFG.showWeather,
    }).then(function () { flash('저장했어요'); })
      .catch(function () { flash('설정을 저장하지 못했어요.', true); });
  }

  // ── 시간 고르기 — 시/분 드롭다운 ──────────────────────────
  // type=time 입력이 불편하다는 피드백(2026-09-02)으로 교체. 타이핑 없이
  // 목록에서 고르고, 폰에서는 기본 휠 피커가 뜬다. 분은 5분 단위.
  var _tpCb = {}, _tpSeq = 0;
  function tpick(val, opts) {
    opts = opts || {};
    var n = 'tp' + (_tpSeq++);
    if (opts.cb) _tpCb[n] = opts.cb;
    var h = Math.floor(val), m = Math.round(((val % 1) * 60) / 5) * 5;
    if (m >= 60) { h += 1; m = 0; }
    var hs = '';
    for (var i = 0; i <= 24; i++) {
      hs += '<option value="' + i + '"' + (i === h ? ' selected' : '') + '>' + pad(i) + '</option>';
    }
    var ms = '';
    for (var j = 0; j < 60; j += 5) {
      ms += '<option value="' + j + '"' + (j === m ? ' selected' : '') + '>' + pad(j) + '</option>';
    }
    var lab = esc(opts.label || '시간');
    return '<span class="tpick" id="' + n + '" data-hid="' + (opts.hiddenId || '') + '">'
      + (opts.hiddenId ? '<input type="hidden" id="' + opts.hiddenId + '" value="' + hm(h + m / 60) + '">' : '')
      + '<select class="fld" aria-label="' + lab + ' — 시" onchange="SCHED._tp(\'' + n + '\')">' + hs + '</select>'
      + '<b>:</b>'
      + '<select class="fld" aria-label="' + lab + ' — 분" onchange="SCHED._tp(\'' + n + '\')">' + ms + '</select>'
      + '</span>';
  }

  // ── 겹치는 근무를 세로줄로 나누기 ──────────────────────────
  function assignLanes(shifts) {
    var sorted = shifts.slice().sort(function (a, b) { return a.s - b.s || a.e - b.e; });
    var laneEnd = [];
    sorted.forEach(function (sh) {
      var lane = -1;
      for (var i = 0; i < laneEnd.length; i++) if (laneEnd[i] <= sh.s) { lane = i; break; }
      if (lane < 0) lane = laneEnd.length;
      laneEnd[lane] = sh.e;
      sh._lane = lane;
    });
    sorted.forEach(function (sh) { sh._lanes = laneEnd.length; });
    return sorted;
  }

  // ── 주간 캘린더 ───────────────────────────────────────────
  function weekCalHTML(wk, wi, opts) {
    opts = opts || {};
    var mine = opts.mine || null;
    var ro = opts.readonly || wk.locked || MODE !== 'admin';
    var y = function (h) { return (h - AXIS_START) * HH; };
    var hours = [];
    for (var h = AXIS_START; h < AXIS_END; h++) hours.push(h);

    var head = '<div class="wkgrid-head"><div class="sp"></div>';
    wk.dates.forEach(function (d, i) {
      var iso = wk.iso[i], n = wk.days[i].length;
      var hol = holidayOf(iso), closed = isClosed(iso, i), w = weatherOf(iso), bz = bizOf(iso);
      var today = iso === TODAY;
      var cls = [today ? 'today' : '', i === 5 ? 'sat' : (i === 6 ? 'sun' : ''),
                 hol ? 'hol' : '', closed ? 'closed' : ''].filter(Boolean).join(' ');
      head += '<div class="dh ' + cls + '">'
        + '<span class="w">' + DOW[i] + (today ? ' · 오늘' : '') + '</span>'
        + '<span class="dt num">' + d + '</span>'
        + (hol ? '<span class="holname">' + esc(hol) + '</span>' : '')
        + (w ? '<span class="wx">' + esc(w.icon || '') + ' <span class="num">' + esc(w.hi) + '°</span></span>' : '')
        + (closed ? '<span class="closedtag">휴무</span>'
                  : '<span class="biz num">' + hm(bz.open) + '–' + hm(bz.close) + '</span>'
                    + '<span class="cnt">' + (n ? n + '명' : '—') + '</span>')
        + '</div>';
    });
    head += '</div>';

    var body = '<div class="wkgrid-body"><div class="wkaxis">'
      + hours.map(function (h) {
          return '<div class="hl" style="height:' + HH + 'px"><span class="num">' + pad(h) + '</span></div>';
        }).join('')
      + '</div>';

    wk.days.forEach(function (day, di) {
      var iso = wk.iso[di], closed = isClosed(iso, di), bz = bizOf(iso);
      body += '<div class="dcol ' + (iso === TODAY ? 'today' : '') + (closed ? ' closed' : '') + '" data-day="' + di + '">'
        + '<div class="bizband" style="top:' + y(bz.open) + 'px;height:' + ((bz.close - bz.open) * HH) + 'px"></div>'
        + hours.map(function () { return '<div class="hl" style="height:' + HH + 'px"></div>'; }).join('')
        + (closed ? '<span class="closedlab">휴 무</span>' : '');

      assignLanes(day).forEach(function (sh) {
        var w = 100 / sh._lanes;
        var dim = (mine && sh.w !== mine) ? 'opacity:.4;' : '';
        var mk = sh.st === 'ok' ? '<span class="mk ok">✓</span>'
               : sh.st === 'diff' ? '<span class="mk diff">' + esc(sh.note || '변동') + '</span>'
               : sh.st === 'pend' ? '<span class="mk pend">기록 전</span>' : '';
        var attr = ro ? '' : ' data-di="' + di + '" data-idx="' + day.indexOf(sh) + '"';
        var rz = ro ? '' : '<i class="rz top"></i><i class="rz bot"></i>';
        body += '<div class="ev' + (ro ? ' ro' : '') + (sh.st === 'diff' ? ' diff' : '') + '"'
          + ' style="top:' + y(sh.s) + 'px;height:' + Math.max(14, dur(sh) * HH - 4) + 'px;'
          + 'left:calc(' + (sh._lane * w) + '% + 2px);width:calc(' + w + '% - 4px);'
          + 'background:' + colorOf(sh.w) + ';' + dim + '"'
          + ' title="' + esc(sh.w) + ' ' + span(sh) + ' (' + hrs(dur(sh)) + '시간)"' + attr + '>'
          + rz + mk
          + '<span class="t num"><span class="s">' + hm(sh.s) + '</span><span class="e">–' + hm(sh.e) + '</span></span>'
          + '<b>' + esc(sh.w) + '</b></div>';
      });
      body += '</div>';
    });

    var ti = wk.iso.indexOf(TODAY);
    var nh = nowHour();
    if (ti >= 0 && nh >= AXIS_START && nh <= AXIS_END) {
      body += '<div class="nowline" style="top:' + y(nh) + 'px"><b class="num">지금 ' + hm(nh) + '</b></div>';
    }
    return '<div class="wkcal"><div class="wkcal-inner">' + head + body + '</div></div></div>';
  }

  function fitEvents(root) {
    (root || document).querySelectorAll('#sched .ev').forEach(function (ev) {
      var w = ev.offsetWidth;
      ev.classList.toggle('compact', w < 104);
      ev.classList.toggle('narrow', w < 46);
    });
  }

  function renderWeek() {
    var host = $('weekhost'); if (!host) return;
    var wk = WEEKS[wkIdx]; if (!wk) return;
    var flat = [].concat.apply([], wk.days);
    var isEmpty = flat.length === 0;
    var totalH = flat.reduce(function (a, sh) { return a + dur(sh); }, 0);
    var people = {};
    flat.forEach(function (sh) { people[sh.w] = 1; });

    var html = '<div class="week ' + (wk.locked ? 'locked' : '') + '">'
      + '<div class="wkhead"><b class="num">' + esc(wk.label) + '</b>'
      + '<span class="tag warning badge-draft">작성 중</span>'
      + '<span class="tag accent badge-locked">🔒 확정</span>'
      + (MODE === 'admin' ? '<div class="wkactions no-print">'
          + '<button class="btn small draftonly" onclick="SCHED.copyPrev()">📋 지난주 복사</button>'
          + '<button class="btn small primary draftonly" onclick="SCHED.lock(true)">🔒 이번 주 확정</button>'
          + '<button class="unlock lockonly" onclick="SCHED.lock(false)">잠금 해제</button>'
          + '</div>' : '')
      + '</div>';

    if (isEmpty && MODE === 'admin') {
      html += '<div class="post emptywk"><p>이 주는 아직 안 짰어요. 지난주를 그대로 가져와서 달라지는 근무만 고치면 돼요.</p>'
        + '<button class="btn primary" onclick="SCHED.copyPrev()">📋 지난주 그대로 가져오기</button></div>';
    }
    html += weekCalHTML(wk, wkIdx);

    var hols = wk.iso.map(function (iso, i) { return { iso: iso, i: i, name: holidayOf(iso) }; })
      .filter(function (h) { return h.name && !isClosed(h.iso, h.i); });
    if (hols.length) {
      html += '<div class="note warn">🇰🇷 이 주에 공휴일이 있어요 — '
        + hols.map(function (h) { return '<b>' + mdOf(h.iso) + '(' + DOW[h.i] + ') ' + esc(h.name) + '</b>'; }).join(', ')
        + '. 손님이 평소와 다를 수 있으니 인원을 한 번 더 확인해보세요.</div>';
    }
    if (!isEmpty) {
      html += '<div class="stats3">'
        + '<div class="stat"><span class="n num">' + hrs(totalH) + '시간</span><small>이번 주 총 근무</small></div>'
        + '<div class="stat"><span class="n num">' + Object.keys(people).length + '명</span><small>근무 인원</small></div>'
        + '<div class="stat"><span class="n num">' + flat.length + '건</span><small>배치된 근무</small></div>'
        + '</div>';
    }
    if (wk.locked) {
      html += '<div class="note">🔒 확정됐어요. 이 주의 예정은 잠기고, 직원 화면에도 <b>"확정"</b>으로 표시돼요. '
        + '이후 달라지는 건 <b>실제 기록</b>으로만 남습니다.</div>';
    }
    host.innerHTML = html + '</div>';
    if ($('wkpos')) $('wkpos').textContent = (wkIdx + 1) + ' / ' + WEEKS.length;
    fitEvents(host);

    if (MODE === 'admin' && !wk.locked) {
      host.querySelectorAll('.dcol').forEach(function (col) {
        var di = +col.dataset.day;
        if (isClosed(wk.iso[di], di)) return;
        col.onclick = function (e) {
          if (justDragged) return;
          var yPos = e.clientY - col.getBoundingClientRect().top;
          var half = Math.max(0, Math.round(yPos / (HH / 2)));
          openAdd(wkIdx, di, Math.min(AXIS_END - 1, AXIS_START + half / 2));
        };
      });
      host.querySelectorAll('.ev[data-idx]').forEach(function (el) {
        el.addEventListener('pointerdown', function (e) { startDrag(e, el, wkIdx); });
      });
    }
  }

  // ── 끌어서 옮기기 · 시간 늘리고 줄이기 ─────────────────────
  var drag = null, justDragged = false;

  function startDrag(e, el, wi) {
    if (e.button != null && e.button !== 0) return;
    var di = +el.dataset.di, idx = +el.dataset.idx;
    var sh = WEEKS[wi].days[di][idx]; if (!sh) return;
    var bodyEl = el.closest('.wkgrid-body'); if (!bodyEl) return;
    drag = {
      wi: wi, di: di, idx: idx, el: el,
      mode: e.target.classList.contains('top') ? 'top'
          : e.target.classList.contains('bot') ? 'bottom' : 'move',
      cols: [].slice.call(bodyEl.querySelectorAll('.dcol')),
      startY: e.clientY, origS: sh.s, origE: sh.e,
      newS: sh.s, newE: sh.e, newDi: di, moved: false,
    };
    el.classList.add('dragging');
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault(); e.stopPropagation();
  }

  function moveDrag(e) {
    if (!drag) return;
    var d = drag;
    var steps = Math.round((e.clientY - d.startY) / (HH / 2)) / 2;   // 30분 단위
    var len = d.origE - d.origS;

    if (d.mode === 'move') {
      var s = Math.max(AXIS_START, Math.min(AXIS_END - len, d.origS + steps));
      d.newS = s; d.newE = s + len;
      for (var i = 0; i < d.cols.length; i++) {
        var r = d.cols[i].getBoundingClientRect();
        if (e.clientX >= r.left && e.clientX <= r.right) {
          if (!isClosed(WEEKS[d.wi].iso[i], i)) d.newDi = i;
          break;
        }
      }
    } else if (d.mode === 'top') {
      d.newS = Math.max(AXIS_START, Math.min(d.origE - 0.5, d.origS + steps));
      d.newE = d.origE;
    } else {
      d.newE = Math.min(AXIS_END, Math.max(d.origS + 0.5, d.origE + steps));
      d.newS = d.origS;
    }
    if (d.newS !== d.origS || d.newE !== d.origE || d.newDi !== d.di) d.moved = true;

    var col = d.cols[d.newDi];
    if (col && d.el.parentElement !== col) col.appendChild(d.el);
    d.cols.forEach(function (c, i) {
      c.classList.toggle('droptarget', d.moved && i === d.newDi && d.newDi !== d.di);
    });
    d.el.style.top = ((d.newS - AXIS_START) * HH) + 'px';
    d.el.style.height = Math.max(14, (d.newE - d.newS) * HH - 4) + 'px';
    d.el.style.left = '2px';
    d.el.style.width = 'calc(100% - 4px)';
    var t = d.el.querySelector('.t');
    if (t) t.innerHTML = '<span class="s">' + hm(d.newS) + '</span><span class="e">–' + hm(d.newE) + '</span>';
  }

  function endDrag() {
    if (!drag) return;
    var d = drag; drag = null;
    d.el.classList.remove('dragging');
    d.cols.forEach(function (c) { c.classList.remove('droptarget'); });
    justDragged = true;
    setTimeout(function () { justDragged = false; }, 0);

    if (!d.moved) { openEdit(d.wi, d.di, d.idx); return; }
    var day = WEEKS[d.wi].days[d.di];
    var sh = day[d.idx];
    if (!sh) { renderAll(); return; }
    sh.s = d.newS; sh.e = d.newE;
    if (d.newDi !== d.di) { day.splice(d.idx, 1); WEEKS[d.wi].days[d.newDi].push(sh); }
    saveWeek(d.wi); renderAll();
  }

  // ── 근무 추가/수정 모달 ───────────────────────────────────
  var md = null;
  function openAdd(wi, di, hour) {
    var ps = CFG.presets || [];
    var p = ps.filter(function (p) { return Math.abs(p.s - hour) < 1.5; })[0] || ps[0] || { s: hour, e: hour + 5 };
    var first = (CFG.staff || [])[0];
    md = { mode: 'add', wi: wi, di: di, idx: -1, who: first ? first.name : '', s: p.s, e: p.e };
    renderModal();
  }
  function openEdit(wi, di, idx) {
    var sh = WEEKS[wi].days[di][idx]; if (!sh) return;
    md = { mode: 'edit', wi: wi, di: di, idx: idx, who: sh.w, s: sh.s, e: sh.e };
    renderModal();
  }
  function renderModal() {
    if (!md || !$('modal')) return;
    var wk = WEEKS[md.wi];
    $('mdTitle').textContent = md.mode === 'add' ? '근무 추가' : '근무 수정';
    $('mdSub').textContent = wk.dates[md.di] + ' (' + DOW[md.di] + ')' + (md.mode === 'edit' ? ' · ' + md.who : '');
    $('mdStaffWrap').hidden = md.mode !== 'add';
    $('mdStaff').innerHTML = (CFG.staff || []).map(function (s) {
      return '<button class="' + (s.name === md.who ? 'on' : '') + '" onclick="SCHED.pickWho(\'' + esc(s.name) + '\')">'
        + '<i class="pdot" style="background:' + s.c + '"></i>' + esc(s.name) + '</button>';
    }).join('') || '<span class="cap">설정에서 직원을 먼저 추가해주세요.</span>';
    $('mdPresets').innerHTML = (CFG.presets || []).map(function (p, i) {
      return '<button class="' + ((p.s === md.s && p.e === md.e) ? 'on' : '') + '" onclick="SCHED.pickPreset(' + i + ')">'
        + esc(p.name) + ' <span class="num cap">' + hm(p.s) + '–' + hm(p.e) + '</span></button>';
    }).join('');
    $('mdStartWrap').innerHTML = tpick(md.s, { hiddenId: 'mdStart', label: '시작', cb: function () { window.SCHED.modalTime(); } });
    $('mdEndWrap').innerHTML = tpick(md.e, { hiddenId: 'mdEnd', label: '종료', cb: function () { window.SCHED.modalTime(); } });
    $('mdLen').textContent = md.e > md.s ? hrs(md.e - md.s) + '시간' : '시간이 거꾸로예요';
    $('mdSave').textContent = md.mode === 'add' ? '추가' : '저장';
    $('mdSave').disabled = !(md.e > md.s && md.who);
    $('mdDelete').hidden = md.mode !== 'edit';
    $('modal').hidden = false;
  }

  // ── 포지션 · 휴게 · WT ────────────────────────────────────
  function posOf(sh) {
    if (sh.pos) return sh.pos;
    var p = (CFG.presets || []).filter(function (p) { return p.s === sh.s && p.e === sh.e; })[0];
    return p ? p.name : '근무';
  }
  function brOf(sh) {
    if (sh.br != null) return sh.br;
    var d = dur(sh);
    return d > 8 ? 60 : (d >= 5 ? 30 : 0);
  }
  function wtOf(sh) { return dur(sh) - brOf(sh) / 60; }
  function brWindow(sh) {
    var len = brOf(sh) / 60; if (!len) return null;
    var s = sh.s + (dur(sh) - len) / 2;
    return { s: s, e: s + len, label: brOf(sh) >= 60 ? 'M' : 'B' };
  }
  function coverAt(list, h) {
    return list.filter(function (sh) {
      if (!(sh.s <= h && h < sh.e)) return false;
      var b = brWindow(sh);
      return !(b && b.s <= h && h < b.e);
    }).length;
  }

  function todayCell() {
    for (var i = 0; i < WEEKS.length; i++) {
      var di = WEEKS[i].iso.indexOf(TODAY);
      if (di >= 0) return { wi: i, di: di };
    }
    return null;
  }
  function todayShifts() {
    var c = todayCell();
    return c ? WEEKS[c.wi].days[c.di] : [];
  }

  // ── 시간대별 예상 매출 ────────────────────────────────────
  function salesAt(iso, h) {
    var dows = SALES.dows;
    if (!dows) return null;
    var arr = dows[String(dowOf(iso))];
    if (!arr) return null;
    var bz = bizOf(iso);
    if (h < bz.open || h >= bz.close) return 0;
    var i = Math.round((h - (SALES.base != null ? SALES.base : 6)) / (SALES.step || 0.5));
    return (i >= 0 && i < arr.length) ? +arr[i] : 0;
  }

  function dayChartHTML() {
    var list = todayShifts(), bz = bizToday();
    var total = AXIS_END - AXIS_START;
    var pct = function (h) { return ((h - AXIS_START) / total) * 100; };
    var cw = (0.5 / total) * 100;
    var off = '<span class="off" style="left:0;width:' + pct(bz.open) + '%"></span>'
            + '<span class="off" style="left:' + pct(bz.close) + '%;right:0"></span>';
    var ticks = '';
    for (var h = Math.ceil(AXIS_START); h <= AXIS_END; h++) ticks += '<span class="tick" style="left:' + pct(h) + '%"></span>';

    var slots = [], hasSales = false;
    for (var t = AXIS_START; t < AXIS_END; t += 0.5) {
      var won = salesAt(TODAY, t);
      if (won != null) hasSales = true;
      slots.push({
        h: t, won: won || 0, n: coverAt(list, t), open: t >= bz.open && t < bz.close,
        need: Math.ceil((won || 0) / (CFG.salesPerHead || 35)),
      });
    }
    var maxWon = Math.max.apply(null, [1].concat(slots.map(function (s) { return s.won; })));
    var maxN = Math.max.apply(null, [2].concat(slots.map(function (s) { return s.n; })));
    var peak = Math.max.apply(null, slots.map(function (s) { return s.won; }));

    var saleRow = hasSales
      ? '<div class="plot" style="height:66px">' + off + ticks
          + '<span class="gl" style="top:33%"></span><span class="gl" style="top:66%"></span>'
          + slots.map(function (s) {
              if (!s.won) return '';
              return '<span class="bar sale' + (s.won === peak ? ' peak' : '') + '"'
                + ' style="left:calc(' + pct(s.h) + '% + 1px);width:calc(' + cw + '% - 2px);height:' + ((s.won / maxWon) * 100) + '%"'
                + ' title="' + hm(s.h) + ' · ' + s.won + '천원">'
                + (s.won === peak ? '<b class="num">' + s.won + '천</b>' : '') + '</span>';
            }).join('')
          + '</div>'
      : '<div class="plot nodata" style="height:66px">아직 매출 데이터가 없어요. POS 매출을 <code>schedule/sales.json</code> 으로 넣으면 여기 그래프가 뜹니다.</div>';

    var headRow = slots.map(function (s) {
      if (!s.n) {
        return s.open
          ? '<span class="bar head none" style="left:calc(' + pct(s.h) + '% + 1px);width:calc(' + cw + '% - 2px);height:8%"'
            + ' title="' + hm(s.h) + ' · 0명"></span>'
          : '';
      }
      var short = hasSales && s.open && s.n < s.need;
      return '<span class="bar head' + (short ? ' short' : '') + '"'
        + ' style="left:calc(' + pct(s.h) + '% + 1px);width:calc(' + cw + '% - 2px);height:' + ((s.n / maxN) * 100) + '%"'
        + ' title="' + hm(s.h) + ' · ' + s.n + '명' + (short ? ' (매출 기준 ' + s.need + '명 필요)' : '') + '">'
        + '<i class="headnum">' + s.n + '</i></span>';
    }).join('');

    var axis = '';
    for (var a = Math.ceil(AXIS_START); a <= AXIS_END; a++) axis += '<span class="num" style="left:' + pct(a) + '%">' + pad(a) + '</span>';
    var shortCount = slots.filter(function (s) { return hasSales && s.open && s.n && s.n < s.need; }).length;

    return '<div class="chart"><div class="chart-inner">'
      + '<div class="crow2"><div class="lab"><b>시간대별 예상 매출</b>'
        + (hasSales ? '최근 4주 같은 요일 중앙값 · 최고 ' + peak + '천원' : '데이터 없음') + '</div>' + saleRow + '</div>'
      + '<div class="crow2"><div class="lab"><b>실제 근무 인원</b>휴게 시간은 뺀 인원'
        + (shortCount ? ' · <span style="color:var(--warn)">부족 ' + shortCount + '칸</span>' : '') + '</div>'
        + '<div class="plot" style="height:42px">' + off + ticks + headRow + '</div></div>'
      + '<div class="axis"><div class="lab"></div><div class="plot">' + axis + '</div></div>'
      + '</div></div>';
  }

  // ── 데일리 근무시간표 ─────────────────────────────────────
  function dailyGridHTML() {
    var list = todayShifts().slice().sort(function (a, b) { return a.s - b.s || a.e - b.e; });
    var bz = bizToday();
    var total = AXIS_END - AXIS_START;
    var pct = function (h) { return ((h - AXIS_START) / total) * 100; };

    var ticks = '', ticksHead = '';
    for (var h = AXIS_START; h <= AXIS_END; h += 0.5) {
      var hr = h % 1 === 0;
      var line = '<span class="tick' + (hr ? ' hr' : '') + '" style="left:' + pct(h) + '%">';
      ticks += line + '</span>';
      ticksHead += line + (hr && h < AXIS_END ? '<b class="num">' + pad(h) + '</b>' : '') + '</span>';
    }
    var off = '<span class="off" style="left:0;width:' + pct(bz.open) + '%"></span>'
            + '<span class="off" style="left:' + pct(bz.close) + '%;right:0"></span>';

    dayGaps = [];
    for (var t = AXIS_START; t < AXIS_END; t += 0.5) {
      if (t >= bz.open && t < bz.close && coverAt(list, t) === 0) dayGaps.push(t);
    }

    var html = '<div class="dgrid"><div class="dgrid-inner">'
      + '<div class="drow head"><div>포지션</div><div>근무자</div><div>WT</div><div>출근</div><div>퇴근</div>'
      + '<div class="tcell">' + off + ticksHead + '</div></div>';

    if (!list.length) {
      html += '<div class="drow"><div style="grid-column:1 / -1;color:var(--muted);">오늘 배치된 근무가 없어요.</div></div>';
    }
    list.forEach(function (sh) {
      var b = brWindow(sh);
      var bar = '<span class="dbar num" style="left:' + pct(sh.s) + '%;width:' + ((dur(sh) / total) * 100) + '%;'
        + 'background:' + colorOf(sh.w) + '">' + span(sh) + '</span>'
        + (b ? '<span class="dbrk" style="left:' + pct(b.s) + '%;width:' + ((brOf(sh) / 60 / total) * 100) + '%"'
             + ' title="휴게 ' + brOf(sh) + '분">' + b.label + '</span>' : '');
      html += '<div class="drow">'
        + '<div>' + esc(posOf(sh)) + '</div>'
        + '<div class="who"><i class="pdot" style="background:' + colorOf(sh.w) + '"></i>' + esc(sh.w) + '</div>'
        + '<div class="wt num">' + hrs(wtOf(sh)) + '</div>'
        + '<div class="num">' + hm(sh.s) + '</div><div class="num">' + hm(sh.e) + '</div>'
        + '<div class="tcell">' + off + ticks + bar + '</div></div>';
    });
    return html + '</div></div>';
  }

  function renderDay() {
    if (!$('dayGrid')) return;
    var list = todayShifts(), bz = bizToday();
    var totalWT = list.reduce(function (a, sh) { return a + wtOf(sh); }, 0);
    var first = list.length ? Math.min.apply(null, list.map(function (s) { return s.s; })) : bz.open;
    var last = list.length ? Math.max.apply(null, list.map(function (s) { return s.e; })) : bz.close;
    var wx = weatherOf(TODAY), hol = holidayOf(TODAY);
    var d = new Date(TODAY + 'T00:00:00');

    $('phDate').innerHTML = d.getFullYear() + '년 ' + (d.getMonth() + 1) + '월 ' + d.getDate() + '일 ('
      + DOW[dowOf(TODAY)] + ')' + (hol ? ' <span style="color:var(--sun)">' + esc(hol) + '</span>' : '');
    $('phMeta').innerHTML =
      '매장영업시간 ' + hm(bz.open) + '–' + hm(bz.close) + ' &nbsp;·&nbsp; 매장근무시간 ' + hm(first) + '–' + hm(last) + '<br>'
      + '총 근무 ' + hrs(totalWT) + '시간 &nbsp;·&nbsp; 근무자 ' + list.length + '명'
      + (wx ? ' &nbsp;·&nbsp; 날씨 ' + esc(wx.icon || '') + ' ' + esc(wx.hi) + '°' : '');

    $('dayChart').innerHTML = dayChartHTML();
    $('dayGrid').innerHTML = dailyGridHTML();

    $('phSign').innerHTML = '<table>'
      + '<tr><th>인수인계</th>' + list.map(function (sh) { return '<td>' + esc(posOf(sh)) + ' ' + esc(sh.w) + '</td>'; }).join('') + '</tr>'
      + '<tr><th>확인 서명</th>' + list.map(function () { return '<td class="sign">&nbsp;</td>'; }).join('') + '</tr>'
      + '<tr><th>전달사항</th><td class="memo" colspan="' + Math.max(1, list.length) + '">&nbsp;</td></tr>'
      + '</table>';

    var gap = $('dayGapNote');
    gap.innerHTML = dayGaps.length
      ? '🚨 <b>영업 중인데 매장에 아무도 없는 시간이 있어요</b> — '
        + dayGaps.map(function (h) { return '<b class="num">' + hm(h) + '</b>'; }).join(', ')
        + '. 휴게 시간을 겹치지 않게 옮기거나 근무를 더 넣어주세요.'
      : '';
    gap.hidden = !dayGaps.length;

    if ($('closeHost')) renderCloseRows(list);
  }

  function renderCloseRows(list) {
    var c = todayCell();
    $('closeHost').innerHTML = list.map(function (sh) {
      var i = c ? WEEKS[c.wi].days[c.di].indexOf(sh) : -1;
      var id = 'cr' + i;
      var absent = sh.note === '결근';
      var cls = sh.st === 'ok' ? 'done' : sh.st === 'diff' ? (absent ? 'absent' : 'diff') : '';
      var result = sh.st === 'ok' ? '<span class="tag success">✓ 기록 완료 — 예정대로</span>'
        : absent ? '<span class="tag warning">결근 · 0시간</span>'
        : sh.st === 'diff' ? '<span class="tag warning">⚠ ' + esc(sh.note) + '</span> <span class="num cap">실제 ' + esc(sh.actual || '') + '</span>'
        : '';
      return '<div class="crow ' + cls + '" id="' + id + '">'
        + '<div class="rhead"><i class="pdot" style="background:' + colorOf(sh.w) + '"></i><b>' + esc(sh.w) + '</b>'
        + '<span class="plan num">예정 ' + span(sh) + '</span><span class="result">' + result + '</span></div>'
        + (cls ? '' :
            '<div class="acts">'
            + '<button class="btn small primary" onclick="SCHED.recOk(' + i + ')">✓ 예정대로</button>'
            + '<button class="btn small" onclick="SCHED.toggleEdit(\'' + id + '\')">시간 수정</button>'
            + '<button class="btn small" onclick="SCHED.recAbsent(' + i + ')">결근</button></div>'
            + '<div class="editform"><div class="grid3">'
            + '<div><label class="lbl">실제 출근</label>' + tpick(sh.s, { hiddenId: id + 's', label: '실제 출근' }) + '</div>'
            + '<div><label class="lbl">실제 퇴근</label>' + tpick(sh.e, { hiddenId: id + 'e', label: '실제 퇴근' }) + '</div>'
            + '<div><label class="lbl" for="' + id + 'r">사유</label><select class="fld" id="' + id + 'r">'
            + '<option>지각</option><option>연장</option><option>조퇴</option><option>대타</option></select></div>'
            + '</div><div style="margin-top:10px;"><button class="btn small primary" onclick="SCHED.recDiff(' + i + ',\'' + id + '\')">기록 저장</button></div></div>')
        + '</div>';
    }).join('') || '<div class="post cap">오늘 배치된 근무가 없어요.</div>';
  }

  // ── 직원용 화면 ───────────────────────────────────────────
  function currentWeekIdx() {
    for (var i = 0; i < WEEKS.length; i++) if (WEEKS[i].iso.indexOf(TODAY) >= 0) return i;
    return 0;
  }
  function renderStaffWeek() {
    if (!$('sWeekList')) return;
    var wk = WEEKS[currentWeekIdx()];
    var badge = '<span class="tag ' + (wk.locked ? 'accent' : 'warning') + '">' + (wk.locked ? '🔒 확정' : '작성 중') + '</span>';
    if ($('sHeadState')) $('sHeadState').innerHTML = '이번 주 ' + badge;
    if ($('sWeekLabel')) $('sWeekLabel').innerHTML = '<span class="num">' + esc(wk.label) + '</span> ' + badge;

    $('sWeekList').innerHTML = wk.dates.map(function (d, i) {
      var iso = wk.iso[i], hol = holidayOf(iso), closed = isClosed(iso, i), w = weatherOf(iso);
      var chips = wk.days[i].slice().sort(function (a, b) { return a.s - b.s; }).map(function (sh) {
        return sh.w === meName
          ? '<span class="schip mine" style="background:' + colorOf(sh.w) + '">' + esc(sh.w) + ' <span class="num">' + span(sh) + '</span></span>'
          : '<span class="schip"><i class="pdot" style="background:' + colorOf(sh.w) + '"></i>' + esc(sh.w) + ' <span class="num">' + span(sh) + '</span></span>';
      }).join('');
      return '<div class="daycard ' + (iso === TODAY ? 'today' : '') + '">'
        + '<div class="dhead"><span class="num"' + (i === 6 || hol ? ' style="color:var(--sun)"' : i === 5 ? ' style="color:var(--sat)"' : '') + '>'
        + d + ' (' + DOW[i] + ')</span>'
        + (hol ? '<small style="color:var(--sun);font-weight:700;">' + esc(hol) + '</small>' : '')
        + (iso === TODAY ? '<small>오늘</small>' : '')
        + (w ? '<small style="margin-left:auto;">' + esc(w.icon || '') + ' ' + esc(w.hi) + '°</small>' : '')
        + '</div>'
        + (closed ? '<span class="tag warning">휴무</span>' : (chips || '<span class="cap">근무 없음</span>'))
        + '</div>';
    }).join('');
    if ($('sWeekCal')) $('sWeekCal').innerHTML = weekCalHTML(wk, currentWeekIdx(), { mine: meName, readonly: true });
  }

  function renderMe() {
    if (!$('mePicker')) return;
    $('mePicker').innerHTML = (CFG.staff || []).map(function (s) {
      return '<button class="' + (s.name === meName ? 'on' : '') + '" onclick="SCHED.pickMe(\'' + esc(s.name) + '\')">'
        + '<i class="pdot" style="background:' + s.c + '"></i>' + esc(s.name) + '</button>';
    }).join('') || '<span class="cap">아직 등록된 직원이 없어요.</span>';
    $('meTitle').textContent = (meName || '내') + '의 근무';

    var rows = [], nh = nowHour();
    WEEKS.forEach(function (wk) {
      wk.iso.forEach(function (iso, di) {
        if (iso < TODAY) return;
        wk.days[di].filter(function (sh) { return sh.w === meName; })
          .sort(function (a, b) { return a.s - b.s; })
          .forEach(function (sh) { rows.push({ iso: iso, di: di, sh: sh, wk: wk }); });
      });
    });
    var next = rows.filter(function (r) { return r.iso > TODAY || r.sh.s > nh; })[0];
    $('meNext').innerHTML = next
      ? '<small>다음 출근</small><div class="big num">' + (next.iso === TODAY ? '오늘' : mdOf(next.iso))
        + ' (' + DOW[next.di] + ') ' + hm(next.sh.s) + '</div>'
        + '<small class="num">' + span(next.sh) + ' · ' + hrs(dur(next.sh)) + '시간</small>'
      : '<small>예정된 근무가 없어요</small>';

    $('meList').innerHTML = rows.map(function (r) {
      return '<div class="row"><b class="num">' + mdOf(r.iso) + ' (' + DOW[r.di] + ')</b>'
        + '<span class="cap num">' + span(r.sh) + ' · ' + hrs(dur(r.sh)) + '시간</span>'
        + '<span style="margin-left:auto"><span class="tag ' + (r.wk.locked ? 'accent' : 'warning') + '">'
        + (r.wk.locked ? '🔒 확정' : '작성 중') + '</span></span></div>';
    }).join('') || '<div class="row"><span class="cap">예정된 근무가 없어요</span></div>';
  }

  // ── 설정 ──────────────────────────────────────────────────
  function currentBizEntry() {
    var list = (CFG.bizHours || []).slice().sort(function (a, b) { return String(a.from).localeCompare(String(b.from)); });
    var e = list[0];
    for (var i = 0; i < list.length; i++) if (list[i].from <= TODAY) e = list[i];
    return e || { from: '2020-01-01', dows: [[7, 21], [7, 21], [7, 21], [7, 21], [7, 21], [7, 21], [7, 21]] };
  }
  function ensureBizDraft() {
    if (!bizDraft) bizDraft = currentBizEntry().dows.map(function (p) { return [+p[0], +p[1]]; });
    return bizDraft;
  }
  function sameHours(dows) {
    return dows.every(function (p) { return p[0] === dows[0][0] && p[1] === dows[0][1]; });
  }
  function bizSummary(dows) {
    if (sameHours(dows)) return '매일 ' + hm(dows[0][0]) + '–' + hm(dows[0][1]);
    var groups = [];
    dows.forEach(function (p, i) {
      var g = groups.filter(function (g) { return g.o === p[0] && g.c === p[1]; })[0];
      if (g) g.d.push(DOW[i]); else groups.push({ o: p[0], c: p[1], d: [DOW[i]] });
    });
    return groups.map(function (g) { return g.d.join('·') + ' ' + hm(g.o) + '–' + hm(g.c); }).join(' / ');
  }

  function renderSettings() {
    if (!$('bizDowHost')) return;
    var d = ensureBizDraft();
    $('bizDowHost').innerHTML = d.map(function (p, i) {
      var color = i === 5 ? 'color:var(--sat)' : i === 6 ? 'color:var(--sun)' : '';
      return '<div class="erow"><b style="width:22px;' + color + '">' + DOW[i] + '</b>'
        + tpick(p[0], { label: DOW[i] + '요일 영업 시작', cb: function (v) { window.SCHED.editBizDow(i, 0, v); } })
        + '<span class="cap">~</span>'
        + tpick(p[1], { label: DOW[i] + '요일 영업 종료', cb: function (v) { window.SCHED.editBizDow(i, 1, v); } })
        + '<span class="cap num">' + (p[1] > p[0] ? hrs(p[1] - p[0]) + '시간' : '시간 거꾸로') + '</span></div>';
    }).join('');

    var cur = currentBizEntry();
    $('bizHistHost').innerHTML = (CFG.bizHours || []).slice()
      .sort(function (a, b) { return String(b.from).localeCompare(String(a.from)); })
      .map(function (e) {
        return '<div class="erow"><b class="num">' + mdOf(e.from) + '부터</b>'
          + '<span class="cap">' + esc(bizSummary(e.dows)) + '</span>'
          + (e === cur ? '<span class="tag accent">지금 적용 중</span>' : '')
          + ((CFG.bizHours || []).length > 1
              ? '<button class="del" title="삭제" onclick="SCHED.delBiz(\'' + e.from + '\')">✕</button>' : '')
          + '</div>';
      }).join('');

    var dirty = JSON.stringify(d) !== JSON.stringify(cur.dows);
    $('bizHint').innerHTML = dirty
      ? '✏️ 바꾼 내용은 아직 반영 전이에요. <b>적용 시작일을 고르고 [적용]</b>을 눌러야 그 날짜부터 반영됩니다.'
      : '🕒 오픈 근무자는 영업 시작 <b>전</b>에 출근하고, 마감 근무자는 영업 종료 <b>후</b>에 퇴근해요. '
        + '영업시간을 바꿔도 <b>과거 근무표는 그대로</b> 남습니다.';
    $('bizHint').className = 'note' + (dirty ? ' warn' : '');

    $('holChk').checked = !!CFG.showHoliday;
    $('wxChk').checked = !!CFG.showWeather;
    $('sphIn').value = CFG.salesPerHead || 35;

    $('closedDowHost').innerHTML = DOW.map(function (x, i) {
      return '<button class="' + ((CFG.closedDows || []).indexOf(i) >= 0 ? 'on' : '') + '"'
        + ' onclick="SCHED.toggleClosedDow(' + i + ')">' + x + '</button>';
    }).join('') + '<span class="cap" style="align-self:center;margin-left:6px;">고르면 매주 그 요일은 휴무</span>';

    $('closedDateHost').innerHTML = (CFG.closedDates || []).length
      ? CFG.closedDates.slice().sort().map(function (iso) {
          var hol = HOL[iso];
          return '<div class="erow"><b class="num">' + mdOf(iso) + '</b>'
            + '<span class="cap">' + DOW[dowOf(iso)] + '요일' + (hol ? ' · ' + esc(hol) : '') + '</span>'
            + '<button class="del" title="삭제" onclick="SCHED.delClosedDate(\'' + iso + '\')">✕</button></div>';
        }).join('')
      : '<div class="cap">임시 휴무일이 없어요.</div>';

    $('presetHost').innerHTML = (CFG.presets || []).map(function (p, i) {
      return '<div class="erow">'
        + '<input class="fld nameIn" type="text" value="' + esc(p.name) + '" aria-label="이름" onchange="SCHED.editPreset(' + i + ',\'name\',this.value)">'
        + tpick(p.s, { label: p.name + ' 시작', cb: function (v) { window.SCHED.editPreset(i, 's', v); } })
        + '<span class="cap">~</span>'
        + tpick(p.e, { label: p.name + ' 종료', cb: function (v) { window.SCHED.editPreset(i, 'e', v); } })
        + '<span class="cap num">' + (p.e > p.s ? hrs(p.e - p.s) + '시간' : '시간 거꾸로') + '</span>'
        + '<button class="del" title="삭제" onclick="SCHED.delPreset(' + i + ')">✕</button></div>';
    }).join('') || '<div class="cap">아래에서 근무 시간대를 추가해주세요.</div>';

    $('staffHost').innerHTML = (CFG.staff || []).map(function (s, i) {
      var cnt = 0;
      WEEKS.forEach(function (w) { w.days.forEach(function (day) { day.forEach(function (sh) { if (sh.w === s.name) cnt++; }); }); });
      return '<div class="erow">'
        + '<input class="sw" type="color" value="' + s.c + '" aria-label="' + esc(s.name) + ' 색" onchange="SCHED.editStaff(' + i + ',\'c\',this.value)">'
        + '<input class="fld nameIn" type="text" value="' + esc(s.name) + '" aria-label="이름" onchange="SCHED.editStaff(' + i + ',\'name\',this.value)">'
        + '<input class="fld roleIn" type="text" value="' + esc(s.role || '') + '" aria-label="역할" onchange="SCHED.editStaff(' + i + ',\'role\',this.value)">'
        + '<span class="cap num">' + cnt + '건</span>'
        + '<button class="del" title="삭제" onclick="SCHED.delStaff(' + i + ')">✕</button></div>';
    }).join('') || '<div class="cap">아래에서 직원을 추가해주세요.</div>';
  }

  // ── 전체 다시 그리기 ──────────────────────────────────────
  function renderAll() {
    recalcAxis();
    renderWeek(); renderDay(); renderSettings();
    renderStaffWeek(); renderMe();
  }

  // ── 바깥에서 부르는 것들 ──────────────────────────────────
  var PAL = ['#5a6b8c', '#2f8f83', '#7b68ae', '#c2587e', '#8a6d3b', '#3d7ea6', '#a35d3d', '#4f7a4a'];

  window.SCHED = {
    render: renderAll,
    moveWeek: function (d) { wkIdx = Math.max(0, Math.min(WEEKS.length - 1, wkIdx + d)); renderWeek(); },
    lock: function (on) { WEEKS[wkIdx].locked = !!on; saveWeek(wkIdx); renderAll(); },
    copyPrev: function () {
      var src = null;
      for (var i = wkIdx - 1; i >= 0; i--) {
        if (WEEKS[i].days.some(function (d) { return d.length; })) { src = WEEKS[i]; break; }
      }
      if (!src) { alert('가져올 지난주 근무가 없어요.'); return; }
      var wk = WEEKS[wkIdx];
      wk.days = src.days.map(function (day, di) {
        return isClosed(wk.iso[di], di) ? [] : day.map(function (sh) { return { w: sh.w, s: sh.s, e: sh.e }; });
      });
      saveWeek(wkIdx); renderAll();
    },
    pickWho: function (n) { md.who = n; renderModal(); },
    pickPreset: function (i) { var p = CFG.presets[i]; md.s = p.s; md.e = p.e; renderModal(); },
    modalTime: function () { md.s = toH($('mdStart').value); md.e = toH($('mdEnd').value); renderModal(); },
    _tp: function (n) {   // 시/분 드롭다운이 바뀌면 값을 합쳐서 전달한다
      var box = $(n); if (!box) return;
      var sels = box.querySelectorAll('select');
      var v = pad(+sels[0].value) + ':' + pad(+sels[1].value);
      if (box.dataset.hid) { var h = $(box.dataset.hid); if (h) h.value = v; }
      if (_tpCb[n]) _tpCb[n](v);
    },
    closeModal: function () { md = null; if ($('modal')) $('modal').hidden = true; },
    saveModal: function () {
      if (!md || !(md.e > md.s) || !md.who) return;
      var day = WEEKS[md.wi].days[md.di];
      if (md.mode === 'add') day.push({ w: md.who, s: md.s, e: md.e });
      else { day[md.idx].w = md.who; day[md.idx].s = md.s; day[md.idx].e = md.e; }
      var wi = md.wi;
      window.SCHED.closeModal(); saveWeek(wi); renderAll();
    },
    deleteModal: function () {
      if (!md || md.mode !== 'edit') return;
      WEEKS[md.wi].days[md.di].splice(md.idx, 1);
      var wi = md.wi;
      window.SCHED.closeModal(); saveWeek(wi); renderAll();
    },
    toggleEdit: function (id) { $(id).classList.toggle('editing'); },
    recOk: function (i) {
      var c = todayCell(); if (!c) return;
      var sh = WEEKS[c.wi].days[c.di][i];
      sh.st = 'ok'; delete sh.note; delete sh.actual;
      saveWeek(c.wi); renderAll();
    },
    recAbsent: function (i) {
      var c = todayCell(); if (!c) return;
      var sh = WEEKS[c.wi].days[c.di][i];
      sh.st = 'diff'; sh.note = '결근'; sh.actual = '결근';
      saveWeek(c.wi); renderAll();
    },
    recDiff: function (i, id) {
      var c = todayCell(); if (!c) return;
      var sh = WEEKS[c.wi].days[c.di][i];
      sh.st = 'diff';
      sh.note = $(id + 'r').value;
      sh.actual = $(id + 's').value + '–' + $(id + 'e').value;
      saveWeek(c.wi); renderAll();
    },
    pickMe: function (n) {
      meName = n;
      try { localStorage.setItem('beargels-sched-me', n); } catch (_) {}
      renderMe(); renderStaffWeek();
    },
    // 설정
    editBizDow: function (i, which, v) { ensureBizDraft()[i][which] = toH(v); renderSettings(); },
    fillBizAll: function () {
      var d = ensureBizDraft();
      for (var i = 1; i < 7; i++) { d[i][0] = d[0][0]; d[i][1] = d[0][1]; }
      renderSettings();
    },
    applyBiz: function () {
      var d = ensureBizDraft();
      for (var i = 0; i < 7; i++) if (!(d[i][1] > d[i][0])) { alert(DOW[i] + '요일 영업 종료가 시작보다 빨라요.'); return; }
      var from = $('bizFromIn').value;
      if (!from) { alert('적용 시작일을 골라주세요.'); return; }
      CFG.bizHours = (CFG.bizHours || []).filter(function (e) { return e.from !== from; });
      CFG.bizHours.push({ from: from, dows: d.map(function (p) { return [p[0], p[1]]; }) });
      CFG.bizHours.sort(function (a, b) { return String(a.from).localeCompare(String(b.from)); });
      bizDraft = null; saveConfig(); renderAll();
    },
    delBiz: function (from) {
      if ((CFG.bizHours || []).length <= 1) { alert('영업시간 기록은 최소 하나는 있어야 해요.'); return; }
      if (!confirm(mdOf(from) + '부터 적용된 영업시간 기록을 지울까요?\n그 이후 날짜는 이전 기록을 따라갑니다.')) return;
      CFG.bizHours = CFG.bizHours.filter(function (e) { return e.from !== from; });
      bizDraft = null; saveConfig(); renderAll();
    },
    toggleClosedDow: function (i) {
      CFG.closedDows = CFG.closedDows || [];
      var at = CFG.closedDows.indexOf(i);
      if (at >= 0) CFG.closedDows.splice(at, 1);
      else {
        var cnt = WEEKS.reduce(function (a, w) { return a + w.days[i].length; }, 0);
        if (cnt && !confirm(DOW[i] + '요일에 짜둔 근무 ' + cnt + '건이 지워져요. 계속할까요?')) return;
        WEEKS.forEach(function (w, wi) { if (w.days[i].length) { w.days[i] = []; saveWeek(wi); } });
        CFG.closedDows.push(i);
      }
      saveConfig(); renderAll();
    },
    addClosedDate: function () {
      var v = $('closedDateIn').value;
      CFG.closedDates = CFG.closedDates || [];
      if (!v || CFG.closedDates.indexOf(v) >= 0) return;
      var cnt = 0;
      WEEKS.forEach(function (w) { w.iso.forEach(function (iso, di) { if (iso === v) cnt += w.days[di].length; }); });
      if (cnt && !confirm(mdOf(v) + '에 짜둔 근무 ' + cnt + '건이 지워져요. 계속할까요?')) return;
      WEEKS.forEach(function (w, wi) {
        w.iso.forEach(function (iso, di) { if (iso === v && w.days[di].length) { w.days[di] = []; saveWeek(wi); } });
      });
      CFG.closedDates.push(v); CFG.closedDates.sort();
      saveConfig(); renderAll();
    },
    delClosedDate: function (iso) {
      CFG.closedDates = (CFG.closedDates || []).filter(function (d) { return d !== iso; });
      saveConfig(); renderAll();
    },
    addPreset: function () {
      var bz = bizToday();
      (CFG.presets = CFG.presets || []).push({ name: '새 근무', s: bz.open, e: Math.min(bz.close, bz.open + 5) });
      saveConfig(); renderAll();
    },
    delPreset: function (i) { CFG.presets.splice(i, 1); saveConfig(); renderAll(); },
    editPreset: function (i, field, v) {
      var p = CFG.presets[i];
      if (field === 'name') p.name = (v || '').trim() || '이름 없음';
      else p[field] = toH(v);
      saveConfig(); renderAll();
    },
    addStaff: function () {
      CFG.staff = CFG.staff || [];
      var n = '새 직원', k = 2;
      while (staffOf(n)) n = '새 직원 ' + (k++);
      CFG.staff.push({ name: n, c: PAL[CFG.staff.length % PAL.length], role: '파트타이머' });
      saveConfig(); renderAll();
    },
    delStaff: function (i) {
      var name = CFG.staff[i].name, cnt = 0;
      WEEKS.forEach(function (w) { w.days.forEach(function (d) { d.forEach(function (sh) { if (sh.w === name) cnt++; }); }); });
      if (cnt && !confirm(name + ' 님의 근무 ' + cnt + '건도 같이 지워져요. 계속할까요?')) return;
      WEEKS.forEach(function (w, wi) {
        var hit = false;
        w.days = w.days.map(function (d) {
          var out = d.filter(function (sh) { return sh.w !== name; });
          if (out.length !== d.length) hit = true;
          return out;
        });
        if (hit) saveWeek(wi);
      });
      CFG.staff.splice(i, 1);
      if (meName === name) meName = null;
      saveConfig(); renderAll();
    },
    editStaff: function (i, field, v) {
      var s = CFG.staff[i];
      if (field === 'name') {
        var nv = (v || '').trim() || s.name;
        if (nv !== s.name && staffOf(nv)) { alert('같은 이름의 직원이 이미 있어요.'); renderSettings(); return; }
        WEEKS.forEach(function (w, wi) {
          var hit = false;
          w.days.forEach(function (d) { d.forEach(function (sh) { if (sh.w === s.name) { sh.w = nv; hit = true; } }); });
          if (hit) saveWeek(wi);
        });
        if (meName === s.name) meName = nv;
        s.name = nv;
      } else s[field] = v;
      saveConfig(); renderAll();
    },
    setSalesPerHead: function (v) {
      var n = parseFloat(v);
      if (n > 0) { CFG.salesPerHead = n; saveConfig(); renderAll(); }
    },
    setFlag: function (key, on) { CFG[key] = !!on; saveConfig(); renderAll(); },
    newToken: function () {
      if (!confirm('직원용 링크를 새로 만들까요?\n지금 링크는 더 이상 열리지 않아요.')) return;
      post(API + '/api/token', {}).then(function (r) {
        if ($('pubLink')) $('pubLink').textContent = r.url;
        flash('새 링크를 만들었어요. 단톡방에 다시 공유해주세요.');
      }).catch(function () { flash('링크를 새로 만들지 못했어요.', true); });
    },
    copyLink: function () {
      var t = $('pubLink') ? $('pubLink').textContent : '';
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(function () { flash('링크를 복사했어요'); });
    },
  };

  // ── 시작 ──────────────────────────────────────────────────
  document.addEventListener('pointermove', moveDrag);
  document.addEventListener('pointerup', endDrag);
  document.addEventListener('pointercancel', endDrag);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') window.SCHED.closeModal(); });
  window.addEventListener('resize', function () { fitEvents(document); });

  try { meName = localStorage.getItem('beargels-sched-me'); } catch (_) {}
  if (!meName && (CFG.staff || []).length) meName = CFG.staff[0].name;
  wkIdx = currentWeekIdx();
  renderAll();
})();
