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
 * Demo mode (default): scripted C57 brake-release scenario (cobot-cellA) — no LiveKit. ?live=1 or ?poll=1 for live stack.
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
  var convoEl = document.getElementById("convo");
  var emptyConvo = document.getElementById("empty-convo");
  var demoBtn = document.getElementById("demo-btn");
  var contextGraphPanel = document.getElementById("context-graph-panel");
  var contextGraphCanvas = document.getElementById("context-graph-canvas");
  var contextGraphStatus = document.getElementById("context-graph-status");
  var contextGraphFeed = document.getElementById("context-graph-feed");
  var contextGraphExpand = document.getElementById("context-graph-expand");
  var contextGraphDetailInline = document.getElementById("context-graph-detail-inline");
  var contextGraphFs = document.getElementById("context-graph-fs");
  var contextGraphFsCanvas = document.getElementById("context-graph-fs-canvas");
  var contextGraphFsStatus = document.getElementById("context-graph-fs-status");
  var contextGraphFsClose = document.getElementById("context-graph-fs-close");
  var contextGraphFsReset = document.getElementById("context-graph-fs-reset");
  var contextGraphDetailBody = document.getElementById("context-graph-detail-body");

  var graphInteraction = {
    fsOpen: false,
    selectedId: null,
    compact: { areas: [] },
    expanded: { areas: [] },
  };

  var forceGraph = {
    rafId: null,
    nodes: [],
    edges: [],
    nodeById: {},
    width: 800,
    height: 600,
    mouse: { x: 0, y: 0, active: false },
    alpha: 1,
    model: null,
    bubble: null,
    fsClickStart: null,
    camera: { x: 0, y: 0, scale: 1, targetX: 0, targetY: 0, targetScale: 1 },
    viewFocused: false,
    stars: [],
    starsKey: "",
  };

  var CONTEXT_POOL_MAX = 18;
  var lastGraphChunkCount = 0;
  var graphPulseUntil = 0;
  var graphAnimFrame = null;
  var demoContextTimer = null;
  var graphBuild = {
    target: null,
    display: null,
    queue: [],
    timer: null,
    chunkStaggerMs: 240,
    queryLeadMs: 140,
  };
  var graphCompactAnimFrame = null;
  var lastContextTurnSeq = null;
  var lastContextQuestion = "";

  var DEMO_QUESTION = "The break release failed and threw error. C57.";
  var DEMO_POOL_BATCHES = [
    {
      query: DEMO_QUESTION,
      source: "seed",
      lines: [
        { sop_id: "UR-ERRCODES", text: "1.40. C57 Brake release failure", score: 0.99, chunk_id: "demo-c57", source: "seed" },
      ],
    },
    {
      query: "LOTO and protective stop for cobot brake work",
      source: "swarm",
      lines: [
        { sop_id: "SOP-2201", text: "Protective stop recovery — E-STOP clear", score: 0.84, chunk_id: "demo-2201", source: "swarm" },
        { sop_id: "SOP-2204", text: "LOTO cobot cell before brake work", score: 0.78, chunk_id: "demo-2204", source: "swarm" },
      ],
    },
    {
      query: "C57 subcodes and fault log guidance",
      source: "swarm",
      lines: [
        { sop_id: "UR-ERRCODES", text: "C57A1–C57A3 subcode log guidance", score: 0.71, chunk_id: "demo-c57-sub", source: "swarm" },
      ],
    },
  ];

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
    context_bubble: { status: "idle", lines: [], updates: [], queries: [], chunk_count: 0 },
  };

  function setConn(state, text) {
    if (connDot) connDot.className = "conn-dot conn-" + state;
    if (connText) connText.textContent = text;
  }

  function formatCorpusLabel(state) {
    if (!state) return "Full corpus indexed";
    var bubble = state.context_bubble || {};
    var bubbleStatus = bubble.status || "idle";
    if (bubbleStatus === "gathering" || bubbleStatus === "refreshing") {
      return "Retrieval active";
    }
    if (state.status === "answered" && state.citations && state.citations.length) {
      var ids = [];
      var seen = {};
      state.citations.forEach(function (c) {
        if (c.sop_id && !seen[c.sop_id]) {
          seen[c.sop_id] = true;
          ids.push(c.sop_id);
        }
      });
      if (ids.length) {
        var shown = ids.slice(0, 2).join(" · ");
        if (ids.length > 2) shown += " +" + (ids.length - 2);
        return "Grounded: " + shown;
      }
    }
    if (bubble.chunk_count > 0 && bubbleStatus === "ready") {
      return bubble.chunk_count + " related chunks";
    }
    if (state.status === "escalated") {
      return "No approved match";
    }
    return "Full corpus indexed";
  }

  function setCorpusBadge(state) {
    var label = formatCorpusLabel(state);
    if (hdrMachine) {
      hdrMachine.textContent = label;
      hdrMachine.title = "Searching all indexed SOPs and manuals (no machine filter)";
    }
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

  function sourceColor(source) {
    if (source === "swarm") return "#2D5A3D";
    if (source === "refresh") return "#9A7B4F";
    return "#111111";
  }

  function forceSourceColor(source) {
    if (source === "swarm") return "#8FB99B";
    if (source === "refresh") return "#D4B896";
    return "#F4E8DC";
  }

  function forceSourceColorRgba(source, alpha) {
    if (source === "swarm") return "rgba(143,185,155," + alpha + ")";
    if (source === "refresh") return "rgba(212,184,150," + alpha + ")";
    return "rgba(244,232,220," + alpha + ")";
  }

  function sourceColorRgba(source, alpha) {
    if (source === "swarm") return "rgba(45,90,61," + alpha + ")";
    if (source === "refresh") return "rgba(154,123,79," + alpha + ")";
    return "rgba(17,17,17," + alpha + ")";
  }

  function truncateGraphLabel(text, maxLen) {
    var s = String(text || "");
    if (s.length <= maxLen) return s;
    return s.slice(0, maxLen - 1) + "\u2026";
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function queryNodeLabel(query, source) {
    if (query) return truncateGraphLabel(query, 16);
    if (source === "seed") return "Operator query";
    if (source === "refresh") return "Refresh search";
    return "Swarm query";
  }

  function sourceLabel(source) {
    if (source === "seed") return "Seed hit (primary retrieval)";
    if (source === "refresh") return "Manual refresh";
    if (source === "swarm") return "Background swarm";
    return source || "Unknown";
  }

  function formatQueryDuration(ms) {
    if (ms == null || isNaN(ms)) return "";
    if (ms < 1000) return ms + " ms";
    return (ms / 1000).toFixed(ms >= 10000 ? 1 : 2) + " s";
  }

  function formatQueryTiming(q) {
    if (!q) return "";
    if (q.status === "running") return "running…";
    if (q.duration_ms != null) return formatQueryDuration(q.duration_ms);
    return "";
  }

  function cloneBubble(b) {
    b = b || empty_bubble();
    return {
      status: b.status || "idle",
      chunk_count: b.chunk_count || 0,
      lines: (b.lines || []).map(function (ln) { return Object.assign({}, ln); }),
      updates: (b.updates || []).map(function (up) { return Object.assign({}, up); }),
      queries: (b.queries || []).map(function (q) { return Object.assign({}, q); }),
    };
  }

  function resetGraphBuild() {
    if (graphBuild.timer) {
      clearTimeout(graphBuild.timer);
      graphBuild.timer = null;
    }
    stopCompactGraphAnim();
    graphBuild.target = null;
    graphBuild.display = cloneBubble(empty_bubble());
    graphBuild.queue = [];
  }

  function beginNewContextPoolTurn() {
    clearDemoContextTimer();
    stopCompactGraphAnim();
    resetGraphBuild();
    lastGraphChunkCount = 0;
    paintContextGraph();
    if (graphInteraction.fsOpen && contextGraphFsCanvas) {
      syncForceGraph(empty_bubble());
      if (!forceGraph.rafId) renderForceGraph();
    }
  }

  function shouldResetContextPoolForTurn(s) {
    if (!s) return false;
    var turnSeq = s.turn_seq;
    if (turnSeq != null && turnSeq !== "" && turnSeq !== lastContextTurnSeq) {
      lastContextTurnSeq = turnSeq;
      lastContextQuestion = (s.question || "").trim();
      return true;
    }
    var question = (s.question || "").trim();
    if (!question) return false;
    if (question === lastContextQuestion) return false;
    var status = s.status || "idle";
    if (status === "idle") return false;
    lastContextQuestion = question;
    return true;
  }

  function upsertDisplayQuery(q) {
    if (!graphBuild.display) graphBuild.display = cloneBubble(empty_bubble());
    var queries = graphBuild.display.queries;
    var idx = -1;
    queries.forEach(function (existing, i) {
      if (existing.id === q.id) idx = i;
    });
    if (idx >= 0) queries[idx] = Object.assign({}, q);
    else queries.push(Object.assign({}, q));
  }

  function pushDisplayLine(ln) {
    if (!graphBuild.display) graphBuild.display = cloneBubble(empty_bubble());
    var exists = graphBuild.display.lines.some(function (l) { return l.chunk_id === ln.chunk_id; });
    if (!exists) graphBuild.display.lines.push(Object.assign({}, ln));
    graphBuild.display.chunk_count = graphBuild.display.lines.length;
  }

  function pushDisplayUpdate(up) {
    if (!graphBuild.display) graphBuild.display = cloneBubble(empty_bubble());
    var exists = graphBuild.display.updates.some(function (u, i) {
      return u.query_id && up.query_id && u.query_id === up.query_id;
    });
    if (!exists) graphBuild.display.updates.push(Object.assign({}, up));
  }

  function displayLineIds() {
    var ids = {};
    (graphBuild.display && graphBuild.display.lines || []).forEach(function (ln) {
      if (ln.chunk_id) ids[ln.chunk_id] = true;
    });
    return ids;
  }

  function displayQueryById() {
    var map = {};
    (graphBuild.display && graphBuild.display.queries || []).forEach(function (q) {
      map[q.id] = q;
    });
    return map;
  }

  function findLineInBubble(bubble, chunkId) {
    return (bubble.lines || []).filter(function (ln) { return ln.chunk_id === chunkId; })[0] || null;
  }

  function planGraphReveal(incoming) {
    graphBuild.target = cloneBubble(incoming);
    if (!graphBuild.display) graphBuild.display = cloneBubble(empty_bubble());
    if (graphBuild.timer) return;

    graphBuild.queue = [];
    var target = graphBuild.target;
    var shownLines = displayLineIds();
    var shownQueries = displayQueryById();

    (target.queries || []).forEach(function (q) {
      var shown = shownQueries[q.id];
      if (!shown) {
        graphBuild.queue.push({ kind: "query", query: Object.assign({}, q) });
        if (q.status === "running") return;
      } else if (shown.status === "running" && q.status === "done") {
        graphBuild.queue.push({ kind: "query", query: Object.assign({}, q) });
      }

      if (q.status !== "done") return;

      var chunkIds = q.chunk_ids || [];
      (target.lines || []).forEach(function (ln) {
        if (!ln.chunk_id || shownLines[ln.chunk_id]) return;
        var match = chunkIds.indexOf(ln.chunk_id) >= 0 ||
          ((ln.query || "") === (q.query || "") && (ln.source || "seed") === (q.source || "seed"));
        if (match) {
          graphBuild.queue.push({ kind: "chunk", line: Object.assign({}, ln), queryId: q.id });
          shownLines[ln.chunk_id] = true;
        }
      });

      (target.updates || []).forEach(function (up) {
        if (up.query_id === q.id) {
          graphBuild.queue.push({ kind: "update", update: Object.assign({}, up) });
        }
      });
    });

    if (!(target.queries || []).length) {
      var shownUpdateCount = (graphBuild.display.updates || []).length;
      (target.updates || []).slice(shownUpdateCount).forEach(function (up) {
        graphBuild.queue.push({ kind: "update", update: Object.assign({}, up) });
        (up.chunk_ids || []).forEach(function (cid) {
          if (shownLines[cid]) return;
          var ln = findLineInBubble(target, cid);
          if (ln) {
            graphBuild.queue.push({ kind: "chunk", line: Object.assign({}, ln) });
            shownLines[cid] = true;
          }
        });
      });
    }

    graphBuild.queue.push({ kind: "sync", status: target.status, chunk_count: target.chunk_count });
    pumpGraphReveal();
  }

  function pumpGraphReveal() {
    if (graphBuild.timer) return;
    function step() {
      if (!graphBuild.queue.length) {
        graphBuild.timer = null;
        paintContextGraph();
        if (needsMoreGraphReveal()) planGraphReveal(graphBuild.target);
        return;
      }
      var item = graphBuild.queue.shift();
      if (item.kind === "query") upsertDisplayQuery(item.query);
      else if (item.kind === "chunk") pushDisplayLine(item.line);
      else if (item.kind === "update") pushDisplayUpdate(item.update);
      else if (item.kind === "sync" && graphBuild.display) {
        graphBuild.display.status = item.status || graphBuild.display.status;
        if (item.chunk_count != null) graphBuild.display.chunk_count = item.chunk_count;
      }
      paintContextGraph();
      scheduleGraphPulse();
      var delay = item.kind === "chunk" ? graphBuild.chunkStaggerMs : graphBuild.queryLeadMs;
      graphBuild.timer = setTimeout(step, delay);
    }
    step();
  }

  function needsMoreGraphReveal() {
    var target = graphBuild.target;
    var display = graphBuild.display;
    if (!target || !display) return false;
    if ((target.lines || []).length > (display.lines || []).length) return true;
    var shownQueries = displayQueryById();
    return (target.queries || []).some(function (q) {
      var shown = shownQueries[q.id];
      if (!shown) return true;
      return shown.status === "running" && q.status === "done";
    });
  }

  function getGraphBubble() {
    return graphBuild.display || graphBuild.target || empty_bubble();
  }

  function ingestContextBubble(incoming) {
    var bubble = incoming || empty_bubble();
    if (!graphBuild.display) graphBuild.display = cloneBubble(empty_bubble());
    planGraphReveal(bubble);
  }

  function hasRunningQueries(bubble) {
    bubble = bubble || getGraphBubble();
    if ((bubble.queries || []).some(function (q) { return q.status === "running"; })) return true;
    try {
      var model = buildContextGraphModel(bubble);
      return (model.batches || []).some(function (b) { return b.running; });
    } catch (e) {
      return false;
    }
  }

  function stopCompactGraphAnim() {
    if (graphCompactAnimFrame) {
      cancelAnimationFrame(graphCompactAnimFrame);
      graphCompactAnimFrame = null;
    }
  }

  function startCompactGraphAnim() {
    if (graphCompactAnimFrame) return;
    function tick() {
      var bubble = getGraphBubble();
      if (!hasRunningQueries(bubble)) {
        graphCompactAnimFrame = null;
        return;
      }
      drawContextGraphOnCanvas(contextGraphCanvas, bubble, 0, {
        compact: true,
        hitStore: graphInteraction.compact,
      });
      graphCompactAnimFrame = requestAnimationFrame(tick);
    }
    graphCompactAnimFrame = requestAnimationFrame(tick);
  }

  function paintContextGraph(extraState) {
    var bubble = getGraphBubble();
    var count = bubble.chunk_count || (bubble.lines ? bubble.lines.length : 0);
    var busy = bubble.status === "gathering" || bubble.status === "refreshing";

    if (contextGraphPanel) contextGraphPanel.classList.toggle("gathering", busy);
    if (contextGraphStatus) {
      contextGraphStatus.textContent = formatGraphStatus(bubble);
      contextGraphStatus.className = busy ? bubble.status : "";
    }
    if (contextGraphFeed) {
      contextGraphFeed.innerHTML = formatGraphFeedHtml(bubble);
    }
    if (contextGraphFsStatus) contextGraphFsStatus.textContent = formatGraphStatus(bubble);

    if (count > lastGraphChunkCount) scheduleGraphPulse();
    lastGraphChunkCount = count;

    if (!graphAnimFrame) {
      drawContextGraphOnCanvas(contextGraphCanvas, bubble, 0, {
        compact: true,
        hitStore: graphInteraction.compact,
      });
      if (graphInteraction.fsOpen) {
        syncForceGraph(bubble);
        forceGraph.alpha = Math.max(forceGraph.alpha, 0.35);
      }
    }

    if (extraState) {
      lastScreenState = Object.assign({}, lastScreenState, extraState);
      if (graphBuild.target) lastScreenState.context_bubble = cloneBubble(graphBuild.target);
      if (extraState.status || extraState.context_bubble) setCorpusBadge(lastScreenState);
    }

    if (hasRunningQueries(bubble)) startCompactGraphAnim();
    else stopCompactGraphAnim();
  }

  function getGraphNeighborIds(nodeId, edges) {
    var neighbors = [];
    (edges || []).forEach(function (edge) {
      if (edge.from === nodeId) neighbors.push(edge.to);
      else if (edge.to === nodeId) neighbors.push(edge.from);
    });
    return neighbors;
  }

  function graphNodeMeta(id, model) {
    var batch = (model.batches || []).filter(function (b) { return b.id === id; })[0];
    if (batch) {
      var timing = formatQueryTiming(batch);
      return {
        type: "query",
        label: (batch.shortLabel || "Q?") + " · " + truncateGraphLabel(batch.label || batch.query, 32) +
          (timing ? " · " + timing : ""),
        source: batch.source || "seed",
      };
    }
    var ln = model.lineById && model.lineById[id];
    if (ln) {
      return {
        type: "chunk",
        label: (ln.sop_id || "SOP") + " · score " + (ln.score != null ? ln.score : "—"),
        source: ln.source || "seed",
      };
    }
    return { type: "chunk", label: id, source: "seed" };
  }

  function screenToForceWorld(sx, sy) {
    var cam = forceGraph.camera;
    var w = forceGraph.width;
    var h = forceGraph.height;
    return {
      x: (sx - w / 2) / cam.scale + cam.x,
      y: (sy - h / 2) / cam.scale + cam.y,
    };
  }

  function fitForceGraphCamera() {
    var nodes = forceGraph.nodes;
    if (!nodes.length) return;
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    nodes.forEach(function (n) {
      minX = Math.min(minX, n.x - n.r - 24);
      minY = Math.min(minY, n.y - n.r - 28);
      maxX = Math.max(maxX, n.x + n.r + 24);
      maxY = Math.max(maxY, n.y + n.r + 36);
    });
    var pad = 48;
    var bw = Math.max(120, maxX - minX + pad * 2);
    var bh = Math.max(120, maxY - minY + pad * 2);
    var w = forceGraph.width;
    var h = forceGraph.height;
    var scale = Math.min(w / bw, h / bh, 1.35);
    forceGraph.camera.targetX = (minX + maxX) / 2;
    forceGraph.camera.targetY = (minY + maxY) / 2;
    forceGraph.camera.targetScale = Math.max(0.55, scale);
    forceGraph.viewFocused = false;
    updateGraphResetButton();
  }

  function focusForceGraphOnNode(nodeId) {
    var node = forceGraph.nodeById[nodeId];
    if (!node) return;
    forceGraph.camera.targetX = node.x;
    forceGraph.camera.targetY = node.y;
    forceGraph.camera.targetScale = 2.15;
    forceGraph.viewFocused = true;
    forceGraph.alpha = Math.max(forceGraph.alpha, 0.45);
    updateGraphResetButton();
  }

  function updateForceGraphCamera() {
    var cam = forceGraph.camera;
    var ease = 0.14;
    cam.x += (cam.targetX - cam.x) * ease;
    cam.y += (cam.targetY - cam.y) * ease;
    cam.scale += (cam.targetScale - cam.scale) * ease;
  }

  function updateGraphResetButton() {
    if (!contextGraphFsReset) return;
    contextGraphFsReset.hidden = !graphInteraction.fsOpen || !forceGraph.viewFocused;
  }

  function forceNodeVisualState(nodeId, selectedId, neighborSet, fsOpen) {
    if (!fsOpen || !selectedId) return "normal";
    if (nodeId === selectedId) return "selected";
    if (neighborSet && neighborSet[nodeId]) return "connected";
    return "dimmed";
  }

  function chunkNodeFillGradient(ctx, x, y, r, source, opts) {
    opts = opts || {};
    var onForce = !!opts.force;
    var grad = ctx.createRadialGradient(x - r * 0.35, y - r * 0.4, r * 0.08, x, y, r);
    if (source === "swarm") {
      grad.addColorStop(0, onForce ? "#A8C9B2" : "#4A7358");
      grad.addColorStop(0.55, onForce ? "#5E8F72" : "#2D5A3D");
      grad.addColorStop(1, onForce ? "#2D5A3D" : "#1E3D29");
    } else if (source === "refresh") {
      grad.addColorStop(0, onForce ? "#E0CBA8" : "#B89560");
      grad.addColorStop(0.55, onForce ? "#B89560" : "#9A7B4F");
      grad.addColorStop(1, onForce ? "#7A6040" : "#6B5435");
    } else {
      grad.addColorStop(0, onForce ? "#F4E8DC" : "#444444");
      grad.addColorStop(0.55, onForce ? "#D4A574" : "#111111");
      grad.addColorStop(1, onForce ? "#B8431F" : "#000000");
    }
    return grad;
  }

  function forceQueryFillGradient(ctx, x, y, r, source) {
    var grad = ctx.createRadialGradient(x - r * 0.3, y - r * 0.35, r * 0.1, x, y, r);
    if (source === "swarm") {
      grad.addColorStop(0, "#CFE3D4");
      grad.addColorStop(0.55, "#6FA384");
      grad.addColorStop(1, "#2D5A3D");
    } else if (source === "refresh") {
      grad.addColorStop(0, "#F0E0C4");
      grad.addColorStop(0.55, "#C4A06A");
      grad.addColorStop(1, "#7A6040");
    } else {
      grad.addColorStop(0, "#FFF8F2");
      grad.addColorStop(0.55, "#E8B896");
      grad.addColorStop(1, "#B8431F");
    }
    return grad;
  }

  function drawForceChunkNode(ctx, node, visualState) {
    var r = node.r;
    var x = node.x;
    var y = node.y;
    var src = node.line.source || "seed";
    var alpha = visualState === "dimmed" ? 0.2 : 1;

    if (visualState === "selected") {
      ctx.beginPath();
      ctx.arc(x, y, r + 14, 0, Math.PI * 2);
      var halo = ctx.createRadialGradient(x, y, r * 0.6, x, y, r + 14);
      halo.addColorStop(0, forceSourceColorRgba(src, 0.45));
      halo.addColorStop(1, forceSourceColorRgba(src, 0));
      ctx.fillStyle = halo;
      ctx.fill();
    } else if (visualState === "connected") {
      ctx.beginPath();
      ctx.arc(x, y, r + 9, 0, Math.PI * 2);
      ctx.strokeStyle = forceSourceColorRgba(src, 0.55);
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.shadowColor = "rgba(0,0,0,0.55)";
    ctx.shadowBlur = visualState === "selected" ? 18 : 10;
    ctx.shadowOffsetY = 4;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = chunkNodeFillGradient(ctx, x, y, r, src, { force: true });
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.strokeStyle = visualState === "selected"
      ? "#FFF8F2"
      : (visualState === "connected" ? forceSourceColorRgba(src, 0.85) : forceSourceColorRgba(src, 0.45));
    ctx.lineWidth = visualState === "selected" ? 2.5 : (visualState === "connected" ? 2 : 1);
    ctx.stroke();

    ctx.beginPath();
    ctx.arc(x - r * 0.28, y - r * 0.32, Math.max(2, r * 0.26), 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,255,255,0.38)";
    ctx.fill();
    ctx.restore();
  }

  function drawForceQueryNode(ctx, node, visualState) {
    var r = node.r;
    var x = node.x;
    var y = node.y;
    var src = (node.batch && node.batch.source) || "seed";
    var running = !!(node.batch && node.batch.running);
    var alpha = visualState === "dimmed" ? 0.22 : 1;

    if (running) {
      var pulse = 0.65 + 0.35 * Math.sin(performance.now() * 0.008);
      ctx.beginPath();
      ctx.arc(x, y, r + 8 + pulse * 4, 0, Math.PI * 2);
      ctx.strokeStyle = forceSourceColorRgba(src, 0.35 + pulse * 0.2);
      ctx.lineWidth = 2;
      ctx.setLineDash([5, 4]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    if (visualState === "selected") {
      ctx.beginPath();
      ctx.arc(x, y, r + 10, 0, Math.PI * 2);
      ctx.strokeStyle = "rgba(255,248,242,0.92)";
      ctx.lineWidth = 2;
      ctx.stroke();
    } else if (visualState === "connected") {
      ctx.beginPath();
      ctx.arc(x, y, r + 7, 0, Math.PI * 2);
      ctx.strokeStyle = forceSourceColorRgba(src, 0.55);
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }

    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.shadowColor = "rgba(0,0,0,0.45)";
    ctx.shadowBlur = visualState === "selected" ? 12 : 6;
    ctx.shadowOffsetY = 3;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = forceQueryFillGradient(ctx, x, y, r, src);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.shadowOffsetY = 0;
    ctx.strokeStyle = visualState === "selected" ? "#FFF8F2" : forceSourceColorRgba(src, 0.75);
    ctx.lineWidth = visualState === "selected" ? 2 : 1.25;
    if (running) ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = src === "seed" ? "#2A1810" : "#111111";
    ctx.font = "bold 10px IBM Plex Mono, ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(running ? "…" : (node.batch.shortLabel || "Q?"), x, y + 0.5);
    ctx.restore();
  }

  function buildConnectedNodesSection(nodeId, model) {
    var neighborIds = getGraphNeighborIds(nodeId, model.edges);
    if (!neighborIds.length) return "";
    var items = neighborIds.map(function (id) {
      var meta = graphNodeMeta(id, model);
      var active = graphInteraction.selectedId === id ? " is-active" : "";
      return (
        '<li><button type="button" class="graph-connected-link' + active + '" data-graph-node-id="' +
        escapeHtml(id) + '">' +
        '<span class="link-dot ' + escapeHtml(meta.type) + " " + escapeHtml(meta.source) + '"></span>' +
        '<span class="link-text">' + escapeHtml(meta.label) + '</span>' +
        '<span class="link-meta">' + (meta.type === "query" ? "Query" : "Chunk") + '</span>' +
        '</button></li>'
      );
    }).join("");
    return (
      '<dl class="detail-block"><dt>Connected nodes</dt><dd>' +
      '<ul class="graph-connected-list">' + items + '</ul></dd></dl>'
    );
  }

  function focusGraphNodeById(nodeId) {
    var node = forceGraph.nodeById[nodeId];
    if (!node) return;
    var hit = {
      id: node.id,
      type: node.type,
      line: node.line,
      batch: node.batch,
    };
    showGraphNodeDetail(hit);
  }

  function groupLinesIntoBatches(lines) {
    var batches = [];
    var cur = null;
    lines.forEach(function (ln) {
      var key = (ln.query || "") + "\0" + (ln.source || "seed");
      if (!cur || cur.key !== key) {
        cur = { key: key, query: ln.query || "", source: ln.source || "seed", chunks: [] };
        batches.push(cur);
      }
      cur.chunks.push(ln);
    });
    return batches;
  }

  function buildContextGraphModel(bubble) {
    var lines = Array.isArray(bubble.lines) ? bubble.lines : [];
    var updates = Array.isArray(bubble.updates) ? bubble.updates : [];
    var lineById = {};
    lines.forEach(function (ln) {
      if (ln.chunk_id) lineById[ln.chunk_id] = ln;
    });

    var batches = [];
    var hasChunkIds = updates.some(function (up) {
      return up.chunk_ids && up.chunk_ids.length;
    });

    if (hasChunkIds) {
      var placed = {};
      updates.forEach(function (up, i) {
        var ids = up.chunk_ids || [];
        var chunks = ids.map(function (id) { return lineById[id]; }).filter(Boolean);
        var qid = up.query_id || ("q" + i);
        var qrec = (bubble.queries || []).filter(function (q) { return q.id === qid; })[0];
        if (!chunks.length && !(qrec && qrec.status === "running")) return;
        ids.forEach(function (id) { if (id) placed[id] = true; });
        batches.push({
          id: qid,
          queryId: qid,
          query: up.query || (qrec && qrec.query) || "",
          source: up.source || (qrec && qrec.source) || "swarm",
          chunks: chunks,
          running: !!(qrec && qrec.status === "running"),
          started_at: qrec && qrec.started_at,
          finished_at: qrec && qrec.finished_at,
          duration_ms: up.duration_ms != null ? up.duration_ms : (qrec && qrec.duration_ms),
        });
      });
      groupLinesIntoBatches(lines.filter(function (ln) {
        return !placed[ln.chunk_id];
      })).forEach(function (g, j) {
        batches.push({
          id: "o" + j,
          query: g.query,
          source: g.source,
          chunks: g.chunks,
        });
      });
    } else if (lines.length) {
      groupLinesIntoBatches(lines).forEach(function (g, i) {
        batches.push({
          id: "b" + i,
          query: g.query,
          source: g.source,
          chunks: g.chunks,
        });
      });
    }

    (bubble.queries || []).forEach(function (q) {
      if (q.status !== "running") return;
      var exists = batches.some(function (b) { return b.id === q.id || b.queryId === q.id; });
      if (exists) return;
      batches.push({
        id: q.id,
        queryId: q.id,
        query: q.query || "",
        source: q.source || "swarm",
        chunks: [],
        running: true,
        started_at: q.started_at,
        duration_ms: q.duration_ms,
      });
    });

    var edges = [];
    batches.forEach(function (batch, i) {
      batch.index = i;
      batch.shortLabel = "Q" + (i + 1);
      batch.label = queryNodeLabel(batch.query, batch.source);
      batch.chunks.forEach(function (ln) {
        edges.push({ from: batch.id, to: ln.chunk_id, type: "fetch", source: batch.source });
      });
      if (i > 0) {
        edges.push({
          from: batches[i - 1].id,
          to: batch.id,
          type: "chain",
          source: batch.source,
        });
        var prevChunks = batches[i - 1].chunks;
        if (prevChunks.length) {
          edges.push({
            from: prevChunks[prevChunks.length - 1].chunk_id,
            to: batch.id,
            type: "flow",
            source: batch.source,
          });
        }
      }
    });

    return { batches: batches, edges: edges, lineById: lineById, updates: updates };
  }

  function layoutContextGraph(model, padL, padT, areaW, innerH, opts) {
    opts = opts || {};
    var compact = !!opts.compact;
    var positions = {};
    var batches = model.batches;
    var n = batches.length;
    if (!n) return { positions: positions, contentW: areaW };

    var minCol = compact ? 44 : 108;
    var contentW = compact ? areaW : Math.max(areaW, n * minCol);
    var queryBandH = compact ? Math.min(32, innerH * 0.2) : Math.min(52, innerH * 0.18);
    var chunkTop = padT + queryBandH + (compact ? 8 : 14);
    var chunkH = Math.max(40, innerH - queryBandH - (compact ? 14 : 20));

    batches.forEach(function (batch, col) {
      var cx = padL + (n === 1 ? contentW / 2 : ((col + 0.5) * contentW) / n);
      positions[batch.id] = {
        x: cx,
        y: padT + queryBandH * 0.45,
        type: "query",
        batch: batch,
      };

      var chunks = batch.chunks;
      var m = chunks.length;
      var spreadStep = compact ? 18 : 28;
      chunks.forEach(function (ln, j) {
        var score = typeof ln.score === "number" ? ln.score : 0.5;
        var spread = m > 1 ? (j - (m - 1) / 2) * spreadStep : 0;
        var y = m === 1
          ? chunkTop + chunkH * (1 - Math.max(0.08, Math.min(0.92, score)))
          : chunkTop + (chunkH * (j + 0.5)) / m;
        positions[ln.chunk_id] = {
          x: cx + spread,
          y: y,
          type: "chunk",
          line: ln,
          batchId: batch.id,
          batchIndex: col,
          isNewestBatch: col === n - 1,
        };
      });
    });

    return { positions: positions, contentW: contentW };
  }

  function drawGraphEdge(ctx, from, to, edge, opts) {
    opts = opts || {};
    ctx.beginPath();
    ctx.moveTo(from.x, from.y);
    if (edge.type === "chain") {
      var midX = (from.x + to.x) / 2;
      ctx.bezierCurveTo(midX, from.y, midX, to.y, to.x, to.y);
      ctx.strokeStyle = opts.expanded ? "rgba(244,241,234,.55)" : "rgba(17,17,17,.35)";
      ctx.lineWidth = opts.expanded ? 2 : 1.5;
      ctx.setLineDash([5, 4]);
    } else if (edge.type === "flow") {
      ctx.bezierCurveTo(from.x, from.y + 24, to.x - 24, to.y, to.x, to.y - 10);
      ctx.strokeStyle = "rgba(92,87,79,.35)";
      ctx.lineWidth = 1.25;
      ctx.setLineDash([3, 5]);
    } else {
      var midY = from.y + (to.y - from.y) * 0.45;
      ctx.bezierCurveTo(from.x, midY, to.x, midY, to.x, to.y);
      ctx.strokeStyle = sourceColorRgba(edge.source, opts.expanded ? 0.65 : 0.55);
      ctx.lineWidth = opts.expanded ? 2 : 1.5;
      ctx.setLineDash([]);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function fillRoundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(x, y, w, h, r);
      return;
    }
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function drawQueryNode(ctx, pos, opts, hitAreas) {
    opts = opts || {};
    var compact = !!opts.compact;
    var batch = pos.batch;
    var running = !!(batch && batch.running);
    var label = running ? "…" : (batch.shortLabel || "Q?");
    var tw = compact ? 30 : 36;
    var th = compact ? 18 : 20;
    var x = pos.x - tw / 2;
    var y = pos.y - th / 2;
    var selected = graphInteraction.selectedId === pos.batch.id;
    var src = batch.source || "seed";

    if (running) {
      var pulse = 0.7 + 0.3 * Math.sin(performance.now() * 0.009);
      fillRoundRect(ctx, x - 3, y - 3, tw + 6, th + 6, 2);
      ctx.fillStyle = "rgba(184,67,31," + (0.08 * pulse) + ")";
      ctx.fill();
    }

    if (selected) {
      fillRoundRect(ctx, x - 2, y - 2, tw + 4, th + 4, 2);
      ctx.fillStyle = "rgba(17,17,17,.08)";
      ctx.fill();
    }

    ctx.fillStyle = running ? "#F3E4DC" : sourceColor(src);
    ctx.strokeStyle = selected ? "#111111" : (running ? "#B8431F" : "#5C574F");
    ctx.lineWidth = selected ? 2 : 1;
    if (running) ctx.setLineDash([3, 2]);
    fillRoundRect(ctx, x, y, tw, th, 2);
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = running ? "#B8431F" : "#FFFFFF";
    ctx.font = (compact ? "9px" : "10px") + " IBM Plex Mono, ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, pos.x, pos.y);

    if (hitAreas) {
      hitAreas.push({
        id: pos.batch.id,
        kind: "rect",
        type: "query",
        x: x,
        y: y,
        w: tw,
        h: th,
        batch: pos.batch,
      });
    }
  }

  function resizeGraphCanvas(canvas, viewportW, viewportH, contentW) {
    if (!canvas) return null;
    var w = contentW || Math.max(280, Math.floor(viewportW));
    var h = Math.max(140, Math.floor(viewportH));
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    var ctx = canvas.getContext("2d");
    if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  function drawContextGraphOnCanvas(canvas, bubble, pulseT, opts) {
    opts = opts || {};
    var compact = !!opts.compact;
    var hitStore = opts.hitStore || graphInteraction.compact;
    hitStore.areas = [];

    var parent = canvas.parentElement;
    var viewportW = parent ? parent.clientWidth : 320;
    var viewportH = parent ? parent.clientHeight : 180;
    if (compact && canvas) {
      var crect = canvas.getBoundingClientRect();
      viewportW = crect.width;
      viewportH = Math.max(140, crect.height);
    } else if (parent) {
      viewportW = parent.clientWidth || viewportW;
      viewportH = Math.max(420, parent.clientHeight || 500);
    }

    var padL = 44;
    var padR = 16;
    var padT = 18;
    var padB = 28;
    var innerH = viewportH - padT - padB;
    var areaW = viewportW - padL - padR;

    var lines = Array.isArray(bubble.lines) ? bubble.lines : [];
    var count = bubble.chunk_count || lines.length;
    var model = lines.length ? buildContextGraphModel(bubble) : null;
    var layout = model
      ? layoutContextGraph(model, padL, padT, areaW, innerH, { compact: compact })
      : { positions: {}, contentW: areaW };
    var canvasW = compact ? viewportW : Math.max(viewportW, padL + padR + layout.contentW);
    var sized = resizeGraphCanvas(canvas, viewportW, viewportH, canvasW);
    if (!sized || !sized.ctx) return;
    var ctx = sized.ctx;
    var w = sized.w;
    var h = sized.h;

    ctx.clearRect(0, 0, w, h);

    var fillH = innerH * Math.min(1, count / CONTEXT_POOL_MAX);
    ctx.fillStyle = "rgba(216,210,198,.7)";
    ctx.fillRect(12, padT, 10, innerH);
    ctx.fillStyle = bubble.status === "gathering" || bubble.status === "refreshing"
      ? "#B8431F" : "#2D5A3D";
    ctx.fillRect(12, padT + innerH - fillH, 10, fillH);
    ctx.fillStyle = "#5C574F";
    ctx.font = "10px IBM Plex Mono, ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.fillText(String(count), 17, padT + innerH + 14);

    ctx.strokeStyle = "rgba(216,210,198,.9)";
    ctx.lineWidth = 1;
    for (var g = 0; g <= 4; g++) {
      var gy = padT + (innerH * g) / 4;
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(w - padR, gy);
      ctx.stroke();
    }

    if (!model || !lines.length) {
      ctx.fillStyle = "#5C574F";
      ctx.font = "12px Archivo, Helvetica Neue, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Context pool empty — chunks appear after retrieval", padL + layout.contentW / 2, padT + innerH / 2);
      return;
    }

    var positions = layout.positions;
    var drawOpts = { compact: compact, expanded: !compact };

    model.edges.forEach(function (edge) {
      var from = positions[edge.from];
      var to = positions[edge.to];
      if (!from || !to) return;
      var start = { x: from.x, y: from.y + (from.type === "query" ? 10 : 0) };
      var end = { x: to.x, y: to.y - (to.type === "chunk" ? 8 : 10) };
      drawGraphEdge(ctx, start, end, edge, drawOpts);
    });

    model.batches.forEach(function (batch) {
      var pos = positions[batch.id];
      if (pos) drawQueryNode(ctx, pos, drawOpts, hitStore.areas);
    });

    lines.forEach(function (ln) {
      var pos = positions[ln.chunk_id];
      if (!pos) return;
      var x = pos.x;
      var y = pos.y;
      var score = typeof ln.score === "number" ? ln.score : 0.5;
      var r = (compact ? 4 : 5) + score * (compact ? 6 : 8);
      var pulseActive = pos.isNewestBatch && pulseT > 0;
      var pulse = pulseActive ? 1 + pulseT * 0.35 : 1;
      var selected = graphInteraction.selectedId === ln.chunk_id;

      if (pulseActive) {
        ctx.beginPath();
        ctx.arc(x, y, r * pulse * 1.8, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(184,67,31," + (0.12 * pulseT) + ")";
        ctx.fill();
      }

      if (selected) {
        ctx.beginPath();
        ctx.arc(x, y, r * pulse + 4, 0, Math.PI * 2);
        ctx.strokeStyle = "#111111";
        ctx.lineWidth = 2;
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.arc(x, y, r * pulse, 0, Math.PI * 2);
      ctx.fillStyle = sourceColor(ln.source);
      ctx.fill();
      ctx.strokeStyle = "rgba(17,17,17,.2)";
      ctx.lineWidth = 1;
      ctx.stroke();

      var showLabel = !compact || lines.length <= 10 || pos.batchIndex === 0 || pos.isNewestBatch;
      if (showLabel) {
        ctx.fillStyle = "#111111";
        ctx.font = (compact ? "8px" : "10px") + " IBM Plex Mono, ui-monospace, monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "alphabetic";
        ctx.fillText(truncateGraphLabel(ln.sop_id || "SOP", compact ? 8 : 12), x, y - r * pulse - 5);
      }

      hitStore.areas.push({
        id: ln.chunk_id,
        kind: "circle",
        type: "chunk",
        x: x,
        y: y,
        r: r * pulse + 4,
        line: ln,
        batch: model.batches[pos.batchIndex],
      });
    });
  }

  function forceNodeRadius(node) {
    if (node.type === "query") return 22;
    var score = node.line && typeof node.line.score === "number" ? node.line.score : 0.5;
    return 6 + score * 10;
  }

  function buildForceGraphNodes(model, width, height) {
    var nodes = [];
    var nodeById = {};
    var cx = width / 2;
    var cy = height / 2;
    var ring = Math.min(width, height) * 0.28;

    model.batches.forEach(function (batch, i) {
      var angle = (i / Math.max(1, model.batches.length)) * Math.PI * 2 - Math.PI / 2;
      var node = {
        id: batch.id,
        type: "query",
        batch: batch,
        x: cx + Math.cos(angle) * ring * 0.35,
        y: cy + Math.sin(angle) * ring * 0.35,
        vx: 0,
        vy: 0,
        fx: 0,
        fy: 0,
      };
      node.r = forceNodeRadius(node);
      nodes.push(node);
      nodeById[node.id] = node;
    });

    var lineNodes = [];
    model.batches.forEach(function (batch) {
      batch.chunks.forEach(function (ln, j) {
        if (nodeById[ln.chunk_id]) return;
        var parent = nodeById[batch.id];
        var angle = (j / Math.max(1, batch.chunks.length)) * Math.PI * 2;
        var dist = 70 + j * 12;
        var node = {
          id: ln.chunk_id,
          type: "chunk",
          line: ln,
          batch: batch,
          x: (parent ? parent.x : cx) + Math.cos(angle) * dist,
          y: (parent ? parent.y : cy) + Math.sin(angle) * dist,
          vx: 0,
          vy: 0,
          fx: 0,
          fy: 0,
        };
        node.r = forceNodeRadius(node);
        lineNodes.push(node);
        nodeById[node.id] = node;
      });
    });
    nodes = nodes.concat(lineNodes);
    return { nodes: nodes, nodeById: nodeById };
  }

  function syncForceGraph(bubble) {
    var lines = Array.isArray(bubble.lines) ? bubble.lines : [];
    var model = lines.length ? buildContextGraphModel(bubble) : null;
    forceGraph.bubble = bubble;
    forceGraph.model = model;

    var parent = contextGraphFsCanvas ? contextGraphFsCanvas.parentElement : null;
    var w = parent ? parent.clientWidth : 800;
    var h = parent ? Math.max(420, parent.clientHeight) : 600;
    forceGraph.width = Math.max(320, w);
    forceGraph.height = Math.max(420, h);

    if (!model || !lines.length) {
      forceGraph.nodes = [];
      forceGraph.edges = [];
      forceGraph.nodeById = {};
      return;
    }

    var built = buildForceGraphNodes(model, forceGraph.width, forceGraph.height);
    var prevById = forceGraph.nodeById || {};
    built.nodes.forEach(function (node) {
      var prev = prevById[node.id];
      if (prev) {
        node.x = prev.x;
        node.y = prev.y;
        node.vx = prev.vx * 0.5;
        node.vy = prev.vy * 0.5;
      }
      node.r = forceNodeRadius(node);
    });

    forceGraph.nodes = built.nodes;
    forceGraph.nodeById = built.nodeById;
    forceGraph.edges = model.edges.slice();
    forceGraph.alpha = Math.max(forceGraph.alpha, 0.45);
  }

  function resizeForceCanvas() {
    if (!contextGraphFsCanvas) return null;
    var w = forceGraph.width;
    var h = forceGraph.height;
    var dpr = window.devicePixelRatio || 1;
    contextGraphFsCanvas.width = Math.floor(w * dpr);
    contextGraphFsCanvas.height = Math.floor(h * dpr);
    contextGraphFsCanvas.style.width = w + "px";
    contextGraphFsCanvas.style.height = h + "px";
    var ctx = contextGraphFsCanvas.getContext("2d");
    if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return ctx;
  }

  function buildForceGraphStars(w, h) {
    var key = w + "x" + h;
    if (forceGraph.starsKey === key && forceGraph.stars.length) return;
    forceGraph.starsKey = key;
    var count = Math.floor((w * h) / 4000);
    count = Math.max(140, Math.min(count, 340));
    var stars = [];
    var i;
    for (i = 0; i < count; i++) {
      stars.push({
        x: Math.random() * w,
        y: Math.random() * h,
        r: Math.random() < 0.07 ? 1.35 : (Math.random() < 0.22 ? 0.95 : 0.5),
        a: 0.22 + Math.random() * 0.72,
        tw: Math.random() * Math.PI * 2,
        tint: Math.random() < 0.12 ? "warm" : (Math.random() < 0.18 ? "cool" : "white"),
        glow: false,
      });
    }
    for (i = 0; i < 10; i++) {
      stars.push({
        x: Math.random() * w,
        y: Math.random() * h,
        r: 1.8 + Math.random() * 1.1,
        a: 0.82 + Math.random() * 0.18,
        tw: Math.random() * Math.PI * 2,
        tint: "white",
        glow: true,
      });
    }
    forceGraph.stars = stars;
  }

  function drawForceGraphStarfield(ctx, w, h) {
    buildForceGraphStars(w, h);
    var t = performance.now() * 0.001;

    var bg = ctx.createRadialGradient(w * 0.5, h * 0.42, 0, w * 0.5, h * 0.5, Math.max(w, h) * 0.92);
    bg.addColorStop(0, "#1A1814");
    bg.addColorStop(0.32, "#121110");
    bg.addColorStop(0.62, "#0C0B0A");
    bg.addColorStop(1, "#0F0E0C");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);

    var neb1 = ctx.createRadialGradient(w * 0.18, h * 0.72, 0, w * 0.18, h * 0.72, w * 0.38);
    neb1.addColorStop(0, "rgba(184,67,31,0.07)");
    neb1.addColorStop(0.55, "rgba(184,67,31,0.02)");
    neb1.addColorStop(1, "rgba(184,67,31,0)");
    ctx.fillStyle = neb1;
    ctx.fillRect(0, 0, w, h);

    var neb2 = ctx.createRadialGradient(w * 0.82, h * 0.22, 0, w * 0.82, h * 0.22, w * 0.32);
    neb2.addColorStop(0, "rgba(154,123,79,0.06)");
    neb2.addColorStop(0.6, "rgba(154,123,79,0.02)");
    neb2.addColorStop(1, "rgba(154,123,79,0)");
    ctx.fillStyle = neb2;
    ctx.fillRect(0, 0, w, h);

    var neb3 = ctx.createRadialGradient(w * 0.55, h * 0.12, 0, w * 0.55, h * 0.12, w * 0.22);
    neb3.addColorStop(0, "rgba(244,241,234,0.04)");
    neb3.addColorStop(1, "rgba(244,241,234,0)");
    ctx.fillStyle = neb3;
    ctx.fillRect(0, 0, w, h);

    forceGraph.stars.forEach(function (s) {
      var alpha = s.a * (0.7 + 0.3 * Math.sin(t * 1.6 + s.tw));
      var color;
      if (s.tint === "warm") color = "rgba(255,228,196," + alpha + ")";
      else if (s.tint === "cool") color = "rgba(216,210,198," + alpha + ")";
      else color = "rgba(244,241,234," + alpha + ")";

      if (s.glow) {
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r * 3.2, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(244,241,234," + (alpha * 0.1) + ")";
        ctx.fill();
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r * 1.6, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(255,255,255," + (alpha * 0.18) + ")";
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    });
  }

  function forceTick() {
    var nodes = forceGraph.nodes;
    var edges = forceGraph.edges;
    var nodeById = forceGraph.nodeById;
    var w = forceGraph.width;
    var h = forceGraph.height;
    var alpha = forceGraph.alpha;
    if (!nodes.length || alpha < 0.002) {
      forceGraph.alpha = 0;
      return;
    }

    nodes.forEach(function (n) {
      n.fx = 0;
      n.fy = 0;
    });

    var cx = w / 2;
    var cy = h / 2;
    nodes.forEach(function (n) {
      n.fx += (cx - n.x) * 0.012 * alpha;
      n.fy += (cy - n.y) * 0.012 * alpha;
    });

    var chargeK = 420 * alpha;
    for (var i = 0; i < nodes.length; i++) {
      for (var j = i + 1; j < nodes.length; j++) {
        var a = nodes[i];
        var b = nodes[j];
        var dx = b.x - a.x;
        var dy = b.y - a.y;
        var distSq = dx * dx + dy * dy + 0.01;
        var dist = Math.sqrt(distSq);
        var minDist = a.r + b.r + 14;
        var repulse = chargeK / distSq;
        if (dist < minDist) repulse += (minDist - dist) * 0.8;
        var fx = (dx / dist) * repulse;
        var fy = (dy / dist) * repulse;
        a.fx -= fx;
        a.fy -= fy;
        b.fx += fx;
        b.fy += fy;
      }
    }

    edges.forEach(function (edge) {
      var from = nodeById[edge.from];
      var to = nodeById[edge.to];
      if (!from || !to) return;
      var dx = to.x - from.x;
      var dy = to.y - from.y;
      var dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      var target = edge.type === "chain" ? 140 : (edge.type === "flow" ? 110 : 72);
      var strength = edge.type === "fetch" ? 0.08 : 0.05;
      var force = (dist - target) * strength * alpha;
      var fx = (dx / dist) * force;
      var fy = (dy / dist) * force;
      from.fx += fx;
      from.fy += fy;
      to.fx -= fx;
      to.fy -= fy;
    });

    if (forceGraph.mouse.active) {
      var mx = forceGraph.mouse.x;
      var my = forceGraph.mouse.y;
      var mouseR = 95;
      nodes.forEach(function (n) {
        var dx = n.x - mx;
        var dy = n.y - my;
        var dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
        if (dist < mouseR + n.r) {
          var push = Math.pow((mouseR + n.r - dist) / mouseR, 1.4) * 2.8;
          n.fx += (dx / dist) * push;
          n.fy += (dy / dist) * push;
        }
      });
    }

    var pad = 36;
    nodes.forEach(function (n) {
      n.vx = (n.vx + n.fx) * 0.82;
      n.vy = (n.vy + n.fy) * 0.82;
      n.x += n.vx * alpha;
      n.y += n.vy * alpha;
      if (n.x < pad + n.r) { n.x = pad + n.r; n.vx *= -0.3; }
      if (n.x > w - pad - n.r) { n.x = w - pad - n.r; n.vx *= -0.3; }
      if (n.y < pad + n.r) { n.y = pad + n.r; n.vy *= -0.3; }
      if (n.y > h - pad - n.r) { n.y = h - pad - n.r; n.vy *= -0.3; }
    });

    forceGraph.alpha += (0.015 - forceGraph.alpha) * 0.02;
    if (forceGraph.mouse.active) forceGraph.alpha = Math.max(forceGraph.alpha, 0.35);
  }

  function drawForceGraphLabel(ctx, text, x, y, opts) {
    opts = opts || {};
    var fontSize = opts.fontSize || 10;
    ctx.font = fontSize + "px IBM Plex Mono, ui-monospace, monospace";
    var tw = ctx.measureText(text).width;
    var padX = 6;
    var padY = 3;
    var bw = tw + padX * 2;
    var bh = fontSize + padY * 2;
    var bx = x - bw / 2;
    var by = y - bh / 2;
    fillRoundRect(ctx, bx, by, bw, bh, 2);
    ctx.fillStyle = "rgba(244,241,234,.94)";
    ctx.fill();
    ctx.fillStyle = "#111111";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, x, y);
    return { x: bx, y: by, w: bw, h: bh };
  }

  function renderForceGraph() {
    if (!contextGraphFsCanvas) return;
    var ctx = resizeForceCanvas();
    if (!ctx) return;
    var w = forceGraph.width;
    var h = forceGraph.height;
    var bubble = forceGraph.bubble || empty_bubble();
    var model = forceGraph.model;
    var nodes = forceGraph.nodes;
    var nodeById = forceGraph.nodeById;
    var cam = forceGraph.camera;
    var selectedId = graphInteraction.selectedId;
    var neighborSet = null;
    if (selectedId && graphInteraction.fsOpen) {
      neighborSet = {};
      getGraphNeighborIds(selectedId, forceGraph.edges).forEach(function (id) {
        neighborSet[id] = true;
      });
    }
    var hasFocus = graphInteraction.fsOpen && !!selectedId;

    ctx.clearRect(0, 0, w, h);
    drawForceGraphStarfield(ctx, w, h);

    var count = bubble.chunk_count || (bubble.lines ? bubble.lines.length : 0);
    if (!model || !nodes.length) {
      ctx.fillStyle = "rgba(216,210,198,.75)";
      ctx.font = "13px Archivo, Helvetica Neue, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("Context pool empty — chunks appear after retrieval", w / 2, h / 2);
      return;
    }

    forceGraph.hitAreas = [];

    ctx.save();
    ctx.translate(w / 2, h / 2);
    ctx.scale(cam.scale, cam.scale);
    ctx.translate(-cam.x, -cam.y);

    forceGraph.edges.forEach(function (edge) {
      var from = nodeById[edge.from];
      var to = nodeById[edge.to];
      if (!from || !to) return;
      var edgeActive = hasFocus && (
        edge.from === selectedId || edge.to === selectedId ||
        (neighborSet && (neighborSet[edge.from] || neighborSet[edge.to]))
      );
      ctx.beginPath();
      ctx.moveTo(from.x, from.y);
      ctx.lineTo(to.x, to.y);
      if (edge.type === "chain") {
        ctx.strokeStyle = edgeActive ? "rgba(244,241,234,.85)" : (hasFocus ? "rgba(244,241,234,.15)" : "rgba(244,241,234,.45)");
        ctx.lineWidth = edgeActive ? 2.5 : 1.5;
        ctx.setLineDash([4, 4]);
      } else if (edge.type === "flow") {
        ctx.strokeStyle = edgeActive ? "rgba(154,123,79,.55)" : (hasFocus ? "rgba(154,123,79,.1)" : "rgba(154,123,79,.28)");
        ctx.lineWidth = edgeActive ? 2 : 1;
        ctx.setLineDash([3, 5]);
      } else {
        ctx.strokeStyle = edgeActive
          ? forceSourceColorRgba(edge.source, 0.75)
          : (hasFocus ? forceSourceColorRgba(edge.source, 0.12) : forceSourceColorRgba(edge.source, 0.42));
        ctx.lineWidth = edgeActive ? 2 : 1;
        ctx.setLineDash([]);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    });

    nodes.forEach(function (node) {
      if (node.type !== "chunk") return;
      var visualState = forceNodeVisualState(node.id, selectedId, neighborSet, graphInteraction.fsOpen);
      drawForceChunkNode(ctx, node, visualState);

      var lbl = truncateGraphLabel(node.line.sop_id || "SOP", 14);
      var labelY = node.y + node.r + 14;
      var pillAlpha = visualState === "dimmed" ? 0.35 : 1;
      ctx.save();
      ctx.globalAlpha = pillAlpha;
      var pill = drawForceGraphLabel(ctx, lbl, node.x, labelY, { fontSize: 9 });
      ctx.restore();
      forceGraph.hitAreas.push({
        id: node.id,
        type: "chunk",
        kind: "circle",
        x: node.x,
        y: node.y,
        r: node.r + 6,
        line: node.line,
        batch: node.batch,
        labelRect: pill,
      });
    });

    nodes.forEach(function (node) {
      if (node.type !== "query") return;
      var visualState = forceNodeVisualState(node.id, selectedId, neighborSet, graphInteraction.fsOpen);
      drawForceQueryNode(ctx, node, visualState);

      var pillAlpha = visualState === "dimmed" ? 0.35 : 1;
      ctx.save();
      ctx.globalAlpha = pillAlpha;
      var pill = drawForceGraphLabel(ctx, truncateGraphLabel(node.batch.label, 18), node.x, node.y + node.r + 16, { fontSize: 9 });
      ctx.restore();
      forceGraph.hitAreas.push({
        id: node.id,
        type: "query",
        kind: "circle",
        x: node.x,
        y: node.y,
        r: node.r + 8,
        batch: node.batch,
        labelRect: pill,
      });
    });

    ctx.restore();
  }

  function forceGraphLoop() {
    if (!graphInteraction.fsOpen) {
      stopForceGraphLoop();
      return;
    }
    updateForceGraphCamera();
    forceTick();
    renderForceGraph();
    forceGraph.rafId = requestAnimationFrame(forceGraphLoop);
  }

  function startForceGraphLoop() {
    if (forceGraph.rafId) return;
    forceGraph.alpha = Math.max(forceGraph.alpha, 0.6);
    forceGraph.rafId = requestAnimationFrame(forceGraphLoop);
  }

  function stopForceGraphLoop() {
    if (forceGraph.rafId) {
      cancelAnimationFrame(forceGraph.rafId);
      forceGraph.rafId = null;
    }
  }

  function findForceGraphHit(x, y) {
    var world = screenToForceWorld(x, y);
    var wx = world.x;
    var wy = world.y;
    var areas = forceGraph.hitAreas || [];
    for (var i = areas.length - 1; i >= 0; i--) {
      var a = areas[i];
      if (a.labelRect) {
        var lr = a.labelRect;
        if (wx >= lr.x && wx <= lr.x + lr.w && wy >= lr.y && wy <= lr.y + lr.h) return a;
      }
      if (a.kind === "circle") {
        var dx = wx - a.x;
        var dy = wy - a.y;
        if (dx * dx + dy * dy <= a.r * a.r) return a;
      }
    }
    return null;
  }

  function drawContextPoolGraph(bubble, pulseT) {
    drawContextGraphOnCanvas(contextGraphCanvas, bubble, pulseT, {
      compact: true,
      hitStore: graphInteraction.compact,
    });
    if (graphInteraction.fsOpen && contextGraphFsCanvas) {
      syncForceGraph(bubble);
      if (!forceGraph.rafId) renderForceGraph();
    }
  }

  function findGraphHit(x, y, areas) {
    for (var i = areas.length - 1; i >= 0; i--) {
      var a = areas[i];
      if (a.kind === "circle") {
        var dx = x - a.x;
        var dy = y - a.y;
        if (dx * dx + dy * dy <= a.r * a.r) return a;
      } else if (a.kind === "rect") {
        if (x >= a.x && x <= a.x + a.w && y >= a.y && y <= a.y + a.h) return a;
      }
    }
    return null;
  }

  function canvasCoords(canvas, event) {
    var rect = canvas.getBoundingClientRect();
    return {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    };
  }

  function findUpdateForBatch(model, batch) {
    if (!model || !model.updates || batch.index == null) return null;
    var idx = batch.index;
    if (model.updates[idx]) return model.updates[idx];
    return model.updates.filter(function (up) {
      return up.query === batch.query && up.source === batch.source;
    }).pop() || null;
  }

  function buildGraphDetailHtml(hit, model) {
    var body = "";
    if (hit.type === "query") {
      var batch = hit.batch;
      var upd = findUpdateForBatch(model, batch);
      var q = batch.query || batch.label || "(no query text)";
      var chunks = (batch.chunks || []).map(function (ln) {
        return ln.sop_id + " · " + truncateGraphLabel(ln.text || "", 40);
      }).join("<br>");
      var timing = formatQueryTiming(batch);
      var timingRow = timing
        ? ('<dt>Query time</dt><dd>' + escapeHtml(timing) +
          (batch.started_at ? ' · started ' + escapeHtml(batch.started_at) : '') + '</dd>')
        : (batch.started_at ? ('<dt>Started</dt><dd>' + escapeHtml(batch.started_at) + '</dd>') : '');
      body = (
        '<div class="detail-title">LLM query ' + escapeHtml(batch.shortLabel || "") +
        (batch.running ? ' · running' : '') + '</div>' +
        '<dl class="detail-block">' +
        '<dt>Search query</dt><dd>' + escapeHtml(q) + '</dd>' +
        '<dt>Source</dt><dd>' + escapeHtml(sourceLabel(batch.source)) + '</dd>' +
        '<dt>Chunks retrieved</dt><dd>' + batch.chunks.length + '</dd>' +
        timingRow +
        (upd && upd.summary ? '<dt>Last update</dt><dd>' + escapeHtml(upd.summary) + '</dd>' : '') +
        '</dl>' +
        (chunks ? '<dl class="detail-block"><dt>Retrieved SOPs</dt><dd>' + chunks + '</dd></dl>' : '')
      );
    } else {
      var ln = hit.line;
      var batch2 = hit.batch;
      body = (
        '<div class="detail-title">Chunk · ' + escapeHtml(ln.sop_id || "SOP") + '</div>' +
        '<dl class="detail-block">' +
        '<dt>Procedure</dt><dd>' + escapeHtml(ln.text || "") + '</dd>' +
        '<dt>Moss score</dt><dd>' + (ln.score != null ? ln.score : "—") + '</dd>' +
        '<dt>Chunk ID</dt><dd class="mono">' + escapeHtml(ln.chunk_id || "") + '</dd>' +
        '<dt>Retrieval source</dt><dd>' + escapeHtml(sourceLabel(ln.source)) + '</dd>' +
        (batch2 ? '<dt>From query</dt><dd>' + escapeHtml(batch2.query || batch2.label || batch2.shortLabel) + '</dd>' : '') +
        (ln.query ? '<dt>Query text</dt><dd class="mono">' + escapeHtml(ln.query) + '</dd>' : '') +
        '</dl>'
      );
    }
    if (graphInteraction.fsOpen) {
      body += buildConnectedNodesSection(hit.id, model);
    }
    return body;
  }

  function buildGraphDetailInlineHtml(hit, model) {
    if (hit.type === "query") {
      var batch = hit.batch;
      var timing = formatQueryTiming(batch);
      return (
        '<div class="detail-title">' + escapeHtml(batch.shortLabel) + ' · LLM query</div>' +
        '<div class="detail-row"><b>Query:</b> ' + escapeHtml(truncateGraphLabel(batch.query || batch.label, 80)) + '</div>' +
        '<div class="detail-row"><b>Source:</b> ' + escapeHtml(sourceLabel(batch.source)) +
        ' · <b>Chunks:</b> ' + batch.chunks.length +
        (timing ? ' · <b>Time:</b> ' + escapeHtml(timing) : '') + '</div>' +
        '<div class="detail-query">Click <b>Expand graph</b> for the full connected view.</div>'
      );
    }
    var ln = hit.line;
    return (
      '<div class="detail-title">' + escapeHtml(ln.sop_id || "SOP") + ' · score ' + (ln.score != null ? ln.score : "—") + '</div>' +
      '<div class="detail-row">' + escapeHtml(truncateGraphLabel(ln.text || "", 100)) + '</div>' +
      '<div class="detail-row"><b>ID:</b> ' + escapeHtml(ln.chunk_id || "") + ' · <b>Source:</b> ' + escapeHtml(sourceLabel(ln.source)) + '</div>'
    );
  }

  function showGraphNodeDetail(hit) {
    if (!hit) return;
    graphInteraction.selectedId = hit.id;
    var bubble = getGraphBubble();
    var model = buildContextGraphModel(bubble);

    if (contextGraphDetailBody) {
      contextGraphDetailBody.innerHTML = buildGraphDetailHtml(hit, model);
    }
    if (contextGraphDetailInline) {
      contextGraphDetailInline.hidden = false;
      contextGraphDetailInline.innerHTML = buildGraphDetailInlineHtml(hit, model);
    }

    drawContextGraphOnCanvas(contextGraphCanvas, bubble, 0, {
      compact: true,
      hitStore: graphInteraction.compact,
    });
    if (graphInteraction.fsOpen) {
      focusForceGraphOnNode(hit.id);
      forceGraph.alpha = Math.max(forceGraph.alpha, 0.45);
    }
  }

  function clearGraphNodeDetail() {
    graphInteraction.selectedId = null;
    if (contextGraphDetailInline) {
      contextGraphDetailInline.hidden = true;
      contextGraphDetailInline.innerHTML = "";
    }
    if (contextGraphDetailBody) {
      contextGraphDetailBody.innerHTML =
        '<p class="detail-empty">Click a query or chunk node to inspect retrieval context, scores, and source.</p>';
    }
    var bubble = getGraphBubble();
    drawContextGraphOnCanvas(contextGraphCanvas, bubble, 0, {
      compact: true,
      hitStore: graphInteraction.compact,
    });
    if (graphInteraction.fsOpen) {
      fitForceGraphCamera();
      forceGraph.alpha = Math.max(forceGraph.alpha, 0.35);
    }
    updateGraphResetButton();
  }

  function handleGraphCanvasClick(canvas, hitStore, event) {
    if (!canvas) return;
    var pt = canvasCoords(canvas, event);
    var hit = findGraphHit(pt.x, pt.y, hitStore.areas || []);
    if (hit) showGraphNodeDetail(hit);
  }

  function openContextGraphFullscreen() {
    if (!contextGraphFs) return;
    graphInteraction.fsOpen = true;
    contextGraphFs.hidden = false;
    document.body.style.overflow = "hidden";
    if (contextGraphFsStatus && contextGraphStatus) {
      contextGraphFsStatus.textContent = contextGraphStatus.textContent;
    }
    requestAnimationFrame(function () {
      syncForceGraph(getGraphBubble());
      fitForceGraphCamera();
      forceGraph.camera.x = forceGraph.camera.targetX;
      forceGraph.camera.y = forceGraph.camera.targetY;
      forceGraph.camera.scale = forceGraph.camera.targetScale;
      if (graphInteraction.selectedId) {
        focusForceGraphOnNode(graphInteraction.selectedId);
      }
      renderForceGraph();
      startForceGraphLoop();
    });
  }

  function closeContextGraphFullscreen() {
    if (!contextGraphFs) return;
    graphInteraction.fsOpen = false;
    contextGraphFs.hidden = true;
    document.body.style.overflow = "";
    stopForceGraphLoop();
    forceGraph.mouse.active = false;
    updateGraphResetButton();
  }

  function handleForceGraphPointer(canvas, event) {
    var pt = canvasCoords(canvas, event);
    var world = screenToForceWorld(pt.x, pt.y);
    forceGraph.mouse.x = world.x;
    forceGraph.mouse.y = world.y;
    forceGraph.mouse.active = true;
    forceGraph.alpha = Math.max(forceGraph.alpha, 0.4);
  }

  function wireContextGraphUI() {
    if (contextGraphExpand) {
      contextGraphExpand.addEventListener("click", openContextGraphFullscreen);
    }
    if (contextGraphFsClose) {
      contextGraphFsClose.addEventListener("click", closeContextGraphFullscreen);
    }
    if (contextGraphFsReset) {
      contextGraphFsReset.addEventListener("click", function () {
        clearGraphNodeDetail();
      });
    }
    if (contextGraphDetailBody) {
      contextGraphDetailBody.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-graph-node-id]");
        if (!btn) return;
        focusGraphNodeById(btn.getAttribute("data-graph-node-id"));
      });
    }
    if (contextGraphCanvas) {
      contextGraphCanvas.addEventListener("click", function (e) {
        handleGraphCanvasClick(contextGraphCanvas, graphInteraction.compact, e);
      });
    }
    if (contextGraphFsCanvas) {
      contextGraphFsCanvas.addEventListener("mousemove", function (e) {
        handleForceGraphPointer(contextGraphFsCanvas, e);
      });
      contextGraphFsCanvas.addEventListener("mouseleave", function () {
        forceGraph.mouse.active = false;
        contextGraphFsCanvas.classList.remove("is-dragging");
      });
      contextGraphFsCanvas.addEventListener("mousedown", function (e) {
        forceGraph.fsClickStart = {
          x: e.clientX,
          y: e.clientY,
          t: performance.now(),
        };
        contextGraphFsCanvas.classList.add("is-dragging");
      });
      contextGraphFsCanvas.addEventListener("mouseup", function (e) {
        contextGraphFsCanvas.classList.remove("is-dragging");
        if (!forceGraph.fsClickStart) return;
        var dx = e.clientX - forceGraph.fsClickStart.x;
        var dy = e.clientY - forceGraph.fsClickStart.y;
        var moved = dx * dx + dy * dy;
        forceGraph.fsClickStart = null;
        if (moved > 64) return;
        var pt = canvasCoords(contextGraphFsCanvas, e);
        var hit = findForceGraphHit(pt.x, pt.y);
        if (hit) showGraphNodeDetail(hit);
      });
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && graphInteraction.fsOpen) closeContextGraphFullscreen();
    });
    window.addEventListener("resize", function () {
      if (graphInteraction.fsOpen) {
        syncForceGraph(getGraphBubble());
        if (forceGraph.viewFocused && graphInteraction.selectedId) {
          focusForceGraphOnNode(graphInteraction.selectedId);
        } else {
          fitForceGraphCamera();
        }
        forceGraph.alpha = Math.max(forceGraph.alpha, 0.5);
      }
    });
  }

  function scheduleGraphPulse() {
    graphPulseUntil = performance.now() + 900;
    if (graphAnimFrame) return;
    function tick() {
      var t = Math.max(0, (graphPulseUntil - performance.now()) / 900);
      var bubble = getGraphBubble();
      drawContextGraphOnCanvas(contextGraphCanvas, bubble, t, {
        compact: true,
        hitStore: graphInteraction.compact,
      });
      if (graphInteraction.fsOpen) {
        syncForceGraph(bubble);
        forceGraph.alpha = Math.max(forceGraph.alpha, 0.35);
      }
      if (t > 0) graphAnimFrame = requestAnimationFrame(tick);
      else graphAnimFrame = null;
    }
    graphAnimFrame = requestAnimationFrame(tick);
  }

  function formatGraphStatus(bubble) {
    var ctxStatus = bubble.status || "idle";
    var count = bubble.chunk_count || (bubble.lines ? bubble.lines.length : 0);
    var label = ctxStatus === "gathering" ? "Gathering"
      : (ctxStatus === "refreshing" ? "Refreshing"
      : (ctxStatus === "ready" ? "Ready" : "Idle"));
    return label + " · " + count + " chunk" + (count === 1 ? "" : "s");
  }

  function formatGraphFeedHtml(bubble) {
    var queries = Array.isArray(bubble.queries) ? bubble.queries : [];
    var updates = Array.isArray(bubble.updates) ? bubble.updates : [];
    var rows = [];

    queries.slice(-4).forEach(function (q) {
      var timing = formatQueryTiming(q);
      var label = truncateGraphLabel(q.query || sourceLabel(q.source), 48);
      rows.push(
        '<div class="graph-feed-row' + (q.status === "running" ? " is-running" : "") + '">' +
        '<span class="graph-feed-q">' + escapeHtml(label) + '</span>' +
        '<span class="graph-feed-meta">' + escapeHtml(timing || q.status || "") + '</span>' +
        '</div>'
      );
    });

    if (!rows.length && updates.length) {
      updates.slice(-3).reverse().forEach(function (up) {
        var timing = up.duration_ms != null ? formatQueryDuration(up.duration_ms) : "";
        rows.push(
          '<div class="graph-feed-row">' +
          '<span class="graph-feed-q">' + escapeHtml(up.summary || "Update") + '</span>' +
          '<span class="graph-feed-meta">' + escapeHtml(timing) + '</span>' +
          '</div>'
        );
      });
    }

    if (!rows.length && (bubble.lines || []).length) {
      var ln = bubble.lines[bubble.lines.length - 1];
      return 'Latest: ' + escapeHtml((ln.sop_id || "SOP") + " · score " + (ln.score != null ? ln.score : "—"));
    }
    if (!rows.length) return 'Waiting for retrieval…';
    return rows.join("");
  }

  function formatGraphFeed(bubble) {
    var el = document.createElement("div");
    el.innerHTML = formatGraphFeedHtml(bubble);
    return el.textContent || el.innerText || "";
  }

  function renderContextPoolGraph(s) {
    if (s) {
      if (shouldResetContextPoolForTurn(s)) beginNewContextPoolTurn();
      lastScreenState = Object.assign({}, lastScreenState, s);
      if (s.context_bubble) lastScreenState.context_bubble = s.context_bubble;
      if (s.status || s.context_bubble) setCorpusBadge(lastScreenState);
    }
    ingestContextBubble((s && s.context_bubble) || empty_bubble());
  }

  function empty_bubble() {
    return { status: "idle", lines: [], updates: [], queries: [], chunk_count: 0 };
  }

  function clearDemoContextTimer() {
    if (demoContextTimer) {
      clearTimeout(demoContextTimer);
      demoContextTimer = null;
    }
  }

  function startDemoContextPool() {
    beginNewContextPoolTurn();
    var batchIdx = 0;
    var demoBubble = {
      status: "gathering",
      lines: [],
      updates: [],
      queries: [],
      chunk_count: 0,
    };

    function finishDemoBatch() {
      if (batchIdx >= DEMO_POOL_BATCHES.length) {
        demoBubble.status = "ready";
        demoBubble.chunk_count = demoBubble.lines.length;
        ingestContextBubble(demoBubble);
        return;
      }

      var batch = DEMO_POOL_BATCHES[batchIdx];
      var qid = "demo-q" + batchIdx;
      var startedAt = new Date().toISOString();
      demoBubble.queries.push({
        id: qid,
        query: batch.query,
        source: batch.source,
        status: "running",
        started_at: startedAt,
        finished_at: null,
        duration_ms: null,
        chunk_ids: [],
        chunks_added: 0,
      });
      ingestContextBubble(demoBubble);

      demoContextTimer = setTimeout(function () {
        var durationMs = 380 + batchIdx * 160;
        batch.lines.forEach(function (ln) {
          demoBubble.lines.push(Object.assign({}, ln, { query: batch.query }));
        });
        demoBubble.queries = demoBubble.queries.map(function (q) {
          if (q.id !== qid) return q;
          return Object.assign({}, q, {
            status: "done",
            finished_at: new Date().toISOString(),
            duration_ms: durationMs,
            chunk_ids: batch.lines.map(function (ln) { return ln.chunk_id; }),
            chunks_added: batch.lines.length,
          });
        });
        demoBubble.chunk_count = demoBubble.lines.length;
        demoBubble.updates.push({
          summary: "+" + batch.lines.length + " chunk" + (batch.lines.length === 1 ? "" : "s") + " added",
          chunk_ids: batch.lines.map(function (ln) { return ln.chunk_id; }),
          query: batch.query,
          source: batch.source,
          query_id: qid,
          duration_ms: durationMs,
        });
        ingestContextBubble(demoBubble);
        batchIdx += 1;
        demoContextTimer = setTimeout(finishDemoBatch, 520);
      }, 420 + batchIdx * 90);
    }

    renderContextPoolGraph({ context_bubble: demoBubble });
    finishDemoBatch();
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

  function applyScreenState(state) {
    if (demoMode && graphBuild.target && ((graphBuild.target.lines || []).length || (graphBuild.target.queries || []).length)) {
      state = Object.assign({}, state, { context_bubble: cloneBubble(graphBuild.target) });
    }
    lastScreenState = state;
    renderContextPoolGraph(state);
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
    setConn("ready", "Agent connected");
    enableButton(true);
  }

  function scanForAgent() {
    if (!room) return;
    room.remoteParticipants.forEach(function (p) {
      if (isAgent(p)) onAgentReady(p);
    });
  }

  function enableButton(_on) {
    /* PTT control removed from operator UI */
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
    lastContextQuestion = "";
    beginNewContextPoolTurn();
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

  function clearDemoTimer() {
    if (demoTimer) {
      clearTimeout(demoTimer);
      demoTimer = null;
    }
  }

  function resetTranscript() {
    stopActiveSpeech();
    clearDemoContextTimer();
    lastContextTurnSeq = null;
    lastContextQuestion = "";
    beginNewContextPoolTurn();
    pendingRevealMsgId = null;
    transcript = [];
    pendingOperatorId = null;
    pendingAgentId = null;
    lastAgentId = null;
    turnActive = false;
    renderContextPoolGraph(IDLE_STATE);
    render();
    setStatus("idle");
  }

  function demoStartTurn() {
    if (turnActive) return;
    clearDemoTimer();
    turnActive = true;
    lastContextQuestion = "";
    setStatus("listening");
    startDemoContextPool();

    var id = nid();
    pendingOperatorId = id;
    transcript.push({ id: id, role: "operator", text: "Listening…", interim: true });
    render();
  }

  function demoStopTurn() {
    if (!turnActive) return;
    turnActive = false;
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
    log("Click Play demo or wait for auto-play. ?live=1 for LiveKit · ?poll=1 for glasses.");
    setCorpusBadge(IDLE_STATE);
    setConn("ready", "Demo mode");
    setStatus("idle");
    renderContextPoolGraph(IDLE_STATE);
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
        scanForAgent();
      })
      .catch(function (e) {
        setConn("error", "Connect failed");
        log("connect() failed: " + e, "error");
      });
  }

  function initPoll() {
    if (demoBtn) demoBtn.hidden = true;
    setCorpusBadge(IDLE_STATE);
    setConn("ready", "Listening — glasses");
    setStatus("idle");
    renderContextPoolGraph(IDLE_STATE);
    log("Poll mode — screen driven by glasses_bridge /state (no LiveKit, no browser mic).");

    var lastSig = null;
    function pollTick() {
      fetch("/state")
        .then(function (r) { return r.json(); })
        .then(function (s) {
          if (!s) return;
          renderContextPoolGraph(s);
          var status = s.status || "idle";
          if (status === "idle") return;
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
    wireDemoButton();
    wireContextGraphUI();
    lastScreenState = IDLE_STATE;
    renderContextPoolGraph(IDLE_STATE);
    setCorpusBadge(IDLE_STATE);

    if (contextGraphCanvas) {
      window.addEventListener("resize", function () {
        drawContextGraphOnCanvas(contextGraphCanvas, getGraphBubble(), 0, {
          compact: true,
          hitStore: graphInteraction.compact,
        });
      });
    }

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
