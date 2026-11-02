(() => {
  const $ = (id) => document.getElementById(id);

  const video = $("video");
  const iframe = $("iframe");
  const placeholder = $("placeholder");
  const videoWrap = $("videoWrap");
  const playPauseBtn = $("playPauseBtn");
  const seekBar = $("seekBar");
  const timeLabel = $("timeLabel");
  const viewersLabel = $("viewersLabel");
  const fsBtn = $("fsBtn");
  const urlInput = $("urlInput");
  const sendUrlBtn = $("sendUrlBtn");
  const createRoomBtn = $("createRoomBtn");
  const joinRoomBtn = $("joinRoomBtn");
  const loadInRoomBtn = $("loadInRoomBtn");
  const roomIdInput = $("roomIdInput");
  const statusMsg = $("statusMsg");
  const mediaList = $("mediaList");
  const roomLabel = $("roomLabel");
  const copyLinkBtn = $("copyLinkBtn");
  const chatLog = $("chatLog");
  const chatName = $("chatName");
  const chatInput = $("chatInput");
  const chatSendBtn = $("chatSendBtn");

  let ws = null;
  let roomId = null;
  let pingTimer = null;
  let reconnectTimer = null;
  let media = [];
  let selected = null;
  let isRemote = false;
  let duration = 0;
  let ytPlayer = null;
  let ytPoll = null;
  let roomUrl = null;

  function setStatus(text, cls) {
    statusMsg.textContent = text || "";
    statusMsg.className = "status " + (cls || "");
  }

  function fmt(t) {
    if (!isFinite(t)) return "0:00";
    const m = Math.floor(t / 60);
    const s = Math.floor(t % 60);
    return m + ":" + String(s).padStart(2, "0");
  }

  function showVideo() {
    video.classList.add("active");
    iframe.classList.remove("active");
    placeholder.classList.add("hidden");
  }
  function showIframe() {
    iframe.classList.add("active");
    video.classList.remove("active");
    placeholder.classList.add("hidden");
  }
  function showPlaceholder() {
    placeholder.classList.remove("hidden");
    video.classList.remove("active");
    iframe.classList.remove("active");
    video.pause();
    video.removeAttribute("src");
    iframe.removeAttribute("src");
  }

  function loadYTApi(cb) {
    if (window.YT && window.YT.Player) { cb(); return; }
    const tag = document.createElement("script");
    tag.src = "https://www.youtube.com/iframe_api";
    document.head.appendChild(tag);
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      if (typeof prev === "function") prev();
      cb();
    };
  }

  function isYT() {
    return !!(ytPlayer && iframe.classList.contains("active") && iframe.src.includes("youtube.com/embed"));
  }

  async function loadKinobox(m) {
    try {
      const res = await fetch(`https://api.kinobox.tv/api/players?kinopoisk=${encodeURIComponent(m.kinopoisk)}`, {
        headers: { "Accept": "application/json" }
      });
      const data = await res.json();
      const players = (data && data.data) || [];
      const p = players.find(x => x && x.iframeUrl) || players[0];
      if (p && p.iframeUrl) {
        iframe.src = p.iframeUrl;
        showIframe();
        return;
      }
    } catch (e) {
      console.error("kinobox error", e);
    }
    // fallback: embed original page (no sync)
    if (roomUrl) {
      iframe.src = roomUrl;
      showIframe();
    }
  }

  function loadSelected() {
    if (selected === null || !media[selected]) {
      showPlaceholder();
      return;
    }
    const m = media[selected];
    if (m.type === "video") {
      if (ytPlayer) { try { ytPlayer.destroy(); } catch(e){} ytPlayer = null; }
      if (ytPoll) { clearInterval(ytPoll); ytPoll = null; }
      showVideo();
      video.src = m.src;
      video.load();
    } else if (m.type === "kinobox") {
      if (ytPlayer) { try { ytPlayer.destroy(); } catch(e){} ytPlayer = null; }
      if (ytPoll) { clearInterval(ytPoll); ytPoll = null; }
      loadKinobox(m);
    } else {
      if (ytPlayer) { try { ytPlayer.destroy(); } catch(e){} ytPlayer = null; }
      if (ytPoll) { clearInterval(ytPoll); ytPoll = null; }
      if (m.src.includes("youtube.com/embed")) {
        const url = new URL(m.src, location.origin);
        url.searchParams.set("enablejsapi", "1");
        url.searchParams.set("origin", location.origin);
        const src = url.toString();
        iframe.src = src;
        showIframe();
        loadYTApi(() => {
          try {
            ytPlayer = new YT.Player(iframe, {
              events: {
                onReady: () => {
                  seekBar.disabled = false;
                  playPauseBtn.disabled = false;
                  // start polling time
                  ytPoll = setInterval(() => {
                    if (!isRemote && ytPlayer && typeof ytPlayer.getCurrentTime === "function") {
                      const t = ytPlayer.getCurrentTime();
                      const dur = ytPlayer.getDuration();
                      if (dur > 0) {
                        seekBar.value = (t / dur) * 1000;
                        timeLabel.textContent = fmt(t) + " / " + fmt(dur);
                      }
                    }
                  }, 500);
                },
                onStateChange: (e) => {
                  if (isRemote) return;
                  if (!ws || ws.readyState !== 1) return;
                  if (!ytPlayer || typeof ytPlayer.getCurrentTime !== "function") return;
                  const t = ytPlayer.getCurrentTime();
                  if (e.data === YT.PlayerState.PLAYING) {
                    ws.send(JSON.stringify({ type: "control", action: "play", time: t }));
                  } else if (e.data === YT.PlayerState.PAUSED) {
                    ws.send(JSON.stringify({ type: "control", action: "pause", time: t }));
                  }
                }
              }
            });
          } catch (err) {
            console.error("YT init error", err);
          }
        });
      } else {
        iframe.src = m.src;
        showIframe();
      }
    }
  }

  function renderMedia() {
    mediaList.innerHTML = "";
    if (!media.length) {
      mediaList.innerHTML = '<div class="empty">Пока ничего не найдено</div>';
      return;
    }
    media.forEach((m, i) => {
      const el = document.createElement("div");
      el.className = "media-item" + (i === selected ? " active" : "");
      el.innerHTML = `<span>${m.label || m.type}</span><span class="label">${m.type}</span>`;
      el.onclick = () => {
        selected = i;
        if (ws && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: "select", index: i }));
        }
        renderMedia();
        loadSelected();
      };
      mediaList.appendChild(el);
    });
  }

  function connectWS(rid) {
    roomId = rid;
    roomLabel.textContent = "Комната: " + rid;
    copyLinkBtn.disabled = false;
    if (pingTimer) clearInterval(pingTimer);
    if (reconnectTimer) clearTimeout(reconnectTimer);
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws/${rid}`);
    } catch (e) {
      setStatus("Не удалось открыть WebSocket: " + e.message, "err");
      scheduleReconnect(rid);
      return;
    }
    ws.onopen = () => {
      setStatus("Подключено", "ok");
      pingTimer = setInterval(() => {
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "ping" }));
      }, 25000);
    };
    ws.onclose = () => {
      setStatus("Отключено. Переподключение...", "err");
      if (pingTimer) clearInterval(pingTimer);
      scheduleReconnect(rid);
    };
    ws.onerror = () => setStatus("Ошибка соединения", "err");
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleMsg(msg);
    };
  }

  function scheduleReconnect(rid) {
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(() => connectWS(rid), 4000);
  }

  function handleMsg(msg) {
    switch (msg.type) {
      case "state":
        if (msg.room) {
          roomUrl = msg.room.url || roomUrl;
          media = msg.room.media || [];
          selected = msg.room.selected;
          renderMedia();
          loadSelected();
          if (msg.room.state) applyState(msg.room.state, true);
          if (msg.room.viewers !== undefined) viewersLabel.textContent = "👁 " + msg.room.viewers;
        }
        break;
      case "loaded":
        roomUrl = msg.url || roomUrl;
        media = msg.media || [];
        selected = msg.selected;
        renderMedia();
        loadSelected();
        if (msg.state) applyState(msg.state, true);
        if (msg.error) setStatus(msg.error, "err");
        else setStatus("Фильм загружен", "ok");
        break;
      case "selected":
        selected = msg.index;
        renderMedia();
        loadSelected();
        break;
      case "control":
        applyControl(msg);
        break;
      case "presence":
        if (msg.viewers !== undefined) viewersLabel.textContent = "👁 " + msg.viewers;
        break;
      case "chat":
        appendChat(msg.name, msg.text);
        break;
    }
  }

  function applyState(state, initial) {
    if (!state) return;
    if (state.playing !== undefined) {
      // store
    }
  }

  function applyControl(msg) {
    if (isYT()) {
      isRemote = true;
      try {
        if (msg.time !== undefined && msg.time !== null && ytPlayer && typeof ytPlayer.seekTo === "function") {
          ytPlayer.seekTo(msg.time, true);
        }
        if (msg.action === "play" && ytPlayer && typeof ytPlayer.playVideo === "function") {
          ytPlayer.playVideo();
        } else if (msg.action === "pause" && ytPlayer && typeof ytPlayer.pauseVideo === "function") {
          ytPlayer.pauseVideo();
        }
      } finally {
        setTimeout(() => { isRemote = false; }, 400);
      }
      playPauseBtn.textContent = msg.playing ? "⏸ Pause" : "▶ Play";
      return;
    }
    if (!video.classList.contains("active")) return;
    isRemote = true;
    try {
      if (msg.time !== undefined && msg.time !== null) {
        video.currentTime = msg.time;
      }
      if (msg.action === "play") {
        const p = video.play();
        if (p && p.catch) p.catch(() => {});
      } else if (msg.action === "pause") {
        video.pause();
      }
    } finally {
      setTimeout(() => { isRemote = false; }, 100);
    }
    playPauseBtn.textContent = msg.playing ? "⏸ Pause" : "▶ Play";
  }

  // video events
  video.addEventListener("loadedmetadata", () => {
    duration = video.duration;
    seekBar.disabled = false;
    playPauseBtn.disabled = false;
    timeLabel.textContent = fmt(0) + " / " + fmt(duration);
  });
  video.addEventListener("timeupdate", () => {
    if (!isRemote) {
      if (duration > 0) seekBar.value = (video.currentTime / duration) * 1000;
      timeLabel.textContent = fmt(video.currentTime) + " / " + fmt(duration || video.duration);
    }
  });
  video.addEventListener("play", () => {
    if (isRemote) return;
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "control", action: "play", time: video.currentTime }));
    }
    playPauseBtn.textContent = "⏸ Pause";
  });
  video.addEventListener("pause", () => {
    if (isRemote) return;
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "control", action: "pause", time: video.currentTime }));
    }
    playPauseBtn.textContent = "▶ Play";
  });
  video.addEventListener("seeked", () => {
    if (isRemote) return;
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: "control", action: "seek", time: video.currentTime }));
    }
  });

  playPauseBtn.addEventListener("click", () => {
    if (isYT()) {
      try {
        const st = ytPlayer.getPlayerState();
        if (st === 1) ytPlayer.pauseVideo(); else ytPlayer.playVideo();
      } catch (e) {}
      return;
    }
    if (video.paused) video.play(); else video.pause();
  });

  seekBar.addEventListener("input", () => {
    if (isYT() && ytPlayer && typeof ytPlayer.getDuration === "function") {
      const dur = ytPlayer.getDuration();
      const t = (seekBar.value / 1000) * dur;
      timeLabel.textContent = fmt(t) + " / " + fmt(dur);
      return;
    }
    if (duration > 0) {
      const t = (seekBar.value / 1000) * duration;
      timeLabel.textContent = fmt(t) + " / " + fmt(duration);
    }
  });
  seekBar.addEventListener("change", () => {
    if (isYT()) {
      if (ytPlayer && typeof ytPlayer.getDuration === "function") {
        const dur = ytPlayer.getDuration();
        const t = (seekBar.value / 1000) * dur;
        try { ytPlayer.seekTo(t, true); } catch(e){}
        if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "control", action: "seek", time: t }));
      }
      return;
    }
    if (duration > 0) {
      const t = (seekBar.value / 1000) * duration;
      video.currentTime = t;
    }
  });

  // fullscreen
  function toggleFullscreen() {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      if (videoWrap.requestFullscreen) videoWrap.requestFullscreen();
      else if (videoWrap.webkitRequestFullscreen) videoWrap.webkitRequestFullscreen();
    }
  }
  fsBtn.addEventListener("click", toggleFullscreen);
  // double click on video
  video.addEventListener("dblclick", toggleFullscreen);
  document.addEventListener("keydown", (e) => {
    if (e.key === "f" || e.key === "F") toggleFullscreen();
    if (e.key === " ") { e.preventDefault(); if (!video.disabled) playPauseBtn.click(); }
  });

  // room actions
  createRoomBtn.addEventListener("click", async () => {
    const url = urlInput.value.trim();
    if (!url) { setStatus("Введите URL фильма", "err"); return; }
    setStatus("Создаю комнату...", "");
    try {
      const res = await fetch("/api/rooms", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "ошибка");
      const rid = data.room_id;
      // update url in browser
      const u = new URL(location.href);
      u.searchParams.set("room", rid);
      history.replaceState(null, "", u.toString());
      connectWS(rid);
      media = data.media || [];
      selected = data.selected;
      renderMedia();
      loadSelected();
      if (data.state) applyState(data.state, true);
      setStatus("Комната создана: " + rid, "ok");
    } catch (e) {
      setStatus("Ошибка: " + e.message, "err");
    }
  });

  joinRoomBtn.addEventListener("click", () => {
    const rid = roomIdInput.value.trim();
    if (!rid) { setStatus("Введите ID комнаты", "err"); return; }
    const u = new URL(location.href);
    u.searchParams.set("room", rid);
    history.replaceState(null, "", u.toString());
    connectWS(rid);
    setStatus("Подключение...", "");
  });

  loadInRoomBtn.addEventListener("click", () => {
    const url = urlInput.value.trim();
    if (!url) { setStatus("Введите URL", "err"); return; }
    if (!ws || ws.readyState !== 1) {
      // create room first
      createRoomBtn.click();
      return;
    }
    ws.send(JSON.stringify({ type: "load", url }));
    setStatus("Загружаю...", "");
  });

  copyLinkBtn.addEventListener("click", () => {
    const u = new URL(location.href);
    navigator.clipboard.writeText(u.toString()).then(() => setStatus("Ссылка скопирована", "ok"));
  });

  function sendUrl() {
    const url = urlInput.value.trim();
    if (!url) { setStatus("Введите URL", "err"); return; }
    if (!ws || ws.readyState !== 1) {
      createRoomBtn.click();
    } else {
      ws.send(JSON.stringify({ type: "load", url }));
      setStatus("Загружаю...", "");
    }
  }
  if (sendUrlBtn) sendUrlBtn.addEventListener("click", sendUrl);
  if (urlInput) urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendUrl(); });

  chatSendBtn.addEventListener("click", sendChat);
  chatInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });
  function sendChat() {
    const text = chatInput.value.trim();
    if (!text || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "chat", name: chatName.value || "Гость", text }));
    chatInput.value = "";
  }
  function appendChat(name, text) {
    const div = document.createElement("div");
    div.className = "chat-msg";
    div.innerHTML = `<span class="name">${escapeHtml(name)}:</span> <span class="text">${escapeHtml(text)}</span>`;
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
  }

  // auto join from URL
  const params = new URLSearchParams(location.search);
  const r = params.get("room");
  if (r) {
    roomIdInput.value = r;
    connectWS(r);
  }
})();