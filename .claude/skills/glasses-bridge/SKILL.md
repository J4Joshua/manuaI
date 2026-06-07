---
name: glasses-bridge
description: Build or run the offline Meta Ray-Ban glasses → ManuAI audio bridge — glasses mic audio streams in over a WebSocket and the grounded answer is spoken + shown on the laptop (no glasses speaker, no video). Use when integrating Ray-Ban / Meta glasses audio input into ManuAI, building the glasses WebSocket bridge, running ManuAI hands-free from the glasses, or reasoning about whether the glasses path works offline (no wifi / no cellular).
---

# Glasses bridge — drive ManuAI from Ray-Ban glasses (audio in, laptop out)

Let an operator wearing **Meta Ray-Ban glasses** speak a fault and have **ManuAI
answer on the laptop** (spoken + the live SOP screen) — fully offline. This is the
existing `src/offline_demo.py` loop with **one thing swapped: the audio source is a
WebSocket from the glasses instead of the laptop mic.** Everything downstream
(Whisper → `core.answer` → Kokoro TTS on the laptop + screen) is unchanged.

> The glasses never talk to the laptop directly. The iOS app (in-repo at `ios/ManuAI`,
> forked from the proven glasses app) relays the glasses mic to a server over a raw
> WebSocket. Your job is to make ManuAI *be* that server for the audio path. Read
> `reference.md` (next to this file) for the exact wire contract and the DAT/Bluetooth
> facts before you start.

## Scope (what to build, what NOT to)

- **IN:** glasses microphone → ManuAI brain. **OUT:** laptop speaker + laptop screen.
- **No video, no photo** — the operator isn't looking at a screen on the glasses.
- **No `/agent-audio` downlink** — output is the laptop, so we never send audio back
  to the glasses speaker.
- **The iOS app lives in-repo at `ios/ManuAI`** (forked + renamed). The audio uplink and
  the camera-free "Start hands-free" path are already wired; the host IP is managed by
  `ios/configure_and_launch.sh` (see "Point the glasses app here"). Keep edits minimal —
  don't re-fork the camera/DAT machinery; the bridge only needs the audio path.
- **Do NOT touch the LiveKit / WebRTC path** (`src/agent.py`, `operator.html`). That's
  the wifi-ON path and can't go offline (ARCHITECTURE G16). The bridge is raw-WS,
  wifi-off.

## The shape of the build

A small new module (e.g. `src/glasses_bridge.py`) that is **`offline_demo.py` with a
WebSocket front-end**. Reuse, don't rebuild.

1. **Transport:** the ManuAI server is **stdlib `http.server`, not FastAPI** — so use
   the **`websockets`** library (already in `.venv`, v15.0.1) for the socket server.
   Listen on **port 8766** (the iOS app's default — keep it so only the IP changes).
2. **Screen:** reuse `offline_demo._start_http_server()` in a daemon thread → it
   serves `screen.html` + `/state` on `PORT` (8000) exactly as the offline demo. The
   SOP card renders live in the browser while audio arrives on 8766.
3. **Per-utterance pipeline:** reuse `offline_demo`'s helpers —
   `transcribe_wav` (Whisper, `language="en"` pinned), `run_pipeline(transcript,
   retriever)` (runs `core.answer`, updates `LATEST`, renders, **speaks on the
   laptop**), `_set_latest`, `WHISPER_MODEL`, `SAMPLE_RATE` (16000), and the VAD
   constants. The only genuinely new code is: WS framing, a streaming VAD fed by
   incoming blocks (mirror `record_until_silence`'s RMS state machine), and a 48k→16k
   resample.

### Endpoints to satisfy

The unmodified iOS app opens **three** WebSockets at startup plus an occasional HTTP
POST (`StreamSessionViewModel.swift` lines 784/792/796/871). Satisfy all of them so
the app doesn't error/reconnect-loop, but only *act* on audio:

| Path | App's intent | What the bridge does |
|---|---|---|
| `ws /publish-audio?agent=1` | mic uplink | **The real work** — header → Float32 frames → VAD → STT → brain → laptop TTS + screen |
| `ws /publish` | video uplink (JPEG) | Accept, **drain & discard**; send `{"type":"video_off"}` back so it stops streaming video |
| `ws /agent-audio` | downlink to glasses speaker | Accept, **idle** — send nothing (output is the laptop) |
| `POST /publish/photo` | full-res still | Return bare `200`; discard. (Only fires on a user capture, never at startup) |

`/publish-audio` wire format (full detail in `reference.md`): **first** message is a
JSON text header `{"sampleRate":48000,"channels":1}`; **subsequent** messages are raw
**Float32 LE mono PCM** binary frames at that rate.

## Pitfalls — read these before writing a line

1. **stdlib server, not FastAPI.** Don't reach for FastAPI/uvicorn. Use `websockets`
   for the socket; keep offline_demo's stdlib HTTP server for the screen. Two servers,
   two ports (8766 WS + 8000 HTTP), one process.
2. **Never block the asyncio loop.** `mlx_whisper.transcribe` and Kokoro's
   `sd.play()/sd.wait()` are blocking and slow (seconds). Run the per-utterance
   pipeline in a worker (`await asyncio.to_thread(...)`), and **serialize utterances**
   (an `asyncio.Lock` / single worker) so two answers never play over each other.
3. **Resample 48k→16k yourself.** Incoming audio is 48 kHz; Whisper wants 16 kHz. Use
   `scipy.signal.resample_poly(audio, 16000, in_rate)` (scipy is installed) and write a
   16 kHz wav, matching what `transcribe_wav` already expects. **Do not** rely on
   Whisper/ffmpeg to resample — ffmpeg may not be installed and offline_demo
   deliberately avoids it by feeding 16 kHz wavs.
4. **Pin Whisper to English.** `transcribe_wav` already passes `language="en"` —
   keep it. Without it Whisper mis-detects Chinese on short clips (a known ManuAI trap).
5. **Echo / barge-in.** The laptop speaker is now in the room with an open glasses
   mic, so TTS leaks back in and can re-trigger the loop. Add a **`speaking` guard**:
   while the pipeline is running (and for a short cooldown after), **drop** incoming
   audio blocks instead of accumulating them. Best-effort, but necessary for a sane demo.
6. **Float32 LE parsing:** `np.frombuffer(frame, dtype="<f4")`. The header's
   `sampleRate` is the glasses' *actual* input rate sent dynamically — read it, don't
   assume 48000.
7. **`HF_TOKEN` must be empty/unset** for anonymous model pulls; offline_demo already
   `os.environ.pop("HF_TOKEN", None)` — inherit that (import offline_demo, or replicate).
8. **Keep port 8766.** It matches the iOS app's hardcoded default, so the operator only
   ever changes the *IP*. Make it overridable by env (e.g. `GLASSES_PORT`) but default
   to 8766.
9. **Retune the VAD for HFP.** `ENERGY_THRESHOLD = 0.010` is tuned for a laptop mic.
   Glasses-over-HFP levels differ — expect to adjust the threshold / speech-start /
   silence-stop constants. Don't assume the laptop values transfer.
10. **Output is the laptop → never open the `/agent-audio` send path.** The glasses
    speaker is intentionally unused; sending Int16 24k back is out of scope.
11. **Don't fork the contracts.** `screen_state` and the chunk schema are shared by
    both retrievers and both UIs (ARCHITECTURE §3b) — feed `core.answer` and let it
    produce `screen_state`; don't invent a parallel shape.
12. **Repo rules:** working branch is `build/demo-mvp`; **don't push to `main` without
    explicit OK.** Add `websockets` to `requirements.txt` (it's installed but unpinned
    there) when you add the module.

## Offline story (why this whole thing works wifi-off)

- **Glasses ↔ phone (audio): Bluetooth HFP — no internet.** The mic rides the standard
  Bluetooth Hands-Free Profile through the iOS audio session (there is **no
  `MWDATAudio` module** in the DAT SDK). 48 kHz Float32 mono — ideal for Whisper.
- **Phone ↔ laptop: LAN only — no WAN.** A local router or **iPhone Personal Hotspot**
  with the internet unplugged is fine (still "wifi off" in the demo sense). See
  `reference.md` → Networking.
- **The one online step: DAT registration, once.** Registering the app with Meta AI
  (`Wearables.shared.startRegistration()`, Developer Mode) needs the internet. **Do it
  during setup while online, then go offline.** `MockDeviceKit` simulates the device +
  registration for a guaranteed-offline / no-glasses fallback.
- **⚠ De-risk early — the one real unknown:** register online → **turn wifi off** →
  try to **start a DAT session**. This tells you whether a *started* session survives
  offline or whether session-start re-checks Meta connectivity. The HFP/audio path is
  unaffected either way, but confirm it before relying on it for a demo. Also note:
  whether the registration *token* expires over a long offline run is undocumented.

## Point the glasses app here + launch Xcode (one command)

The iOS app now lives **in-repo at `ios/ManuAI`** (forked from the proven glasses app;
the audio path plus a camera-free **"Start hands-free (audio only)"** button are wired).
Its server host is a single Swift constant in
`ios/ManuAI/ViewModels/StreamSessionViewModel.swift` →
`private let streamPublishHost = "ws://<ip>:8766"`.

**Don't hand-edit it — run the helper.** It detects this Mac's IP, rewrites that
constant, and opens the project in Xcode:

```bash
ios/configure_and_launch.sh               # auto-detect (en0 WiFi, then en1)
ios/configure_and_launch.sh 172.20.10.2   # or force an IP (e.g. iPhone Personal Hotspot)
```

Then in Xcode: set the **Signing Team** (target ManuAI → Signing & Capabilities → your
Apple ID), **Run** on the iPhone, and tap **Start hands-free (audio only)**. Keep **port
8766** (the bridge's audio socket; `:8000` is the separate screen server).
`NSAllowsLocalNetworking` in `Info.plist` already permits plaintext `ws://` to RFC-1918
ranges (incl. `172.20.10.x`), so no plist change is needed for a hotspot IP. Re-run the
helper whenever your IP changes (new WiFi, DHCP renew, switching to the hotspot).

To confirm real mic audio is flowing, watch the bridge terminal for
`[glasses] audio client connected` → `header: sampleRate=… channels=1` →
`heard: '…your words…'`. A connection with no `heard:` line means audio isn't reaching
the VAD (HFP mic not routing, or `ENERGY_THRESHOLD` too high for Bluetooth levels).

## Verify without glasses

1. **Loopback client:** write a tiny WS client that connects to
   `ws://localhost:8766/publish-audio`, sends the JSON header, then streams a wav as
   Float32 48 kHz frames. Use `offline_demo.synth_to_wav("The labeler on line 3 jammed
   and threw error E-42.", ...)` to make the fixture. Expect `GET
   http://localhost:8000/state` to flip to `status:"answered"` citing **SOP-1187** and
   the **laptop to speak**.
2. **Brain unchanged:** `.venv/bin/python src/offline_demo.py --selftest` and
   `src/test_beats.py` still pass — you only *import* from offline_demo, never modify it.
3. **Optional headless gate** in the bridge (`--selftest`) that runs the two canonical
   beats (jam→answered+SOP-1187, bypass→escalated) through the WS segmentation path,
   mirroring `offline_demo.selftest`.

## Source of truth

- `reference.md` (next to this file) — wire contract, DAT facts, networking, ManuAI
  internals to reuse.
- `ios/PROTOCOL.md` §1 — the authoritative glasses↔server WebSocket contract (in-repo).
- `~/manuaI/GLASSESINTEGRATION.md` — the original integration assessment (effort, paths).
- `~/manuaI/src/offline_demo.py` — the pipeline you're wrapping.
- **DAT SDK — vendored into this repo** at `vendor/meta-wearables-dat-ios/` (the full
  `facebook/meta-wearables-dat-ios` plugin repo). The skills are under
  `vendor/meta-wearables-dat-ios/plugins/mwdat-ios/skills/` (`camera-streaming`,
  `session-lifecycle`, `permissions-registration`, `mockdevice-testing`,
  `getting-started`, …); the deep agent guide is `vendor/.../AGENTS.md`; runnable
  reference apps are in `vendor/.../samples/` (CameraAccess, DisplayAccess). On this
  machine they're also installed as the `mwdat-ios:*` plugin, so `mwdat-ios:<name>`
  resolves too — but the vendored copy is the portable source of truth.
