/* ManuAI — unified operator frontend logic.
 *
 * Transport layer ONLY. The render path (applyState + panels + CSS) is reused
 * VERBATIM from screen.html and lives inline in operator.html — this file never
 * touches the DOM panels except through that one applyState() function.
 *
 * What this does:
 *   1. fetch /token  → { url, token, room }   (minted by server.py, local creds)
 *   2. Room.connect(url, token)               (livekit-client UMD, bundled local)
 *   3. discover the agent participant (kind === AGENT), both races handled
 *   4. hold-to-talk button → performRpc start_turn / end_turn on the agent
 *   5. publish mic on first press; subscribe + play the agent's audio track
 *   6. screen_state arrives on the data channel (topic "screen_state") → applyState
 *
 * No CDN: LivekitClient is the global from /static/livekit-client.umd.min.js.
 * The agent contract is in agent.py (do not change it; this matches it):
 *   - RPC methods on the AGENT participant: start_turn / end_turn / cancel_turn
 *   - data channel topic "screen_state", json.dumps(state).encode()
 *   - agent participant kind === AGENT, agent_name "manuai"
 */
(function () {
  "use strict";

  // LivekitClient is the UMD global (verified: bundle header sets
  // (globalThis||self).LivekitClient = {...}). lowercase 'k'.
  var LK = window.LivekitClient;

  // ---- DOM handles for the connection UI (NOT the render panels) ----
  var statusDot = document.getElementById("conn-dot");
  var statusText = document.getElementById("conn-text");
  var talkBtn = document.getElementById("talk-btn");
  var talkLabel = document.getElementById("talk-label");
  var logEl = document.getElementById("op-log");

  // ---- small structured logger (visible on the page + console) ----
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
      // keep the newest visible; cap the buffer
      while (logEl.childNodes.length > 200) logEl.removeChild(logEl.firstChild);
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  // ---- connection-status indicator ----
  // state: "offline" | "connecting" | "connected" | "ready" | "error"
  function setConn(state, text) {
    if (statusDot) statusDot.className = "conn-dot conn-" + state;
    if (statusText) statusText.textContent = text;
  }

  // ---- idle screen_state seed (shape copied from server.py LATEST) so the
  //      panels render immediately instead of staying blank until turn 1 ----
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

  // ---- module state ----
  var room = null;
  var agentIdentity = null; // set once the agent participant is discovered
  var turnActive = false; // guard against double start / end
  var audioUnlocked = false;
  var lastScreenState = IDLE_STATE;

  // ---- agent discovery: ONE idempotent path for both races ----
  // (a) agent already in the room when we connect → scan remoteParticipants
  // (b) agent joins after us → ParticipantConnected event
  function isAgent(p) {
    try {
      return p && p.kind === LK.ParticipantKind.AGENT;
    } catch (e) {
      return false;
    }
  }

  function onAgentReady(p) {
    if (agentIdentity) return; // idempotent
    agentIdentity = p.identity;
    log("Agent ready: identity=" + agentIdentity + " (kind=AGENT)");
    setConn("ready", "Ready — hold to talk");
    enableButton(true);
  }

  function scanForAgent() {
    if (!room) return;
    // remoteParticipants is a Map<identity, RemoteParticipant> in livekit-client v2
    room.remoteParticipants.forEach(function (p) {
      if (isAgent(p)) onAgentReady(p);
    });
  }

  function enableButton(on) {
    if (!talkBtn) return;
    talkBtn.disabled = !on;
    if (talkLabel && on) talkLabel.textContent = "Hold to talk";
  }

  // ---- audio: unlock playback (autoplay policy) + attach agent audio ----
  function unlockAudio() {
    if (audioUnlocked || !room) return Promise.resolve();
    // room.startAudio() must be called from a user gesture to satisfy the
    // browser autoplay policy; the agent greets before any gesture so its
    // audio is queued until this resolves.
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
    var el = track.attach(); // creates an <audio> element wired to the track
    el.setAttribute("data-livekit-audio", "1");
    el.autoplay = true;
    document.body.appendChild(el);
    log("Attached agent audio track");
  }

  // ---- push-to-talk RPC calls on the AGENT participant ----
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
    if (turnActive || !agentIdentity) return;
    turnActive = true;
    if (talkBtn) talkBtn.classList.add("talking");
    if (talkLabel) talkLabel.textContent = "Listening… release to send";
    // 1) unlock audio playback, 2) publish mic, 3) tell the agent to start.
    unlockAudio()
      .then(function () {
        return room.localParticipant.setMicrophoneEnabled(true);
      })
      .then(function () {
        log("Mic published");
        return rpc("start_turn");
      })
      .catch(function (e) {
        log("startTurn error: " + e, "error");
      });
  }

  function endTurn() {
    if (!turnActive) return;
    turnActive = false;
    if (talkBtn) talkBtn.classList.remove("talking");
    if (talkLabel) talkLabel.textContent = "Hold to talk";
    // tell the agent the turn is over (commit → STT → brain → TTS), then mute.
    rpc("end_turn")
      .catch(function () {
        /* logged in rpc() */
      })
      .finally(function () {
        if (room && room.localParticipant) {
          room.localParticipant
            .setMicrophoneEnabled(false)
            .catch(function (e) {
              log("mic mute failed: " + e, "error");
            });
        }
      });
  }

  // ---- wire the hold-to-talk button (pointer + touch + keyboard) ----
  function wireButton() {
    if (!talkBtn) return;

    talkBtn.addEventListener("pointerdown", function (e) {
      e.preventDefault();
      if (talkBtn.disabled) return;
      // pointer capture so a release OFF the button still ends the turn
      // (prevents a wedged-open mic if the finger slides away).
      try {
        talkBtn.setPointerCapture(e.pointerId);
      } catch (err) {
        /* not all pointer types support capture */
      }
      startTurn();
    });

    function release(e) {
      if (e && e.pointerId !== undefined) {
        try {
          talkBtn.releasePointerCapture(e.pointerId);
        } catch (err) {
          /* ignore */
        }
      }
      endTurn();
    }
    talkBtn.addEventListener("pointerup", release);
    talkBtn.addEventListener("pointercancel", release);

    // keyboard affordance: hold Space while focused = hold to talk
    talkBtn.addEventListener("keydown", function (e) {
      if (e.code === "Space" && !e.repeat && !talkBtn.disabled) {
        e.preventDefault();
        startTurn();
      }
    });
    talkBtn.addEventListener("keyup", function (e) {
      if (e.code === "Space") {
        e.preventDefault();
        endTurn();
      }
    });

    // safety net: if the pointer is released anywhere (window) while a turn is
    // active, end it — covers edge cases where the button never sees pointerup.
    window.addEventListener("pointerup", function () {
      if (turnActive) endTurn();
    });
    window.addEventListener("blur", function () {
      if (turnActive) endTurn();
    });
  }

  // ---- main: token → connect → wire events ----
  function connect() {
    setConn("connecting", "Fetching token…");
    log("Fetching /token …");

    // unique identity per load (so multiple tabs don't collide in the room)
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

        // ----- data channel: screen_state → applyState (the reused renderer) -----
        // emit signature (verified in the bundle): (payload, participant, kind, topic, ...)
        room.on(LK.RoomEvent.DataReceived, function (payload, participant, kind, topic) {
          if (topic !== "screen_state") return;
          try {
            var state = JSON.parse(new TextDecoder().decode(payload));
            lastScreenState = state;
            applyState(state); // applyState lives in operator.html, copied verbatim
            log("screen_state ← status=" + (state.status || "?"));
          } catch (err) {
            log("bad screen_state payload: " + err, "error");
          }
        });

        // ----- play the agent's audio -----
        room.on(LK.RoomEvent.TrackSubscribed, function (track, pub, participant) {
          attachTrack(track);
        });

        // ----- agent discovery race (b): joins after us -----
        room.on(LK.RoomEvent.ParticipantConnected, function (p) {
          log("participant joined: " + p.identity + " kind=" + p.kind);
          if (isAgent(p)) onAgentReady(p);
        });

        room.on(LK.RoomEvent.ParticipantDisconnected, function (p) {
          log("participant left: " + p.identity);
          if (agentIdentity && p.identity === agentIdentity) {
            agentIdentity = null;
            enableButton(false);
            setConn("connected", "Connected — waiting for agent…");
            if (talkLabel) talkLabel.textContent = "Waiting for agent…";
          }
        });

        room.on(LK.RoomEvent.Disconnected, function (reason) {
          log("room disconnected: " + reason, "error");
          setConn("error", "Disconnected");
          enableButton(false);
        });

        room.on(LK.RoomEvent.Reconnecting, function () {
          setConn("connecting", "Reconnecting…");
        });
        room.on(LK.RoomEvent.Reconnected, function () {
          setConn("connected", "Reconnected");
          scanForAgent();
        });

        return room.connect(cfg.url, cfg.token).then(function () {
          return cfg;
        });
      })
      .then(function (cfg) {
        setConn("connected", "Connected — waiting for agent…");
        log("Connected to room " + cfg.room + " as " + ident);
        if (talkLabel) talkLabel.textContent = "Waiting for agent…";
        // agent discovery race (a): already present when we connected
        scanForAgent();
      })
      .catch(function (e) {
        setConn("error", "Connect failed");
        log("connect() failed: " + e, "error");
      });
  }

  // ---- typed-input footer (R2 fallback) — same behavior as screen.html ----
  // Goes through /ask (server.py → core.answer) → applyState directly. This is
  // independent of the voice path and survives if the mic/agent is flaky.
  function wireTypedInput() {
    var askInput = document.getElementById("ask-input");
    var askBtn = document.getElementById("btn-ask");
    var spinnerEl = document.getElementById("spinner");
    var machineSelect = document.getElementById("machine-select");
    if (!askInput || !askBtn) return;

    function doAsk() {
      var q = askInput.value.trim();
      if (!q) return;
      var machine = (machineSelect && machineSelect.value) || "labeler-line3";
      askBtn.disabled = true;
      if (spinnerEl) spinnerEl.style.display = "block";
      var url = "/ask?q=" + encodeURIComponent(q) + "&machine=" + encodeURIComponent(machine);
      fetch(url)
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          lastScreenState = data;
          applyState(data);
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

  function wireContextRefresh() {
    var refreshBtn = document.getElementById("context-refresh-btn");
    var machineSelect = document.getElementById("machine-select");
    var askInput = document.getElementById("ask-input");
    if (!refreshBtn) return;

    function refreshViaHttp() {
      var state = lastScreenState || IDLE_STATE;
      var machine = state.machine_id ||
        (machineSelect && machineSelect.value) ||
        "labeler-line3";
      var q = state.question || (askInput && askInput.value.trim()) || "";
      var url = "/context/refresh?machine=" + encodeURIComponent(machine);
      if (q) url += "&q=" + encodeURIComponent(q);
      return fetch(url)
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          if (data.error) throw new Error(data.error);
          lastScreenState = data;
          applyState(data);
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

  // ---- boot ----
  function boot() {
    if (!LK) {
      setConn("error", "livekit-client failed to load");
      log("FATAL: window.LivekitClient is undefined — bundle did not load", "error");
      return;
    }
    lastScreenState = IDLE_STATE;
    applyState(IDLE_STATE); // render idle panels immediately
    enableButton(false);
    setConn("offline", "Connecting…");
    wireButton();
    wireTypedInput();
    wireContextRefresh();
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
