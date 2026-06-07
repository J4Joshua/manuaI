# glasses-bridge — reference

Deep contract for the [`glasses-bridge`](SKILL.md) skill. Self-contained: the wire
protocol, the DAT/Bluetooth facts, the offline networking, and the ManuAI internals
to reuse. Sources: `~/mc-goggles/PROTOCOL.md` §1; the **DAT SDK repo vendored at
`vendor/meta-wearables-dat-ios/`** (skills under `plugins/mwdat-ios/skills/`, the
`AGENTS.md` agent guide, `samples/` reference apps); and the live `~/manuaI/src/`.

---

## 1. The pipeline (current, what the audio flows through)

```
 Meta Ray-Bans            iPhone (mc-goggles app, unmodified)        Mac (ManuAI)
┌───────────┐  Bluetooth  ┌──────────────────────────────┐  LAN WS  ┌──────────────────────┐
│ mic ──────┼── HFP ─────►│ HFP PCM → Float32 48k mono    ├─/publish-audio─────────────────►│ VAD segment          │
│           │             │                               │         │  → resample 48k→16k  │
│ speaker   │  (unused —  │                               │         │  → Whisper (en)      │
│   ✗       │   output is │                               │         │  → core.answer       │
│           │   laptop)   │                               │         │  → Kokoro TTS ──► 🔊 LAPTOP speaker
└───────────┘             └──────────────────────────────┘         │  → screen_state ─► 🖥 LAPTOP screen (:8000)
  Glasses↔Phone = Bluetooth (no internet)   Phone↔Mac = LAN only (no WAN needed)
```

Only the **left third changes** vs `offline_demo.py`: the audio source is a WebSocket
frame stream, not the laptop's `sd.InputStream`. The brain, TTS, and screen are reused
verbatim.

---

## 2. WebSocket wire contract (`~/mc-goggles/PROTOCOL.md` §1)

The iOS app's host is `ws://<ip>:8766` (one constant,
`StreamSessionViewModel.swift:16`). All four below hang off that host.

### 2.1 `ws /publish-audio?agent=1` — mic uplink **(implement for real)**
- **First message:** JSON **text** frame — `{"sampleRate":48000,"channels":1}`. The
  rate is the glasses' *actual* input rate, sent dynamically — read it, don't hardcode.
- **Subsequent messages:** raw **Float32 little-endian mono PCM** binary frames.
  Parse with `np.frombuffer(frame, dtype="<f4")`.
- `agent=1` query flag = "bridge this into the voice agent" (i.e. the brain). The app
  always sends it for this socket.

### 2.2 `ws /publish` — video uplink **(accept + drain)**
- Binary frames = JPEG (quality 0.5, ~24 fps) — **discard them**.
- Control JSON on the *same* socket:
  - app → server: `{"type":"pause"}`, `{"type":"resume"}`
  - server → app: `{"type":"video_off"}`, `{"type":"video_on"}`,
    `{"type":"capture_photo","request_id":"<id>"}`
- **Send `{"type":"video_off"}` on connect** so the glasses stop spending Bluetooth
  bandwidth on video you throw away.

### 2.3 `ws /agent-audio` — downlink to glasses speaker **(accept + idle, send nothing)**
- *Contract* (not used here): server sends `{"sampleRate":24000}` then **Int16 LE mono
  PCM @ 24 kHz**; the app plays it on the glasses speaker over HFP.
- **We don't use it** — output is the laptop. Accept the socket and stay silent so the
  app doesn't reconnect-loop.

### 2.4 `POST /publish/photo` — still **(answer 200, discard)**
- Body = full-res JPEG, `Content-Type: image/jpeg`, `X-Request-Id` echoes a
  `capture_photo` request. Only fires on a user-triggered capture, never at startup.
- A `process_request`-style hook on the `websockets` server can return a bare `200` for
  non-WebSocket HTTP so a stray POST fails harmlessly.

---

## 3. DAT / glasses facts (from the vendored `mwdat-ios` skills)

> Skill files are at `vendor/meta-wearables-dat-ios/plugins/mwdat-ios/skills/<name>/SKILL.md`;
> the full agent guide is `vendor/meta-wearables-dat-ios/AGENTS.md`.

- **Transport glasses↔phone is Bluetooth.** Audio (the mic) rides **HFP** (Hands-Free
  Profile) through the normal iOS audio session — there is **no `MWDATAudio` module**
  in the DAT SDK; it exposes no microphone API. Video/photo go over **Bluetooth
  Classic** via DAT (`camera-streaming`: "Resolution and frame rate are constrained by
  Bluetooth Classic bandwidth").
- **Session teardown is all Bluetooth** (`session-lifecycle`): folding/removing the
  glasses disconnects Bluetooth → session `stopped`; out-of-range disconnects.
- **Registration is the only online step** (`permissions-registration`, `debugging`):
  - "Registration requires an internet connection"; "No internet → registration fails".
  - `Wearables.shared.startRegistration()` runs through the **Meta AI companion app**
    with a URL callback; the SDK defines `RegistrationError.networkUnavailable`.
  - **Developer Mode** (Meta AI app → *Your glasses → Developer Mode*) with
    `MetaAppID = 0` → "Registration always allowed."
  - Registration is a *state you reach* (`.registered`), not a per-session call → once
    registered + camera permission granted, sessions start over Bluetooth alone.
  - **Undocumented:** whether the registration token expires over a long offline run.
    Flag and test if the demo runs offline for an extended period.
- **`MockDeviceKit`** simulates the device *and* registration/permissions without the
  Meta AI app — the guaranteed-offline / no-glasses fallback.
- **iOS prerequisites** (`getting-started`): `NSBluetoothAlwaysUsageDescription`,
  `NSMicrophoneUsageDescription`, background modes `bluetooth-peripheral` +
  `external-accessory`, Bluetooth on, glasses in range.

### ⚠ De-risk test to run early
Register **online**, then **turn wifi off**, then **start a DAT session**. Confirms
whether a *started* session survives offline or whether session-start re-checks Meta
connectivity. The HFP/audio path is unaffected either way — this only gates the
camera/photo features, which this skill doesn't use, but it's the cheapest way to
learn the offline boundary.

---

## 4. Networking without router or signal

The phone↔laptop hop needs **a local network, not the internet**. Three ways to get
one when there's no router and no reception:

1. **iPhone Personal Hotspot (recommended) — the phone *is* the router.** Enable
   hotspot on the iPhone, join it from the Mac. Both land on `172.20.10.x` (phone
   `…10.1`, Mac `…10.2`). The app dials the Mac's `172.20.10.x` IP; traffic stays local
   over WiFi radio — no tower, no internet. *Caveat:* the hotspot toggle needs a SIM
   with a provisioned plan — **no reception is fine**, but a **SIM-less** phone may hide
   the toggle entirely. Test on the real device.
2. **VPN** where phone + Mac share a routable subnet (works with zero code change).
3. **Same WiFi SSID** (easiest when a router exists, even one with the WAN unplugged).

**ATS:** the app's `Info.plist` sets `NSAllowsLocalNetworking=true`, which permits
plaintext `ws://` to RFC-1918 private ranges — `10.x`, `172.16–31.x` (includes the
hotspot's `172.20.10.x`), `192.168.x`, link-local `169.254.x`. So a hotspot/LAN IP
needs **no extra plist exception**; the hardcoded `10.10.10.121` exception is just for
that specific historical IP. (For a *public* URL you'd instead use `wss://` + a real
cert — out of scope here.)

The WebSocket transport itself is route-agnostic — `URLSession.webSocketTask` works
over WiFi, cellular, hotspot, or VPN identically. The LAN requirement is purely
because the app points at a private IP.

---

## 5. ManuAI internals to reuse (don't rebuild)

All in `~/manuaI/src/`. Import from `offline_demo` rather than copy.

### `offline_demo.py` — the template (the bridge is this + a WS front-end)
| Symbol | Line | Use |
|---|---|---|
| `transcribe_wav(wav_path)` | 216 | mlx-whisper, **`language="en"` pinned** → text. Feed it a **16 kHz** wav. |
| `run_pipeline(transcript, retriever)` | 306 | sync wrapper: `core.answer` → `_set_latest` → `render` → **`speak` on the laptop**. The whole downstream in one call. |
| `process_transcript(transcript, retriever)` | 299 | async: `core.answer` + `_set_latest` (if you want to drive it yourself). |
| `speak(text)` | 204 | Kokoro → `sd.play`/`sd.wait` on the **default output (laptop)**. Blocking. |
| `synth_to_numpy` / `synth_to_wav` | 190 / 197 | Kokoro synth — `synth_to_wav` makes test fixtures. |
| `_set_latest` / `_get_latest` | 110 / 116 | write/read the shared `LATEST` screen_state (lock-guarded). |
| `_start_http_server()` | 162 | daemon-thread stdlib server: `GET /` → `screen.html`, `GET /state` → `LATEST` JSON, on `PORT` (8000). |
| `record_until_silence()` | 232 | the **RMS VAD state machine to mirror** for the streamed feed (PRE_SPEECH → RECORDING → silence-stop). |
| `WHISPER_MODEL` | 75 | resolved repo id (`_resolve_whisper_repo` adds the `-mlx` suffix). |
| Constants | 83–88 | `SAMPLE_RATE=16000`, `BLOCK_SIZE=512`, `ENERGY_THRESHOLD=0.010`, `SPEECH_START_BLOCKS=3`, `SILENCE_STOP_SECS=1.2`, `HARD_CAP_SECS=15.0`. **Retune the energy threshold for HFP.** |
| `MACHINE_ID` | 61 | default `labeler-line3` (env `MACHINE_ID`). |

Import-safety: `offline_demo` only runs `load_env()` + constant defs at import; the
servers/loop start in `main()`. So `import offline_demo` is side-effect-light. It also
pops a blank `HF_TOKEN` (line 51) so anonymous model pulls work — inherit that.

### `core.py` — the brain
- `async def answer(question, machine_id, retriever, k=5) -> screen_state` (line 68).
  **The one function every consumer programs to.** Returns a `screen_state` dict on
  every path. Don't fork its output shape.
- `screen_state` keys (also the idle placeholder in `offline_demo.LATEST`, lines
  93–106): `question, machine_id, status (idle|answered|escalated), answer,
  citations[], steps_source, steps[], safety_warnings[], safety_flag, top_score,
  threshold, source_excerpt`.

### `retriever.py` — offline retrieval
- `CosineRetriever` = the **fully-local stub** (nomic vectors in `index.json`,
  threshold gate **0.70**). **Use this for wifi-off** — Moss's `load_index` is a network
  call (cloud-anchored, ARCHITECTURE G14). `offline_demo` instantiates
  `CosineRetriever()` once and reuses it; do the same (one instance, shared across
  utterances).
- `load_env()` — stdlib `.env` loader (no python-dotenv dependency at import).

### Deps already in `.venv` (no new installs needed)
`websockets` 15.0.1 · `scipy` 1.17.1 (`resample_poly`) · `sounddevice` 0.5.5 ·
`soundfile` 0.14.0 · `mlx-whisper` 0.4.3 · `kokoro-onnx` 0.5.0 · `numpy` 2.4.6. Add
`websockets` to `requirements.txt` (installed but currently unpinned there).

---

## 6. Canonical test beats (ground truth)

From `offline_demo.selftest` / `test_beats.py`:
- **Jam** → answered, cites **SOP-1187**:
  `"The labeler on line 3 jammed and threw error E-42."`
- **Bypass** → escalated:
  `"Can I bypass the safety interlock and run with the guard open?"`

A loopback WS client that replays the **jam** utterance (synth it via
`synth_to_wav`, stream as Float32 48 kHz frames to `/publish-audio`) should drive
`/state` to `answered` + SOP-1187 and make the laptop speak — proving the bridge
end-to-end without glasses.
