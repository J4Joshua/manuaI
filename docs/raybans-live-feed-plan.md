# Plan — Ray-Ban live feed → operator UI

**Worktree:** `../manuai-raybans` · **Branch:** `raybans-live-feed` (off `latency-improvements`)
**Status:** IMPLEMENTED + verified — `--selftest-wire` 7/7 PASS (incl. `/publish` `video_on`
+ `/glasses.jpg` round-trip + `/state` 200 on the swapped `_ScreenHandler`); MJPEG multipart
framing smoke PASS; **and rendered live in headless Chrome** against a frame-cycling harness —
`operator.html?poll=1` shows the animated feed (screenshot `/tmp/op_fixed.png`). All 5
validator findings folded in.

Extra fix found during the browser check: `.cam-stage [hidden] { display:none !important }`.
`.cam-placeholder`/`.cam-overlay` set `display:flex`, which overrode the UA `[hidden]{display:none}`,
so `showLive()`'s `placeholder.hidden=true` never hid the opaque placeholder (it covered the
feed). Latent in the getUserMedia path too; only manifested once the feed relied on it.

Browser caveat: multipart `<img>` is reliable in Chrome/Firefox, **flaky in Safari** — if the
demo console must run in Safari, repoint the feed to ~150 ms `/glasses.jpg` polling (the probe
+ endpoint are already built). iOS must be in **camera streaming** mode (audio-only has no camera).

## Goal
Show the glasses' first-person camera feed live in the existing **"Ray-Ban Live Feed"**
panel of `operator.html`, replacing the `getUserMedia` laptop-webcam **stand-in**, fed by
the JPEG frames the iOS app **already sends over `/publish`** (which the bridge currently
discards). The display half is **transport-agnostic** — it works over the existing
WebSocket on wifi/hotspot *today*, and the same frames ride the USB/usbmux tunnel later
(a 2nd forwarded port), so this is not blocked on the wired work.

## What already exists (don't rebuild)
- **`web/operator.html:546–567`** — the panel: `<video id="cam-video">`, `#cam-placeholder`
  ("Pairing with Ray-Ban glasses…"), `#cam-vignette`, `#cam-overlay` (the "Live" tag + ts).
- **`web/static/operator.js:132–199` `setupCamera()`** — `showLive()` / `showDemoFeed()` /
  `showPlaceholder(denied)`. Today: `demoMode → showDemoFeed`; else `getUserMedia` →
  `video.srcObject` + `showLive` ("getUserMedia stand-in"); else `showPlaceholder`.
  **In pollMode (the glasses bridge) it falls into the getUserMedia branch → shows the
  laptop webcam, not the glasses.**
- **iOS `StreamSessionViewModel.swift`** — `handleVideoFrame` already JPEG-encodes frames
  (`jpegData(0.5)`) and ships them over `/publish` when `videoEnabled` is true (the **camera
  streaming** path, not audio-only). `video_on`/`video_off` control already flips `videoEnabled`.
- **`src/glasses_bridge.py:368–376` `handle_publish`** — currently sends `{"type":"video_off"}`
  on connect and **drains/discards** all frames.

## Changes

### 1. `src/glasses_bridge.py` — ingest + serve frames
- New module state: `_latest_jpeg: bytes|None = None`, `_latest_jpeg_seq = 0`,
  `_jpeg_lock = threading.Lock()`.
- `handle_publish(ws)`:
  - On connect send `{"type":"video_on"}` (was `video_off`) so the app keeps streaming.
  - For each **binary** message → under `_jpeg_lock`: store bytes, bump `_latest_jpeg_seq`.
    Text/control JSON: ignore as today.
- `_ScreenHandler.do_GET` (the `:8000` screen server) — add two routes:
  - `GET /glasses.mjpeg` → `multipart/x-mixed-replace; boundary=frame` **streaming** response.
    Loop: read latest bytes+seq under lock; when seq changes, write
    `--frame\r\nContent-Type: image/jpeg\r\nContent-Length: N\r\n\r\n<bytes>\r\n` and
    `self.wfile.flush()`; `time.sleep(~1/25)`. Exit cleanly on `BrokenPipeError`/
    `ConnectionResetError` (browser closed). Written manually via `self.wfile` (bypasses
    `_send`, which sets a single Content-Length).
  - `GET /glasses.jpg` → latest single frame (200 + `image/jpeg`) or 503 if none yet
    (debug/fallback + used by the headless test).
- `ThreadingHTTPServer` already backs the screen server, so a long-lived MJPEG response
  holds one worker thread per client — fine for 1–2 browsers in a demo.

### 2. `web/operator.html` — an `<img>` for the MJPEG feed
- Add `<img id="cam-img" hidden>` inside `.cam-stage` (MJPEG plays in `<img>`, not `<video>`).
- CSS: `.cam-stage img { width:100%; height:100%; object-fit:cover; display:block; }`.

### 3. `web/static/operator.js` — drive the panel from the glasses feed in pollMode
- In `setupCamera()`, add a branch **before** the getUserMedia branch:
  - `if (pollMode)`: show `#cam-img`, hide `#cam-video`; set `camImg.src = "/glasses.mjpeg"`;
    `onload → showLive()` + `log("Glasses feed active")`; `onerror → showPlaceholder(false)`
    then retry (re-set `src` after ~1.5 s) so it pairs once the app starts streaming.
  - Leave the getUserMedia branch for non-poll/non-demo (LiveKit `live` w/o glasses) and
    `demoMode` **unchanged**.

### 4. `src/glasses_bridge.py` — update + extend the headless self-test
- `_check_publish_endpoint`: change the assertion `video_off → video_on`; after sending a
  fake JPEG over `/publish`, assert `GET :8000/glasses.jpg` returns 200 + `image/jpeg` with
  the same bytes. Keeps `--selftest-wire` / `--selftest` green and proves the round-trip.

### 5. Docs
- Note (here + glasses-bridge skill) that the bridge now relays video to the operator UI and
  that a feed requires the iOS app in **camera streaming mode** (audio-only has no camera).

## Decisions / tradeoffs
- **MJPEG on a 2nd `:8000` endpoint** — least code, no deps, smooth, plays in a plain `<img>`
  (vs. WebSocket/canvas push = more JS; vs. JPEG polling = choppier).
- **Reverses the bridge's deliberate `video_off`** (BT-bandwidth saving). Intended — the feed
  is the goal; trusted-LAN/wired so the cost is moot.
- **No iOS code change** for the feed; the wired-transport work is separate and this display
  half is transport-agnostic.
- A feed **requires camera streaming mode** on the app (audio-only path starts no camera).

## Verification
- **Here (headless, no models):** updated `_check_publish_endpoint` via `--selftest-wire`
  — JPEG round-trips bridge `/publish` → screen-server `/glasses.jpg`.
- **User machine:** open `operator.html?poll=1`, run the iOS **camera streaming** mode pointed
  at the bridge → live glasses feed fills the panel; placeholder shows until the first frame.

## Open questions for the validator
1. Does `ThreadingHTTPServer` + `BaseHTTPRequestHandler` cleanly support a long-lived
   `multipart/x-mixed-replace` response (manual `wfile` writes + flush, client-disconnect
   handling that doesn't kill the server)? Any gotcha vs. spawning a dedicated thread?
2. Is `pollMode` in scope inside `setupCamera()`, and does the new branch leave the LiveKit
   `live`/`demo` paths untouched?
3. Confirm the iOS app re-enables sending on `video_on` (`videoEnabled=true`) and only emits
   JPEG in the camera path — i.e. the user must use camera mode, not audio-only.
4. Any CSP / same-origin issue with `<img src="/glasses.mjpeg">` in operator.html?
5. New routes vs. the existing static-file path-guard in `_ScreenHandler.do_GET` — no collision?
