/* HomeTube, the TV page. One page for every screen:
 *  - TV (remote): a 10-foot layout, rows that slide with the arrows, a player whose controls rise on
 *    any key and hide again; nothing depends on the TV browser scrolling for us.
 *  - Phone and tablet (touch): YouTube-style, the video pinned on top with the device's own controls
 *    (a tap always brings them back), chips under it, Queue and Up next as a list, back gesture closes.
 *  - Desktop (mouse, keyboard): the watch page with the browser's controls, hover and shortcuts.
 * Everything personal (subscriptions, history, Watch Later) is kept on the board. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var body = document.body;
  var BACK = [8, 27, 461, 10009, 166];          // Backspace, Escape and the TV remotes' Back keys
  var PLAYPAUSE = [179, 415, 19, 463, 10252];   // media play/pause, play, pause
  var FFWD = [417, 228], REWIND = [412, 227];
  var NEXTV = [425, 176, 427], PREVV = [424, 177, 428];   // track next/previous, channel up/down
  var DIRS = {37: 'left', 38: 'up', 39: 'right', 40: 'down'};
  var ICON = {play: 'M8 5v14l11-7z', pause: 'M6 19h4V5H6v14zm8-14v14h4V5h-4z',
              enter: 'M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
              exit: 'M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z'};
  var SPEEDS = [1, 1.25, 1.5, 2, 0.75];
  var qChoice = -1;   // the quality chosen: hls.js reports the one playing, which changes only after the switch
  var HLS_SRC = (document.currentScript && document.currentScript.getAttribute('data-hls')) || '/assets/hometube/hls.min.js';

  // ---- which screen is this: a TV with a remote, a touch screen, or a desktop
  var UA = navigator.userAgent || '';
  // a first guess from what the browser says; what the person actually does decides (below): TV browsers
  // often claim a touch screen, and do not all name themselves
  var isTouch = !!(window.matchMedia && matchMedia('(pointer: coarse)').matches) && ('ontouchstart' in window);
  var isTV = /VIDAA|Hisense|HiSmart|SMART-TV|SmartTV|Smart TV|Tizen|Web0S|webOS|NetCast|HbbTV|BRAVIA|AFT[A-Z]|CrKey|Android TV|GoogleTV|Opera TV|Roku|Philips|TV Safari/i.test(UA);
  var touched = false;   // a finger really touched the screen
  function setMode(tv) {
    isTV = tv;
    body.classList.toggle('tv', tv);
    body.classList.toggle('touch', !tv && isTouch);
    body.classList.toggle('desk', !tv && !isTouch);
  }
  setMode(isTV);
  // a real touch: touch mode (the layout for fingers, the device's own video controls)
  window.addEventListener('touchstart', function () {
    if (touched) return;
    touched = true; isTouch = true;
    if (isTV) { setMode(false); if (inPlayer()) { setFull(false); $('video').controls = true; } } else setMode(false);
  }, {passive: true});
  // arrow keys with no touch before them: a remote (or a keyboard): TV mode, even if the browser claims touch
  function remoteMode() {
    if (isTV || touched) return false;
    setMode(true);
    if (inPlayer()) { $('video').controls = false; setFull(true); $('playBtn').focus(); }
    return true;
  }

  var current = null, hls = null, dockTimer = null, saveTimer = null, warmTimer = null, upTimer = null, retryTimer = null;
  var subs = [], account = false, items = {}, warmed = {}, TOPICS = [];
  var queue = [], qi = -1, queueName = '', played = [], related = [], full = true, homeScroll = 0, lastCard = null, opening = 0;

  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function dur(s) { if (!s && s !== 0) return ''; s = Math.round(s); var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return (h ? h + ':' + (m < 10 ? '0' : '') : '') + m + ':' + (x < 10 ? '0' : '') + x; }
  function api(path, payload, keep) {
    // keep: the request finishes even if the page is closing (the position when the phone locks)
    return fetch('/api/tv/' + path, payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload), keepalive: !!keep} : {})
      .then(function (r) { return r.json().then(function (d) { if (!r.ok || d.ok === false) throw new Error(d.error || ('error ' + r.status)); return d; }); });
  }
  function status(text) { $('status').textContent = text || ''; }
  function inPlayer() { return !$('player').hidden; }
  function trackOf(strip) { return strip.querySelector('.tv-track') || strip; }

  // ---- lists: home is rows you scroll sideways; search, history and Watch Later are a grid
  function card(v) {
    items[v.id] = v;
    var progress = v.position && v.duration ? '<i class="bar" style="width:' + Math.min(100, v.position / v.duration * 100).toFixed(0) + '%"></i>' : '';
    var badge = v.live ? '<span class="d live">LIVE</span>' : (v.duration ? '<span class="d">' + dur(v.duration) + '</span>' : '');
    return '<article class="tv-card" tabindex="0" data-nav data-id="' + esc(v.id) + '"><div class="thumb"><img loading="lazy" decoding="async" alt="" src="' + esc(v.thumb) + '">' +
      badge + progress + '</div><div class="meta"><div class="t">' + esc(v.title) + '</div><div class="c">' + esc(v.channel || '') + '</div></div></article>';
  }
  function skeleton(n) { var out = ''; for (var i = 0; i < n; i++) out += '<div class="tv-card tv-skel"><div class="thumb"></div><div class="meta"><div class="t"></div></div></div>'; return out; }
  function tab(id) { ['homeBtn', 'recBtn', 'historyBtn', 'laterBtn'].forEach(function (b) { $(b).classList.toggle('on', b === id); }); }
  function focusFirst(scope, opened) {
    if (!isTV || inPlayer()) return;   // touch and mouse: nothing to focus, it would only show a ring
    var f = scope.querySelector('.tv-card[data-nav]'), a = document.activeElement;
    // a list you just opened (results, a section, a channel) takes focus on its first card; a home row
    // filling in later only takes it when nothing has focus, so it never pulls you off the sections
    var free = !a || a === body || !a.offsetParent;
    if (f && (free || (opened && a.closest && a.closest('.tv-top')))) focusCard(f);
  }
  function show(title, list, note) {
    $('rows').hidden = true; $('list').hidden = false;
    $('rowTitle').textContent = title;
    status(note || (list.length ? '' : 'Nothing here yet.'));
    $('grid').innerHTML = list.map(card).join('');
    window.scrollTo(0, 0);
    focusFirst($('grid'), true);
  }
  function row(title, key) {
    var sec = document.createElement('section'); sec.className = 'tv-row';
    if (key) sec.dataset.row = key;
    sec.innerHTML = '<h2>' + esc(title) + '</h2><div class="tv-strip"><div class="tv-track">' + skeleton(6) + '</div></div>';
    $('rows').appendChild(sec);
    return {fill: function (list) {
      if (!list.length) { sec.remove(); return; }
      sec.querySelector('.tv-track').innerHTML = list.map(card).join('');
      focusFirst($('rows'));
    }, drop: function () { sec.remove(); }};
  }
  // started and not finished (more than 30 s left), newest first
  function continueItems(h) {
    return (h.items || []).filter(function (v) { return v.position && v.duration && v.position < v.duration - 30; }).slice(0, 12);
  }
  // after a video closes: the row shows it at the front, with how far you got, without a reload
  function refreshContinue(lastId) {
    if ($('rows').hidden) return;
    api('history').then(function (h) {
      var list = continueItems(h), sec = $('rows').querySelector('.tv-row[data-row="continue"]');
      if (!list.length) { if (sec) sec.remove(); return; }
      if (!sec) {
        sec = document.createElement('section'); sec.className = 'tv-row'; sec.dataset.row = 'continue';
        sec.innerHTML = '<h2>Continue watching</h2><div class="tv-strip"><div class="tv-track"></div></div>';
        $('rows').insertBefore(sec, $('rows').firstChild);
      }
      var hadFocus = sec.contains(document.activeElement), strip = sec.querySelector('.tv-strip');
      trackOf(strip).innerHTML = list.map(card).join('');
      slide(strip, 0); strip.scrollLeft = 0;
      if (isTV && hadFocus) focusCard(trackOf(strip).querySelector('.tv-card[data-id="' + lastId + '"]') || trackOf(strip).querySelector('.tv-card'));
    }).catch(function () {});
  }
  function loadHome() {
    tab('homeBtn');
    $('list').hidden = true; $('rows').hidden = false; $('rows').innerHTML = '';
    status('');
    var waits = [];
    var cont = row('Continue watching', 'continue');
    waits.push(api('history').then(function (h) {
      var list = continueItems(h);
      cont.fill(list);
      list.slice(0, 2).forEach(function (v) { warm(v.id); });   // the likeliest picks get looked up now
    }).catch(cont.drop));
    var subsRow = row(account ? 'Your subscriptions' : 'Subscriptions');
    waits.push((account ? api('feed?name=subs') : api('home')).then(function (d) {
      if (d.subscriptions) subs = d.subscriptions;
      subsRow.fill(d.items || []);
    }).catch(subsRow.drop));
    TOPICS.forEach(function (t) {   // the board keeps these warm, so they come straight back
      var r = row(t[0]);
      waits.push(api('search?q=' + encodeURIComponent(t[1]) + '&topic=1').then(function (d) { r.fill((d.items || []).slice(0, 16)); }).catch(r.drop));
    });
    return Promise.all(waits);
  }
  function search(q) {
    if (!q.trim()) return;
    closeSearch();
    navigate({kind: 'search', q: q});
  }
  function openChannel(id, name) { navigate({kind: 'channel', id: id, name: name}); }
  function runList(title, path, waiting) {
    status(waiting || '');
    return api(path).then(function (d) { show(title, d.items || []); }).catch(function (e) { status(e.message); });
  }

  // ---- where you are: Home, then whatever was opened from it. Back steps back through that, on the
  // remote and with the phone's back gesture, instead of the browser leaving for the previous address.
  var stack = [{kind: 'home'}], exitArmed = 0;
  var TABS = {home: 'homeBtn', history: 'historyBtn', later: 'laterBtn', rec: 'recBtn'};
  function render(v, focusId) {
    var done;
    if (v.kind === 'home') done = loadHome();
    else if (v.kind === 'search') done = runList('Results for "' + v.q + '"', 'search?q=' + encodeURIComponent(v.q), 'Searching…');
    else if (v.kind === 'channel') done = runList(v.name || 'Channel', 'channel?id=' + encodeURIComponent(v.id), 'Loading channel…');
    else if (v.kind === 'history') done = runList('History', 'history');
    else if (v.kind === 'later') done = runList('Watch Later', account ? 'feed?name=later' : 'later');
    else if (v.kind === 'rec') done = runList('For you', 'feed?name=rec', 'Loading recommendations…');
    tab(TABS[v.kind] || null);
    if (focusId && isTV) done.then(function () {
      var c = document.querySelector('#rows:not([hidden]) .tv-card[data-id="' + focusId + '"], #list:not([hidden]) .tv-card[data-id="' + focusId + '"]');
      if (c) focusCard(c);
    });
  }
  function navigate(v) {
    var top = stack[stack.length - 1], a = document.activeElement;
    top.focusId = a && a.dataset ? a.dataset.id : null;
    var section = v.kind in TABS;
    var replaceTab = section && stack.length === 2 && stack[1].kind in TABS;   // section to section: no extra step
    stack = section ? [stack[0]].concat(v.kind === 'home' ? [] : [v]) : stack.concat([v]);
    if (!isTV && v.kind !== 'home') {
      try { history[replaceTab ? 'replaceState' : 'pushState']({hometube: 'view'}, ''); } catch (e) { /* no history */ }
    }
    render(v);
  }
  function back() {
    if (stack.length < 2) return false;
    stack.pop();
    var v = stack[stack.length - 1];
    render(v, v.focusId);
    return true;
  }
  function toast(text) {
    var el = $('toast');
    el.textContent = text; el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(function () { el.hidden = true; }, 2800);
  }
  function openSearch() { body.classList.add('searching'); $('q').focus(); }
  function closeSearch() { body.classList.remove('searching'); if (document.activeElement === $('q') && !isTV) $('q').blur(); }

  // a lookup takes 15 to 18 s on the board: start it while a card is focused, before OK is pressed
  function warm(id) {
    if (!id || warmed[id] && Date.now() - warmed[id] < 600000) return;
    warmed[id] = Date.now();
    api('warm?id=' + encodeURIComponent(id)).catch(function () {});
  }

  // ---- the player library is loaded the first time something plays, not with the page
  var hlsLoading = null;
  function needHls() {
    if (window.Hls) return Promise.resolve();
    if (!hlsLoading) hlsLoading = new Promise(function (ok, fail) {
      var s = document.createElement('script'); s.src = HLS_SRC; s.onload = ok; s.onerror = function () { hlsLoading = null; fail(new Error('The player did not load.')); };
      document.head.appendChild(s);
    });
    return hlsLoading;
  }

  // ---- player
  function siblingsOf(el) {
    var box = el && el.closest('.tv-track, .tv-grid');
    return box ? Array.prototype.map.call(box.querySelectorAll('.tv-card[data-id]'), function (c) { return c.dataset.id; }) : [];
  }
  function openFromCard(el) {
    var ids = siblingsOf(el), r = el.closest('.tv-row');
    var name = r ? r.querySelector('h2').textContent : $('rowTitle').textContent;
    play(el.dataset.id, ids, ids.indexOf(el.dataset.id), name);
  }
  function friendly(msg) {   // no yt-dlp internals on a TV: the gist, without URLs and tool names
    msg = String(msg || 'This video did not open.').replace(/https?:\/\/\S+/g, '').replace(/^ERROR:\s*(\[[^\]]+\]\s*)?[A-Za-z0-9_-]{11}:\s*/, '');
    msg = msg.replace(/\s*(Use|See|Also see)\b.*$/, '').trim();
    return msg.length > 160 ? msg.slice(0, 157) + '…' : (msg || 'This video did not open.');
  }
  function play(id, list, index, name, tries) {
    clearTimeout(retryTimer);
    queue = list && list.length ? list : [id];
    qi = index >= 0 ? index : queue.indexOf(id);
    if (name !== undefined) queueName = name;
    if (!inPlayer()) {
      homeScroll = window.pageYOffset || 0;
      body.classList.add('playing');
      $('player').hidden = false;
      setFull(isTV);   // the TV opens full screen; phones and desktops open the watch page
      // phones and desktops: an entry in the history, so the back gesture closes the video. Not on a TV:
      // its browser may go back by itself on Back, which would then leave the page
      if (!isTV) { try { if (!history.state || !history.state.hometube) history.pushState({hometube: 'player'}, ''); } catch (e) { /* no history */ } }
    }
    if (current && current.id !== id) played.push(current.id);
    teardown();
    var seen = items[id] || {};
    current = {id: id, title: seen.title, channel: seen.channel};
    $('vTitle').textContent = seen.title || '';
    $('vChannel').textContent = seen.channel || '';
    $('player').scrollTop = 0;
    spinner('Opening…');
    wake(true);
    if (isTV) $('playBtn').focus();
    renderQueue();
    showLower(queue.length > 1 ? 'queue' : 'next');
    loadRelated(id);
    var token = ++opening;
    // the resume point is read alongside the lookup, so the player starts loading right there
    var resumeAt = api('history').then(function (h) {
      var s = (h.items || []).filter(function (x) { return x.id === id; })[0];
      return s && s.position && (!s.duration || s.position < s.duration - 30) ? s.position : -1;
    }).catch(function () { return -1; });
    Promise.all([api('video?id=' + encodeURIComponent(id)), resumeAt, needHls().catch(function () {})]).then(function (all) {
      if (token !== opening) return;   // another video was chosen meanwhile
      var v = all[0], startAt = all[1];
      if (!v.hls) throw new Error('No playable stream for this video.');
      current = v; items[v.id] = v;
      $('vTitle').textContent = v.title || '';
      $('vChannel').textContent = (v.channel || '') + (v.live ? ' · LIVE' : '');
      $('subBtn').textContent = subs.some(function (s) { return s.id === v.channel_id; }) ? 'Unsubscribe' : 'Subscribe';
      $('laterAdd').textContent = 'Watch Later';
      buildMenus(v);
      spinner('Loading video…');
      var video = $('video');
      video.controls = !isTV;   // touch and mouse get the device's own controls
      if (window.Hls && window.Hls.isSupported()) {
        hls = new window.Hls({maxBufferLength: 30, startPosition: startAt});
        hls.loadSource(v.hls);
        hls.attachMedia(video);
        hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
          // always the highest quality: lock to the top level (hls.js sorts levels by bitrate)
          var top = hls.levels.length - 1;
          if (top >= 0) { qChoice = top; hls.currentLevel = top; }
          syncQuality();
          start();
        });
        hls.on(window.Hls.Events.LEVEL_SWITCHED, syncQuality);
        hls.on(window.Hls.Events.ERROR, function (_e, data) { if (data.fatal) spinner('Playback error: ' + data.details, true); });
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = v.hls;   // Safari and iPhone play HLS themselves
        video.addEventListener('loadedmetadata', function () { if (startAt > 0) video.currentTime = startAt; start(); }, {once: true});
      } else { throw new Error('This browser cannot play HLS video.'); }
    }).catch(function (e) {
      if (token !== opening) return;
      if (/not a bot|confirm you|sign in to confirm/i.test(e.message) && (tries || 0) < 9) {
        // YouTube refuses the server this board goes out by: the board moves to another one within a
        // minute or two, so try again by ourselves instead of leaving an error on the screen
        spinner('YouTube is refusing this connection right now. Moving to another server; the video starts by itself.', false);
        retryTimer = setTimeout(function () { if (token === opening) play(id, queue, qi, undefined, (tries || 0) + 1); }, 20000);
        return;
      }
      spinner(friendly(e.message), true);
    });
  }
  function start() {
    $('video').play().catch(function () { syncPlay(); });
    clearInterval(saveTimer);
    saveTimer = setInterval(remember, 10000);
  }
  function remember(keep) {   // how far you got: every 10 s, on pause and seek, when the page goes away, on close
    var video = $('video');
    if (!current || !current.hls || !current.duration && !isFinite(video.duration) || !isFinite(video.currentTime) || !video.currentTime) return Promise.resolve();
    return api('history', {item: {id: current.id, title: current.title, channel: current.channel, channel_id: current.channel_id,
                                  position: video.currentTime, duration: isFinite(video.duration) ? video.duration : current.duration}}, keep).catch(function () {});
  }
  function teardown() {
    clearInterval(saveTimer); cancelUpNext(); clearTimeout(retryTimer);
    var saved = remember();
    var video = $('video');
    video.pause();
    if (hls) { hls.destroy(); hls = null; }
    video.removeAttribute('src'); video.load();
    while (video.firstChild) video.removeChild(video.firstChild);
    setProgress(0, 0, 0);
    setMore(false);
    return saved;
  }
  // closing goes through the browser history, so the phone's back gesture and Back do the same thing
  function closePlayer() {
    if (history.state && history.state.hometube === 'player') { history.back(); return; }
    reallyClose();
  }
  function reallyClose() {
    if (!inPlayer()) return;
    var last = current;
    var saved = teardown();
    opening++;
    current = null; played = [];
    try { if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(function () {}); } catch (e) { /* none */ }
    $('player').hidden = true;
    body.classList.remove('playing');
    window.scrollTo(0, homeScroll);
    if (isTV) {
      var c = last && document.querySelector('#rows:not([hidden]) .tv-card[data-id="' + last.id + '"], #list:not([hidden]) .tv-card[data-id="' + last.id + '"]');
      focusCard(c || lastCard || $('q'));
    }
    if (last) saved.then(function () { refreshContinue(last.id); });   // once the position is stored
  }
  window.addEventListener('popstate', function () {
    if (inPlayer()) { reallyClose(); return; }
    back();   // the phone's back gesture: one step back inside HomeTube
  });
  function nextId() {
    if (qi + 1 < queue.length) return {id: queue[qi + 1], list: queue, index: qi + 1};
    if (related.length) return {id: related[0].id, list: related.map(function (r) { return r.id; }), index: 0, name: 'Suggested'};
    return null;
  }
  function next() { var n = nextId(); if (n) play(n.id, n.list, n.index, n.name); }
  function prev() {
    var video = $('video');
    if (video.currentTime > 15 || (!played.length && qi <= 0)) { video.currentTime = 0; return; }   // like YouTube: first back to the start
    if (played.length) {   // back through what was played, without adding the current video to that list
      var id = played.pop();
      remember(); current = null;
      play(id, queue, queue.indexOf(id)); return;
    }
    play(queue[qi - 1], queue, qi - 1);
  }
  function loadRelated(id) {
    related = [];
    trackOf($('relStrip')).innerHTML = skeleton(6);
    api('related?id=' + encodeURIComponent(id)).then(function (d) {
      if (!current || current.id !== id) return;
      related = d.items || [];
      trackOf($('relStrip')).innerHTML = related.length ? related.map(card).join('') : '<p class="tv-empty">Nothing suggested for this video.</p>';
      slide($('relStrip'), 0);
    }).catch(function () {
      // no suggestions from YouTube: the rest of the list it was opened from
      related = queue.slice(qi + 1).map(function (q) { return items[q]; }).filter(Boolean);
      trackOf($('relStrip')).innerHTML = related.map(card).join('');
    });
  }

  // ---- the queue: what was opened together (a row, a search, a mix or playlist), now-playing marked
  function renderQueue() {
    var has = queue.length > 1;
    $('tabQueue').hidden = !has;
    $('tabQueue').textContent = (queueName || 'Queue') + ' · ' + (qi + 1) + ' of ' + queue.length;
    $('vQueue').textContent = has ? (queueName || 'Queue') + ' · ' + (qi + 1) + ' of ' + queue.length : '';
    var t = trackOf($('queueStrip'));
    if (!has) { t.innerHTML = ''; return; }
    t.innerHTML = queue.map(function (qid, i) {
      return card(items[qid] || {id: qid, title: '', thumb: '/assets/hometube/mark.svg'}).replace('class="tv-card"', 'class="tv-card' + (i === qi ? ' now' : '') + '"');
    }).join('');
    revealNow();
  }
  function revealNow() {   // bring the playing video into view in the queue, without moving focus
    var strip = $('queueStrip'), now = strip.querySelector('.tv-card.now');
    if (now && !strip.hidden) reveal(now, true);
  }
  function showLower(which) {
    if (which === 'queue' && $('tabQueue').hidden) which = 'next';
    $('queueStrip').hidden = which !== 'queue'; $('relStrip').hidden = which === 'queue';
    $('tabQueue').classList.toggle('on', which === 'queue'); $('tabNext').classList.toggle('on', which !== 'queue');
    if (which === 'queue') revealNow();
  }
  function lowerCards() { return cardsIn($('queueStrip').hidden ? $('relStrip') : $('queueStrip')); }
  function startMix() {
    if (!current) return;
    var id = current.id, label = $('mixLabel');
    label.textContent = 'Loading…';
    api('playlist?list=RD' + encodeURIComponent(id) + '&v=' + encodeURIComponent(id)).then(function (d) {
      label.textContent = 'Mix';
      if (!current || current.id !== id) return;
      (d.items || []).forEach(function (v) { if (!items[v.id]) items[v.id] = v; });
      var ids = (d.items || []).map(function (v) { return v.id; });
      if (ids.indexOf(id) < 0) ids.unshift(id);
      queue = ids; qi = ids.indexOf(id); queueName = d.title || 'Mix';
      renderQueue(); showLower('queue');
      if (isTV) { var now = $('queueStrip').querySelector('.tv-card.now'); focusCard(now && now.nextElementSibling || now); }
    }).catch(function (e) { label.textContent = 'Mix'; spinner(e.message, true); setTimeout(function () { if (!$('video').paused) spinner(null); }, 4000); });
  }
  function playFromStrip(el) {
    if (el.closest('#queueStrip')) { play(el.dataset.id, queue, cardsIn($('queueStrip')).indexOf(el)); return; }
    var ids = related.map(function (r) { return r.id; });
    play(el.dataset.id, ids, ids.indexOf(el.dataset.id), 'Suggested');
  }

  // ---- player chrome: spinner, progress, the dock (controls + lists), full screen or windowed
  function spinner(text, isError) {
    $('spin').hidden = !text;
    $('spin').classList.toggle('err', !!isError);
    $('spinText').textContent = text || '';
  }
  function setProgress(pos, total, buffered) {
    $('tCur').textContent = dur(pos || 0); $('tDur').textContent = total ? dur(total) : (current && current.live ? 'LIVE' : '0:00');
    $('tPos').style.width = total ? Math.min(100, pos / total * 100).toFixed(2) + '%' : '0';
    $('tBuf').style.width = total ? Math.min(100, buffered / total * 100).toFixed(2) + '%' : '0';
  }
  function syncPlay() {
    var paused = $('video').paused;
    $('playIcon').setAttribute('d', paused ? ICON.play : ICON.pause);
    $('playLabel').textContent = paused ? 'Play' : 'Pause';
    $('playBtn').setAttribute('aria-label', paused ? 'Play' : 'Pause');
  }
  function dockShown() { return !$('dock').classList.contains('hide'); }
  function wake(keep) {
    $('dock').classList.remove('hide');
    clearTimeout(dockTimer);
    if (!full || keep) return;
    dockTimer = setTimeout(function () {
      var a = document.activeElement;
      if (full && !$('video').paused && !(a && a.closest && a.closest('.tv-related, #more'))) { $('dock').classList.add('hide'); if (a && a.blur) a.blur(); }
    }, 6000);
  }
  function hideDock() { if (full) { clearTimeout(dockTimer); setMore(false); $('dock').classList.add('hide'); if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); } }
  function setFull(on) {
    full = on;
    $('player').classList.toggle('full', on); $('player').classList.toggle('windowed', !on);
    $('fsIcon').setAttribute('d', on ? ICON.exit : ICON.enter);
    $('fsLabel').textContent = on ? 'Exit full screen' : 'Full screen';
    $('fsBtn').setAttribute('aria-label', $('fsLabel').textContent);
    wake();
  }
  function setMore(open) {
    $('dock').classList.toggle('more-open', open);
    $('moreBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  function togglePlay() { var v = $('video'); if (v.paused) v.play().catch(function () {}); else v.pause(); }
  function seek(by) {
    var v = $('video');
    if (!isFinite(v.duration) && !(current && current.live)) return;
    v.currentTime = Math.max(0, Math.min((v.duration || 1e9) - 1, v.currentTime + by));
  }
  function upNext() {
    var n = nextId();
    if (!n) { wake(true); return; }
    var left = 5;
    $('upnextTitle').textContent = (items[n.id] || {}).title || '';
    $('upnextSec').textContent = left;
    $('upnextHint').textContent = isTV ? 'OK: play now · Back: stay' : 'Tap to play now';
    $('upnext').hidden = false;
    upTimer = setInterval(function () {
      left -= 1; $('upnextSec').textContent = left;
      if (left <= 0) { cancelUpNext(); play(n.id, n.list, n.index, n.name); }
    }, 1000);
  }
  function cancelUpNext() { clearInterval(upTimer); upTimer = null; $('upnext').hidden = true; }

  // quality, speed, subtitles and chapters: one press steps to the next setting (no pop-up lists)
  function buildMenus(v) {
    $('ccLabel').textContent = 'Off'; $('ccBtn').hidden = !(v.subtitles || []).length; $('ccBtn').dataset.i = '-1';
    $('chapBtn').hidden = !(v.chapters || []).length;
    qChoice = -1; $('qLabel').textContent = 'Auto'; $('speedLabel').textContent = '1×'; $('video').playbackRate = 1;
  }
  function syncQuality() {
    var l = hls && hls.levels[qChoice];
    $('qLabel').textContent = !hls || qChoice < 0 || !l ? 'Auto' : (l.height || '?') + 'p';
  }
  function stepQuality() {   // highest, next lower, ..., Auto, highest again
    if (!hls || !hls.levels.length) return;
    var n = hls.levels.length;
    qChoice = qChoice < 0 ? n - 1 : (qChoice === 0 ? -1 : qChoice - 1);
    hls.currentLevel = qChoice;   // -1 hands the choice back to hls.js (Auto)
    syncQuality();
  }
  function stepSpeed() {
    var v = $('video'), i = SPEEDS.indexOf(v.playbackRate);
    v.playbackRate = SPEEDS[(i + 1) % SPEEDS.length];
    $('speedLabel').textContent = v.playbackRate + '×';
  }
  function stepCaptions() {
    if (!current || !(current.subtitles || []).length) return;
    var i = +$('ccBtn').dataset.i + 1;
    if (i >= current.subtitles.length) i = -1;
    $('ccBtn').dataset.i = String(i);
    setCaptions(i);
    $('ccLabel').textContent = i < 0 ? 'Off' : current.subtitles[i].name;
  }
  function nextChapter() {
    var v = $('video'), list = (current && current.chapters) || [];
    var n = list.filter(function (c) { return c.start > v.currentTime + 1; })[0];
    if (n) v.currentTime = n.start;
  }
  function setCaptions(idx) {
    var video = $('video');
    while (video.firstChild) video.removeChild(video.firstChild);
    if (idx < 0 || !current) return;
    var s = current.subtitles[idx];
    var track = document.createElement('track');
    track.kind = 'subtitles'; track.srclang = s.lang; track.label = s.name; track.default = true;
    track.src = '/api/tv/captions?id=' + encodeURIComponent(current.id) + '&lang=' + encodeURIComponent(s.lang) + '&auto=' + (s.auto ? 1 : 0);
    video.appendChild(track);
    track.addEventListener('load', function () { track.track.mode = 'showing'; });
  }

  // ---- rows. On the TV a row's cards slide with a transform: TV browsers often will not scroll a
  // row for a script, a transform always moves. Touch and mouse scroll the row themselves.
  function cardsIn(box) { return Array.prototype.slice.call(box.querySelectorAll('.tv-card[data-id]')); }
  function slide(strip, x) {
    var t = trackOf(strip);
    t._x = x;
    t.style.webkitTransform = t.style.transform = 'translate3d(' + (-x) + 'px,0,0)';
  }
  function reveal(el, quiet) {
    var strip = el.closest('.tv-strip');
    if (!strip) return;
    var pad = strip.clientWidth * 0.04;
    if (isTV) {
      var t = trackOf(strip), x = t._x || 0, left = el.offsetLeft, right = left + el.offsetWidth;
      if (left - x < pad) x = left - pad;
      else if (right - x > strip.clientWidth - pad) x = right - strip.clientWidth + pad;
      x = Math.max(0, Math.min(x, t.scrollWidth - strip.clientWidth + pad));
      if (x !== (t._x || 0)) slide(strip, x);
      strip.scrollLeft = 0;   // undo any scrolling the browser did on focus
    } else if (!quiet) {
      var r = el.getBoundingClientRect(), sr = strip.getBoundingClientRect();
      if (r.left < sr.left + pad) strip.scrollLeft -= sr.left + pad - r.left;
      else if (r.right > sr.right - pad) strip.scrollLeft += r.right - (sr.right - pad);
    } else {
      strip.scrollLeft = Math.max(0, el.offsetLeft - pad);
    }
  }
  function focusCard(el) {
    if (!el) return;
    el.focus({preventScroll: true});
    if (!el.classList || !el.classList.contains('tv-card')) return;
    if (!inPlayer()) lastCard = el;
    reveal(el);
    clearTimeout(warmTimer);
    warmTimer = setTimeout(function () { if (document.activeElement === el) warm(el.dataset.id); }, 900);
    if (inPlayer()) return;
    var box = el.closest('.tv-row') || el;
    var br = box.getBoundingClientRect(), top = document.querySelector('.tv-top').getBoundingClientRect().bottom;
    if (br.top < top + 8) window.scrollBy(0, br.top - top - 8);
    else if (br.bottom > window.innerHeight - 8) window.scrollBy(0, br.bottom - window.innerHeight + 8);
  }
  function nearestX(list, x) {
    var best = null, bestD = Infinity;
    list.forEach(function (c) { var r = c.getBoundingClientRect(), d = Math.abs(r.left + r.width / 2 - x); if (d < bestD) { bestD = d; best = c; } });
    return best;
  }
  function spatial(dir, nodes, from) {
    var r0 = from && from.getBoundingClientRect ? from.getBoundingClientRect() : {left: 0, top: 0, width: 0, height: 0};
    var cx = r0.left + r0.width / 2, cy = r0.top + r0.height / 2, best = null, bestScore = Infinity;
    nodes.forEach(function (n) {
      if (n === from) return;
      var r = n.getBoundingClientRect(); if (!r.width) return;
      var dx = r.left + r.width / 2 - cx, dy = r.top + r.height / 2 - cy;
      var ok = dir === 'left' ? dx < -4 : dir === 'right' ? dx > 4 : dir === 'up' ? dy < -4 : dy > 4;
      var horiz = dir === 'left' || dir === 'right';
      if (horiz && Math.abs(dy) > Math.max(r0.height, r.height) * 0.6) return;   // left/right stay on their own row
      if (!ok) return;
      var score = (horiz ? Math.abs(dx) : Math.abs(dy)) + (horiz ? Math.abs(dy) : Math.abs(dx)) * 2.5;
      if (score < bestScore) { bestScore = score; best = n; }
    });
    return best;
  }
  function headerNodes() { return Array.prototype.slice.call(document.querySelectorAll('.tv-top [data-nav]')).filter(function (n) { return n.offsetParent !== null; }); }
  function toTabs() { window.scrollTo(0, 0); (document.querySelector('.tv-tabs .on') || $('homeBtn')).focus(); }
  function moveHome(dir) {
    var from = document.activeElement;
    var isCard = from && from.classList && from.classList.contains('tv-card');
    var strip = isCard && from.closest('.tv-strip');
    if (strip) {
      var cards = cardsIn(strip), i = cards.indexOf(from);
      if (dir === 'left' || dir === 'right') { focusCard(cards[i + (dir === 'right' ? 1 : -1)]); return; }
      var rowsList = Array.prototype.slice.call(document.querySelectorAll('#rows .tv-row')).filter(function (r) { return cardsIn(r).length; });
      var target = rowsList[rowsList.indexOf(from.closest('.tv-row')) + (dir === 'down' ? 1 : -1)];
      var x = from.getBoundingClientRect(); x = x.left + x.width / 2;
      if (target) { focusCard(nearestX(cardsIn(target), x)); return; }
      if (dir === 'up') toTabs();
      return;
    }
    if (isCard && from.closest('#grid')) {
      var g = cardsIn($('grid')), k = g.indexOf(from), cols = 0, t0 = g[0].offsetTop;
      while (cols < g.length && g[cols].offsetTop === t0) cols++;
      var j = dir === 'left' ? k - 1 : dir === 'right' ? k + 1 : dir === 'up' ? k - cols : k + cols;
      if (dir === 'down' && j >= g.length && Math.floor(k / cols) < Math.floor((g.length - 1) / cols)) j = g.length - 1;
      if (j >= 0 && j < g.length && (dir === 'up' || dir === 'down' || Math.floor(j / cols) === Math.floor(k / cols))) { focusCard(g[j]); return; }
      if (dir === 'up' && j < 0) toTabs();
      return;
    }
    // in the header: search, tabs; down goes back to the cards
    var best = spatial(dir, headerNodes(), from);
    if (best) { best.focus(); return; }
    if (dir === 'down') {
      var back = lastCard && document.body.contains(lastCard) && lastCard.offsetParent ? lastCard : document.querySelector('#rows:not([hidden]) .tv-card[data-id], #list:not([hidden]) .tv-card[data-id]');
      focusCard(back);
    }
  }
  function movePlayer(dir) {
    var from = document.activeElement;
    var strip = from && from.classList && from.classList.contains('tv-card') && from.closest('.tv-strip');
    if (strip) {
      if (dir === 'left' || dir === 'right') { var cards = cardsIn(strip); focusCard(cards[cards.indexOf(from) + (dir === 'right' ? 1 : -1)]); }
      else if (dir === 'up') ($('tabQueue').classList.contains('on') ? $('tabQueue') : $('tabNext')).focus();
      return;
    }
    // the buttons only (about twenty): strip cards are entered from the tabs, so a key press stays cheap
    var nodes = Array.prototype.slice.call($('dock').querySelectorAll('[data-pnav]')).filter(function (n) { return n.offsetParent !== null; });
    if (!from || nodes.indexOf(from) < 0) { $('playBtn').focus(); return; }
    var best = spatial(dir, nodes, from);
    var tabs = [$('tabQueue'), $('tabNext')];
    if (best && tabs.indexOf(best) >= 0 && tabs.indexOf(from) < 0) best = $('tabQueue').classList.contains('on') ? $('tabQueue') : $('tabNext');
    if (best) { best.focus(); return; }
    if (dir === 'down') {
      var now = $('queueStrip').hidden ? null : $('queueStrip').querySelector('.tv-card.now');
      focusCard(now || lowerCards()[0]);
    }
  }

  document.addEventListener('keydown', function (e) {
    var k = e.keyCode, video = $('video'), act = document.activeElement;
    var typing = act && (act.tagName === 'INPUT' || act.tagName === 'TEXTAREA');
    if (DIRS[k] && !isTV && !typing && !touched && remoteMode() && !inPlayer() && !(act && act.closest && act.closest('[data-nav]'))) {
      // arrows before any touch: a remote. With nothing chosen yet, the first press lands on the first card
      var first = document.querySelector('#rows:not([hidden]) .tv-card[data-id], #list:not([hidden]) .tv-card[data-id]');
      if (first) { e.preventDefault(); focusCard(first); return; }
    }
    if (!inPlayer()) {
      if (BACK.indexOf(k) >= 0 && !(k === 8 && typing)) {
        if (body.classList.contains('searching')) { e.preventDefault(); closeSearch(); return; }
        if (!isTV) {   // phones and desktops: the same step as the browser's back
          if (history.state && history.state.hometube === 'view') { e.preventDefault(); history.back(); }
          return;
        }
        e.preventDefault();
        if (typing) { act.blur(); toTabs(); return; }
        if (back()) return;   // one step back: search results, a channel, a section
        // Home: first up to the sections, then a second Back within 3 s leaves HomeTube
        if (!(act && act.closest && act.closest('.tv-tabs'))) { toTabs(); return; }
        if (Date.now() - exitArmed > 3000) { exitArmed = Date.now(); toast('Press Back again to exit HomeTube'); return; }
        try { history.back(); } catch (err) { /* nowhere to go */ }
        return;
      }
      if (!isTV) return;
      if (typing && (k === 37 || k === 39)) return;   // moving inside the search words
      if (DIRS[k]) { e.preventDefault(); moveHome(DIRS[k]); return; }
      if (k === 13 && act && act.dataset && act.dataset.id) { e.preventDefault(); openFromCard(act); }
      return;
    }
    if (!$('upnext').hidden) {   // the countdown before the next video
      if (BACK.indexOf(k) >= 0) { e.preventDefault(); cancelUpNext(); wake(true); if (isTV) $('playBtn').focus(); return; }
      if (k === 13) { e.preventDefault(); var n = nextId(); cancelUpNext(); if (n) play(n.id, n.list, n.index, n.name); return; }
    }
    if (BACK.indexOf(k) >= 0 && !(k === 8 && typing)) {
      e.preventDefault();
      if (isTV && $('dock').classList.contains('more-open')) { setMore(false); $('moreBtn').focus(); return; }
      if (isTV && full && dockShown() && !video.paused) { hideDock(); return; }
      closePlayer(); return;
    }
    if (PLAYPAUSE.indexOf(k) >= 0) { e.preventDefault(); togglePlay(); wake(); return; }
    if (FFWD.indexOf(k) >= 0 || REWIND.indexOf(k) >= 0) { e.preventDefault(); seek(FFWD.indexOf(k) >= 0 ? 30 : -30); wake(); return; }
    if (NEXTV.indexOf(k) >= 0) { e.preventDefault(); next(); return; }
    if (PREVV.indexOf(k) >= 0) { e.preventDefault(); prev(); return; }
    if (!isTV) {   // desktop shortcuts, as on YouTube
      if (typing) return;
      var key = (e.key || '').toLowerCase();
      if (key === ' ' || key === 'k') { e.preventDefault(); togglePlay(); }
      else if (key === 'j' || k === 37) { e.preventDefault(); seek(-10); }
      else if (key === 'l' || k === 39) { e.preventDefault(); seek(10); }
      else if (key === 'n' && e.shiftKey) next();
      else if (key === 'p' && e.shiftKey) prev();
      else if (key === 'f' && video.requestFullscreen) { if (document.fullscreenElement) document.exitFullscreen(); else video.requestFullscreen(); }
      return;
    }
    if (full && !dockShown()) {   // nothing on screen but the picture: quick keys
      e.preventDefault();
      if (k === 37 || k === 39) { seek(k === 39 ? 10 : -10); wake(); $('playBtn').focus(); return; }
      if (k === 13) { togglePlay(); wake(); $('playBtn').focus(); return; }
      wake(); $('playBtn').focus(); return;
    }
    wake();
    if (DIRS[k]) { e.preventDefault(); movePlayer(DIRS[k]); return; }
    if (k === 13 && act && act.dataset && act.dataset.id && act.closest('.tv-related')) { e.preventDefault(); playFromStrip(act); }
  });
  document.addEventListener('click', function (e) {
    var c = e.target.closest('.tv-card[data-id]');
    if (!c) return;
    if (c.closest('.tv-related')) playFromStrip(c);
    else openFromCard(c);
  });
  document.addEventListener('focusin', function (e) {
    if (!inPlayer()) return;
    var browsing = !!(e.target.closest && e.target.closest('.tv-related'));
    if (browsing && !$('dock').classList.contains('browse')) { $('dock').classList.add('browse'); if (!e.target.dataset.id) revealNow(); }
    else if (!browsing) $('dock').classList.remove('browse');
    if ($('dock').contains(e.target)) wake();
  });
  // TV full screen with a pointer (some remotes have one): a click on the picture shows the controls
  $('stage').addEventListener('click', function (e) {
    if (!isTV || !full || e.target.closest('button')) return;
    if (dockShown()) hideDock(); else { wake(); $('playBtn').focus(); }
  });

  $('searchForm').addEventListener('submit', function (e) { e.preventDefault(); search($('q').value); });
  $('searchOpen').addEventListener('click', openSearch);
  $('searchClose').addEventListener('click', closeSearch);
  $('homeBtn').addEventListener('click', function () { navigate({kind: 'home'}); });
  $('historyBtn').addEventListener('click', function () { navigate({kind: 'history'}); });
  $('laterBtn').addEventListener('click', function () { navigate({kind: 'later'}); });
  $('recBtn').addEventListener('click', function () { navigate({kind: 'rec'}); });
  $('closeBtn').addEventListener('click', closePlayer);
  $('backFab').addEventListener('click', closePlayer);
  $('playBtn').addEventListener('click', togglePlay);
  $('backTen').addEventListener('click', function () { seek(-10); });
  $('fwdTen').addEventListener('click', function () { seek(10); });
  $('nextBtn').addEventListener('click', next);
  $('prevBtn').addEventListener('click', prev);
  $('mixBtn').addEventListener('click', startMix);
  $('moreBtn').addEventListener('click', function () {
    var open = !$('dock').classList.contains('more-open');
    setMore(open); wake(open);
    if (open) $('subBtn').focus();
  });
  $('fsBtn').addEventListener('click', function () { setFull(!full); $('fsBtn').focus(); });
  $('qBtn').addEventListener('click', stepQuality);
  $('speedBtn').addEventListener('click', stepSpeed);
  $('ccBtn').addEventListener('click', stepCaptions);
  $('chapBtn').addEventListener('click', nextChapter);
  $('upnext').addEventListener('click', function () { var n = nextId(); cancelUpNext(); if (n) play(n.id, n.list, n.index, n.name); });
  $('tabQueue').addEventListener('click', function () { showLower('queue'); });
  $('tabNext').addEventListener('click', function () { showLower('next'); });
  $('tabQueue').addEventListener('focus', function () { showLower('queue'); });
  $('tabNext').addEventListener('focus', function () { showLower('next'); });
  $('subBtn').addEventListener('click', function () {
    if (!current || !current.channel_id) return;
    var add = $('subBtn').textContent === 'Subscribe';
    api('subscribe', {id: current.channel_id, name: current.channel, add: add}).then(function (d) { subs = d.subscriptions; $('subBtn').textContent = add ? 'Unsubscribe' : 'Subscribe'; });
  });
  $('laterAdd').addEventListener('click', function () { if (current) api('later', {item: current}).then(function () { $('laterAdd').textContent = 'Saved'; }); });
  $('channelBtn').addEventListener('click', function () {
    if (!current || !current.channel_id) return;
    var id = current.channel_id, name = current.channel;
    reallyClose();
    try { if (history.state && history.state.hometube === 'player') history.replaceState(null, ''); } catch (e) { /* none */ }
    openChannel(id, name);
  });
  var video = $('video');
  video.addEventListener('waiting', function () { spinner('Loading…'); });
  video.addEventListener('playing', function () { spinner(null); status(''); syncPlay(); wake(); });
  video.addEventListener('pause', function () { syncPlay(); wake(true); remember(); });
  video.addEventListener('seeked', function () { remember(); });
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') remember(true); });
  window.addEventListener('pagehide', function () { remember(true); });
  video.addEventListener('play', syncPlay);
  video.addEventListener('timeupdate', function () {
    var b = video.buffered, end = b && b.length ? b.end(b.length - 1) : 0;
    setProgress(video.currentTime, isFinite(video.duration) ? video.duration : 0, end);
  });
  video.addEventListener('ended', function () { remember(); syncPlay(); upNext(); });
  Promise.all([
    api('config').then(function (c) { TOPICS = c.rows || []; }).catch(function () {}),
    api('account').then(function (d) { account = !!d.connected; $('recBtn').hidden = !account; }).catch(function () {})
  ]).then(function () { render(stack[0]); });
})();
