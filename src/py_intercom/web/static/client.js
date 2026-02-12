(() => {
  const FRAME_SAMPLES = 480;
  const TARGET_SR = 48000;

  const el = {
    serverIp: document.getElementById("serverIp"),
    serverPort: document.getElementById("serverPort"),
    name: document.getElementById("name"),
    mode: document.getElementById("mode"),
    btnConnect: document.getElementById("btnConnect"),
    btnDisconnect: document.getElementById("btnDisconnect"),
    btnPtt: document.getElementById("btnPtt"),
    statusLine: document.getElementById("statusLine"),
    log: document.getElementById("log"),
    rxBar: document.getElementById("rxBar"),
  };

  const logLine = (msg) => {
    const t = new Date().toLocaleTimeString();
    const div = document.createElement("div");
    div.textContent = `[${t}] ${msg}`;
    el.log.prepend(div);
  };

  const setStatus = (s) => {
    el.statusLine.textContent = s;
  };

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

  let socket = null;
  let joined = false;
  let audioCtx = null;
  let micStream = null;
  let micNode = null;
  let micProc = null;
  let playProc = null;

  let pttActive = false;

  // Playback jitter buffer (Float32 at context sample rate)
  let playQueue = new Float32Array(0);
  let rxMeter = 0;

  const appendPlay = (x) => {
    if (playQueue.length === 0) {
      playQueue = x;
      return;
    }
    const y = new Float32Array(playQueue.length + x.length);
    y.set(playQueue, 0);
    y.set(x, playQueue.length);
    playQueue = y;

    const max = audioCtx ? Math.floor(audioCtx.sampleRate * 3) : (TARGET_SR * 3);
    if (playQueue.length > max) {
      playQueue = playQueue.subarray(playQueue.length - max);
    }
  };

  const popPlay = (n) => {
    if (playQueue.length >= n) {
      const out = playQueue.subarray(0, n);
      playQueue = playQueue.subarray(n);
      return out;
    }
    const out = new Float32Array(n);
    out.fill(0);
    if (playQueue.length > 0) {
      out.set(playQueue, 0);
      playQueue = new Float32Array(0);
    }
    return out;
  };

  const floatToInt16BytesLE = (x) => {
    const buf = new ArrayBuffer(x.length * 2);
    const dv = new DataView(buf);
    for (let i = 0; i < x.length; i++) {
      let s = Math.max(-1, Math.min(1, x[i]));
      const v = s < 0 ? Math.round(s * 32768) : Math.round(s * 32767);
      dv.setInt16(i * 2, v, true);
    }
    return new Uint8Array(buf);
  };

  const int16BytesLEToFloat = (u8) => {
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const n = Math.floor(u8.byteLength / 2);
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      out[i] = dv.getInt16(i * 2, true) / 32768;
    }
    return out;
  };

  // Linear resample from srcSR to dstSR
  const resampleLinear = (src, srcSR, dstSR) => {
    if (srcSR === dstSR) return src;
    const ratio = srcSR / dstSR;
    const dstLen = Math.floor(src.length / ratio);
    const out = new Float32Array(dstLen);
    for (let i = 0; i < dstLen; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, src.length - 1);
      const frac = pos - i0;
      out[i] = src[i0] * (1 - frac) + src[i1] * frac;
    }
    return out;
  };

  const updateRxMeter = () => {
    const v = Math.max(0, Math.min(1, rxMeter));
    el.rxBar.style.width = `${Math.round(v * 100)}%`;
    rxMeter *= 0.92;
    requestAnimationFrame(updateRxMeter);
  };
  requestAnimationFrame(updateRxMeter);

  const setUiConnected = (isConnected) => {
    el.btnConnect.disabled = isConnected;
    el.btnDisconnect.disabled = !isConnected;
    el.btnPtt.disabled = !isConnected;
    if (!isConnected) {
      el.btnPtt.classList.remove("active");
    }
  };

  const setPtt = (active) => {
    pttActive = !!active;
    if (pttActive) {
      el.btnPtt.classList.add("active");
    } else {
      el.btnPtt.classList.remove("active");
    }
    if (socket && joined) {
      socket.emit("ptt", { active: !!pttActive });
    }
  };

  const setupAudio = async () => {
    if (audioCtx) return;

    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: TARGET_SR });

    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    micNode = audioCtx.createMediaStreamSource(micStream);

    // Capture
    const bufSize = 2048;
    micProc = audioCtx.createScriptProcessor(bufSize, micNode.channelCount, 1);
    micNode.connect(micProc);
    micProc.connect(audioCtx.destination);

    let captureBuf = new Float32Array(0);

    micProc.onaudioprocess = (e) => {
      if (!socket || !joined) return;

      const mode = el.mode.value;
      if (mode === "ptt" && !pttActive) return;

      const input = e.inputBuffer;
      const ch0 = input.getChannelData(0);
      let mono = ch0;
      if (input.numberOfChannels > 1) {
        // average
        const ch1 = input.getChannelData(1);
        const tmp = new Float32Array(ch0.length);
        for (let i = 0; i < tmp.length; i++) tmp[i] = 0.5 * (ch0[i] + ch1[i]);
        mono = tmp;
      }

      // accumulate in context SR
      const merged = new Float32Array(captureBuf.length + mono.length);
      merged.set(captureBuf, 0);
      merged.set(mono, captureBuf.length);
      captureBuf = merged;

      // convert to 48k frames for backend
      const srcSR = audioCtx.sampleRate;

      // resample in chunks to keep latency low
      let src = captureBuf;
      if (srcSR !== TARGET_SR) {
        // if context didn't honor requested SR, resample
        src = resampleLinear(captureBuf, srcSR, TARGET_SR);
        captureBuf = new Float32Array(0);
      }

      while (src.length >= FRAME_SAMPLES) {
        const frame = src.subarray(0, FRAME_SAMPLES);
        const bytes = floatToInt16BytesLE(frame);
        socket.emit("audio_in", bytes);
        src = src.subarray(FRAME_SAMPLES);
      }

      if (srcSR === TARGET_SR) {
        captureBuf = src;
      } else {
        // already cleared
        captureBuf = new Float32Array(0);
      }
    };

    // Playback
    playProc = audioCtx.createScriptProcessor(2048, 1, 1);
    playProc.onaudioprocess = (e) => {
      const out = e.outputBuffer.getChannelData(0);
      const chunk = popPlay(out.length);
      out.set(chunk);
    };
    playProc.connect(audioCtx.destination);
  };

  const teardownAudio = async () => {
    try {
      if (micProc) micProc.disconnect();
    } catch (_) {}
    try {
      if (micNode) micNode.disconnect();
    } catch (_) {}
    try {
      if (playProc) playProc.disconnect();
    } catch (_) {}

    micProc = null;
    micNode = null;
    playProc = null;

    try {
      if (micStream) micStream.getTracks().forEach((t) => t.stop());
    } catch (_) {}
    micStream = null;

    try {
      if (audioCtx) await audioCtx.close();
    } catch (_) {}
    audioCtx = null;

    playQueue = new Float32Array(0);
  };

  const connect = async () => {
    if (socket) return;

    await setupAudio();

    socket = io({ transports: ["websocket"] });

    socket.on("connect", () => {
      setStatus("Connecté au backend web");
      setUiConnected(true);
      logLine("socket.io connect");

      const payload = {
        server_ip: el.serverIp.value.trim(),
        server_port: parseInt(el.serverPort.value, 10),
        name: el.name.value.trim() || "Plateau",
        client_uuid: getClientUuid(),
        mode: el.mode.value,
      };
      socket.emit("join", payload);
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
        setStatus(`Connecté à ${msg.server_ip}:${msg.server_port} (id=${msg.client_id})`);
        logLine(`joined server (client_id=${msg.client_id})`);
      } else if (msg.type === "kick") {
        logLine(`kick: ${msg.message || ""}`);
        disconnect();
      } else if (msg.type === "error") {
        logLine(`error: ${msg.message || ""}`);
      }
    });

    socket.on("control", (msg) => {
      if (msg && msg.type === "update" && msg.config) {
        // no-op for now
      }
    });

    socket.on("audio_out", (pcm) => {
      if (!audioCtx) return;
      if (!(pcm instanceof ArrayBuffer) && !(pcm && pcm.buffer)) return;

      const u8 = pcm instanceof ArrayBuffer ? new Uint8Array(pcm) : new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
      const f48 = int16BytesLEToFloat(u8);

      let peak = 0;
      for (let i = 0; i < f48.length; i++) {
        const a = Math.abs(f48[i]);
        if (a > peak) peak = a;
      }
      rxMeter = Math.max(rxMeter, peak);

      const sr = audioCtx.sampleRate;
      const f = sr === TARGET_SR ? f48 : resampleLinear(f48, TARGET_SR, sr);
      appendPlay(f);
    });

    socket.on("connect_error", (e) => {
      logLine(`connect_error: ${e && e.message ? e.message : e}`);
    });
  };

  const disconnect = async () => {
    if (socket) {
      try {
        socket.emit("leave");
      } catch (_) {}
      try {
        socket.disconnect();
      } catch (_) {}
      socket = null;
    }
    joined = false;
    setUiConnected(false);
    await teardownAudio();
  };

  el.btnConnect.addEventListener("click", () => {
    connect().catch((e) => {
      logLine(`audio init error: ${e && e.message ? e.message : e}`);
    });
  });

  el.btnDisconnect.addEventListener("click", () => {
    disconnect();
  });

  el.btnPtt.addEventListener("mousedown", () => setPtt(true));
  el.btnPtt.addEventListener("mouseup", () => setPtt(false));
  el.btnPtt.addEventListener("mouseleave", () => setPtt(false));
  el.btnPtt.addEventListener("touchstart", (e) => {
    e.preventDefault();
    setPtt(true);
  });
  el.btnPtt.addEventListener("touchend", () => setPtt(false));

  document.addEventListener("keydown", (e) => {
    if (e.code === "Space") {
      e.preventDefault();
      setPtt(true);
    }
  });

  document.addEventListener("keyup", (e) => {
    if (e.code === "Space") {
      e.preventDefault();
      setPtt(false);
    }
  });

  setUiConnected(false);
  setStatus("Déconnecté");
})();
