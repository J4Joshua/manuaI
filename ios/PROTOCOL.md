# Glasses Streaming Protocol

The semantic contract between the iOS glasses app and its server. This is
transport-independent: it describes *what* flows and *what* control messages
exist. Section 1 is the current (proven) WebSocket transport. Section 2 maps
the same contract onto WebRTC, the target transport for the template.

The host is currently hardcoded at
`RayBan/ViewModels/StreamSessionViewModel.swift:16`
(`ws://10.10.10.121:57308`) and must become configurable in the template.

---

## 1. Current transport — raw WebSocket + HTTP (what exists today)

### 1.1 Video uplink — `ws /publish`
- iOS captures `VideoFrame` from DAT (`VideoCodec.raw`, 24fps), converts to
  `UIImage`, JPEG-encodes at quality 0.5, sends each frame as a **binary**
  WebSocket message.
- Only sent while `videoEnabled == true`.

**Control messages on this same socket (JSON text frames):**
- iOS → server: `{"type":"pause"}`, `{"type":"resume"}`
- server → iOS: `{"type":"video_off"}`, `{"type":"video_on"}`,
  `{"type":"capture_photo","request_id":"<id>"}`

### 1.2 Audio uplink — `ws /publish-audio?agent=1`
- First message: JSON header `{"sampleRate":48000,"channels":1}` (rate is the
  glasses' actual input rate, sent dynamically).
- Subsequent messages: raw **Float32 LE mono PCM** binary frames.
- The `agent=1` query flag tells the server to bridge this audio into the
  voice agent.

### 1.3 Audio downlink — `ws /agent-audio`
- Server sends JSON header `{"sampleRate":24000}`, then **Int16 LE mono PCM @
  24kHz** binary frames (the voice agent talking back).
- iOS plays these through the glasses speaker over HFP.

### 1.4 Photo — `POST /publish/photo`
- Full-resolution JPEG body, `Content-Type: image/jpeg`.
- `X-Request-Id` header echoes the `request_id` from a `capture_photo` control
  message (when the capture was remote-triggered).

---

## 2. Target transport — WebRTC (template)

One peer connection per session. Recommended: a LiveKit (or comparable SFU)
"room" rather than hand-rolled signaling + TURN.

### 2.1 Video → outbound video track
- Custom video source: `VideoFrame` → `CVPixelBuffer` → `RTCVideoFrame` (or
  LiveKit buffer capturer). **No JPEG re-encode** — WebRTC encodes VP8/H.264.
- The `videoEnabled` toggle becomes muting/unmuting the track (or a DataChannel
  message — see 2.4).

### 2.2 Audio uplink → outbound audio track
- WebRTC's audio device module captures the system input route, which is the
  glasses HFP mic. The current AVAudioEngine tap is likely removed.
- No manual sample-rate header — SDP negotiates Opus.

### 2.3 Audio downlink → inbound audio track
- The agent's audio arrives as a remote track and auto-plays to the output
  route (glasses speaker). The `AgentAudioPlayer` class is likely removed.

### 2.4 Control → DataChannel
- Same messages as 1.1, sent as JSON over a reliable DataChannel:
  `pause` / `resume` / `video_off` / `video_on` / `capture_photo`.

### 2.5 Photo → keep HTTP `POST /publish/photo`
- Stills are not real-time media; leave this on HTTP exactly as today.

### 2.6 Open risk to de-risk first
- WebRTC wants to own `AVAudioSession`. DAT's HFP routing is delicate (today's
  code adds the camera stream *before* activating HFP). Spike a single bidi
  WebRTC audio track over HFP alongside a live DAT camera stream before
  building the rest.
