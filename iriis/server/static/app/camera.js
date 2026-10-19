/* Seryn camera screen: a video-call style view where the AI watches through the phone camera and you just talk.
 * Shared by the Iriis app (through Iriis: /v1/camera, /v1/bench/*) and the Seryn Vision page (/ws, /api/*).
 *
 *   SerynCamera.open({ ws, api, media, stt, onClose })
 *
 * Protocol (same as Seryn Vision /ws): binary JPEG frames up (next one only after "frame_ack"), JSON messages both
 * ways: ask, goal, procedure, step, coach, recent, ping / hello, state, perception, thinking, answer, alert, busy,
 * job, video_report, relay, viewer_join, rtc, error.
 */
(function () {
  "use strict";
  const CSS = `
  .sc-root{position:fixed;inset:0;z-index:50;background:#000;color:#fff;font:15px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    display:flex;flex-direction:column;overflow:hidden;-webkit-user-select:none;user-select:none}
  .sc-root *{box-sizing:border-box}
  .sc-root button{font:inherit;color:inherit;border:none;background:none;cursor:pointer}
  .sc-video,.sc-canvas{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .sc-top{position:relative;display:flex;align-items:center;gap:8px;padding:10px 12px;padding-top:max(10px,env(safe-area-inset-top));
    background:linear-gradient(rgba(0,0,0,.55),transparent)}
  .sc-title{flex:1;min-width:0}.sc-title b{display:block}.sc-title small{opacity:.8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
  .sc-ib{width:42px;height:42px;border-radius:50%;background:rgba(0,0,0,.35);display:grid;place-items:center;font-size:19px;backdrop-filter:blur(6px)}
  .sc-chip{position:absolute;right:12px;top:calc(68px + env(safe-area-inset-top));padding:6px 12px;border-radius:12px;background:rgba(0,0,0,.6);
    font-weight:700;font-size:17px;display:none;backdrop-filter:blur(6px)}
  .sc-step{position:relative;margin:4px 12px 0;padding:10px 12px;border-radius:14px;background:rgba(17,24,39,.72);display:none;backdrop-filter:blur(8px)}
  .sc-step .row{display:flex;gap:8px;margin-top:8px}.sc-step button{padding:6px 12px;border-radius:99px;background:rgba(255,255,255,.14);font-weight:600}
  .sc-badge{position:relative;align-self:flex-start;margin:8px 12px 0;padding:6px 12px;border-radius:12px;font-weight:600;display:none;max-width:85%}
  .sc-ok{background:#16a34a}.sc-warning{background:#d97706}.sc-danger{background:#dc2626}.sc-unsure{background:#4b5563}
  .sc-space{flex:1}
  .sc-caps{position:relative;display:flex;flex-direction:column;gap:6px;padding:0 12px 8px;max-height:38vh;overflow:hidden;justify-content:flex-end}
  .sc-cap{max-width:85%;padding:8px 12px;border-radius:16px;background:rgba(17,24,39,.72);backdrop-filter:blur(8px);animation:scin .2s ease-out;
    -webkit-user-select:text;user-select:text}
  .sc-cap.me{align-self:flex-end;background:rgba(59,130,246,.85)}
  .sc-cap.dim{opacity:.55}
  .sc-cap small{display:block;opacity:.7;font-size:11px;margin-top:2px}
  @keyframes scin{from{transform:translateY(6px);opacity:.3}}
  .sc-dots span{display:inline-block;width:6px;height:6px;margin:0 2px;border-radius:50%;background:#fff;animation:scb 1.2s infinite}
  .sc-dots span:nth-child(2){animation-delay:.2s}.sc-dots span:nth-child(3){animation-delay:.4s}@keyframes scb{50%{opacity:.25}}
  .sc-bar{position:relative;display:flex;justify-content:space-around;align-items:center;padding:12px 8px;padding-bottom:max(14px,env(safe-area-inset-bottom));
    background:linear-gradient(transparent,rgba(0,0,0,.7))}
  .sc-btn{display:flex;flex-direction:column;align-items:center;gap:4px;font-size:11px;opacity:.95;min-width:58px}
  .sc-btn .c{width:54px;height:54px;border-radius:50%;background:rgba(255,255,255,.16);display:grid;place-items:center;font-size:22px;backdrop-filter:blur(6px)}
  .sc-btn.on .c{background:#16a34a}.sc-btn.rec .c{background:#dc2626;animation:scr 1s infinite}.sc-btn.coach.on .c{background:#7c3aed}
  .sc-btn.big .c{width:66px;height:66px;font-size:26px}
  .sc-btn.listen .c{box-shadow:0 0 0 6px rgba(22,163,74,.35)}
  @keyframes scr{50%{box-shadow:0 0 0 10px rgba(220,38,38,.25)}}
  .sc-sheet{position:absolute;left:0;right:0;bottom:0;max-height:78vh;overflow-y:auto;background:#0f1720;border-radius:20px 20px 0 0;padding:16px;
    padding-bottom:max(16px,env(safe-area-inset-bottom));transform:translateY(105%);transition:transform .22s ease;z-index:3}
  .sc-sheet.open{transform:none}
  .sc-sheet h4{margin:14px 0 8px;font-size:13px;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em}
  .sc-sheet input,.sc-sheet select{width:100%;padding:11px 12px;border-radius:12px;border:1px solid #233246;background:#0b1118;color:#fff;font:inherit}
  .sc-sheet .row{display:flex;gap:8px;margin-top:8px}.sc-sheet .row>*{flex:1}
  .sc-pill{padding:10px 14px;border-radius:12px;background:#1e293b;font-weight:600}.sc-pill.p{background:#3b82f6}
  .sc-grab{width:40px;height:5px;border-radius:9px;background:#334155;margin:0 auto 8px}
  .sc-msg{position:absolute;inset:0;display:none;place-items:center;text-align:center;padding:28px;background:rgba(0,0,0,.82);z-index:4}
  .sc-msg div{max-width:420px}.sc-msg button{margin-top:14px;padding:10px 18px;border-radius:12px;background:#3b82f6;font-weight:600}
  .sc-report{white-space:pre-wrap;-webkit-user-select:text;user-select:text}
  `;

  const $ = (root, sel) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function open(opts) {
    const o = Object.assign({ ws: "/ws", api: "/api", media: "/media", stt: "/api/stt", title: "Camera", onClose: null }, opts || {});
    if (!document.getElementById("sc-style")) { const st = document.createElement("style"); st.id = "sc-style"; st.textContent = CSS; document.head.appendChild(st); }
    const root = document.createElement("div"); root.className = "sc-root";
    root.innerHTML = `
      <video class="sc-video" playsinline muted autoplay></video><canvas class="sc-canvas"></canvas>
      <div class="sc-top">
        <button class="sc-ib" data-a="close" title="End">✕</button>
        <div class="sc-title"><b>${esc(o.title)}</b><small class="sc-sub">connecting…</small></div>
        <button class="sc-ib" data-a="torch" title="Light" style="display:none">🔦</button>
        <button class="sc-ib" data-a="flip" title="Switch camera">🔄</button>
      </div>
      <div class="sc-chip"></div>
      <div class="sc-step"><div class="sc-steptext"></div><div class="row"><button data-a="prev">◀</button><button data-a="check">✓ Check</button><button data-a="next">Next ▶</button></div></div>
      <div class="sc-badge"></div>
      <div class="sc-space"></div>
      <div class="sc-caps"></div>
      <div class="sc-bar">
        <button class="sc-btn coach" data-a="coach"><span class="c">🧭</span>Coach</button>
        <button class="sc-btn" data-a="record"><span class="c">⏺</span>Record</button>
        <button class="sc-btn big on" data-a="talk"><span class="c">🎙</span><span class="sc-talklbl">Listening</span></button>
        <button class="sc-btn" data-a="recent"><span class="c">⏪</span>What happened</button>
        <button class="sc-btn" data-a="more"><span class="c">⋯</span>More</button>
      </div>
      <div class="sc-sheet">
        <div class="sc-grab"></div>
        <h4>Ask by typing</h4>
        <div class="row"><input class="sc-q" placeholder="Ask about what the camera sees…" enterkeyhint="send"><button class="sc-pill p" data-a="ask" style="flex:0 0 auto">Ask</button></div>
        <h4>What are you doing?</h4>
        <div class="row"><input class="sc-goal" placeholder="Goal, e.g. fill 12 °C packs, solder the heater…"><button class="sc-pill" data-a="goal" style="flex:0 0 auto">Set</button></div>
        <div class="row"><select class="sc-procs"><option value="">Step-by-step procedure…</option></select><button class="sc-pill" data-a="start" style="flex:0 0 auto">Start</button></div>
        <div class="row"><input class="sc-seryn" placeholder="Import a Seryn protocol, e.g. P2"><button class="sc-pill" data-a="import" style="flex:0 0 auto">Import</button></div>
        <h4>Teach it</h4>
        <div class="row"><input class="sc-files" type="file" multiple accept=".pdf,.docx,.xlsx,.csv,.txt,.md,.json,.yaml,.yml,.html"><button class="sc-pill" data-a="upload" style="flex:0 0 auto">Upload</button></div>
        <div class="row"><input class="sc-url" placeholder="https://… a manual, datasheet or page"><button class="sc-pill" data-a="addurl" style="flex:0 0 auto">Add</button></div>
        <h4>Camera</h4>
        <div class="row"><button class="sc-pill" data-a="snap">📷 Snapshot</button><select class="sc-res"><option value="480">480p</option><option value="720" selected>720p</option><option value="1080">1080p</option></select></div>
        <div class="row"><button class="sc-pill" data-a="mute">🔊 Voice on</button><button class="sc-pill" data-a="close2">End camera</button></div>
        <h4 class="sc-reph" style="display:none">Last report</h4><div class="sc-report"></div>
      </div>
      <div class="sc-msg"><div><div class="sc-msgtext"></div><button data-a="retry">Try again</button></div></div>`;
    document.body.appendChild(root);
    const video = $(root, ".sc-video"), canvas = $(root, ".sc-canvas"), ctx2 = canvas.getContext("2d");
    const grab = document.createElement("canvas"), gctx = grab.getContext("2d");
    let ws = null, stream = null, track = null, facing = "environment", waitingAck = false, ackTimer = null, lastSent = 0, relayFps = 0;
    let detections = [], closed = false, thinking = false, retry = 0, ping = null, muted = false, torchOn = false, wakeLock = null;
    let recorder = null, recChunks = [], mic = null, coachOn = false, lastQ = null, listening = true;
    const peers = {}, ICE = [{ urls: ["stun:stun.l.google.com:19302", "stun:stun.cloudflare.com:3478"] }];

    let slow = false;
    const sub = (t) => { $(root, ".sc-sub").textContent = t; };
    function fatal(text) { $(root, ".sc-msgtext").textContent = text; $(root, ".sc-msg").style.display = "grid"; }
    function badge(status, text) { const b = $(root, ".sc-badge"); b.className = "sc-badge sc-" + status; b.textContent = text; b.style.display = "block";
      clearTimeout(b._t); b._t = setTimeout(() => b.style.display = "none", 9000); }
    function cap(role, text, meta) {
      const caps = $(root, ".sc-caps"); [...caps.children].forEach((c) => c.classList.add("dim"));
      const d = document.createElement("div"); d.className = "sc-cap" + (role === "me" ? " me" : "");
      d.innerHTML = esc(text) + (meta ? `<small>${esc(meta)}</small>` : ""); caps.appendChild(d);
      while (caps.children.length > 4) caps.removeChild(caps.firstChild);
      return d;
    }
    function thinkingCap() { const d = cap("her", ""); d.innerHTML = '<span class="sc-dots"><span></span><span></span><span></span></span>'; d.dataset.t = "1"; return d; }

    // ------------------------------------------------------------ camera
    async function startCamera() {
      if (!window.isSecureContext) { fatal("The camera needs a secure (https) page."); return false; }
      if (!navigator.mediaDevices?.getUserMedia) { fatal("This browser can't use the camera."); return false; }
      if (stream) stream.getVideoTracks().forEach((t) => t.stop());
      const h = +$(root, ".sc-res").value;
      for (const v of [{ facingMode: { ideal: facing }, height: { ideal: h }, width: { ideal: Math.round(h * 16 / 9) } }, { facingMode: { ideal: facing } }, true]) {
        try { stream = await navigator.mediaDevices.getUserMedia({ video: v, audio: false }); break; }
        catch (e) {
          stream = null;
          if (["NotAllowedError", "SecurityError"].includes(e.name)) { fatal("Camera access is blocked. Allow the camera for this site (the lock icon by the address), then try again."); return false; }
          if (e.name === "NotReadableError") { fatal("The camera is busy in another app. Close it and try again."); return false; }
        }
      }
      if (!stream) { fatal("Couldn't open the camera."); return false; }
      video.srcObject = stream; try { await video.play(); } catch (e) {}
      track = stream.getVideoTracks()[0];
      track.onended = () => { if (!closed) setTimeout(startCamera, 800); };
      const caps = track.getCapabilities ? track.getCapabilities() : {};
      $(root, '[data-a="torch"]').style.display = caps.torch ? "grid" : "none";
      for (const pc of Object.values(peers)) for (const s of pc.getSenders()) if (s.track?.kind === "video") s.replaceTrack(track).catch(() => {});
      return true;
    }
    function frameBlob(max, q) {
      const w = video.videoWidth, h = video.videoHeight; if (!w) return Promise.resolve(null);
      const s = Math.min(1, max / Math.max(w, h)); grab.width = Math.round(w * s); grab.height = Math.round(h * s);
      gctx.drawImage(video, 0, 0, grab.width, grab.height);
      return new Promise((r) => grab.toBlob(r, "image/jpeg", q));
    }
    async function loop() {
      if (closed) return;
      const every = relayFps > 0 ? 1000 / relayFps : 1000;
      if (ws?.readyState === 1 && !waitingAck && performance.now() - lastSent >= every && !document.hidden) {
        const b = await frameBlob(960, .72);
        if (b && ws?.readyState === 1) { waitingAck = true; lastSent = performance.now(); ws.send(await b.arrayBuffer());
          clearTimeout(ackTimer); ackTimer = setTimeout(() => waitingAck = false, 12000); }
      }
      draw(); requestAnimationFrame(loop);
    }
    function draw() {
      const r = canvas.getBoundingClientRect(), W = Math.round(r.width * devicePixelRatio), H = Math.round(r.height * devicePixelRatio);
      if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
      ctx2.clearRect(0, 0, W, H);
      const vw = video.videoWidth, vh = video.videoHeight; if (!vw || !detections.length) return;
      const sc = Math.max(W / vw, H / vh), ox = (W - vw * sc) / 2, oy = (H - vh * sc) / 2, fs = grab.width ? vw / grab.width : 1;
      ctx2.lineWidth = 2 * devicePixelRatio; ctx2.font = `${12 * devicePixelRatio}px system-ui`;
      for (const d of detections) { if (!d.box) continue; const [x1, y1, x2, y2] = d.box.map((v) => v * fs * sc);
        ctx2.strokeStyle = ctx2.fillStyle = "rgba(59,130,246,.9)"; ctx2.strokeRect(ox + x1, oy + y1, x2 - x1, y2 - y1);
        const l = `${d.label} ${Math.round(d.conf * 100)}%`; ctx2.fillRect(ox + x1, oy + y1 - 16 * devicePixelRatio, ctx2.measureText(l).width + 8 * devicePixelRatio, 16 * devicePixelRatio);
        ctx2.fillStyle = "#fff"; ctx2.fillText(l, ox + x1 + 4 * devicePixelRatio, oy + y1 - 4 * devicePixelRatio); }
    }

    // ------------------------------------------------------------ voice: always listening (hands-free), barge-in
    let speakUntil = 0;
    function say(t) {
      if (muted || !t || !window.speechSynthesis) return;
      try { speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(t); u.rate = 1.05; speakUntil = performance.now() + 60000;
        u.onend = u.onerror = () => speakUntil = performance.now() + 500; speechSynthesis.speak(u); } catch (e) {}
    }
    const talkingNow = () => performance.now() < speakUntil || window.speechSynthesis?.speaking;
    const vad = { an: null, buf: null, floor: .01, sp: 0, si: 0, rec: null, chunks: [], t0: 0, timer: null };
    async function startListening() {
      try { mic = mic || await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
      catch (e) { listening = false; setTalk(); badge("unsure", "Microphone blocked: allow it to talk, or type under More."); return; }
      const AC = window.AudioContext || window.webkitAudioContext; const ac = new AC(); await ac.resume().catch(() => {});
      const src = ac.createMediaStreamSource(mic); vad.an = ac.createAnalyser(); vad.an.fftSize = 1024; src.connect(vad.an);
      vad.buf = new Float32Array(1024); clearInterval(vad.timer); vad.timer = setInterval(tick, 50);
    }
    function tick() {
      if (!listening || !vad.an) return;
      vad.an.getFloatTimeDomainData(vad.buf); let s = 0; for (const v of vad.buf) s += v * v; const rms = Math.sqrt(s / vad.buf.length);
      const over = talkingNow(), th = Math.max(.015, vad.floor * (over ? 6 : 3));
      if (!vad.rec && rms < th) vad.floor = vad.floor * .98 + rms * .02;
      const voiced = rms > th;
      if (!vad.rec) { vad.sp = voiced ? vad.sp + 50 : 0; if (vad.sp >= 150) { if (over) { try { speechSynthesis.cancel(); } catch (e) {} speakUntil = 0; } segStart(); } return; }
      vad.si = voiced ? 0 : vad.si + 50;
      if (vad.si >= 850 || performance.now() - vad.t0 > 15000) vad.rec.stop();
    }
    function segStart() {
      const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
      try { vad.rec = new MediaRecorder(mic, type ? { mimeType: type } : undefined); } catch (e) { vad.rec = null; return; }
      vad.chunks = []; vad.t0 = performance.now(); vad.si = 0; $(root, '[data-a="talk"]').classList.add("listen");
      vad.rec.ondataavailable = (e) => e.data.size && vad.chunks.push(e.data);
      vad.rec.onstop = async () => {
        const mime = vad.rec.mimeType || "audio/webm", dur = performance.now() - vad.t0; vad.rec = null; vad.sp = 0;
        $(root, '[data-a="talk"]').classList.remove("listen");
        if (dur < 600) return;
        const f = new FormData(); f.append("file", new Blob(vad.chunks, { type: mime }), "q." + (mime.includes("mp4") ? "mp4" : mime.includes("ogg") ? "ogg" : "webm"));
        try { const r = await fetch(o.stt, { method: "POST", body: f, credentials: "same-origin" }); const j = await r.json(); const t = (j.text || "").trim();
          if (r.ok && t.length > 1 && !/^(thank you\.?|thanks for watching\.?|you\.?|\.+)$/i.test(t)) ask(t, true);
          else if (!r.ok) badge("unsure", `Couldn't hear that (voice error ${r.status}). Type below instead.`);
        } catch (e) { badge("unsure", "Couldn't send your voice: no connection"); }
      };
      vad.rec.start(250);
    }
    function setTalk() { const b = $(root, '[data-a="talk"]'); b.classList.toggle("on", listening); $(root, ".sc-talklbl").textContent = listening ? "Listening" : "Muted mic"; }

    // ------------------------------------------------------------ the link
    function send(o2) { if (ws?.readyState === 1) { ws.send(JSON.stringify(o2)); return true; } badge("unsure", "Reconnecting…"); return false; }
    function ask(text, byVoice) {
      if (!text) return;
      if (thinking) { if (byVoice) say("One moment."); return; }
      if (!send({ type: "ask", text })) return;
      cap("me", text); lastQ = thinkingCap(); if (byVoice || slow) say(slow ? "Let me look. This takes a minute or two." : "Let me look.");
    }
    function connect() {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = o.ws.startsWith("ws") ? o.ws : `${proto}://${location.host}${o.ws}`;
      ws = new WebSocket(url); ws.binaryType = "arraybuffer";
      ws.onopen = () => { retry = 0; waitingAck = false; clearInterval(ping); ping = setInterval(() => send({ type: "ping", t: Date.now() }), 20000); };
      ws.onclose = (e) => {
        clearInterval(ping); if (closed) return;
        if (e.code === 4401) { fatal("Your login expired. Close this and log in again."); return; }
        if (e.code === 4404) return;
        sub("reconnecting…"); setTimeout(connect, Math.min(15000, 1000 * 2 ** retry++));
      };
      ws.onmessage = (e) => { if (typeof e.data !== "string") return; let m; try { m = JSON.parse(e.data); } catch (x) { return; } handle(m); };
    }
    function handle(m) {
      switch (m.type) {
        case "hello":
          slow = m.ready && !/^fast/.test(m.note || "");
          sub(!m.ready ? `model not ready: ${m.note || ""}` : slow ? "slow mode · answers take 1–2 min" : "ready · just talk");
          if (slow) badge("unsure", "Slow mode: no GROQ_API_KEY or GEMINI_API_KEY on the vision server, so answers take 1–2 minutes");
          fillProcs(m.procedures || []); break;
        case "frame_ack": waitingAck = false; clearTimeout(ackTimer); break;
        case "perception": detections = [...(m.detections || [])];
          if (m.reading) { const c = $(root, ".sc-chip"); c.textContent = `${(+m.reading.value).toFixed(1)} ${m.reading.unit || ""}`; c.style.display = "block";
            c.style.color = m.reading.in_range === false ? "#fbbf24" : "#fff"; } break;
        case "thinking": thinking = true; if (!lastQ) lastQ = thinkingCap(); break;
        case "busy": thinking = false; if (lastQ) { lastQ.innerHTML = "Still on the last one, ask again in a second."; lastQ = null; } break;
        case "answer": {
          thinking = false;
          if (m.coach && m.quiet) { if (lastQ?.dataset.t) { lastQ.remove(); lastQ = null; } break; }
          const meta = `${m.coach ? "coach · " : ""}${m.latency_ms ? (m.latency_ms / 1000).toFixed(1) + " s" : ""}${m.sources?.length ? " · " + m.sources.join(", ") : ""}`;
          if (lastQ && lastQ.dataset.t) { lastQ.innerHTML = esc(m.say) + `<small>${esc(meta)}</small>`; delete lastQ.dataset.t; } else cap("her", m.say, meta);
          lastQ = null;
          if (m.status && m.status !== "ok") badge(m.status, { warning: "⚠ Check this", danger: "⛔ Stop", unsure: "? Not sure" }[m.status] || m.status);
          if (m.reading) { const c = $(root, ".sc-chip"); c.textContent = `${(+m.reading.value).toFixed(1)} ${m.reading.unit || ""}`; c.style.display = "block"; }
          say(m.say + (m.note ? " " + m.note : "")); if (navigator.vibrate && m.status === "danger") navigator.vibrate([80, 60, 80]);
          sub(`answered in ${m.latency_ms ? (m.latency_ms / 1000).toFixed(1) : "?"} s · ${m.model || ""}`);
          break; }
        case "alert": badge(m.status || "warning", m.say); cap("her", m.say, "alert"); say(m.say); break;
        case "state": showState(m); break;
        case "coach": coachOn = m.seconds > 0; $(root, '[data-a="coach"]').classList.toggle("on", coachOn); break;
        case "relay": relayFps = m.fps || 0; break;
        case "viewer_join": offer(m.viewer); break;
        case "viewer_leave": if (peers[m.viewer]) { peers[m.viewer].close(); delete peers[m.viewer]; } break;
        case "rtc": onRtc(m.viewer, m.data); break;
        case "job": watchJob(m.id); break;
        case "video_report": report(m); break;
        case "error": if (m.fatal) fatal(m.text); else badge("unsure", m.text); break;
      }
    }
    function showState(s) {
      const box = $(root, ".sc-step");
      if (!s.procedure) { box.style.display = "none"; if (s.goal) sub("goal: " + s.goal); return; }
      box.style.display = "block";
      $(root, ".sc-steptext").textContent = s.done ? `✓ ${s.procedure}: all ${s.steps} steps done` : `Step ${s.step_number}/${s.steps}: ${s.step.instruction}`;
      if (!s.done) say(`Step ${s.step_number}. ${s.step.instruction}`);
    }
    async function fillProcs(list) { const sel = $(root, ".sc-procs"); sel.length = 1;
      for (const p of list) { const op = document.createElement("option"); op.value = p.file; op.textContent = `${p.name} (${p.steps} steps)`; sel.appendChild(op); } }

    // live video to watch screens (WebRTC), as on the Seryn Vision page
    async function offer(viewer) {
      if (!stream || !window.RTCPeerConnection) return; if (peers[viewer]) peers[viewer].close();
      const pc = new RTCPeerConnection({ iceServers: ICE }); peers[viewer] = pc;
      stream.getVideoTracks().forEach((t) => pc.addTrack(t, stream));
      pc.onicecandidate = (e) => e.candidate && send({ type: "rtc", viewer, data: { candidate: e.candidate } });
      try { await pc.setLocalDescription(await pc.createOffer()); send({ type: "rtc", viewer, data: { sdp: pc.localDescription } }); } catch (e) {}
    }
    async function onRtc(v, d) { const pc = peers[v]; if (!pc || !d) return;
      try { if (d.sdp) await pc.setRemoteDescription(d.sdp); else if (d.candidate) await pc.addIceCandidate(d.candidate); } catch (e) {} }

    // reports
    async function watchJob(id) {
      sheet(true); $(root, ".sc-reph").style.display = "block"; const box = $(root, ".sc-report"); box.textContent = "Looking through the video…";
      for (let i = 0; i < 600 && !closed; i++) {
        try { const r = await fetch(`${o.api}/jobs/${id}`, { credentials: "same-origin" }); if (!r.ok) return; const j = await r.json();
          if (j.status !== "running") return report(j); box.textContent = `Looking through the video… ${Math.round((j.progress || 0) * 100)}%`; } catch (e) {}
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    function report(j) {
      $(root, ".sc-reph").style.display = "block"; const r = j.result || {};
      $(root, ".sc-report").textContent = j.status === "failed" ? "It failed: " + (j.error || "") :
        [r.summary, r.answer, (r.problems || []).length ? "⚠ " + r.problems.join(" · ") : "", ...(r.timeline || []).map((e) => `${e.t}  ${e.event}`)].filter(Boolean).join("\n");
      cap("her", r.summary || "Report ready (More ⋯).", j.kind === "recent" ? "what just happened" : "video report"); say(r.summary || "");
    }

    // ------------------------------------------------------------ buttons
    async function upload(blob, kind, name) {
      const f = new FormData(); f.append("file", blob, name); f.append("kind", kind);
      const r = await fetch(`${o.api}/upload`, { method: "POST", body: f, credentials: "same-origin" }); return r.ok ? r.json() : null;
    }
    function sheet(show) { $(root, ".sc-sheet").classList.toggle("open", show ?? !$(root, ".sc-sheet").classList.contains("open")); }
    async function act(a) {
      switch (a) {
        case "close": case "close2": close(); break;
        case "retry": $(root, ".sc-msg").style.display = "none"; if (await startCamera()) { if (!ws || ws.readyState > 1) connect(); } break;
        case "flip": facing = facing === "environment" ? "user" : "environment"; startCamera(); break;
        case "torch": torchOn = !torchOn; try { await track.applyConstraints({ advanced: [{ torch: torchOn }] }); } catch (e) { torchOn = false; } break;
        case "talk": listening = !listening; setTalk(); if (listening && !vad.an) startListening(); break;
        case "coach": if (!coachOn && !$(root, ".sc-steptext").textContent && !/^goal:/.test($(root, ".sc-sub").textContent)) {
            badge("unsure", "Tell me the goal first (More ⋯ → What are you doing?)"); sheet(true); $(root, ".sc-goal").focus(); break; }
          send({ type: "coach", seconds: coachOn ? 0 : 25 }); break;
        case "recent": send({ type: "recent", seconds: 90 }); cap("me", "What did I just do?"); break;
        case "more": sheet(); break;
        case "ask": { const q = $(root, ".sc-q"); ask(q.value.trim()); q.value = ""; sheet(false); break; }
        case "goal": { const g = $(root, ".sc-goal"); if (g.value.trim()) { send({ type: "goal", text: g.value.trim() }); sub("goal: " + g.value.trim()); g.value = ""; sheet(false); } break; }
        case "start": { const v = $(root, ".sc-procs").value; if (v) { send({ type: "procedure", name: v }); sheet(false); } break; }
        case "prev": send({ type: "step", action: "prev" }); break;
        case "next": send({ type: "step", action: "next" }); break;
        case "check": send({ type: "step", action: "check" }); lastQ = thinkingCap(); break;
        case "import": { const p = $(root, ".sc-seryn").value.trim(); if (!p) break;
          const r = await fetch(`${o.api}/procedures/import-seryn`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ protocol: p }) });
          const j = await r.json().catch(() => ({})); badge(r.ok ? "ok" : "unsure", r.ok ? `Imported ${j.name || p}` : (j.detail || "Import failed"));
          if (r.ok) { const pr = await fetch(`${o.api}/procedures`, { credentials: "same-origin" }); if (pr.ok) fillProcs(await pr.json()); } break; }
        case "upload": { const fs = $(root, ".sc-files").files; if (!fs.length) break; const f = new FormData(); for (const x of fs) f.append("files", x, x.name);
          const r = await fetch(`${o.api}/knowledge/upload`, { method: "POST", body: f, credentials: "same-origin" }); badge(r.ok ? "ok" : "unsure", r.ok ? "Added to what it knows" : "Upload failed"); break; }
        case "addurl": { const u = $(root, ".sc-url").value.trim(); if (!/^https?:\/\//.test(u)) break;
          const r = await fetch(`${o.api}/knowledge/url`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: u }) });
          badge(r.ok ? "ok" : "unsure", r.ok ? "Link added" : "Couldn't add that link"); break; }
        case "snap": { const b = await frameBlob(4096, .92); if (b && await upload(b, "photo", "snap.jpg")) badge("ok", "Photo saved"); break; }
        case "mute": muted = !muted; $(root, '[data-a="mute"]').textContent = muted ? "🔇 Voice off" : "🔊 Voice on"; if (muted) try { speechSynthesis.cancel(); } catch (e) {} break;
        case "record": await toggleRecord(); break;
      }
    }
    async function toggleRecord() {
      const btn = $(root, '[data-a="record"]');
      if (recorder) { recorder.stop(); return; }
      let rs = stream; try { if (mic) rs = new MediaStream([...stream.getVideoTracks(), ...mic.getAudioTracks()]); } catch (e) {}
      const type = ["video/webm;codecs=vp9,opus", "video/webm", "video/mp4"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
      try { recorder = new MediaRecorder(rs, type ? { mimeType: type } : undefined); } catch (e) { badge("unsure", "Recording isn't supported here"); return; }
      recChunks = []; recorder.ondataavailable = (e) => e.data.size && recChunks.push(e.data);
      recorder.onstop = async () => {
        const blob = new Blob(recChunks, { type: recorder.mimeType }); recorder = null; btn.classList.remove("rec");
        badge("unsure", "Saving the video…");
        const j = await upload(blob, "video", blob.type.includes("mp4") ? "clip.mp4" : "clip.webm");
        if (!j) { badge("unsure", "Couldn't save the video"); return; }
        badge("ok", "Saved. Reviewing what happened…");
        const r = await fetch(`${o.api}/media/${j.id}/analyze`, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" });
        if (r.ok) watchJob((await r.json()).job);
      };
      recorder.start(1000); btn.classList.add("rec");
    }
    root.addEventListener("click", (e) => { const b = e.target.closest("[data-a]"); if (b) act(b.dataset.a); });
    $(root, ".sc-q").addEventListener("keydown", (e) => { if (e.key === "Enter") act("ask"); });
    $(root, ".sc-res").addEventListener("change", startCamera);
    const vis = () => { if (!document.hidden && track?.readyState === "ended") startCamera(); };
    document.addEventListener("visibilitychange", vis);

    function close() {
      closed = true; clearInterval(ping); clearInterval(vad.timer);
      try { ws?.close(); } catch (e) {} try { speechSynthesis.cancel(); } catch (e) {}
      stream?.getTracks().forEach((t) => t.stop()); mic?.getTracks().forEach((t) => t.stop());
      Object.values(peers).forEach((p) => p.close()); try { wakeLock?.release(); } catch (e) {}
      document.removeEventListener("visibilitychange", vis); root.remove(); o.onClose && o.onClose();
    }

    (async () => {
      try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (e) {}
      try { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; speechSynthesis.speak(u); } catch (e) {}   // iOS: unlock voice
      if (await startCamera()) { connect(); requestAnimationFrame(loop); startListening(); }
    })();
    return { close, ask };
  }
  window.SerynCamera = { open };
})();
