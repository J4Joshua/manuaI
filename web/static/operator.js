/* ManuAI — Operator transport + chat render logic.
 *
 * Pairs with operator.html (camera + live transcription console).
 * Connection/PTT plumbing matches agent.py: /token → Room.connect →
 * start_turn/end_turn RPC → screen_state data channel.
 *
 * Each turn's screen_state becomes operator + ManuAI chat bubbles (citation
 * chip, safety line, steps, corroboration, escalation). Camera uses
 * getUserMedia as a stand-in for Ray-Ban glasses until wired.
 *
 * Typed /ask fallback (G3) survives if mic/agent is flaky.
 * Demo mode (default): scripted C57 brake-release scenario (cobot-cellA) — no LiveKit/mic. ?live=1 for real stack.
 * No CDN: LivekitClient from /static/livekit-client.umd.min.js.
 */
(function () {
  "use strict";

  var params = new URLSearchParams(window.location.search);
  // poll mode = offline, driven by glasses_bridge's /state HTTP poll (no LiveKit,
  // no browser mic; the bridge captures glasses audio and speaks on the laptop).
  var pollMode = params.get("poll") === "1";
  var liveMode = !pollMode && params.get("live") === "1";
  var demoMode = !pollMode && !liveMode;

  var LK = window.LivekitClient;

  var connDot = document.getElementById("conn-dot");
  var connText = document.getElementById("conn-text");
  var hdrMachine = document.getElementById("hdr-machine");
  var hdrStatus = document.getElementById("hdr-status");
  var pttBtn = document.getElementById("ptt-btn");
  var pttCap = document.getElementById("ptt-cap");
  var pttMachine = document.getElementById("ptt-machine");
  var convoEl = document.getElementById("convo");
  var emptyConvo = document.getElementById("empty-convo");
  var logEl = document.getElementById("op-log");
  var demoBtn = document.getElementById("demo-btn");

  var DEMO_QUESTION = "The break release failed and threw error. C57.";
  var DEMO_ANSWER_STATE = {
    question: DEMO_QUESTION,
    machine_id: "cobot-cellA",
    status: "answered",
    answer: "First check brake and solenoid, then check TCP configuration, payload, and mounting settings.",
    citations: [{
      sop_id: "UR-ERRCODES",
      section: "1.40. C57 Brake release failure",
      page: null,
      procedure_title: "1.40. C57 Brake release failure",
    }],
    steps_source: {
      sop_id: "UR-ERRCODES",
      section: "1.40. C57 Brake release failure",
      procedure_title: "1.40. C57 Brake release failure",
    },
    steps: [
      "Check brake and solenoid (per UR Error Codes Directory C57).",
      "Check TCP configuration, payload, and mounting settings.",
      "If the fault repeats, record any C57A1–C57A3 subcode from the Log Tab and escalate to automation maintenance.",
    ],
    safety_warnings: ["The arm can move when re-enabled — keep clear of the operating space and know the nearest E-STOP (SOP-2201 §3)."],
    safety_flag: true,
    top_score: 1.0,
    threshold: 0.7,
    source_excerpt: "",
    corroboration: [],
    corroboration_note: "Grounded to UR Error Codes Directory §1.40 (C57 brake release failure).",
  };
  var demoTimer = null;
  var pendingRevealMsgId = null;
  var activeSpeech = { cleanup: null, demoAudio: null, demoUrl: null, rafId: null };

  var STATUS_LABEL = {
    idle: "Idle",
    listening: "Listening",
    thinking: "Thinking",
    answered: "Answered",
    escalated: "Escalated",
  };

  function log(msg, level) {
    var ts = new Date().toLocaleTimeString();
    var line = "[" + ts + "] " + msg;
    if (level === "error") console.error(line);
    else console.log(line);
    if (logEl) {
      var div = document.createElement("div");
      div.className = "log-line" + (level ? " log-" + level : "");
      div.textContent = line;
      logEl.appendChild(div);
      while (logEl.childNodes.length > 200) logEl.removeChild(logEl.firstChild);
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function setStatus(status) {
    var s = status || "idle";
    if (hdrStatus) {
      hdrStatus.textContent = STATUS_LABEL[s] || "Idle";
      hdrStatus.className = "status-chip " + s;
    }
  }

  var IDLE_STATE = {
    question: "",
    machine_id: "",
    status: "idle",
    answer: "",
    citations: [],
    steps_source: null,
    steps: [],
    safety_warnings: [],
    safety_flag: false,
    top_score: 0.0,
    threshold: null,
    source_excerpt: "",
    context_bubble: { status: "idle", lines: [], updates: [], chunk_count: 0 },
  };

  function setConn(state, text) {
    if (connDot) connDot.className = "conn-dot conn-" + state;
    if (connText) connText.textContent = text;
  }

  function setMachine(id) {
    var label = id || "—";
    if (hdrMachine) hdrMachine.textContent = label;
    if (pttMachine) pttMachine.textContent = label;
  }

  // ── Camera panel ────────────────────────────────────────────────────────
  (function setupCamera() {
    var video = document.getElementById("cam-video");
    var placeholder = document.getElementById("cam-placeholder");
    var ptxt = document.getElementById("cam-ptxt");
    var phint = document.getElementById("cam-phint");
    var vignette = document.getElementById("cam-vignette");
    var overlay = document.getElementById("cam-overlay");
    var tsEl = document.getElementById("cam-ts");
    var stage = document.querySelector(".cam-stage");

    function showLive() {
      placeholder.hidden = true;
      vignette.hidden = false;
      overlay.hidden = false;
    }
    function showDemoFeed() {
      if (video) video.style.display = "none";
      if (stage) stage.classList.add("demo-feed");
      placeholder.hidden = false;
      vignette.hidden = false;
      overlay.hidden = false;
      ptxt.textContent = "Demo — Cell A cobot POV";
      phint.textContent = "Simulated Ray-Ban feed (no hardware required).";
    }
    function showPlaceholder(denied) {
      placeholder.hidden = false;
      vignette.hidden = true;
      overlay.hidden = true;
      if (denied) {
        ptxt.textContent = "Ray-Ban feed unavailable";
        phint.textContent = "Allow camera access to mirror the operator's first-person view here.";
      } else {
        ptxt.textContent = "Pairing with Ray-Ban glasses…";
        phint.textContent = "Waiting for the headset camera stream.";
      }
    }

    if (demoMode) {
      showDemoFeed();
    } else if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
      var cancelled = false;
      navigator.mediaDevices
        .getUserMedia({ video: { facingMode: "environment", width: 1280, height: 720 }, audio: false })
        .then(function (stream) {
          if (cancelled) {
            stream.getTracks().forEach(function (t) { t.stop(); });
            return;
          }
          video.srcObject = stream;
          showLive();
          log("Camera feed active (getUserMedia stand-in)");
        })
        .catch(function () {
          if (!cancelled) showPlaceholder(true);
        });
      window.addEventListener("beforeunload", function () { cancelled = true; });
    } else {
      showPlaceholder(true);
    }

    function tick() {
      var d = new Date();
      var p = function (n) { return String(n).padStart(2, "0"); };
      tsEl.textContent = p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
    }
    tick();
    setInterval(tick, 1000);
  })();

  // ── Transcript state + render ───────────────────────────────────────────
  var transcript = [];
  var idc = 0;
  function nid() { return "m" + idc++; }

  var pendingOperatorId = null;
  var pendingAgentId = null;
  var lastAgentId = null;

  function findMsg(id) {
    for (var i = 0; i < transcript.length; i++) {
      if (transcript[i].id === id) return transcript[i];
    }
    return null;
  }

  function render() {
    Array.prototype.slice.call(convoEl.querySelectorAll(".msg")).forEach(function (el) { el.remove(); });
    if (transcript.length === 0) {
      emptyConvo.style.display = "";
      return;
    }
    emptyConvo.style.display = "none";
    transcript.forEach(function (m) { convoEl.appendChild(renderMessage(m)); });
    convoEl.scrollTop = convoEl.scrollHeight;
  }

  function appendSteps(container, steps, stepsSource) {
    if (!steps || !steps.length) return;
    var ol = document.createElement("ol");
    ol.className = "steps-inline";
    steps.forEach(function (step) {
      var li = document.createElement("li");
      li.textContent = step;
      ol.appendChild(li);
    });
    container.appendChild(ol);
    if (stepsSource) {
      var src = document.createElement("div");
      src.className = "steps-source-inline";
      var parts = [];
      if (stepsSource.sop_id) parts.push(stepsSource.sop_id);
      if (stepsSource.section) parts.push("§" + stepsSource.section);
      if (stepsSource.procedure_title) parts.push("— " + stepsSource.procedure_title);
      src.textContent = parts.join(" ");
      container.appendChild(src);
    }
  }

  function hasGroundedExtras(m) {
    if (!m || m.role === "operator") return false;
    if (m.escalated) return true;
    if (m.safety) return true;
    if (m.steps && m.steps.length) return true;
    if (m.cite) return true;
    if (m.corroborationNote) return true;
    return false;
  }

  function groundedExtrasLabel(m) {
    var parts = [];
    if (m.steps && m.steps.length) parts.push(m.steps.length + " step" + (m.steps.length === 1 ? "" : "s"));
    if (m.safety) parts.push("safety");
    if (m.cite) parts.push("citation");
    if (m.corroborationNote) parts.push("context");
    if (m.escalated) parts.push("escalation");
    if (!parts.length) return "Grounded details";
    return "Grounded details · " + parts.join(", ");
  }

  function buildSpeakingBadge(escalated) {
    var speak = document.createElement("span");
    speak.className = "speaking-badge" + (escalated ? " escalated-speaking" : "");
    var wave = document.createElement("span");
    wave.className = "wave";
    for (var i = 0; i < 5; i++) wave.appendChild(document.createElement("span"));
    speak.appendChild(wave);
    speak.appendChild(document.createTextNode("Speaking"));
    return speak;
  }

  function buildGroundedDetails(m) {
    var details = document.createElement("details");
    details.className = "answer-details";

    var summary = document.createElement("summary");
    summary.textContent = groundedExtrasLabel(m);
    details.appendChild(summary);

    var inner = document.createElement("div");
    inner.className = "details-inner";

    if (m.escalated) {
      var escEl = document.createElement("div");
      escEl.className = "escalate-inline";
      var escIco = document.createElement("span");
      escIco.className = "ico";
      escIco.textContent = "🛑";
      escEl.appendChild(escIco);
      escEl.appendChild(document.createTextNode("Escalated to supervisor"));
      inner.appendChild(escEl);
    }

    if (m.safety) {
      var safetyEl = document.createElement("div");
      safetyEl.className = "safety-inline";
      var safetyIco = document.createElement("span");
      safetyIco.className = "ico";
      safetyIco.textContent = "⚠";
      safetyEl.appendChild(safetyIco);
      safetyEl.appendChild(document.createTextNode(m.safety));
      inner.appendChild(safetyEl);
    }

    if (!m.escalated) {
      appendSteps(inner, m.steps, m.stepsSource);
    }

    if (m.corroborationNote) {
      var corr = document.createElement("div");
      corr.className = "corroboration-inline";
      corr.textContent = m.corroborationNote;
      inner.appendChild(corr);
    }

    if (m.cite) {
      var chip = document.createElement("div");
      chip.className = "cite-chip";
      var cid = document.createElement("span");
      cid.className = "cid";
      cid.textContent = m.cite.id + (m.cite.page != null ? " · p." + m.cite.page : "");
      var sep = document.createElement("span");
      sep.className = "sep";
      var ctitle = document.createElement("span");
      ctitle.className = "ctitle";
      ctitle.textContent = m.cite.title || "";
      var verified = document.createElement("span");
      verified.className = "verified";
      verified.textContent = "✓ Grounded";
      chip.appendChild(cid);
      chip.appendChild(sep);
      chip.appendChild(ctitle);
      chip.appendChild(verified);
      inner.appendChild(chip);
    }

    details.appendChild(inner);
    return details;
  }

  function renderMessage(m) {
    var wrap = document.createElement("div");
    if (m.id) wrap.setAttribute("data-msg-id", m.id);

    if (m.role === "thinking") {
      wrap.className = "msg agent";
      var avatarT = document.createElement("div");
      avatarT.className = "avatar";
      avatarT.textContent = "AI";
      var bwT = document.createElement("div");
      bwT.className = "bubble-wrap";
      var whoT = document.createElement("span");
      whoT.className = "who";
      whoT.textContent = "ManuAI";
      var bubbleT = document.createElement("div");
      bubbleT.className = "bubble thinking-bubble";
      bubbleT.appendChild(document.createElement("i"));
      bubbleT.appendChild(document.createElement("i"));
      bubbleT.appendChild(document.createElement("i"));
      bwT.appendChild(whoT);
      bwT.appendChild(bubbleT);
      wrap.appendChild(avatarT);
      wrap.appendChild(bwT);
      return wrap;
    }

    var isOp = m.role === "operator";
    wrap.className = "msg " + (isOp ? "operator" : "agent") + (m.escalated ? " escalated" : "");

    var avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = isOp ? "OP" : "AI";

    var bubbleWrap = document.createElement("div");
    bubbleWrap.className = "bubble-wrap";

    var who = document.createElement("span");
    who.className = "who";
    who.textContent = isOp ? "Operator" : "ManuAI";
    if (!isOp && m.speaking) who.appendChild(buildSpeakingBadge(m.escalated));

    var bubble = document.createElement("div");
    bubble.className = "bubble" + (isOp && m.interim ? " interim" : "");

    var answerBody = document.createElement("div");
    answerBody.className = "answer-body" + (m.revealing ? " is-revealing" : "");

    var answerText = document.createElement("span");
    answerText.className = "answer-text";
    answerText.textContent = m.text || "";
    answerBody.appendChild(answerText);

    if (isOp && m.interim) {
      var opCursor = document.createElement("span");
      opCursor.className = "cursor";
      answerBody.appendChild(opCursor);
    } else if (!isOp && m.revealing) {
      var cursor = document.createElement("span");
      cursor.className = "cursor";
      answerBody.appendChild(cursor);
    }

    bubble.appendChild(answerBody);

    bubbleWrap.appendChild(who);
    bubbleWrap.appendChild(bubble);
    if (!isOp && m.showExtras && hasGroundedExtras(m)) {
      bubbleWrap.appendChild(buildGroundedDetails(m));
    }
    wrap.appendChild(avatar);
    wrap.appendChild(bubbleWrap);
    return wrap;
  }

  function firstCitation(citations) {
    if (!citations || !citations.length) return null;
    var c = citations[0];
    var id = (c.sop_id || "") + (c.section ? " §" + c.section : "");
    if (!id) return null;
    return {
      id: id,
      page: (c.page !== null && c.page !== undefined) ? c.page : null,
      title: c.procedure_title || "",
    };
  }

  function stopActiveSpeech() {
    if (activeSpeech.cleanup) activeSpeech.cleanup();
    if (activeSpeech.rafId) {
      cancelAnimationFrame(activeSpeech.rafId);
      activeSpeech.rafId = null;
    }
    if (activeSpeech.demoAudio) {
      activeSpeech.demoAudio.pause();
      activeSpeech.demoAudio = null;
    }
    if (activeSpeech.demoUrl) {
      URL.revokeObjectURL(activeSpeech.demoUrl);
      activeSpeech.demoUrl = null;
    }
    activeSpeech.cleanup = null;
  }

  function patchRevealBubble(msgId, text, revealing) {
    if (!convoEl) return false;
    var wrap = convoEl.querySelector('.msg[data-msg-id="' + msgId + '"]');
    if (!wrap) return false;
    var bubble = wrap.querySelector(".bubble");
    if (!bubble) return false;

    var answerText = bubble.querySelector(".answer-text");
    if (!answerText) return false;
    if (answerText.textContent !== text) answerText.textContent = text;

    var answerBody = bubble.querySelector(".answer-body");
    if (answerBody) answerBody.classList.toggle("is-revealing", revealing);

    var cursor = answerBody ? answerBody.querySelector(".cursor") : null;
    if (revealing) {
      if (!cursor && answerBody) {
        cursor = document.createElement("span");
        cursor.className = "cursor";
        answerBody.appendChild(cursor);
      }
    } else if (cursor) {
      cursor.remove();
    }

    convoEl.scrollTop = convoEl.scrollHeight;
    return true;
  }

  function textLengthForRatio(fullText, ratio) {
    if (!fullText) return 0;
    if (ratio >= 1) return fullText.length;
    return Math.floor(fullText.length * ratio);
  }

  function applyRevealFrame(msgId, ratio, speaking) {
    var m = findMsg(msgId);
    if (!m || !m.fullText) return;
    var len = textLengthForRatio(m.fullText, ratio);
    var newText = m.fullText.slice(0, len);
    var revealing = ratio < 1;
    if (m.text !== newText || m.revealing !== revealing || m.speaking !== speaking) {
      m.text = newText;
      m.revealing = revealing;
      m.speaking = speaking;
      if (!patchRevealBubble(msgId, newText, revealing)) render();
    }
  }

  function startRevealLoop(msgId, getRatio, isSpeaking, onComplete) {
    function tick() {
      if (!findMsg(msgId)) {
        activeSpeech.rafId = null;
        return;
      }
      var ratio = getRatio();
      var speaking = isSpeaking ? isSpeaking() : ratio < 1;
      applyRevealFrame(msgId, ratio, speaking);
      if (ratio >= 1) {
        activeSpeech.rafId = null;
        if (onComplete) onComplete();
        return;
      }
      activeSpeech.rafId = requestAnimationFrame(tick);
    }
    activeSpeech.rafId = requestAnimationFrame(tick);
    return function stopRaf() {
      if (activeSpeech.rafId) {
        cancelAnimationFrame(activeSpeech.rafId);
        activeSpeech.rafId = null;
      }
    };
  }

  function finishAgentReveal(msgId) {
    var m = findMsg(msgId);
    if (!m) return;
    m.text = m.fullText || m.text || "";
    m.revealing = false;
    m.speaking = false;
    m.showExtras = true;
    render();
  }

  function bindRevealToAudio(msgId, audioEl, demoUrl) {
    stopActiveSpeech();
    var stopRaf = null;

    function getRatio() {
      var dur = audioEl.duration;
      if (!dur || !isFinite(dur) || dur <= 0) return 0;
      return Math.min(1, audioEl.currentTime / dur);
    }

    function onEnd() {
      applyRevealFrame(msgId, 1, false);
      finishAgentReveal(msgId);
      cleanup();
    }

    function cleanup() {
      if (stopRaf) stopRaf();
      audioEl.removeEventListener("ended", onEnd);
      if (activeSpeech.demoAudio === audioEl) activeSpeech.demoAudio = null;
      if (demoUrl && activeSpeech.demoUrl === demoUrl) {
        URL.revokeObjectURL(demoUrl);
        activeSpeech.demoUrl = null;
      }
      activeSpeech.cleanup = null;
    }

    stopRaf = startRevealLoop(msgId, getRatio, function () {
      return !audioEl.paused && !audioEl.ended;
    });

    audioEl.addEventListener("ended", onEnd);

    activeSpeech.cleanup = cleanup;
    if (demoUrl) activeSpeech.demoUrl = demoUrl;
    if (audioEl.hasAttribute("data-demo-audio")) activeSpeech.demoAudio = audioEl;

    pendingRevealMsgId = null;
  }

  function timedTextReveal(msgId, text, durationMs) {
    var m = findMsg(msgId);
    if (!m) return;
    m.fullText = text;
    m.text = "";
    m.revealing = true;
    m.speaking = true;
    m.showExtras = false;
    render();

    var start = performance.now();
    var stopRaf = startRevealLoop(msgId, function () {
      return Math.min(1, (performance.now() - start) / durationMs);
    }, function () {
      return (performance.now() - start) < durationMs;
    }, function () {
      finishAgentReveal(msgId);
    });
    activeSpeech.cleanup = function () { stopRaf(); };
  }

  function speakWithBrowserTts(msgId, text) {
    if (!window.speechSynthesis) {
      timedTextReveal(msgId, text, Math.max(3500, text.length * 48));
      return;
    }
    window.speechSynthesis.cancel();
    var m = findMsg(msgId);
    if (m) {
      m.fullText = text;
      m.text = "";
      m.revealing = true;
      m.speaking = true;
      m.showExtras = false;
      render();
    }

    var utter = new SpeechSynthesisUtterance(text);
    utter.rate = 1;
    var estMs = Math.max(3500, text.length * 48);
    var start = performance.now();

    var stopRaf = startRevealLoop(msgId, function () {
      return Math.min(1, (performance.now() - start) / estMs);
    }, function () {
      return window.speechSynthesis.speaking;
    });

    utter.onend = function () {
      stopRaf();
      finishAgentReveal(msgId);
    };
    utter.onerror = function () {
      stopRaf();
      timedTextReveal(msgId, text, estMs);
    };
    window.speechSynthesis.speak(utter);
    activeSpeech.cleanup = function () {
      stopRaf();
      window.speechSynthesis.cancel();
    };
  }

  function getLivekitAudioEl() {
    var els = document.querySelectorAll("audio[data-livekit-audio]");
    return els.length ? els[els.length - 1] : null;
  }

  function startAgentSpeech(msgId, text) {
    stopActiveSpeech();
    if (!text) {
      finishAgentReveal(msgId);
      return;
    }

    var m = findMsg(msgId);
    if (m) {
      m.fullText = text;
      m.text = "";
      m.revealing = true;
      m.speaking = true;
      m.showExtras = false;
      render();
    }

    if (pollMode) {
      // The glasses bridge already speaks the answer on the laptop (Kokoro).
      // No browser audio — reveal the full text + extras (citations/steps/safety).
      finishAgentReveal(msgId);
      return;
    }

    if (demoMode) {
      fetch("/tts?text=" + encodeURIComponent(text))
        .then(function (r) {
          if (!r.ok) throw new Error("/tts HTTP " + r.status);
          return r.blob();
        })
        .then(function (blob) {
          var url = URL.createObjectURL(blob);
          var audio = new Audio(url);
          audio.setAttribute("data-demo-audio", "1");
          bindRevealToAudio(msgId, audio, url);
          return audio.play().then(function () { return audio; });
        })
        .then(function () { log("Demo TTS playing (Kokoro)"); })
        .catch(function (e) {
          stopActiveSpeech();
          log("Kokoro TTS unavailable — browser voice fallback: " + e, "error");
          speakWithBrowserTts(msgId, text);
        });
      return;
    }

    pendingRevealMsgId = msgId;
    var liveEl = getLivekitAudioEl();
    if (liveEl && !liveEl.paused) {
      bindRevealToAudio(msgId, liveEl, null);
    }
  }

  function updateContextBubble(s) {
    var panelContext = document.getElementById("panel-context");
    var contextList = document.getElementById("context-list");
    var contextStatus = document.getElementById("context-status");
    var contextUpdates = document.getElementById("context-updates");
    var contextRefresh = document.getElementById("context-refresh-btn");
    if (!panelContext) return;

    var bubble = (s && s.context_bubble) || {};
    var ctxLines = Array.isArray(bubble.lines) ? bubble.lines : [];
    var ctxUpdates = Array.isArray(bubble.updates) ? bubble.updates : [];
    var ctxStatus = bubble.status || "idle";
    var busyContext = ctxStatus === "gathering" || ctxStatus === "refreshing";

    if (ctxLines.length > 0 || ctxUpdates.length > 0 || busyContext) {
      panelContext.hidden = false;
      panelContext.classList.toggle("gathering", busyContext);
      var contextStatusLabel = ctxStatus === "gathering"
        ? "Moss swarm gathering related SOPs"
        : (ctxStatus === "refreshing" ? "Refreshing Moss context"
        : (ctxStatus === "ready" ? "Context ready" : "Background context"));
      if (ctxStatus !== "gathering" && ctxStatus !== "refreshing" && bubble.chunk_count) {
        contextStatusLabel += " - " + bubble.chunk_count + " chunks";
      }
      if (contextStatus) {
        contextStatus.textContent = contextStatusLabel;
        contextStatus.className = busyContext ? ctxStatus : "";
      }
      if (contextRefresh) contextRefresh.disabled = busyContext;
      if (contextUpdates) {
        contextUpdates.innerHTML = "";
        ctxUpdates.slice(-4).reverse().forEach(function (up) {
          var row = document.createElement("div");
          row.className = "context-update";
          row.title = up.query ? "Query: " + up.query : "";
          var delta = document.createElement("span");
          delta.className = "ctx-delta";
          delta.textContent = up.summary || "";
          var preview = document.createElement("span");
          preview.className = "ctx-preview";
          preview.textContent = up.preview || "";
          row.appendChild(delta);
          if (preview.textContent) row.appendChild(preview);
          contextUpdates.appendChild(row);
        });
      }
      if (contextList) {
        contextList.innerHTML = "";
        ctxLines.forEach(function (ln) {
          var row = document.createElement("div");
          row.className = "context-line";
          var sop = document.createElement("span");
          sop.className = "ctx-sop";
          sop.textContent = ln.sop_id || "SOP";
          var txt = document.createElement("span");
          txt.className = "ctx-text";
          txt.textContent = ln.text || "";
          row.appendChild(sop);
          row.appendChild(txt);
          contextList.appendChild(row);
        });
      }
    } else {
      panelContext.hidden = true;
      panelContext.classList.remove("gathering");
      if (contextRefresh) contextRefresh.disabled = false;
    }
  }

  function applyScreenState(state) {
    lastScreenState = state;
    updateContextBubble(state);
    setMachine(state.machine_id);
    var status = state.status || "idle";
    if (status === "idle") return;

    if (pendingOperatorId) {
      var op = findMsg(pendingOperatorId);
      if (op) {
        op.text = state.question || op.text;
        op.interim = false;
      }
      pendingOperatorId = null;
    } else if (state.question) {
      transcript.push({ id: nid(), role: "operator", text: state.question, interim: false });
    }

    var escalated = status === "escalated";
    var fullAnswer = state.answer || "";
    var agentMsg = {
      role: "agent",
      text: "",
      fullText: fullAnswer,
      revealing: !!fullAnswer,
      showExtras: !fullAnswer,
      safety: (!escalated && state.safety_warnings && state.safety_warnings.length)
        ? state.safety_warnings.join("; ")
        : null,
      steps: (!escalated && state.steps && state.steps.length) ? state.steps : null,
      stepsSource: (!escalated && state.steps_source) ? state.steps_source : null,
      corroborationNote: (state.corroboration_note || "").trim() || null,
      cite: !escalated ? firstCitation(state.citations) : null,
      escalated: escalated,
      speaking: !!fullAnswer,
    };

    if (pendingAgentId) {
      var ag = findMsg(pendingAgentId);
      var idx = ag ? transcript.indexOf(ag) : -1;
      if (idx !== -1) {
        agentMsg.id = pendingAgentId;
        transcript[idx] = agentMsg;
      } else {
        agentMsg.id = nid();
        transcript.push(agentMsg);
      }
      lastAgentId = agentMsg.id;
      pendingAgentId = null;
    } else {
      agentMsg.id = nid();
      transcript.push(agentMsg);
      lastAgentId = agentMsg.id;
    }

    setStatus(status);
    render();
    log("screen_state ← status=" + status);
    startAgentSpeech(lastAgentId, fullAnswer);
  }

  // ── LiveKit transport ───────────────────────────────────────────────────
  var room = null;
  var agentIdentity = null;
  var turnActive = false;
  var audioUnlocked = false;
  var lastScreenState = IDLE_STATE;

  function isAgent(p) {
    try {
      return p && p.kind === LK.ParticipantKind.AGENT;
    } catch (e) {
      return false;
    }
  }

  function onAgentReady(p) {
    if (agentIdentity) return;
    agentIdentity = p.identity;
    log("Agent ready: identity=" + agentIdentity);
    setConn("ready", "Ready — hold to talk");
    enableButton(true);
  }

  function scanForAgent() {
    if (!room) return;
    room.remoteParticipants.forEach(function (p) {
      if (isAgent(p)) onAgentReady(p);
    });
  }

  function enableButton(on) {
    if (!pttBtn) return;
    pttBtn.disabled = !on;
    if (on && pttCap && !turnActive) pttCap.textContent = "Hold";
  }

  function unlockAudio() {
    if (audioUnlocked || !room) return Promise.resolve();
    if (typeof room.startAudio === "function") {
      return room
        .startAudio()
        .then(function () {
          audioUnlocked = true;
          log("Audio playback unlocked");
        })
        .catch(function (e) {
          log("startAudio failed: " + e, "error");
        });
    }
    audioUnlocked = true;
    return Promise.resolve();
  }

  function attachTrack(track) {
    if (track.kind !== LK.Track.Kind.Audio) return;
    var el = track.attach();
    el.setAttribute("data-livekit-audio", "1");
    el.autoplay = true;
    el.addEventListener("playing", function () {
      if (pendingRevealMsgId) {
        bindRevealToAudio(pendingRevealMsgId, el, null);
      }
    });
    document.body.appendChild(el);
    log("Attached agent audio track");
  }

  function rpc(method, payload) {
    if (!room || !agentIdentity) {
      log("rpc(" + method + ") skipped: agent not ready", "error");
      return Promise.reject(new Error("agent not ready"));
    }
    return room.localParticipant
      .performRpc({
        destinationIdentity: agentIdentity,
        method: method,
        payload: payload || "",
      })
      .then(function (resp) {
        log("rpc " + method + " → " + resp);
        return resp;
      })
      .catch(function (e) {
        log("rpc " + method + " FAILED: " + e, "error");
        throw e;
      });
  }

  function startTurn() {
    if (demoMode) {
      demoStartTurn();
      return;
    }
    if (turnActive || !agentIdentity) return;
    turnActive = true;
    pttBtn.classList.add("talking");
    if (pttCap) pttCap.textContent = "Listening";
    setStatus("listening");

    var id = nid();
    pendingOperatorId = id;
    transcript.push({ id: id, role: "operator", text: "Listening…", interim: true });
    render();

    unlockAudio()
      .then(function () { return room.localParticipant.setMicrophoneEnabled(true); })
      .then(function () {
        log("Mic published");
        return rpc("start_turn");
      })
      .catch(function (e) {
        log("startTurn error: " + e, "error");
      });
  }

  function stopTurn() {
    if (demoMode) {
      demoStopTurn();
      return;
    }
    if (!turnActive) return;
    turnActive = false;
    pttBtn.classList.remove("talking");
    if (pttCap) pttCap.textContent = "Hold";
    setStatus("thinking");

    if (pendingOperatorId) {
      var op = findMsg(pendingOperatorId);
      if (op) op.text = "Transcribing…";
    }
    var thinkId = nid();
    pendingAgentId = thinkId;
    transcript.push({ id: thinkId, role: "thinking" });
    render();

    rpc("end_turn")
      .catch(function () {})
      .finally(function () {
        if (room && room.localParticipant) {
          room.localParticipant.setMicrophoneEnabled(false).catch(function (e) {
            log("mic mute failed: " + e, "error");
          });
        }
      });
  }

  function wireButton() {
    if (!pttBtn) return;

    pttBtn.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      if (pttBtn.disabled) return;
      try { pttBtn.setPointerCapture(e.pointerId); } catch (err) {}
      startTurn();
    });
    function release(e) {
      if (e && e.pointerId !== undefined) {
        try { pttBtn.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      stopTurn();
    }
    pttBtn.addEventListener("pointerup", release);
    pttBtn.addEventListener("pointercancel", release);

    pttBtn.addEventListener("keydown", function (e) {
      if (e.code === "Space" && !e.repeat && !pttBtn.disabled) {
        e.preventDefault();
        startTurn();
      }
    });
    pttBtn.addEventListener("keyup", function (e) {
      if (e.code === "Space") {
        e.preventDefault();
        stopTurn();
      }
    });

    window.addEventListener("pointerup", function () { if (turnActive) stopTurn(); });
    window.addEventListener("blur", function () { if (turnActive) stopTurn(); });
  }

  function wireTypedInput() {
    var askInput = document.getElementById("ask-input");
    var askBtn = document.getElementById("btn-ask");
    var spinnerEl = document.getElementById("spinner");
    var machineSelect = document.getElementById("machine-select");
    if (!askInput || !askBtn) return;

    function doAsk() {
      var q = askInput.value.trim();
      if (!q) return;
      var machine = (machineSelect && machineSelect.value) || "cobot-cellA";
      askBtn.disabled = true;
      if (spinnerEl) spinnerEl.style.display = "block";
      pendingOperatorId = null;
      pendingAgentId = null;
      var url = "/ask?q=" + encodeURIComponent(q) + "&machine=" + encodeURIComponent(machine);
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          applyScreenState(data);
          askInput.value = "";
        })
        .catch(function (e) {
          log("Ask failed: " + e, "error");
        })
        .finally(function () {
          askBtn.disabled = false;
          if (spinnerEl) spinnerEl.style.display = "none";
        });
    }
    askBtn.addEventListener("click", doAsk);
    askInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") doAsk();
    });
  }

  function clearDemoTimer() {
    if (demoTimer) {
      clearTimeout(demoTimer);
      demoTimer = null;
    }
  }

  function resetTranscript() {
    stopActiveSpeech();
    pendingRevealMsgId = null;
    transcript = [];
    pendingOperatorId = null;
    pendingAgentId = null;
    lastAgentId = null;
    turnActive = false;
    if (pttBtn) pttBtn.classList.remove("talking");
    render();
    setStatus("idle");
  }

  function demoStartTurn() {
    if (turnActive) return;
    clearDemoTimer();
    turnActive = true;
    if (pttBtn) pttBtn.classList.add("talking");
    if (pttCap) pttCap.textContent = "Listening";
    setStatus("listening");

    var id = nid();
    pendingOperatorId = id;
    transcript.push({ id: id, role: "operator", text: "Listening…", interim: true });
    render();
  }

  function demoStopTurn() {
    if (!turnActive) return;
    turnActive = false;
    if (pttBtn) pttBtn.classList.remove("talking");
    if (pttCap) pttCap.textContent = "Hold";
    setStatus("thinking");

    if (pendingOperatorId) {
      var op = findMsg(pendingOperatorId);
      if (op) {
        op.text = DEMO_QUESTION;
        op.interim = false;
      }
      pendingOperatorId = null;
    } else {
      transcript.push({ id: nid(), role: "operator", text: DEMO_QUESTION, interim: false });
    }

    var thinkId = nid();
    pendingAgentId = thinkId;
    transcript.push({ id: thinkId, role: "thinking" });
    render();

    demoTimer = setTimeout(function () {
      applyScreenState(DEMO_ANSWER_STATE);
    }, 1400);
  }

  function runDemoScenario() {
    if (turnActive) return;
    resetTranscript();
    log("Playing demo scenario — Cell A C57 brake release");
    demoStartTurn();
    demoTimer = setTimeout(function () {
      demoStopTurn();
    }, 1100);
  }

  function initDemo() {
    if (demoBtn) demoBtn.hidden = false;
    log("Demo mode — scripted Cell A C57 brake-release scenario");
    log("Hold to talk, click Play demo, or wait for auto-play. ?live=1 for LiveKit.");
    setMachine("cobot-cellA");
    var machineSelect = document.getElementById("machine-select");
    if (machineSelect) machineSelect.value = "cobot-cellA";
    setConn("ready", "Demo mode");
    setStatus("idle");
    enableButton(true);
    if (pttCap) pttCap.textContent = "Hold";
    demoTimer = setTimeout(runDemoScenario, 1500);
  }

  function wireDemoButton() {
    if (!demoBtn) return;
    demoBtn.addEventListener("click", function () {
      clearDemoTimer();
      runDemoScenario();
    });
  }

  function connect() {
    setConn("connecting", "Fetching token…");
    log("Fetching /token …");
    var ident = "operator-" + Math.random().toString(36).slice(2, 8);

    fetch("/token?identity=" + encodeURIComponent(ident))
      .then(function (r) {
        if (!r.ok) throw new Error("/token HTTP " + r.status);
        return r.json();
      })
      .then(function (cfg) {
        log(
          "Token OK — url=" + cfg.url + " room=" + cfg.room +
          " agent_dispatch=" + cfg.agent_dispatch
        );
        if (cfg.agent_dispatch === false) {
          log(
            "NOTE: token has no agent dispatch — start the agent manually: " +
            "lk dispatch create --room " + cfg.room + " --agent-name manuai",
            "error"
          );
        }
        room = new LK.Room({ adaptiveStream: true, dynacast: true });

        room.on(LK.RoomEvent.DataReceived, function (payload, participant, kind, topic) {
          if (topic !== "screen_state") return;
          try {
            var state = JSON.parse(new TextDecoder().decode(payload));
            applyScreenState(state);
          } catch (err) {
            log("bad screen_state payload: " + err, "error");
          }
        });

        room.on(LK.RoomEvent.TrackSubscribed, function (track) { attachTrack(track); });

        room.on(LK.RoomEvent.ParticipantConnected, function (p) {
          log("participant joined: " + p.identity);
          if (isAgent(p)) onAgentReady(p);
        });
        room.on(LK.RoomEvent.ParticipantDisconnected, function (p) {
          log("participant left: " + p.identity);
          if (agentIdentity && p.identity === agentIdentity) {
            agentIdentity = null;
            enableButton(false);
            setConn("connected", "Connected — waiting for agent…");
            if (pttCap) pttCap.textContent = "Waiting…";
          }
        });
        room.on(LK.RoomEvent.Disconnected, function (reason) {
          log("room disconnected: " + reason, "error");
          setConn("error", "Disconnected");
          enableButton(false);
        });
        room.on(LK.RoomEvent.Reconnecting, function () { setConn("connecting", "Reconnecting…"); });
        room.on(LK.RoomEvent.Reconnected, function () {
          setConn("connected", "Reconnected");
          scanForAgent();
        });

        return room.connect(cfg.url, cfg.token);
      })
      .then(function () {
        setConn("connected", "Connected — waiting for agent…");
        log("Connected to room as " + ident);
        if (pttCap) pttCap.textContent = "Waiting…";
        scanForAgent();
      })
      .catch(function (e) {
        setConn("error", "Connect failed");
        log("connect() failed: " + e, "error");
      });
  }

  function wireContextRefresh() {
    var refreshBtn = document.getElementById("context-refresh-btn");
    var machineSelect = document.getElementById("machine-select");
    var askInput = document.getElementById("ask-input");
    if (!refreshBtn) return;

    function refreshViaHttp() {
      var state = lastScreenState || IDLE_STATE;
      var machine = state.machine_id ||
        (machineSelect && machineSelect.value) ||
        "cobot-cellA";
      var q = state.question || (askInput && askInput.value.trim()) || "";
      var url = "/context/refresh?machine=" + encodeURIComponent(machine);
      if (q) url += "&q=" + encodeURIComponent(q);
      return fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) throw new Error(data.error);
          updateContextBubble(data);
          lastScreenState = data;
          return data;
        });
    }

    refreshBtn.addEventListener("click", function () {
      var state = lastScreenState || IDLE_STATE;
      var payload = JSON.stringify({ question: state.question || "" });
      refreshBtn.disabled = true;

      var refreshPromise = room && agentIdentity
        ? rpc("refresh_context", payload)
        : refreshViaHttp();

      refreshPromise
        .then(function (resp) {
          if (typeof resp === "string") {
            log("context refresh → " + resp);
          } else {
            log("context refresh → HTTP fallback");
          }
        })
        .catch(function (e) {
          log("context refresh failed: " + e, "error");
          if (room && agentIdentity) {
            return refreshViaHttp().catch(function (fallbackErr) {
              log("context refresh fallback failed: " + fallbackErr, "error");
            });
          }
        })
        .finally(function () {
          refreshBtn.disabled = false;
        });
    });
  }

  function initPoll() {
    // Offline glasses display: hide the LiveKit/demo/typed controls (none of
    // their endpoints exist on the bridge) and just poll /state.
    [demoBtn, pttBtn,
     document.getElementById("ask-input"),
     document.getElementById("context-refresh-btn")].forEach(function (el) {
      if (el) el.hidden = true;
    });
    setMachine("—");
    setConn("ready", "Listening — glasses");
    setStatus("idle");
    if (pttCap) pttCap.textContent = "Speak into the glasses";
    log("Poll mode — screen driven by glasses_bridge /state (no LiveKit, no browser mic).");

    var lastSig = null;
    function pollTick() {
      fetch("/state")
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (!s) return;
          updateContextBubble(s);
          var status = s.status || "idle";
          if (status === "idle") return;
          // turn_seq (stamped by the bridge per utterance) distinguishes a fresh
          // repeat of the same beat from the same answer still on screen.
          var sig = [s.turn_seq, s.question, status, s.answer, s.top_score].join("|");
          if (sig === lastSig) return;
          lastSig = sig;
          applyScreenState(s);
        })
        .catch(function () { /* bridge restarting — keep polling */ });
    }
    setInterval(pollTick, 600);
    pollTick();
  }

  function boot() {
    wireButton();
    wireTypedInput();
    wireContextRefresh();
    wireDemoButton();
    lastScreenState = IDLE_STATE;
    updateContextBubble(IDLE_STATE);

    if (pollMode) {
      initPoll();
      return;
    }

    if (demoMode) {
      initDemo();
      return;
    }

    if (demoBtn) demoBtn.hidden = true;

    if (!LK) {
      setConn("error", "livekit-client failed to load");
      log("FATAL: window.LivekitClient is undefined", "error");
      return;
    }
    setStatus("idle");
    enableButton(false);
    setConn("offline", "Connecting…");
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
