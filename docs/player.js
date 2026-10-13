/* Stream player logic. Text, timings and stage labels come from player-config.json. */
(() => {
  'use strict';

  const SELF = document.currentScript ? document.currentScript.src : location.href;
  const CONFIG_URL = new URL('player-config.json', SELF).href;

  // Used only if player-config.json cannot be loaded.
  const FALLBACK = {
    api_base_on_pages: 'https://automatic-stream-capture.onrender.com',
    pages_host_suffix: 'github.io',
    poll_ms: 2000,
    status_every_ticks: 8,
    keepalive_ms: 240000,
    ctrl_hide_ms: 3000,
    last_stream_max_age_hours: 6,
    bad_hosts_max: 20,
    stage_progress: {},
    stage_labels: {},
    messages: {
      waking_server: 'Server is waking up…',
      paste_hint: 'Paste a page or .m3u8 link below.',
      waiting_next: 'Waiting for the next capture…',
      no_stream: 'No stream found.',
      cannot_play_hls: 'This browser cannot play HLS.',
      reconnecting: 'Reconnecting to a fresh stream…',
      stream_set: 'Stream set',
      capture_started: 'Capture started',
      copied: 'Stream link copied',
      copy_failed: 'Copy failed',
      no_stream_yet: 'No stream yet',
      live_toast: 'Live',
      pill: {
        ready: 'Ready', idle: 'Idle', capturing: 'Capturing', loading: 'Loading',
        live: 'Live', no_stream: 'No stream', waking: 'Waking server', offline: 'Offline',
      },
    },
  };

  async function loadConfig() {
    try {
      const res = await fetch(CONFIG_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error('config ' + res.status);
      return { ...FALLBACK, ...(await res.json()) };
    } catch (_) {
      return FALLBACK;
    }
  }

  function start(C) {
    const M = C.messages;
    const API = location.hostname.endsWith(C.pages_host_suffix) ? C.api_base_on_pages : '';
    const M3U8 = /\.m3u8(\?|#|$)/i;
    const LAST_KEY = 'stream:last';
    const BAD_KEY = 'stream:badhosts';
    const LAST_MAX_AGE_MS = C.last_stream_max_age_hours * 3600 * 1000;

    const $ = (id) => document.getElementById(id);
    const stage = $('stage');
    const video = $('v');
    const overlay = $('overlay');
    const msg = $('msg');
    const spin = $('spin');
    const pill = $('pill');
    const bar = $('progress');
    const form = $('form');
    const src = $('src');
    const go = $('go');
    const copyBtn = $('copy');
    const age = $('age');
    const toast = $('toast');
    const playBtn = $('play');
    const seek = $('seek');
    const liveEl = $('live');
    const goLiveBtn = $('golive');
    const timeEl = $('time');
    const muteBtn = $('mute');
    const qualitySel = $('quality');
    const fsBtn = $('fs');

    let hls = null;
    let rawUrl = null;
    let proxyUrl = null;
    let usingProxy = false;
    let fragOk = false;
    let netRetries = 0;
    let pollTick = 0;
    let seeking = false;
    let isLive = false;
    let hideTimer = null;

    // ---------- helpers ----------
    function toastMsg(text) {
      toast.textContent = text;
      toast.classList.add('show');
      clearTimeout(toastMsg.t);
      toastMsg.t = setTimeout(() => toast.classList.remove('show'), 1600);
    }

    function setPill(kind, text) {
      pill.className = 'pill ' + kind;
      pill.textContent = text;
    }

    function showOverlay(text, isError = false) {
      overlay.classList.remove('hide');
      msg.textContent = text;
      msg.className = 'msg' + (isError ? ' err' : '');
      spin.style.display = isError ? 'none' : 'block';
    }

    function hideOverlay() { overlay.classList.add('hide'); }

    function ago(iso) {
      const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
      if (s < 60) return `${Math.floor(s)}s ago`;
      if (s < 3600) return `${Math.floor(s / 60)}m ago`;
      return `${Math.floor(s / 3600)}h ago`;
    }

    function explain(text) {
      try {
        const d = JSON.parse(text);
        return d.diagnosis || text;
      } catch (_) {
        return text || M.no_stream;
      }
    }

    function fmt(sec) {
      if (!isFinite(sec) || sec < 0) return '0:00';
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      const mm = h ? String(m).padStart(2, '0') : String(m);
      return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
    }

    function readJSON(key, fallback) {
      try {
        const v = JSON.parse(localStorage.getItem(key));
        return v === null || v === undefined ? fallback : v;
      } catch (_) { return fallback; }
    }

    function writeJSON(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* storage blocked */ }
    }

    function hostOf(url) {
      try { return new URL(url).host; } catch (_) { return ''; }
    }

    function directBlocked(url) {
      return readJSON(BAD_KEY, []).includes(hostOf(url));
    }

    function markDirectBlocked(url) {
      const host = hostOf(url);
      const list = readJSON(BAD_KEY, []);
      if (host && !list.includes(host)) {
        list.push(host);
        writeJSON(BAD_KEY, list.slice(-C.bad_hosts_max));
      }
    }

    function saveLast(url, proxy) {
      writeJSON(LAST_KEY, { url, proxy, t: Date.now() });
    }

    function loadLast() {
      const v = readJSON(LAST_KEY, null);
      if (v && v.url && Date.now() - v.t < LAST_MAX_AGE_MS) return v;
      return null;
    }

    // ---------- controls: show on tap, hide while playing ----------
    function showControls() {
      stage.classList.add('show-ctrl');
      clearTimeout(hideTimer);
      if (!video.paused) {
        hideTimer = setTimeout(() => stage.classList.remove('show-ctrl'), C.ctrl_hide_ms);
      }
    }

    // Tapping the picture only toggles the controls. It never pauses playback.
    stage.addEventListener('click', (e) => {
      if (e.target.closest('.ctrl')) return;
      if (stage.classList.contains('show-ctrl')) stage.classList.remove('show-ctrl');
      else showControls();
    });

    function setLive(live) {
      isLive = live;
      liveEl.classList.toggle('hide', !live);
      goLiveBtn.classList.toggle('hide', !live);
      seek.classList.toggle('hide', live);
      timeEl.classList.toggle('hide', live);
    }

    function updateTime() {
      const d = video.duration;
      if (d === Infinity) { setLive(true); return; }
      if (!isFinite(d) || isLive) return;
      timeEl.textContent = `${fmt(video.currentTime)} / ${fmt(d)}`;
      if (!seeking) seek.value = String(Math.round((video.currentTime / d) * 1000));
    }

    function fillQuality(inst) {
      qualitySel.innerHTML = '';
      const add = (value, label) => {
        const o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        qualitySel.appendChild(o);
      };
      add('-1', 'Auto');

      // One entry per resolution. If a resolution has several bitrates, keep the highest.
      const best = new Map();
      inst.levels.forEach((lvl, idx) => {
        if (!lvl.height) return;
        const cur = best.get(lvl.height);
        if (cur === undefined || (lvl.bitrate || 0) > (inst.levels[cur].bitrate || 0)) best.set(lvl.height, idx);
      });
      [...best.keys()].sort((a, b) => b - a).forEach((h) => add(String(best.get(h)), h + 'p'));
      qualitySel.value = '-1';
    }

    qualitySel.addEventListener('change', () => {
      if (hls) hls.currentLevel = Number(qualitySel.value); // -1 = Auto.
    });

    playBtn.addEventListener('click', () => {
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    });

    goLiveBtn.addEventListener('click', () => {
      if (hls && hls.liveSyncPosition) video.currentTime = hls.liveSyncPosition;
      else if (isFinite(video.duration)) video.currentTime = video.duration;
      video.play().catch(() => {});
      toastMsg(M.live_toast);
    });

    video.addEventListener('play', () => { playBtn.textContent = '❚❚'; showControls(); });
    video.addEventListener('pause', () => { playBtn.textContent = '▶'; showControls(); });
    video.addEventListener('timeupdate', updateTime);
    video.addEventListener('durationchange', updateTime);

    seek.addEventListener('input', () => { seeking = true; });
    seek.addEventListener('change', () => {
      const d = video.duration;
      if (isFinite(d)) video.currentTime = (Number(seek.value) / 1000) * d;
      seeking = false;
    });

    muteBtn.addEventListener('click', () => {
      video.muted = !video.muted;
      muteBtn.textContent = video.muted ? '🔇' : '🔊';
    });

    // Fullscreen only from this button, so the screen does not rotate by accident.
    fsBtn.addEventListener('click', () => {
      if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {});
      else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen(); // iPhone
    });

    // ---------- playback ----------
    function onHlsError(_e, d) {
      if (!d.fatal) return;

      if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
        if (!usingProxy && proxyUrl) {
          if (!fragOk) markDirectBlocked(rawUrl);
          usingProxy = true;
          fragOk = false;
          netRetries = 0;
          hls.loadSource(proxyUrl);
          return;
        }
        netRetries += 1;
        if (netRetries > 5) {
          rawUrl = null;
          netRetries = 0;
          showOverlay(M.reconnecting);
          return;
        }
        hls.startLoad();
      } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
        hls.recoverMediaError();
      } else {
        showOverlay('Player error: ' + d.details, true);
      }
    }

    function createHls() {
      hls = new Hls({
        enableWorker: true,
        capLevelToPlayerSize: true,
        startFragPrefetch: true,
        maxBufferLength: 10,
        backBufferLength: 20,
      });
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        fillQuality(hls);
        video.play().catch(() => {});
      });
      hls.on(Hls.Events.LEVEL_LOADED, (_e, data) => setLive(!!data.details.live));
      hls.on(Hls.Events.FRAG_LOADED, () => { fragOk = true; netRetries = 0; });
      hls.on(Hls.Events.ERROR, onHlsError);
    }

    function attach(url) {
      const hasHls = window.Hls && Hls.isSupported();
      qualitySel.classList.toggle('hide', !hasHls);

      if (hasHls) {
        if (!hls) createHls();
        usingProxy = !!proxyUrl && directBlocked(url);
        fragOk = false;
        netRetries = 0;
        hls.loadSource(usingProxy ? proxyUrl : url);
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = proxyUrl || url;
        video.play().catch(() => {});
      } else {
        showOverlay(M.cannot_play_hls, true);
      }
    }

    function playStream(url, proxy) {
      rawUrl = url;
      proxyUrl = proxy && !proxy.startsWith('http') ? API + proxy : proxy;
      hideOverlay();
      setPill('ok', M.pill.live);
      attach(url);
      saveLast(url, proxyUrl);
    }

    // ---------- server sync ----------
    async function keepAlive() {
      try {
        await fetch(API + '/api/status?ka=' + Date.now(), { cache: 'no-store' });
      } catch (_) { /* offline or server asleep */ }
    }

    async function checkStream() {
      try {
        const res = await fetch(API + '/api/stream?t=' + Date.now(), { cache: 'no-store' });
        if (res.status === 404) return;
        const data = await res.json();

        if (data.updated_at) {
          age.textContent = 'updated ' + ago(data.updated_at);
          age.classList.remove('hide');
        }
        if (data.url && data.url !== rawUrl) playStream(data.url, data.proxy_url);
      } catch (_) { /* server waking up; next tick retries */ }
    }

    async function checkStatus() {
      try {
        const res = await fetch(API + '/api/status?t=' + Date.now(), { cache: 'no-store' });
        const s = await res.json();
        const name = (s.stage && s.stage.name) || 'idle';

        if (rawUrl) return;

        const label = C.stage_labels[name];
        if (label) {
          setPill('warn', M.pill.capturing);
          bar.style.width = (C.stage_progress[name] || 0) + '%';
          showOverlay(label);
        } else if (name === 'timeout_no_stream') {
          setPill('err', M.pill.no_stream);
          bar.style.width = '0%';
          showOverlay(explain(s.stage.error), true);
        } else if (s.page_url) {
          setPill('', M.pill.idle);
          showOverlay(s.last_error ? explain(s.last_error) : M.waiting_next);
        } else {
          setPill('', M.pill.ready);
          showOverlay(M.paste_hint);
        }
      } catch (_) {
        if (!rawUrl) {
          setPill('warn', M.pill.waking);
          showOverlay(M.waking_server);
        }
      }
    }

    // ---------- user input ----------
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const value = src.value.trim();
      if (!value) return;

      const isStream = M3U8.test(value);
      const body = isStream ? { stream_url: value } : { page_url: value };

      go.disabled = true;
      try {
        const res = await fetch(API + '/api/target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

        rawUrl = null;
        proxyUrl = null;
        src.value = '';
        bar.style.width = '5%';
        setPill('warn', isStream ? M.pill.loading : M.pill.capturing);
        showOverlay(isStream ? M.pill.loading + '…' : M.capture_started + '…');
        toastMsg(isStream ? M.stream_set : M.capture_started);
        checkStream();
      } catch (err) {
        toastMsg('Failed: ' + err.message);
      } finally {
        go.disabled = false;
      }
    });

    copyBtn.addEventListener('click', async () => {
      if (!proxyUrl) return toastMsg(M.no_stream_yet);
      const link = proxyUrl.startsWith('http') ? proxyUrl : location.origin + proxyUrl;
      try {
        await navigator.clipboard.writeText(link);
        toastMsg(M.copied);
      } catch (_) {
        toastMsg(M.copy_failed);
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        keepAlive();
        checkStream();
        checkStatus();
      }
    });

    // ---------- start ----------
    // 1. Last known stream plays from cache immediately, before the server answers.
    const last = loadLast();
    if (last) playStream(last.url, last.proxy);

    // 2. Then sync with the server in the background.
    keepAlive();
    checkStream();
    checkStatus();
    setInterval(keepAlive, C.keepalive_ms);
    setInterval(() => {
      checkStream();
      pollTick += 1;
      if (!rawUrl || pollTick >= C.status_every_ticks) {
        pollTick = 0;
        checkStatus();
      }
    }, C.poll_ms);
  }

  loadConfig().then(start);
})();
