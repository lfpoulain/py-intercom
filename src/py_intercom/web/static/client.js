(() => {
  const FRAME_SAMPLES = 480;
  const TARGET_SR = 48000;
  const MAX_LOG_LINES = 80;
  const SETTINGS_KEY = "py-intercom-web-settings";
  const DISCOVERY_POLL_MS = 3000;

  const el = {
    serverIp: document.getElementById("serverIp"),
    serverPort: document.getElementById("serverPort"),
    name: document.getElementById("name"),
    mode: document.getElementById("mode"),
    btnConnect: document.getElementById("btnConnect"),
    btnDisconnect: document.getElementById("btnDisconnect"),
    btnPtt: document.getElementById("btnPtt"),
    btnMute: document.getElementById("btnMute"),
    muteIconOff: document.getElementById("muteIconOff"),
    muteIconOn: document.getElementById("muteIconOn"),
    muteLabel: document.getElementById("muteLabel"),
    statusLine: document.getElementById("statusLine"),
    log: document.getElementById("log"),
    txBar: document.getElementById("txBar"),
    rxBar: document.getElementById("rxBar"),
    connDot: document.getElementById("connDot"),
    connLabel: document.getElementById("connLabel"),
    volumeSlider: document.getElementById("volumeSlider"),
    volumeValue: document.getElementById("volumeValue"),
    discoverySelect: document.getElementById("discoverySelect"),
    btnRefreshDiscovery: document.getElementById("btnRefreshDiscovery"),
  };

  // --- Persistence ---
  const saveSettings = () => {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        serverIp: el.serverIp.value,
        serverPort: el.serverPort.value,
        name: el.name.value,
        mode: el.mode.value,
        volume: el.volumeSlider.value,
      }));
    } catch (_) {}
  };

  const loadSettings = () => {
    try {
      const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null");
      if (!s) return;
      if (s.serverIp) el.serverIp.value = s.serverIp;
      if (s.serverPort) el.serverPort.value = s.serverPort;
      if (s.name) el.name.value = s.name;
      if (s.mode) el.mode.value = s.mode;
      if (s.volume != null) {
        el.volumeSlider.value = s.volume;
        el.volumeValue.textContent = s.volume + "%";
      }
    } catch (_) {}
  };

  // --- Log ---
  const logLine = (msg) => {
    const d = document.createElement("div");
    d.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
    el.log.prepend(d);
    while (el.log.children.length > MAX_LOG_LINES) el.log.removeChild(el.log.lastChild);
  };

  const setStatus = (s) => { el.statusLine.textContent = s; };

  // --- UUID ---
  const uuidKey = "py-intercom-web-client-uuid";
  const getClientUuid = () => {
    let u = localStorage.getItem(uuidKey);
    if (!u) {
      u = ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
        (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
      );
      localStorage.setItem(uuidKey, u);
    }
    return u;
  };

  // --- State ---
  let socket = null;
  let joined = false;
  let audioCtx = null;
  let micStream = null;
  let micNode = null;
  let micProc = null;
  let playProc = null;
  let gainNode = null;
  let discoveryTimer = null;

  let pttActive = false;
  let muteActive = false;
  let outputVolume = 1.0;

  let playQueue = new Float32Array(0);
  let txMeter = 0;
  let rxMeter = 0;

  // --- Discovery ---
  const fetchDiscovery = async () => {
    try {
      const r = await fetch("/api/discovery");
      if (!r.ok) return;
      const list = await r.json();
      updateDiscoveryList(list);
    } catch (_) {}
  };

  const updateDiscoveryList = (list) => {
    const prev = el.discoverySelect.value;
    // Keep the first option
    while (el.discoverySelect.options.length > 1) el.discoverySelect.remove(1);
    if (!Array.isArray(list) || list.length === 0) return;
    for (const srv of list) {
      const opt = document.createElement("option");
      opt.value = JSON.stringify({ ip: srv.ip, port: srv.audio_port });
      opt.textContent = `${srv.server_name || "serveur"} (${srv.ip}:${srv.audio_port})`;
      el.discoverySelect.appendChild(opt);
    }
    // Restore selection if still present
    for (const opt of el.discoverySelect.options) {
      if (opt.value === prev) { el.discoverySelect.value = prev; break; }
    }
    // Auto-select first if IP field is empty
    if (!el.serverIp.value.trim() && list.length > 0) {
      el.discoverySelect.selectedIndex = 1;
      applyDiscoverySelection();
    }
  };

  const applyDiscoverySelection = () => {
    const val = el.discoverySelect.value;
    if (!val) return;
    try {
      const s = JSON.parse(val);
      el.serverIp.value = s.ip || "";
      el.serverPort.value = s.port || 5000;
    } catch (_) {}
  };

  const startDiscoveryPolling = () => {
    stopDiscoveryPolling();
    fetchDiscovery();
    discoveryTimer = setInterval(fetchDiscovery, DISCOVERY_POLL_MS);
  };

  const stopDiscoveryPolling = () => {
    if (discoveryTimer) { clearInterval(discoveryTimer); discoveryTimer = null; }
  };

  // --- Audio helpers ---
  const appendPlay = (x) => {
    if (playQueue.length === 0) { playQueue = x; return; }
    const y = new Float32Array(playQueue.length + x.length);
    y.set(playQueue, 0);
    y.set(x, playQueue.length);
    playQueue = y;
    const max = audioCtx ? Math.floor(audioCtx.sampleRate * 3) : (TARGET_SR * 3);
    if (playQueue.length > max) playQueue = playQueue.subarray(playQueue.length - max);
  };

  const popPlay = (n) => {
    if (playQueue.length >= n) {
      const out = new Float32Array(playQueue.subarray(0, n));
      playQueue = playQueue.subarray(n);
      return out;
    }
    const out = new Float32Array(n);
    if (playQueue.length > 0) { out.set(playQueue, 0); playQueue = new Float32Array(0); }
    return out;
  };

  const floatToInt16BytesLE = (x) => {
    const buf = new ArrayBuffer(x.length * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < x.length; i++) {
      let s = Math.max(-1, Math.min(1, x[i]));
      dv.setInt16(i * 2, s < 0 ? Math.round(s * 32768) : Math.round(s * 32767), true);
    }
    return new Uint8Array(buf);
  };

  const int16BytesLEToFloat = (u8) => {
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const n = Math.floor(u8.byteLength / 2);
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) out[i] = dv.getInt16(i * 2, true) / 32768;
    return out;
  };

  const peakOf = (buf) => {
    let p = 0;
    for (let i = 0; i < buf.length; i++) { const a = Math.abs(buf[i]); if (a > p) p = a; }
    return p;
  };

  // --- Meters ---
  const updateMeters = () => {
    el.txBar.style.width = `${Math.round(Math.max(0, Math.min(1, txMeter)) * 100)}%`;
    el.rxBar.style.width = `${Math.round(Math.max(0, Math.min(1, rxMeter)) * 100)}%`;
    txMeter *= 0.88;
    rxMeter *= 0.88;
    requestAnimationFrame(updateMeters);
  };
  requestAnimationFrame(updateMeters);

  // --- UI State ---
  const setConnState = (on) => {
    el.connDot.className = "conn-indicator " + (on ? "on" : "off");
    el.connLabel.textContent = on ? "Connecté" : "Déconnecté";
  };

  const setUiConnected = (isConnected) => {
    el.btnConnect.disabled = isConnected;
    el.btnDisconnect.disabled = !isConnected;
    el.btnPtt.disabled = !isConnected;
    el.btnMute.disabled = !isConnected;
    setConnState(isConnected);
    if (!isConnected) {
      el.btnPtt.classList.remove("active");
      setMuteUi(false);
      pttActive = false;
    }
  };

  const setMuteUi = (muted) => {
    muteActive = muted;
    el.btnMute.classList.toggle("active", muteActive);
    el.muteIconOff.classList.toggle("hidden", muteActive);
    el.muteIconOn.classList.toggle("hidden", !muteActive);
    el.muteLabel.textContent = muteActive ? "Unmute" : "Mute";
  };

  const setPtt = (active) => {
    if (el.btnPtt.disabled) return;
    pttActive = !!active;
    el.btnPtt.classList.toggle("active", pttActive);
    if (socket && joined) socket.emit("ptt", { active: pttActive });
  };

  const toggleMute = () => {
    if (el.btnMute.disabled) return;
    setMuteUi(!muteActive);
    if (socket && joined) socket.emit("mute", { muted: muteActive });
  };

  // --- Audio ---
  const setupAudio = async () => {
    if (audioCtx) return;

    // Create AudioContext — mobile browsers may ignore sampleRate hint
    const AC = window.AudioContext || window.webkitAudioContext;
    try {
      audioCtx = new AC({ sampleRate: TARGET_SR });
    } catch (_) {
      audioCtx = new AC();
    }

    // Mobile: AudioContext starts suspended, must resume inside user gesture
    if (audioCtx.state === "suspended") {
      await audioCtx.resume();
    }
    logLine(`audio: sr=${audioCtx.sampleRate} state=${audioCtx.state}`);

    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      try { await audioCtx.close(); } catch (_) {}
      audioCtx = null;
      throw new Error(
        "Micro indisponible: navigateur en HTTP non sécurisé. Ouvre l'UI en HTTPS (ou localhost) puis réessaie."
      );
    }

    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false }
      });
    } catch (err) {
      // Teardown context if mic denied
      try { await audioCtx.close(); } catch (_) {}
      audioCtx = null;
      throw new Error("Micro refusé: " + (err.message || err));
    }
    micNode = audioCtx.createMediaStreamSource(micStream);

    micProc = audioCtx.createScriptProcessor(2048, 1, 1);
    micNode.connect(micProc);
    micProc.connect(audioCtx.destination);

    let captureBuf = new Float32Array(0);
    let capturePhase = 0.0;

    micProc.onaudioprocess = (e) => {
      if (!socket || !joined) return;
      if (el.mode.value === "ptt" && !pttActive) { txMeter = 0; return; }
      if (muteActive) { txMeter = 0; return; }

      const ch0 = e.inputBuffer.getChannelData(0);
      txMeter = Math.max(txMeter, peakOf(ch0));
      const srcSR = audioCtx.sampleRate;

      const merged = new Float32Array(captureBuf.length + ch0.length);
      merged.set(captureBuf, 0);
      merged.set(ch0, captureBuf.length);
      captureBuf = merged;

      if (srcSR === TARGET_SR) {
        while (captureBuf.length >= FRAME_SAMPLES) {
          socket.emit("audio_in", floatToInt16BytesLE(captureBuf.subarray(0, FRAME_SAMPLES)));
          captureBuf = captureBuf.subarray(FRAME_SAMPLES);
        }
      } else {
        const ratio = srcSR / TARGET_SR;
        while (true) {
          const need = Math.ceil((FRAME_SAMPLES - 1) * ratio + capturePhase + 2);
          if (captureBuf.length < need) break;
          const out = new Float32Array(FRAME_SAMPLES);
          for (let i = 0; i < FRAME_SAMPLES; i++) {
            const pos = capturePhase + i * ratio;
            const i0 = Math.floor(pos);
            const frac = pos - i0;
            out[i] = captureBuf[i0] * (1 - frac) + captureBuf[Math.min(i0 + 1, captureBuf.length - 1)] * frac;
          }
          socket.emit("audio_in", floatToInt16BytesLE(out));
          const lastPos = capturePhase + (FRAME_SAMPLES - 1) * ratio + ratio;
          const drop = Math.floor(lastPos);
          capturePhase = lastPos - drop;
          if (drop > 0 && captureBuf.length >= drop) captureBuf = captureBuf.subarray(drop);
        }
      }
      if (captureBuf.length > srcSR * 2) { captureBuf = captureBuf.subarray(captureBuf.length - srcSR * 2); capturePhase = 0; }
    };

    gainNode = audioCtx.createGain();
    gainNode.gain.value = outputVolume;
    gainNode.connect(audioCtx.destination);

    playProc = audioCtx.createScriptProcessor(2048, 1, 1);
    playProc.onaudioprocess = (e) => { e.outputBuffer.getChannelData(0).set(popPlay(e.outputBuffer.length)); };
    playProc.connect(gainNode);
  };

  const teardownAudio = async () => {
    try { if (micProc) micProc.disconnect(); } catch (_) {}
    try { if (micNode) micNode.disconnect(); } catch (_) {}
    try { if (playProc) playProc.disconnect(); } catch (_) {}
    try { if (gainNode) gainNode.disconnect(); } catch (_) {}
    micProc = null; micNode = null; playProc = null; gainNode = null;
    try { if (micStream) micStream.getTracks().forEach(t => t.stop()); } catch (_) {}
    micStream = null;
    try { if (audioCtx) await audioCtx.close(); } catch (_) {}
    audioCtx = null;
    playQueue = new Float32Array(0);
  };

  // --- Connect / Disconnect ---
  const connect = async () => {
    if (socket) return;
    saveSettings();
    await setupAudio();

    socket = io({ transports: ["websocket", "polling"] });

    socket.on("connect", () => {
      setStatus("Backend web");
      setUiConnected(true);
      logLine("socket.io connect");
      socket.emit("join", {
        server_ip: el.serverIp.value.trim(),
        server_port: parseInt(el.serverPort.value, 10),
        name: el.name.value.trim() || "Plateau",
        client_uuid: getClientUuid(),
        mode: el.mode.value,
      });
    });

    socket.on("disconnect", () => {
      setStatus("Déconnecté");
      setUiConnected(false);
      joined = false;
      logLine("socket.io disconnect");
    });

    socket.on("server", (msg) => {
      if (!msg || !msg.type) return;
      if (msg.type === "joined") {
        joined = true;
        setStatus(`${msg.server_ip}:${msg.server_port}`);
        logLine(`joined (id=${msg.client_id})`);
      } else if (msg.type === "kick") {
        logLine(`kick: ${msg.message || ""}`);
        disconnect();
      } else if (msg.type === "error") {
        logLine(`error: ${msg.message || ""}`);
      } else if (msg.type === "left") {
        logLine("left server");
      }
    });

    socket.on("control", (msg) => {
      if (!msg) return;
      if (msg.type === "update" || msg.type === "welcome") {
        const cfg = msg.config || msg;
        if (cfg && cfg.muted !== undefined) setMuteUi(!!cfg.muted);
      }
    });

    socket.on("discovery", (list) => {
      updateDiscoveryList(list);
    });

    socket.on("audio_out", (pcm) => {
      if (!audioCtx) return;
      if (!(pcm instanceof ArrayBuffer) && !(pcm && pcm.buffer)) return;
      const u8 = pcm instanceof ArrayBuffer ? new Uint8Array(pcm) : new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
      const f48 = int16BytesLEToFloat(u8);
      rxMeter = Math.max(rxMeter, peakOf(f48));
      const sr = audioCtx.sampleRate;
      if (sr === TARGET_SR) { appendPlay(f48); return; }
      const ratio = TARGET_SR / sr;
      const dstLen = Math.floor(f48.length / ratio);
      const out = new Float32Array(dstLen);
      for (let i = 0; i < dstLen; i++) {
        const pos = i * ratio;
        const i0 = Math.floor(pos);
        const frac = pos - i0;
        out[i] = f48[i0] * (1 - frac) + f48[Math.min(i0 + 1, f48.length - 1)] * frac;
      }
      appendPlay(out);
    });

    socket.on("connect_error", (e) => {
      logLine(`connect_error: ${e && e.message ? e.message : e}`);
    });
  };

  const disconnect = async () => {
    if (socket) {
      try { socket.emit("leave"); } catch (_) {}
      try { socket.disconnect(); } catch (_) {}
      socket = null;
    }
    joined = false;
    setUiConnected(false);
    setStatus("Déconnecté");
    await teardownAudio();
  };

  // --- Event Listeners ---
  el.btnConnect.addEventListener("click", () => {
    connect().catch(e => logLine(`erreur: ${e && e.message ? e.message : e}`));
  });
  el.btnDisconnect.addEventListener("click", () => disconnect());

  el.btnPtt.addEventListener("mousedown", () => setPtt(true));
  el.btnPtt.addEventListener("mouseup", () => setPtt(false));
  el.btnPtt.addEventListener("mouseleave", () => { if (pttActive) setPtt(false); });
  el.btnPtt.addEventListener("touchstart", (e) => { e.preventDefault(); setPtt(true); });
  el.btnPtt.addEventListener("touchend", () => setPtt(false));

  el.btnMute.addEventListener("click", toggleMute);

  el.discoverySelect.addEventListener("change", applyDiscoverySelection);
  el.btnRefreshDiscovery.addEventListener("click", fetchDiscovery);

  el.mode.addEventListener("change", () => {
    if (socket && joined) socket.emit("mode", { mode: el.mode.value });
    saveSettings();
  });

  el.volumeSlider.addEventListener("input", () => {
    const pct = parseInt(el.volumeSlider.value, 10);
    el.volumeValue.textContent = pct + "%";
    outputVolume = pct / 100;
    if (gainNode) gainNode.gain.value = outputVolume;
    saveSettings();
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.repeat) return;
    if (e.code === "Space") { e.preventDefault(); setPtt(true); }
    else if (e.code === "KeyM") { e.preventDefault(); toggleMute(); }
  });

  document.addEventListener("keyup", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.code === "Space") { e.preventDefault(); setPtt(false); }
  });

  // --- Init ---
  loadSettings();
  setUiConnected(false);
  setStatus("Déconnecté");
  startDiscoveryPolling();
})();
