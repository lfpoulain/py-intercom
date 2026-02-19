(() => {
  const FRAME_SAMPLES = 480;
  const TARGET_SR = 48000;
  const MAX_LOG_LINES = 80;
  const SETTINGS_KEY = "py-intercom-web-settings";
  const DISCOVERY_POLL_MS = 3000;

  const el = {
    serverIp: document.getElementById("serverIp"),
    name: document.getElementById("name"),
    btnConnect: document.getElementById("btnConnect"),
    btnDisconnect: document.getElementById("btnDisconnect"),
    btnBus0: document.getElementById("btnBus0"),
    btnBus1: document.getElementById("btnBus1"),
    btnBus2: document.getElementById("btnBus2"),
    listenRegie: document.getElementById("listenRegie"),
    listenReturnBus: document.getElementById("listenReturnBus"),
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
    inputDeviceSelect: document.getElementById("inputDeviceSelect"),
    outputDeviceSelect: document.getElementById("outputDeviceSelect"),
    outputDeviceGroup: document.getElementById("outputDeviceGroup"),
    btnRefreshDevices: document.getElementById("btnRefreshDevices"),
  };

  // --- Persistence ---
  const saveSettings = () => {
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify({
        serverIp: el.serverIp.value,
        name: el.name.value,
        listenRegie: el.listenRegie.checked,
        listenReturnBus: el.listenReturnBus.checked,
        volume: el.volumeSlider.value,
        inputDeviceId: el.inputDeviceSelect.value,
        outputDeviceId: el.outputDeviceSelect.value,
      }));
    } catch (_) {}
  };

  const loadSettings = () => {
    try {
      const s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "null");
      if (!s) return;
      if (s.serverIp) el.serverIp.value = s.serverIp;
      if (s.name) el.name.value = s.name;
      if (s.listenRegie != null) el.listenRegie.checked = !!s.listenRegie;
      if (s.listenReturnBus != null) el.listenReturnBus.checked = !!s.listenReturnBus;
      if (s.volume != null) {
        el.volumeSlider.value = s.volume;
        el.volumeValue.textContent = s.volume + "%";
      }
      // Device IDs are restored after enumerateDevices populates the lists
      if (s.inputDeviceId) el.inputDeviceSelect.dataset.savedId = s.inputDeviceId;
      if (s.outputDeviceId) el.outputDeviceSelect.dataset.savedId = s.outputDeviceId;
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
  let audioRateOk = true;

  const pttBuses = new Map([[0, false], [1, false], [2, false]]);
  let outputVolume = 1.0;

  let playQueue = new Float32Array(0);
  let txMeter = 0;
  let rxMeter = 0;
  let intercomAlive = false;
  let lastControlTs = 0;
  const CTRL_TIMEOUT_MS = 3000;
  let actualSR = TARGET_SR;

  // --- Device enumeration ---
  const sinkIdSupported = typeof HTMLMediaElement !== "undefined" &&
    typeof HTMLMediaElement.prototype.setSinkId === "function";

  const populateDeviceSelect = (select, devices, kind, restoreId) => {
    const prev = restoreId !== undefined ? restoreId : select.value;
    while (select.options.length > 1) select.remove(1);
    let idx = 0;
    let matched = false;
    for (const d of devices) {
      if (d.kind !== kind) continue;
      idx++;
      const opt = document.createElement("option");
      opt.value = d.deviceId;
      opt.textContent = d.label || `${kind === "audioinput" ? "Micro" : "Sortie"} ${idx}`;
      select.appendChild(opt);
      if (prev && d.deviceId === prev) { select.value = d.deviceId; matched = true; }
    }
    if (!matched) select.value = "";
  };

  const _doEnumerate = async (restoreIn, restoreOut) => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    const devices = await navigator.mediaDevices.enumerateDevices();
    populateDeviceSelect(el.inputDeviceSelect, devices, "audioinput", restoreIn);
    if (sinkIdSupported) {
      populateDeviceSelect(el.outputDeviceSelect, devices, "audiooutput", restoreOut);
    } else {
      if (el.outputDeviceGroup) el.outputDeviceGroup.style.display = "none";
    }
  };

  // Request mic permission to get real labels, then enumerate
  const enumerateWithPermission = async () => {
    if (!navigator.mediaDevices) return;
    const savedIn = el.inputDeviceSelect.dataset.savedId || el.inputDeviceSelect.value || "";
    const savedOut = el.outputDeviceSelect.dataset.savedId || el.outputDeviceSelect.value || "";
    if (el.btnRefreshDevices) el.btnRefreshDevices.disabled = true;
    try {
      // Brief getUserMedia to unlock labels, then immediately stop
      const tmp = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
      tmp.getTracks().forEach(t => t.stop());
    } catch (_) {
      // Permission denied or unavailable — enumerate anyway (labels may be empty)
    }
    try {
      await _doEnumerate(savedIn, savedOut);
      // Clear saved dataset hints after first successful restore
      delete el.inputDeviceSelect.dataset.savedId;
      delete el.outputDeviceSelect.dataset.savedId;
    } catch (_) {}
    if (el.btnRefreshDevices) el.btnRefreshDevices.disabled = false;
  };

  // Enumerate without requesting permission (uses cached permission state)
  const enumerateDevices = async () => {
    if (!navigator.mediaDevices) return;
    const savedIn = el.inputDeviceSelect.dataset.savedId || "";
    const savedOut = el.outputDeviceSelect.dataset.savedId || "";
    // Check if permission already granted — if so, labels will be available
    try {
      const perm = await navigator.permissions.query({ name: "microphone" });
      if (perm.state === "granted") {
        await _doEnumerate(savedIn, savedOut);
        delete el.inputDeviceSelect.dataset.savedId;
        delete el.outputDeviceSelect.dataset.savedId;
        return;
      }
    } catch (_) {}
    // Permission not yet granted — just hide output if needed, leave selects at default
    if (!sinkIdSupported && el.outputDeviceGroup) el.outputDeviceGroup.style.display = "none";
  };

  // Re-enumerate after getUserMedia during connect (labels now available)
  const enumerateAfterPermission = async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    try {
      await _doEnumerate(el.inputDeviceSelect.value, el.outputDeviceSelect.value);
    } catch (_) {}
  };

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

  const rmsDbfs = (buf) => {
    if (!buf || buf.length === 0) return -60;
    let sum = 0;
    for (let i = 0; i < buf.length; i++) { const v = buf[i]; sum += v * v; }
    const rms = Math.sqrt(sum / buf.length);
    if (!isFinite(rms) || rms <= 0) return -60;
    const db = 20 * Math.log10(rms);
    return Math.max(-60, Math.min(0, db));
  };

  const meterFromDb = (db) => Math.max(0, Math.min(1, (db + 60) / 60));

  // --- Meters ---
  const updateMeters = () => {
    el.txBar.style.width = `${Math.round(Math.max(0, Math.min(1, txMeter)) * 100)}%`;
    el.rxBar.style.width = `${Math.round(Math.max(0, Math.min(1, rxMeter)) * 100)}%`;
    txMeter *= 0.88;
    rxMeter *= 0.88;
    requestAnimationFrame(updateMeters);
  };
  requestAnimationFrame(updateMeters);

  // --- Collapse ---
  const cardConfig = document.getElementById("cardConfig");
  const cardConfigHeader = document.getElementById("cardConfigHeader");

  const setConfigCollapsed = (collapsed) => {
    if (!cardConfig) return;
    cardConfig.classList.toggle("card-collapsed", collapsed);
  };

  if (cardConfigHeader) {
    cardConfigHeader.addEventListener("click", () => {
      if (!cardConfig) return;
      setConfigCollapsed(!cardConfig.classList.contains("card-collapsed"));
    });
  }

  // --- UI State ---
  const setConnState = (on) => {
    el.connDot.className = "conn-indicator " + (on ? "on" : "off");
    el.connLabel.textContent = on ? "Connecté" : "Déconnecté";
  };

  const setUiConnected = (isConnected) => {
    el.btnConnect.disabled = isConnected;
    el.btnDisconnect.disabled = !isConnected;
    el.btnBus0.disabled = !isConnected;
    el.btnBus1.disabled = !isConnected;
    el.btnBus2.disabled = !isConnected;
    el.listenRegie.disabled = !isConnected;
    el.listenReturnBus.disabled = !isConnected;
    setConnState(isConnected);
    if (isConnected) {
      setConfigCollapsed(true);
    } else {
      setConfigCollapsed(false);
      for (const [bid] of pttBuses.entries()) {
        setPttBus(bid, false, { emit: false });
      }
    }
  };

  const setPttBus = (busId, active, opts = { emit: true }) => {
    if (!pttBuses.has(busId)) return;
    pttBuses.set(busId, !!active);
    const btn = busId === 0 ? el.btnBus0 : busId === 1 ? el.btnBus1 : el.btnBus2;
    if (btn) btn.classList.toggle("active", !!active);
    if (socket && joined && opts.emit !== false) {
      socket.emit("ptt_bus", { bus_id: busId, active: !!active });
    }
  };

  const anyPttActive = () => Array.from(pttBuses.values()).some(Boolean);

  const setListenRegie = (enabled) => {
    if (socket && joined) socket.emit("listen_regie", { enabled: !!enabled });
  };

  const setListenReturn = (enabled) => {
    if (socket && joined) socket.emit("listen_return_bus", { enabled: !!enabled });
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
    actualSR = audioCtx.sampleRate;
    audioRateOk = true;
    logLine(`audio: sr=${actualSR} state=${audioCtx.state}${actualSR !== TARGET_SR ? " (resampling TX)" : ""}`);

    // Resume AudioContext after interruptions (phone calls, notifications on mobile)
    audioCtx.onstatechange = () => {
      if (audioCtx && audioCtx.state === "suspended") {
        audioCtx.resume().catch(() => {});
      }
    };

    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      try { await audioCtx.close(); } catch (_) {}
      audioCtx = null;
      throw new Error(
        "Micro indisponible: navigateur en HTTP non sécurisé. Ouvre l'UI en HTTPS (ou localhost) puis réessaie."
      );
    }

    const inputDeviceId = el.inputDeviceSelect.value || "";
    const audioConstraints = {
      channelCount: 1,
      sampleRate: TARGET_SR,
      sampleSize: 16,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    };
    if (inputDeviceId) audioConstraints.deviceId = { exact: inputDeviceId };

    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    } catch (err) {
      if (inputDeviceId && err.name === "OverconstrainedError") {
        // Fallback: retry without deviceId constraint
        try {
          micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
              channelCount: 1, sampleRate: TARGET_SR, sampleSize: 16,
              echoCancellation: false, noiseSuppression: false, autoGainControl: false,
            }
          });
          logLine("micro: périphérique sélectionné indisponible, utilisation du défaut");
        } catch (err2) {
          try { await audioCtx.close(); } catch (_) {}
          audioCtx = null;
          throw new Error("Micro refusé: " + (err2.message || err2));
        }
      } else {
        try { await audioCtx.close(); } catch (_) {}
        audioCtx = null;
        throw new Error("Micro refusé: " + (err.message || err));
      }
    }
    // Labels are now available — refresh device lists
    enumerateAfterPermission();
    micNode = audioCtx.createMediaStreamSource(micStream);

    micProc = audioCtx.createScriptProcessor(2048, 1, 1);
    micNode.connect(micProc);
    micProc.connect(audioCtx.destination);

    let captureBuf = new Float32Array(0);

    micProc.onaudioprocess = (e) => {
      if (!socket || !joined) return;
      if (!anyPttActive()) { txMeter = 0; return; }

      const ch0 = e.inputBuffer.getChannelData(0);
      txMeter = Math.max(txMeter, meterFromDb(rmsDbfs(ch0)));

      // Resample to 48 kHz if needed (e.g. iOS Safari at 44100 Hz)
      let samples = ch0;
      if (actualSR !== TARGET_SR) {
        const ratio = TARGET_SR / actualSR;
        const outLen = Math.round(ch0.length * ratio);
        const resampled = new Float32Array(outLen);
        for (let i = 0; i < outLen; i++) {
          const pos = i / ratio;
          const idx = Math.floor(pos);
          const frac = pos - idx;
          const a = ch0[idx] || 0;
          const b = ch0[Math.min(idx + 1, ch0.length - 1)] || 0;
          resampled[i] = a + frac * (b - a);
        }
        samples = resampled;
      }

      const merged = new Float32Array(captureBuf.length + samples.length);
      merged.set(captureBuf, 0);
      merged.set(samples, captureBuf.length);
      captureBuf = merged;

      while (captureBuf.length >= FRAME_SAMPLES) {
        socket.emit("audio_in", floatToInt16BytesLE(captureBuf.subarray(0, FRAME_SAMPLES)));
        captureBuf = captureBuf.subarray(FRAME_SAMPLES);
      }
      if (captureBuf.length > TARGET_SR * 2) { captureBuf = captureBuf.subarray(captureBuf.length - TARGET_SR * 2); }
    };

    gainNode = audioCtx.createGain();
    gainNode.gain.value = outputVolume;

    // Output device routing via setSinkId on a hidden Audio element
    const outputDeviceId = el.outputDeviceSelect.value || "";
    if (sinkIdSupported && outputDeviceId) {
      try {
        const dest = audioCtx.createMediaStreamDestination();
        gainNode.connect(dest);
        const audioEl = new Audio();
        audioEl.srcObject = dest.stream;
        audioEl.autoplay = true;
        await audioEl.setSinkId(outputDeviceId);
        audioEl.play().catch(() => {});
        logLine(`sortie: ${el.outputDeviceSelect.options[el.outputDeviceSelect.selectedIndex]?.text || outputDeviceId}`);
      } catch (e) {
        // Fallback to default output
        gainNode.connect(audioCtx.destination);
        logLine("sortie: périphérique sélectionné indisponible, utilisation du défaut");
      }
    } else {
      gainNode.connect(audioCtx.destination);
    }

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
      setUiConnected(false);
      logLine("socket.io connect");
      socket.emit("join", {
        server_ip: el.serverIp.value.trim(),
        server_port: 5000,
        name: el.name.value.trim() || "Plateau",
        client_uuid: getClientUuid(),
        listen_return_bus: el.listenReturnBus.checked,
        listen_regie: el.listenRegie.checked,
      });
    });

    socket.on("disconnect", () => {
      setStatus("Déconnecté");
      setUiConnected(false);
      joined = false;
      intercomAlive = false;
      logLine("socket.io disconnect");
    });

    socket.on("server", (msg) => {
      if (!msg || !msg.type) return;
      if (msg.type === "joined") {
        joined = true;
        intercomAlive = true;
        lastControlTs = Date.now();
        setStatus(`${msg.server_ip}:${msg.server_port}`);
        logLine(`joined (id=${msg.client_id})`);
        setListenRegie(el.listenRegie.checked);
        setListenReturn(el.listenReturnBus.checked);
        setUiConnected(true);
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
      lastControlTs = Date.now();
      if (joined && !intercomAlive) {
        intercomAlive = true;
        setUiConnected(true);
      }
      if (msg.type === "update" || msg.type === "welcome") {
        const cfg = msg.config || msg;
        if (cfg && Array.isArray(cfg.buses)) {
          for (const b of cfg.buses) {
            try { pttBuses.set(parseInt(b.bus_id, 10), !!pttBuses.get(parseInt(b.bus_id, 10))); } catch (_) {}
          }
        }
      }
    });

    socket.on("discovery", (list) => {
      updateDiscoveryList(list);
    });

    socket.on("audio_out", (pcm) => {
      if (!audioCtx) return;
      if (!audioRateOk) return;
      if (!(pcm instanceof ArrayBuffer) && !(pcm && pcm.buffer)) return;
      const u8 = pcm instanceof ArrayBuffer ? new Uint8Array(pcm) : new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
      const f48 = int16BytesLEToFloat(u8);
      rxMeter = Math.max(rxMeter, meterFromDb(rmsDbfs(f48)));
      appendPlay(f48);
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
    intercomAlive = false;
    setUiConnected(false);
    setStatus("Déconnecté");
    await teardownAudio();
  };

  const monitorIntercom = () => {
    if (joined && intercomAlive && Date.now() - lastControlTs > CTRL_TIMEOUT_MS) {
      intercomAlive = false;
      setUiConnected(false);
      setStatus("Serveur intercom perdu");
    }
    requestAnimationFrame(monitorIntercom);
  };

  // --- Event Listeners ---
  el.btnConnect.addEventListener("click", () => {
    connect().catch(e => logLine(`erreur: ${e && e.message ? e.message : e}`));
  });
  el.btnDisconnect.addEventListener("click", () => disconnect());

  const bindPttButton = (btn, busId) => {
    if (!btn) return;
    btn.addEventListener("mousedown", () => setPttBus(busId, true));
    btn.addEventListener("mouseup", () => setPttBus(busId, false));
    btn.addEventListener("mouseleave", () => { if (pttBuses.get(busId)) setPttBus(busId, false); });
    btn.addEventListener("touchstart", (e) => { e.preventDefault(); setPttBus(busId, true); });
    btn.addEventListener("touchend", () => setPttBus(busId, false));
    btn.addEventListener("touchcancel", () => setPttBus(busId, false));
  };

  bindPttButton(el.btnBus0, 0);
  bindPttButton(el.btnBus1, 1);
  bindPttButton(el.btnBus2, 2);

  el.discoverySelect.addEventListener("change", applyDiscoverySelection);
  el.btnRefreshDiscovery.addEventListener("click", fetchDiscovery);

  if (el.btnRefreshDevices) {
    el.btnRefreshDevices.addEventListener("click", () => {
      enumerateWithPermission().catch(() => {});
    });
  }

  el.inputDeviceSelect.addEventListener("change", saveSettings);
  el.outputDeviceSelect.addEventListener("change", saveSettings);

  el.listenRegie.addEventListener("change", () => {
    setListenRegie(el.listenRegie.checked);
    saveSettings();
  });

  el.listenReturnBus.addEventListener("change", () => {
    setListenReturn(el.listenReturnBus.checked);
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
    if (e.code === "Space") { e.preventDefault(); setPttBus(0, true); }
    else if (e.code === "Digit1") { e.preventDefault(); setPttBus(0, true); }
    else if (e.code === "Digit2") { e.preventDefault(); setPttBus(1, true); }
    else if (e.code === "Digit3") { e.preventDefault(); setPttBus(2, true); }
  });

  document.addEventListener("keyup", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.code === "Space") { e.preventDefault(); setPttBus(0, false); }
    else if (e.code === "Digit1") { e.preventDefault(); setPttBus(0, false); }
    else if (e.code === "Digit2") { e.preventDefault(); setPttBus(1, false); }
    else if (e.code === "Digit3") { e.preventDefault(); setPttBus(2, false); }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      setPttBus(0, false);
      setPttBus(1, false);
      setPttBus(2, false);
    } else {
      // Flush stale audio accumulated while tab was throttled
      playQueue = new Float32Array(0);
      // Re-resume AudioContext if suspended by browser while hidden
      if (audioCtx && audioCtx.state === "suspended") {
        audioCtx.resume().catch(() => {});
      }
    }
  });

  window.addEventListener("blur", () => {
    setPttBus(0, false);
    setPttBus(1, false);
    setPttBus(2, false);
  });

  // --- Init ---
  loadSettings();
  enumerateDevices();
  setUiConnected(false);
  setStatus("Déconnecté");
  startDiscoveryPolling();
  requestAnimationFrame(monitorIntercom);
})();
