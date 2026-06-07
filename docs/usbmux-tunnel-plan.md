# Plan — usbmux TCP tunnel (air-gapped over the USB-C/Lightning wire)

**Worktree:** `../manuai-usbmux` · **Branch:** `usbmux-tunnel` (off `raybans-live-feed`)
**Status:** IMPLEMENTED (Mac side built + headless-verified; iOS side written, unverified) —
subagent-validated (VERDICT: SOUND WITH FIXES; findings folded in). Builds on the committed
video feed (`5a5aa89`).

- **Mac** (`src/glasses_bridge.py`): `--wired`, `--probe-wired`, `--selftest-wired`.
  `--selftest-wired` **PASSES 5/5** (two beats + framing + speaking-guard + Stage-5 video);
  the existing `--selftest-wire` still **7/7** (no regression). Verified on real hardware
  (iPhone, iOS 26.5): `--probe-wired` green and the full `--wired` loop transcribed live
  glasses audio over the cable (status=answered).
- **iOS** (`StreamSessionViewModel.swift`): `AudioUplinkServer` (NWListener + framing) + a
  `wiredMode` switch. Building it in Xcode is YOUR step — not runnable headless here.
- **Validator fixes folded in:** observer-teardown-on-disconnect (iOS), frame-length sanity
  bound + a wired speaking-guard test (Mac), Developer-Mode prereq, NWListener raw-socket
  fallback, red-probe ambiguity, two-forwarder Stage 5 (all below).

## Goal
Make the iOS app ↔ Mac bridge work over the USB cable with **Wi-Fi *and* Cellular OFF**
(Bluetooth on for the glasses) — genuinely air-gapped, no IP network at all. Today
everything rides a WebSocket over IP (the phone dials `ws://<mac-ip>:8766`), which a bare
cable can't provide. This swaps that transport, for the wired demo, to **raw TCP carried
over Apple's `usbmux` channel** (the same pipe Xcode/Finder use to reach the device).

## The one hard constraint that shapes everything: role inversion
`usbmux` / `iproxy` / `pymobiledevice3 usbmux forward` is **host-initiated** — the Mac
connects to a port that is **listening on the phone**. There is no device-side API for a
sandboxed app to dial the Mac over USB. So the client/server roles flip:
- **iOS app = server** — an `NWListener` TCP server (instead of dialing out as a WS client).
- **Mac bridge = client** — connects through a forwarder to `127.0.0.1:<mac-port>`.

(This is exactly the model PeerTalk has shipped for years.)

## Transport: raw TCP + length-prefix framing (no WebSocket on the wire)
Avoids pulling a WebSocket-*server* dependency into the iOS app. Each message:
`[1-byte type][4-byte big-endian length][payload]`.
- `0x00` = UTF-8 JSON — the `{"sampleRate":48000,"channels":1}` header + any control
- `0x01` = raw Float32-LE audio (matches the bridge's existing `np.frombuffer(..., "<f4")`)
- `0x02` = JPEG — only if video shares this socket; we instead give video its own port (Stage 5)

## Scope of the FIRST cut: mic uplink only (phone → Mac)
The bridge's answer comes out the **laptop** (speaker + screen) — the locked audio-only
scope. So the only thing that MUST cross the wire for a working demo is **mic audio,
phone → Mac**. That is the make-or-break. The video feed is a clean add-on (Stage 5), not
part of the first cut.

## Build order — front-loads the one thing we can't test without hardware
The single unverifiable-without-a-device assumption is *"usbmux delivers an inbound
connection to a sandboxed iOS app's `NWListener`."* If that's wrong, the whole iOS
transport is dead. So the first on-device checkpoint is the smallest thing that proves
bytes flow — **before any audio code**.

- **Stage 0 — tooling — ✅ DONE.** `pymobiledevice3` 9.16.0 is installed in an **isolated
  venv** (`~/.manuai-tools`, symlinked onto PATH as `pymobiledevice3`) — deliberately NOT in
  the ManuAI venv, because it needs `typer>=0.26` while huggingface-hub needs `<0.26`; the
  ManuAI env stays HF-compliant (typer 0.25.1). YOUR remaining device steps: plug in the
  iPhone, trust it, and enable **Developer Mode** (Settings → Privacy & Security → Developer
  Mode; iOS 16+ — app-port usbmux forwarding requires it, or Stage 4 fails with an opaque
  refusal). Then, in its own terminal, leave running:
  `pymobiledevice3 usbmux forward 8766 8766` (forwards Mac-localhost:8766 → device:8766 —
  confirm exact arg order/`--serial` at build time). Documented as a **separate step, not
  spawned by the bridge** — spawning drags UDID-selection/PATH/zombie-process risk onto the
  critical path; auto-spawn can come later once it's proven.
- **Stage 1 — prove the tunnel (make-or-break, smallest possible).** iOS: an `NWListener`
  on :8766 that, on accept, sends ONLY a framed JSON header — no audio, no VAD. Mac: a tiny
  `--probe-wired` that connects to `127.0.0.1:8766`, reads one frame, prints it.
  **A GREEN probe is the only conclusive signal** — it proves usbmux→NWListener delivery;
  a RED probe is ambiguous (forwarder down vs app not foregrounded vs usbmux genuinely
  failing), so check the forwarder process + a foregrounded app before trusting a red.
  Use the **default** `NWListener` (all-interfaces) here — do NOT add a `127.0.0.1`
  `requiredLocalEndpoint` restriction (it only adds a failure mode to the one test you
  can't run yourself). **Plan B if NWListener refuses the usbmux connection:** drop to a raw
  BSD socket / GCDAsyncSocket listener — PeerTalk's exact approach, proven over usbmux.
- **Stage 2 — Mac wired receiver + headless self-test (verifiable here).** Add `--wired`:
  reconnect loop → framed reader → the *existing* `StreamingVAD` → `_dispatch_utterance` →
  `run_pipeline` (laptop speaks + screen updates; echo/barge-in guard reused unchanged).
  Add `--selftest-wired`: a local "fake phone" TCP loopback streams a tone through the
  framing into a stubbed pipeline — mirrors `--selftest-wire`, no device.
- **Stage 3 — iOS audio uplink.** New `AudioUplinkServer` reusing `AudioPublisher`'s exact
  capture (HFP pin, Float32 mono, route/interruption observers); only the sink swaps from
  `URLSessionWebSocketTask.send` → framed `NWConnection.send`. A `wiredMode` switch in
  `startAudioOnly()` picks WS (wifi) vs wired. Added as a NEW class so the working WS path
  is untouched.
- **Stage 4 — on-device end-to-end (you).** Wi-Fi + Cellular OFF, Bluetooth ON (glasses),
  cable in, forwarder running, `python src/glasses_bridge.py --wired`. Speak a fault →
  laptop speaks the SOP + screen updates. Fully air-gapped over the wire.
- **Stage 5 — video feed over the wire — ✅ IMPLEMENTED.** iOS `VideoUplinkServer`
  (NWListener on :8767, frames JPEG as `[0x02][len][jpeg]`); in `wiredMode` the **camera**
  path (`startSession`, NOT the audio-only path) pushes frames there instead of the `/publish`
  WS. The Mac `--wired` bridge runs a second connect loop reading JPEG into the **same
  `_latest_jpeg` buffer** `/glasses.mjpeg` already serves — so the operator UI shows it with
  zero display changes (Mac receiver headless-verified in `--selftest-wired`). Run a **second**
  forwarder: `pymobiledevice3 usbmux forward 8767 8767` (pmd3 = one pair per process → two
  terminals). A separate port keeps video bursts off the audio/VAD path. **Requires camera
  streaming mode** on the app (the glasses camera must be live — the audio-only hands-free
  path has no camera).

## Files
- `src/glasses_bridge.py` — `--wired` + `--selftest-wired` (+ `--probe-wired`); additive,
  WS path untouched.
- `ios/ManuAI/ViewModels/StreamSessionViewModel.swift` — `AudioUplinkServer` (NWListener +
  framing) + `wiredMode` switch; additive, WS path untouched.
- `docs/` — a short wired-USB run note.
- `Info.plist` — *probably nothing*: already has `NSAllowsLocalNetworking` +
  `NSLocalNetworkUsageDescription`, and usbmux delivers as loopback. Only touch it if a
  Local Network prompt actually appears on device.

## Verification split
- **Here (headless):** `--selftest-wired` (framing + VAD + pipeline stub); confirm no
  regression to the existing `--selftest-wire` / WS path.
- **Your machine:** Stage 1 tunnel probe + Stage 4 end-to-end (needs Xcode, a device, the
  cable, pymobiledevice3 — none runnable in a sandbox).

## Watch-fors (noted, not pre-solved)
- `NWListener` accepts only in the **foreground** — fine for a demo (keep the app open).
- The reconnect loop recovers **app background→foreground** transparently, but NOT a **cable
  unplug**: that kills the out-of-process forwarder, so recovery = manually restart
  `pymobiledevice3 usbmux forward` (the bridge can't revive it).
- usbmux delivers to the phone's listener as a **loopback** connection → does NOT trip the
  iOS Local Network prompt, and `Info.plist` already has the keys (validator-confirmed).
- **Stage 5 needs TWO forwarder processes** — `pymobiledevice3 usbmux forward` carries one
  port-pair per invocation, so audio (8766) and video (8767) each get their own terminal.
- The one stock alternative that keeps the phone DIALING OUT (less iOS change) is
  **Bonjour-over-USB** — Apple's *supported* path. Rejected here for usbmux's more
  deterministic discovery, but it's the fallback if role-inversion proves too costly.
- Confirm plugging in USB doesn't perturb the **BT-HFP mic** route (different transports;
  shouldn't). Confirm `pymobiledevice3 usbmux forward` arg order / `--serial` at build time.

## How this composes with the feed (this branch's base)
The feed (`raybans-live-feed`) is a **display** change over whatever transport delivers
bytes. This branch swaps the **transport** (WebSocket-over-IP → raw-TCP-over-usbmux). They
compose cleanly: once the tunnel works, the feed rides it (Stage 5). The WS/wifi path stays
intact behind the `wiredMode` flag, so the current same-wifi demo can't regress.

## Open questions for a validator
1. Does usbmux actually deliver an inbound connection to a sandboxed app's `NWListener`
   (loopback on the device)? — the load-bearing unknown; Stage 1 is designed to answer it.
2. `pymobiledevice3 usbmux forward` exact CLI (arg order, `--serial`, multiple concurrent
   connections for Stage 5's second port)?
3. Does an `NWListener` on iOS need `NSLocalNetworkUsageDescription` for usbmux-delivered
   (loopback) connections, or only for real LAN/Bonjour?
4. Reusing `AudioPublisher`'s capture for the uplink server — factor a shared tap, or
   duplicate the small setup to avoid touching the working WS class?
5. Reconnect/teardown semantics when the phone app foregrounds/backgrounds or the cable is
   unplugged mid-stream.
