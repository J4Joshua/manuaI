/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

import AVFoundation
import MWDATCamera
import MWDATCore
import Network
import Observation
import SwiftUI

// ⚙️ CONFIG — the one knob to point the app at ManuAI's glasses_bridge.py.
// This is the bridge's AUDIO WebSocket port (8766); the on-screen SOP card is a
// SEPARATE HTTP server on :8000 — do not point here at :8000. Set this to the
// LAN IP of the Mac running `python src/glasses_bridge.py`:
//   • Same-WiFi dev:  ws://<mac-ip>:8766   (find it on the Mac: `ipconfig getifaddr en0`)
//   • Offline demo:   iPhone Personal Hotspot → the Mac gets a 172.20.10.x address →
//                     ws://172.20.10.x:8766  (no WAN — keeps the wifi-off headline)
// Plaintext ws:// to these private ranges is allowed by NSAllowsLocalNetworking
// (Info.plist) — no ATS change needed.
private let streamPublishHost = "ws://10.0.0.98:8766"

// Wired (usbmux) transport — flip ON for the air-gapped USB demo (Wi-Fi + Cellular OFF,
// Bluetooth ON for the glasses). When true, the audio-only path runs an NWListener TCP
// SERVER on `wiredListenPort` and the Mac connects IN over the cable via
// `pymobiledevice3 usbmux forward <local> <wiredListenPort>`, instead of dialing the
// WebSocket above. The camera/video path still uses the WebSocket. Requires the device
// paired/trusted with Developer Mode ON (iOS 16+). See docs/usbmux-tunnel-plan.md.
private let wiredMode = true   // ON for the air-gapped USB demo (set false to use the WebSocket path)
private let wiredListenPort: UInt16 = 8766
private let wiredVideoPort: UInt16 = 8767     // Stage 5: Ray-Ban camera feed (camera streaming mode)

private func publishURL(path: String) -> URL {
  URL(string: "\(streamPublishHost)\(path)")!
}

private func publishHTTPURL(path: String) -> URL {
  let httpHost = streamPublishHost
    .replacingOccurrences(of: "wss://", with: "https://")
    .replacingOccurrences(of: "ws://", with: "http://")
  return URL(string: "\(httpHost)\(path)")!
}

enum StreamingStatus {
  case streaming
  case waiting
  case stopped
}

private final class StreamPublisher {
  private let queue = DispatchQueue(label: "stream.publisher")
  private var task: URLSessionWebSocketTask?
  private var sentCount = 0
  private var paused = false

  /// Invoked on the main thread when the server pushes a JSON control text
  /// frame (e.g. `{"type":"video_on"}`). Only the `type` field is forwarded.
  var onControl: (@MainActor (String) -> Void)?

  /// Invoked when the server asks for a high-res still via
  /// `{"type":"capture_photo","request_id":"..."}`. The request_id is
  /// echoed back as an `X-Request-Id` header on the upload so the server's
  /// awaiting request resolves.
  var onCapturePhoto: (@MainActor (String) -> Void)?

  func start(url: URL) {
    queue.async {
      print("[StreamPublisher] start \(url)")
      let t = URLSession.shared.webSocketTask(with: url)
      t.resume()
      self.task = t
      self.sentCount = 0
      self.paused = false
      self.drain(t)
    }
  }

  func stop() {
    queue.async {
      print("[StreamPublisher] stop (sent=\(self.sentCount))")
      self.task?.cancel(with: .normalClosure, reason: nil)
      self.task = nil
      self.paused = false
    }
  }

  func pause() {
    queue.async {
      guard !self.paused else { return }
      self.paused = true
      self.sendControl("pause")
    }
  }

  func resume() {
    queue.async {
      guard self.paused else { return }
      self.paused = false
      self.sendControl("resume")
    }
  }

  private func sendControl(_ type: String) {
    guard let task = self.task else { return }
    let payload = "{\"type\":\"\(type)\"}"
    task.send(.string(payload)) { error in
      if let error { print("[StreamPublisher] control \(type) send error: \(error)") }
    }
    print("[StreamPublisher] control \(type)")
  }

  func send(_ data: Data) {
    queue.async {
      guard let task = self.task, !self.paused else { return }
      task.send(.data(data)) { error in
        if let error {
          print("[StreamPublisher] send error: \(error)")
        }
      }
      self.sentCount += 1
      if self.sentCount == 1 || self.sentCount % 30 == 0 {
        print("[StreamPublisher] sent #\(self.sentCount) bytes=\(data.count)")
      }
    }
  }

  private func drain(_ task: URLSessionWebSocketTask) {
    task.receive { [weak self] result in
      guard let self else { return }
      switch result {
      case .success(let message):
        if case .string(let text) = message {
          self.handleControl(text)
        }
        self.drain(task)
      case .failure(let err):
        print("[StreamPublisher] receive failure: \(err)")
        self.queue.async { if self.task === task { self.task = nil } }
      }
    }
  }

  private func handleControl(_ text: String) {
    guard let data = text.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let type = obj["type"] as? String else {
      print("[StreamPublisher] non-JSON server text: \(text)")
      return
    }
    print("[StreamPublisher] control from server: \(type)")
    if type == "capture_photo", let requestId = obj["request_id"] as? String {
      Task { @MainActor in self.onCapturePhoto?(requestId) }
      return
    }
    Task { @MainActor in self.onControl?(type) }
  }
}

/// Captures audio from the current input route (Bluetooth HFP when glasses are
/// the active audio source) and ships Float32 mono PCM over a WebSocket.
private final class AudioPublisher {
  private let queue = DispatchQueue(label: "audio.publisher")
  private var task: URLSessionWebSocketTask?
  private let engine = AVAudioEngine()
  private var converter: AVAudioConverter?
  private var targetFormat: AVAudioFormat?
  private var headerSent = false
  private var sentChunks = 0
  private var configChangeObserver: NSObjectProtocol?
  private var routeChangeObserver: NSObjectProtocol?
  private var interruptionObserver: NSObjectProtocol?
  private var mediaServicesLostObserver: NSObjectProtocol?
  private var mediaServicesResetObserver: NSObjectProtocol?
  private var paused = false

  /// Configure the HFP audio session. Per Meta DAT docs, do this BEFORE
  /// starting the video stream and allow HFP time to negotiate.
  ///
  /// `.defaultToSpeaker` overrides `.playAndRecord`'s built-in-receiver
  /// default so any playback (agent voice) is audible. It only
  /// applies when no BT output route is connected, so it's harmless when
  /// the glasses are paired and HFP/A2DP is active.
  static func configureAudioSession() {
    let session = AVAudioSession.sharedInstance()
    do {
      try session.setCategory(
        .playAndRecord,
        mode: .default,
        options: [.allowBluetooth, .allowBluetoothA2DP, .defaultToSpeaker]
      )
      try session.setActive(true, options: .notifyOthersOnDeactivation)
      if let hfp = session.availableInputs?.first(where: { $0.portType == .bluetoothHFP }) {
        try session.setPreferredInput(hfp)
        print("[AudioPublisher] pinned input to \(hfp.portName)")
      } else {
        let available = session.availableInputs?.map { "\($0.portName)(\($0.portType.rawValue))" } ?? []
        print("[AudioPublisher] no BluetoothHFP input yet; available=\(available)")
      }
      let outputs = session.currentRoute.outputs.map { "\($0.portName)(\($0.portType.rawValue))" }
      print("[AudioPublisher] audio session configured outputs=\(outputs)")
    } catch {
      print("[AudioPublisher] session error: \(error)")
    }
  }

  func start(url: URL) {
    queue.async {
      print("[AudioPublisher] start \(url)")

      let t = URLSession.shared.webSocketTask(with: url)
      t.resume()
      self.task = t
      self.headerSent = false
      self.sentChunks = 0
      self.drain(t)

      self.installTapAndStartEngine()
      self.installSystemObservers()
    }
  }

  private func installTapAndStartEngine() {
    let input = engine.inputNode
    let inFormat = input.outputFormat(forBus: 0)
    guard inFormat.sampleRate > 0 else {
      print("[AudioPublisher] input format not ready, sampleRate=\(inFormat.sampleRate)")
      return
    }
    guard let outFormat = AVAudioFormat(
      commonFormat: .pcmFormatFloat32,
      sampleRate: inFormat.sampleRate,
      channels: 1,
      interleaved: false
    ) else {
      print("[AudioPublisher] could not create output format")
      return
    }
    targetFormat = outFormat
    converter = AVAudioConverter(from: inFormat, to: outFormat)
    headerSent = false

    input.removeTap(onBus: 0)
    input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] buffer, _ in
      self?.handle(buffer)
    }

    do {
      try engine.start()
      print("[AudioPublisher] engine started, sampleRate=\(inFormat.sampleRate) channels=\(inFormat.channelCount)")
    } catch {
      print("[AudioPublisher] engine start error: \(error)")
    }
  }

  private func installSystemObservers() {
    let nc = NotificationCenter.default
    configChangeObserver = nc.addObserver(
      forName: .AVAudioEngineConfigurationChange,
      object: engine,
      queue: nil
    ) { [weak self] _ in
      guard let self else { return }
      print("[AudioPublisher] engine configuration changed — restarting tap")
      self.queue.async {
        self.engine.inputNode.removeTap(onBus: 0)
        if self.engine.isRunning { self.engine.stop() }
        self.installTapAndStartEngine()
      }
    }
    routeChangeObserver = nc.addObserver(
      forName: AVAudioSession.routeChangeNotification,
      object: nil,
      queue: nil
    ) { [weak self] note in
      let reason = (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt).flatMap(AVAudioSession.RouteChangeReason.init(rawValue:))
      let inputs = AVAudioSession.sharedInstance().currentRoute.inputs.map { "\($0.portName)(\($0.portType.rawValue))" }
      print("[AudioPublisher] route changed reason=\(reason.map(String.init(describing:)) ?? "?") inputs=\(inputs)")
      // Re-pin HFP if it's available but isn't the current input.
      let session = AVAudioSession.sharedInstance()
      if let hfp = session.availableInputs?.first(where: { $0.portType == .bluetoothHFP }),
         session.currentRoute.inputs.contains(where: { $0.portType == .bluetoothHFP }) == false {
        try? session.setPreferredInput(hfp)
        print("[AudioPublisher] re-pinned input to \(hfp.portName)")
        self?.queue.async {
          guard let self else { return }
          self.engine.inputNode.removeTap(onBus: 0)
          if self.engine.isRunning { self.engine.stop() }
          self.installTapAndStartEngine()
        }
      }
    }
    interruptionObserver = nc.addObserver(
      forName: AVAudioSession.interruptionNotification,
      object: AVAudioSession.sharedInstance(),
      queue: nil
    ) { [weak self] note in
      guard let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt)
        .flatMap(AVAudioSession.InterruptionType.init(rawValue:)) else { return }
      print("[AudioPublisher] interruption \(type == .began ? "began" : "ended")")
      guard let self else { return }
      self.queue.async {
        switch type {
        case .began:
          if self.engine.isRunning { self.engine.pause() }
        case .ended:
          let opts = (note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt).map(AVAudioSession.InterruptionOptions.init(rawValue:))
          if opts?.contains(.shouldResume) == true {
            try? self.engine.start()
          }
        @unknown default: break
        }
      }
    }
    mediaServicesLostObserver = nc.addObserver(
      forName: AVAudioSession.mediaServicesWereLostNotification,
      object: AVAudioSession.sharedInstance(),
      queue: nil
    ) { _ in
      print("[AudioPublisher] media services were LOST")
    }
    mediaServicesResetObserver = nc.addObserver(
      forName: AVAudioSession.mediaServicesWereResetNotification,
      object: AVAudioSession.sharedInstance(),
      queue: nil
    ) { [weak self] _ in
      print("[AudioPublisher] media services were reset — rebuilding")
      guard let self else { return }
      self.queue.async {
        AudioPublisher.configureAudioSession()
        self.installTapAndStartEngine()
      }
    }
  }

  private func removeSystemObservers() {
    let nc = NotificationCenter.default
    if let o = configChangeObserver { nc.removeObserver(o) }
    if let o = routeChangeObserver { nc.removeObserver(o) }
    if let o = interruptionObserver { nc.removeObserver(o) }
    if let o = mediaServicesLostObserver { nc.removeObserver(o) }
    if let o = mediaServicesResetObserver { nc.removeObserver(o) }
    configChangeObserver = nil
    routeChangeObserver = nil
    interruptionObserver = nil
    mediaServicesLostObserver = nil
    mediaServicesResetObserver = nil
  }

  func stop() {
    queue.async {
      print("[AudioPublisher] stop (chunks=\(self.sentChunks))")
      self.removeSystemObservers()
      self.engine.inputNode.removeTap(onBus: 0)
      if self.engine.isRunning { self.engine.stop() }
      self.task?.cancel(with: .normalClosure, reason: nil)
      self.task = nil
      self.paused = false
      try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
  }

  func pause() {
    queue.async {
      guard !self.paused else { return }
      self.paused = true
      print("[AudioPublisher] pause")
    }
  }

  func resume() {
    queue.async {
      guard self.paused else { return }
      self.paused = false
      print("[AudioPublisher] resume")
    }
  }

  private func handle(_ buffer: AVAudioPCMBuffer) {
    queue.async {
      guard let task = self.task, let target = self.targetFormat else { return }

      if !self.headerSent {
        let header = "{\"sampleRate\":\(Int(target.sampleRate)),\"channels\":1}"
        task.send(.string(header)) { error in
          if let error { print("[AudioPublisher] header send error: \(error)") }
        }
        self.headerSent = true
      }

      if self.paused { return }

      // Fast path: input already matches our target (Float32 mono, same SR) —
      // just ship the channel data directly.
      let inFormat = buffer.format
      let sameFormat = inFormat.commonFormat == .pcmFormatFloat32
        && inFormat.channelCount == 1
        && inFormat.sampleRate == target.sampleRate
      if sameFormat, let channelData = buffer.floatChannelData?[0] {
        let byteCount = Int(buffer.frameLength) * MemoryLayout<Float>.size
        let data = Data(bytes: channelData, count: byteCount)
        task.send(.data(data)) { error in
          if let error { print("[AudioPublisher] send error: \(error)") }
        }
        self.sentChunks += 1
        if self.sentChunks == 1 || self.sentChunks % 50 == 0 {
          print("[AudioPublisher] sent #\(self.sentChunks) bytes=\(byteCount)")
        }
        return
      }

      // Slow path: format mismatch (multi-channel, different SR, Int16, etc.) — convert.
      guard let converter = self.converter else { return }
      let capacity = AVAudioFrameCount(Double(buffer.frameLength) * target.sampleRate / inFormat.sampleRate) + 64
      guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
      var error: NSError?
      var supplied = false
      let status = converter.convert(to: out, error: &error) { _, status in
        if supplied {
          // Do NOT signal .endOfStream — that permanently retires the converter.
          status.pointee = .noDataNow
          return nil
        }
        supplied = true
        status.pointee = .haveData
        return buffer
      }
      if status == .error || out.frameLength == 0 {
        print("[AudioPublisher] convert produced no data status=\(status.rawValue) outFrames=\(out.frameLength) inFrames=\(buffer.frameLength) error=\(error?.localizedDescription ?? "nil")")
        return
      }
      guard let channelData = out.floatChannelData?[0] else { return }
      let byteCount = Int(out.frameLength) * MemoryLayout<Float>.size
      let data = Data(bytes: channelData, count: byteCount)
      task.send(.data(data)) { error in
        if let error { print("[AudioPublisher] send error: \(error)") }
      }
      self.sentChunks += 1
      if self.sentChunks == 1 || self.sentChunks % 50 == 0 {
        print("[AudioPublisher] sent #\(self.sentChunks) bytes=\(byteCount)")
      }
    }
  }

  private func drain(_ task: URLSessionWebSocketTask) {
    task.receive { [weak self] result in
      guard let self else { return }
      switch result {
      case .success:
        self.drain(task)
      case .failure(let err):
        print("[AudioPublisher] receive failure: \(err)")
        self.queue.async { if self.task === task { self.task = nil } }
      }
    }
  }
}

/// Wired (usbmux) uplink: an NWListener TCP SERVER that the Mac connects INTO over the USB
/// cable (via `pymobiledevice3 usbmux forward`), so the glasses mic works with Wi-Fi +
/// Cellular OFF. The capture (HFP pin, Float32 mono, route/interruption handling) MIRRORS
/// AudioPublisher and must be kept in sync by hand — only the transport differs: framed
/// bytes over an NWConnection instead of a WebSocket. Frame = [1-byte type][4-byte
/// big-endian length][payload]: 0x00 = JSON header, 0x01 = Float32-LE mono PCM (the SAME
/// bytes AudioPublisher sends). See docs/usbmux-tunnel-plan.md.
///
/// NOTE: usbmux delivers the Mac's connection to the device as LOOPBACK, so the default
/// all-interfaces NWListener accepts it; we deliberately do NOT restrict to 127.0.0.1.
/// If NWListener ever refuses the usbmux connection, the documented fallback is a raw BSD
/// socket / GCDAsyncSocket listener (PeerTalk's approach) — see the plan's Stage 1.
private final class AudioUplinkServer {
  private let queue = DispatchQueue(label: "audio.uplink.server")
  private let engine = AVAudioEngine()
  private var converter: AVAudioConverter?
  private var targetFormat: AVAudioFormat?
  private var listener: NWListener?
  private var connection: NWConnection?
  private var headerSent = false
  private var sentChunks = 0
  private var paused = false
  private var configChangeObserver: NSObjectProtocol?
  private var routeChangeObserver: NSObjectProtocol?
  private var interruptionObserver: NSObjectProtocol?
  private var mediaServicesLostObserver: NSObjectProtocol?
  private var mediaServicesResetObserver: NSObjectProtocol?

  private static let kText: UInt8 = 0x00
  private static let kAudio: UInt8 = 0x01

  func start(port: UInt16) {
    queue.async {
      self.stopLocked()
      let params = NWParameters.tcp
      params.allowLocalEndpointReuse = true
      guard let nwPort = NWEndpoint.Port(rawValue: port),
            let listener = try? NWListener(using: params, on: nwPort) else {
        print("[AudioUplink] could not create NWListener on :\(port)")
        return
      }
      self.listener = listener
      listener.stateUpdateHandler = { state in
        print("[AudioUplink] listener state: \(state)")
      }
      listener.newConnectionHandler = { [weak self] conn in
        self?.queue.async { self?.accept(conn) }
      }
      listener.start(queue: self.queue)
      print("[AudioUplink] listening on TCP :\(port) — waiting for the Mac via usbmux")
    }
  }

  /// Caller runs on `queue`. One active uplink at a time: a new Mac connection replaces
  /// the prior one and (re)starts capture once the connection is ready.
  private func accept(_ conn: NWConnection) {
    print("[AudioUplink] Mac connected")
    connection?.cancel()
    connection = conn
    headerSent = false
    sentChunks = 0
    conn.stateUpdateHandler = { [weak self] state in
      guard let self else { return }
      self.queue.async {
        guard self.connection === conn else { return }
        switch state {
        case .ready:
          self.startCaptureLocked()
        case .failed, .cancelled:
          self.teardownCaptureLocked()
        default:
          break
        }
      }
    }
    conn.start(queue: queue)
  }

  func stop() {
    queue.async { self.stopLocked() }
  }

  /// Caller runs on `queue`.
  private func stopLocked() {
    print("[AudioUplink] stop (chunks=\(sentChunks))")
    teardownCaptureLocked()
    connection?.cancel()
    connection = nil
    listener?.cancel()
    listener = nil
    try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
  }

  /// Caller runs on `queue`. Idempotent: clears any prior tap + observers first, so a
  /// reconnecting Mac never double-installs or leaks the audio-session observers.
  private func startCaptureLocked() {
    teardownCaptureLocked()
    headerSent = false
    installTapAndStartEngine()
    installSystemObservers()
  }

  /// Caller runs on `queue`.
  private func teardownCaptureLocked() {
    removeSystemObservers()
    engine.inputNode.removeTap(onBus: 0)
    if engine.isRunning { engine.stop() }
  }

  func pause() { queue.async { self.paused = true; print("[AudioUplink] pause") } }
  func resume() { queue.async { self.paused = false; print("[AudioUplink] resume") } }

  private func frame(_ type: UInt8, _ payload: Data) -> Data {
    var out = Data([type])
    var len = UInt32(payload.count).bigEndian
    out.append(Data(bytes: &len, count: 4))
    out.append(payload)
    return out
  }

  /// Caller runs on `queue`.
  private func sendFrame(_ type: UInt8, _ payload: Data) {
    guard let conn = connection else { return }
    conn.send(content: frame(type, payload), completion: .contentProcessed { error in
      if let error { print("[AudioUplink] send error: \(error)") }
    })
  }

  private func installTapAndStartEngine() {
    let input = engine.inputNode
    let inFormat = input.outputFormat(forBus: 0)
    guard inFormat.sampleRate > 0 else {
      print("[AudioUplink] input format not ready, sampleRate=\(inFormat.sampleRate)")
      return
    }
    guard let outFormat = AVAudioFormat(
      commonFormat: .pcmFormatFloat32,
      sampleRate: inFormat.sampleRate,
      channels: 1,
      interleaved: false
    ) else {
      print("[AudioUplink] could not create output format")
      return
    }
    targetFormat = outFormat
    converter = AVAudioConverter(from: inFormat, to: outFormat)
    headerSent = false

    input.removeTap(onBus: 0)
    input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { [weak self] buffer, _ in
      self?.handle(buffer)
    }

    do {
      try engine.start()
      print("[AudioUplink] engine started, sampleRate=\(inFormat.sampleRate) channels=\(inFormat.channelCount)")
    } catch {
      print("[AudioUplink] engine start error: \(error)")
    }
  }

  private func installSystemObservers() {
    let nc = NotificationCenter.default
    configChangeObserver = nc.addObserver(
      forName: .AVAudioEngineConfigurationChange,
      object: engine,
      queue: nil
    ) { [weak self] _ in
      guard let self else { return }
      print("[AudioUplink] engine configuration changed — restarting tap")
      self.queue.async {
        self.engine.inputNode.removeTap(onBus: 0)
        if self.engine.isRunning { self.engine.stop() }
        self.installTapAndStartEngine()
      }
    }
    routeChangeObserver = nc.addObserver(
      forName: AVAudioSession.routeChangeNotification,
      object: nil,
      queue: nil
    ) { [weak self] _ in
      let session = AVAudioSession.sharedInstance()
      if let hfp = session.availableInputs?.first(where: { $0.portType == .bluetoothHFP }),
         session.currentRoute.inputs.contains(where: { $0.portType == .bluetoothHFP }) == false {
        try? session.setPreferredInput(hfp)
        print("[AudioUplink] re-pinned input to \(hfp.portName)")
        self?.queue.async {
          guard let self else { return }
          self.engine.inputNode.removeTap(onBus: 0)
          if self.engine.isRunning { self.engine.stop() }
          self.installTapAndStartEngine()
        }
      }
    }
    interruptionObserver = nc.addObserver(
      forName: AVAudioSession.interruptionNotification,
      object: AVAudioSession.sharedInstance(),
      queue: nil
    ) { [weak self] note in
      guard let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt)
        .flatMap(AVAudioSession.InterruptionType.init(rawValue:)) else { return }
      guard let self else { return }
      self.queue.async {
        switch type {
        case .began:
          if self.engine.isRunning { self.engine.pause() }
        case .ended:
          let opts = (note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt).map(AVAudioSession.InterruptionOptions.init(rawValue:))
          if opts?.contains(.shouldResume) == true {
            try? self.engine.start()
          }
        @unknown default: break
        }
      }
    }
    mediaServicesLostObserver = nc.addObserver(
      forName: AVAudioSession.mediaServicesWereLostNotification,
      object: AVAudioSession.sharedInstance(),
      queue: nil
    ) { _ in
      print("[AudioUplink] media services were LOST")
    }
    mediaServicesResetObserver = nc.addObserver(
      forName: AVAudioSession.mediaServicesWereResetNotification,
      object: AVAudioSession.sharedInstance(),
      queue: nil
    ) { [weak self] _ in
      print("[AudioUplink] media services were reset — rebuilding")
      guard let self else { return }
      self.queue.async {
        AudioPublisher.configureAudioSession()
        self.installTapAndStartEngine()
      }
    }
  }

  private func removeSystemObservers() {
    let nc = NotificationCenter.default
    if let o = configChangeObserver { nc.removeObserver(o) }
    if let o = routeChangeObserver { nc.removeObserver(o) }
    if let o = interruptionObserver { nc.removeObserver(o) }
    if let o = mediaServicesLostObserver { nc.removeObserver(o) }
    if let o = mediaServicesResetObserver { nc.removeObserver(o) }
    configChangeObserver = nil
    routeChangeObserver = nil
    interruptionObserver = nil
    mediaServicesLostObserver = nil
    mediaServicesResetObserver = nil
  }

  private func handle(_ buffer: AVAudioPCMBuffer) {
    queue.async {
      guard self.connection != nil, let target = self.targetFormat else { return }

      if !self.headerSent {
        let header = "{\"sampleRate\":\(Int(target.sampleRate)),\"channels\":1}"
        self.sendFrame(Self.kText, Data(header.utf8))
        self.headerSent = true
      }

      if self.paused { return }

      // Fast path: input already Float32 mono at the target rate — ship channel data.
      let inFormat = buffer.format
      let sameFormat = inFormat.commonFormat == .pcmFormatFloat32
        && inFormat.channelCount == 1
        && inFormat.sampleRate == target.sampleRate
      if sameFormat, let channelData = buffer.floatChannelData?[0] {
        let byteCount = Int(buffer.frameLength) * MemoryLayout<Float>.size
        self.sendFrame(Self.kAudio, Data(bytes: channelData, count: byteCount))
        self.sentChunks += 1
        if self.sentChunks == 1 || self.sentChunks % 50 == 0 {
          print("[AudioUplink] sent #\(self.sentChunks) bytes=\(byteCount)")
        }
        return
      }

      // Slow path: convert (multi-channel / different SR / Int16, etc.).
      guard let converter = self.converter else { return }
      let capacity = AVAudioFrameCount(Double(buffer.frameLength) * target.sampleRate / inFormat.sampleRate) + 64
      guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
      var error: NSError?
      var supplied = false
      let status = converter.convert(to: out, error: &error) { _, status in
        if supplied {
          status.pointee = .noDataNow   // do NOT signal .endOfStream — that retires the converter
          return nil
        }
        supplied = true
        status.pointee = .haveData
        return buffer
      }
      if status == .error || out.frameLength == 0 {
        print("[AudioUplink] convert produced no data status=\(status.rawValue) error=\(error?.localizedDescription ?? "nil")")
        return
      }
      guard let channelData = out.floatChannelData?[0] else { return }
      let byteCount = Int(out.frameLength) * MemoryLayout<Float>.size
      self.sendFrame(Self.kAudio, Data(bytes: channelData, count: byteCount))
      self.sentChunks += 1
      if self.sentChunks == 1 || self.sentChunks % 50 == 0 {
        print("[AudioUplink] sent #\(self.sentChunks) bytes=\(byteCount)")
      }
    }
  }
}

/// Wired (usbmux) video uplink (Stage 5): an NWListener TCP server the Mac connects into
/// over the cable for the Ray-Ban camera feed. `send(_:)` frames each JPEG as
/// [0x02][big-endian len][jpeg]; the bridge stores the latest into the same `_latest_jpeg`
/// buffer `/glasses.mjpeg` serves to the operator UI. The camera path pushes frames here in
/// wiredMode instead of the `/publish` WebSocket. See docs/usbmux-tunnel-plan.md (Stage 5).
private final class VideoUplinkServer {
  private let queue = DispatchQueue(label: "video.uplink.server")
  private var listener: NWListener?
  private var connection: NWConnection?
  private var sentCount = 0
  private static let kJpeg: UInt8 = 0x02

  func start(port: UInt16) {
    queue.async {
      self.stopLocked()
      let params = NWParameters.tcp
      params.allowLocalEndpointReuse = true
      guard let nwPort = NWEndpoint.Port(rawValue: port),
            let listener = try? NWListener(using: params, on: nwPort) else {
        print("[VideoUplink] could not create NWListener on :\(port)")
        return
      }
      self.listener = listener
      listener.stateUpdateHandler = { state in print("[VideoUplink] listener state: \(state)") }
      listener.newConnectionHandler = { [weak self] conn in self?.queue.async { self?.accept(conn) } }
      listener.start(queue: self.queue)
      print("[VideoUplink] listening on TCP :\(port) — waiting for the Mac via usbmux")
    }
  }

  /// Caller runs on `queue`.
  private func accept(_ conn: NWConnection) {
    print("[VideoUplink] Mac connected")
    connection?.cancel()
    connection = conn
    sentCount = 0
    conn.stateUpdateHandler = { [weak self] state in
      guard let self else { return }
      switch state {
      case .failed, .cancelled:
        self.queue.async { if self.connection === conn { self.connection = nil } }
      default:
        break
      }
    }
    conn.start(queue: queue)
  }

  /// Push one JPEG frame (from the camera frame callback). Dropped if no Mac is connected
  /// yet — video frames are disposable.
  func send(_ jpeg: Data) {
    queue.async {
      guard let conn = self.connection else { return }
      var out = Data([Self.kJpeg])
      var len = UInt32(jpeg.count).bigEndian
      out.append(Data(bytes: &len, count: 4))
      out.append(jpeg)
      conn.send(content: out, completion: .contentProcessed { error in
        if let error { print("[VideoUplink] send error: \(error)") }
      })
      self.sentCount += 1
      if self.sentCount == 1 || self.sentCount % 30 == 0 {
        print("[VideoUplink] sent #\(self.sentCount) bytes=\(jpeg.count)")
      }
    }
  }

  func stop() { queue.async { self.stopLocked() } }

  /// Caller runs on `queue`.
  private func stopLocked() {
    connection?.cancel()
    connection = nil
    listener?.cancel()
    listener = nil
  }
}

/// Plays PCM frames received from the server-side voice agent. The
/// server pushes Int16 LE PCM (24 kHz mono) after a JSON header. We schedule
/// them on an AVAudioPlayerNode whose output routes via the shared
/// `.playAndRecord` HFP session — i.e. out through the glasses' speakers.
private final class AgentAudioPlayer {
  private let queue = DispatchQueue(label: "agent.audio.player")
  private var task: URLSessionWebSocketTask?
  private let engine = AVAudioEngine()
  private let player = AVAudioPlayerNode()
  private var playerFormat: AVAudioFormat?
  private var receivedChunks = 0

  func start(url: URL) {
    queue.async {
      self.teardownLocked()
      print("[AgentAudioPlayer] start \(url)")
      let t = URLSession.shared.webSocketTask(with: url)
      t.resume()
      self.task = t
      self.receivedChunks = 0
      self.receive(t)
    }
  }

  func stop() {
    queue.async {
      print("[AgentAudioPlayer] stop (chunks=\(self.receivedChunks))")
      self.teardownLocked()
    }
  }

  /// Caller MUST already be running on `queue`.
  private func teardownLocked() {
    task?.cancel(with: .normalClosure, reason: nil)
    task = nil
    if player.isPlaying { player.stop() }
    if engine.isRunning { engine.stop() }
    if playerFormat != nil { engine.detach(player) }
    playerFormat = nil
  }

  private func configure(sampleRate: Double) {
    guard let format = AVAudioFormat(
      commonFormat: .pcmFormatFloat32,
      sampleRate: sampleRate,
      channels: 1,
      interleaved: false
    ) else {
      print("[AgentAudioPlayer] could not create player format at \(sampleRate)")
      return
    }
    playerFormat = format
    engine.attach(player)
    engine.connect(player, to: engine.mainMixerNode, format: format)
    do {
      try engine.start()
      player.play()
      let outputs = AVAudioSession.sharedInstance().currentRoute.outputs
        .map { "\($0.portName)(\($0.portType.rawValue))" }
      print("[AgentAudioPlayer] engine started, sampleRate=\(sampleRate) outputs=\(outputs)")
    } catch {
      print("[AgentAudioPlayer] engine start error: \(error)")
    }
  }

  private func handleHeader(_ text: String) {
    guard let data = text.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
      print("[AgentAudioPlayer] bad header text=\(text)")
      return
    }
    let rate = (obj["sampleRate"] as? Double)
      ?? (obj["sampleRate"] as? Int).map(Double.init)
      ?? 24000
    print("[AgentAudioPlayer] header sampleRate=\(rate)")
    configure(sampleRate: rate)
  }

  private func handleAudio(_ data: Data) {
    guard let format = playerFormat else { return }
    let frameCount = data.count / 2
    guard frameCount > 0,
          let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frameCount)),
          let channel = buffer.floatChannelData?[0] else { return }
    buffer.frameLength = AVAudioFrameCount(frameCount)
    data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
      let int16Ptr = raw.bindMemory(to: Int16.self)
      for i in 0..<frameCount {
        channel[i] = Float(int16Ptr[i]) / 32767.0
      }
    }
    player.scheduleBuffer(buffer, completionHandler: nil)
    receivedChunks += 1
    if receivedChunks == 1 || receivedChunks % 50 == 0 {
      print("[AgentAudioPlayer] played #\(receivedChunks) frames=\(frameCount)")
    }
  }

  private func receive(_ task: URLSessionWebSocketTask) {
    task.receive { [weak self] result in
      guard let self else { return }
      switch result {
      case .success(let message):
        self.queue.async {
          switch message {
          case .string(let text):
            self.handleHeader(text)
          case .data(let data):
            self.handleAudio(data)
          @unknown default:
            break
          }
        }
        self.receive(task)
      case .failure(let err):
        print("[AgentAudioPlayer] receive failure: \(err)")
        self.queue.async { if self.task === task { self.task = nil } }
      }
    }
  }
}

/// ViewModel for video streaming UI. Delegates device management to DeviceSessionManager.
@Observable
@MainActor
final class StreamSessionViewModel {
  // MARK: - State

  var currentVideoFrame: UIImage?
  var hasReceivedFirstFrame: Bool = false
  var streamingStatus: StreamingStatus = .stopped
  var isPaused: Bool = false
  var showError: Bool = false
  var errorMessage: String = ""
  var requiresDATAppUpdate: Bool = false

  /// When false, captured frames are still rendered locally but NOT forwarded
  /// over the publish WebSocket. Audio always publishes. The server flips this
  /// back on via a `{"type":"video_on"}` control message.
  var videoEnabled: Bool = true

  var capturedPhoto: UIImage?
  var showPhotoPreview: Bool = false
  var showPhotoCaptureError: Bool = false
  var isCapturingPhoto: Bool = false

  var hasActiveDevice: Bool { sessionManager.hasActiveDevice }
  var isDeviceSessionReady: Bool { sessionManager.isReady }

  var isStreaming: Bool { streamingStatus != .stopped }

  // MARK: - Private

  private let sessionManager: DeviceSessionManager
  private let wearables: WearablesInterface
  private var stream: MWDATCamera.Stream?
  private let publisher = StreamPublisher()
  private let audioPublisher = AudioPublisher()
  private let audioUplink = AudioUplinkServer()   // wired (usbmux) mic uplink; used when wiredMode
  private let videoUplink = VideoUplinkServer()   // wired (usbmux) camera uplink (Stage 5)
  private let agentPlayer = AgentAudioPlayer()

  private var stateListenerToken: AnyListenerToken?
  private var videoFrameListenerToken: AnyListenerToken?
  private var errorListenerToken: AnyListenerToken?
  private var photoDataListenerToken: AnyListenerToken?

  /// Set when a remote `capture_photo` control kicks off a capture. The next
  /// PhotoData callback uploads the JPEG to the server (instead of showing
  /// the local preview UI) and clears this.
  private var pendingRemotePhotoRequestId: String?

  // MARK: - Init

  init(wearables: WearablesInterface) {
    self.wearables = wearables
    self.sessionManager = DeviceSessionManager(wearables: wearables)
    publisher.onControl = { [weak self] type in
      guard let self else { return }
      switch type {
      case "video_on": self.videoEnabled = true
      case "video_off": self.videoEnabled = false
      default: break
      }
    }
    publisher.onCapturePhoto = { [weak self] requestId in
      self?.captureRemotePhoto(requestId: requestId)
    }
  }

  func toggleVideoFeed() {
    videoEnabled.toggle()
  }

  // MARK: - Public API

  func handleStartStreaming() async {
    let permission = Permission.camera
    do {
      var status = try await wearables.checkPermissionStatus(permission)
      if status != .granted {
        status = try await wearables.requestPermission(permission)
      }
      guard status == .granted else {
        showError("Permission denied")
        return
      }
      await startSession()
    } catch {
      showError("Permission error: \(error.description)")
    }
  }

  func stopSession() async {
    guard let activeStream = stream else { return }
    stream = nil
    clearListeners()
    publisher.stop()
    audioPublisher.stop()
    audioUplink.stop()
    videoUplink.stop()
    agentPlayer.stop()
    streamingStatus = .stopped
    isPaused = false
    currentVideoFrame = nil
    hasReceivedFirstFrame = false
    await activeStream.stop()
    // MWDAT 0.7 has no removeStream API; the next streaming run needs a fresh
    // DeviceSession or addStream returns nil. Awaits .stopped so the next
    // createSession() doesn't race the glasses-side activity manager.
    await sessionManager.stopDeviceSession()
  }

  func pauseSession() {
    guard streamingStatus == .streaming, !isPaused else { return }
    isPaused = true
    publisher.pause()
    audioPublisher.pause()
  }

  func resumeSession() {
    guard isPaused else { return }
    isPaused = false
    publisher.resume()
    audioPublisher.resume()
  }

  // MARK: - Audio-only (camera-free) path

  /// True while the camera-free hands-free path is streaming the glasses mic to
  /// ManuAI's glasses_bridge `/publish-audio`. Independent of the DAT camera
  /// session — this is the offline hands-free MVP path, and the test of the PRD
  /// bet that the Ray-Ban mic rides Bluetooth HFP without a DAT stream.
  private(set) var audioOnlyActive = false

  /// Bring up ONLY the HFP mic + audio publisher: no `DeviceSession`, no
  /// `addStream`, no camera. Mutually exclusive with the camera stream (the UI
  /// disables each while the other is live). Watch the Xcode console for
  /// `[AudioPublisher] pinned input to …` (found the glasses HFP mic) vs
  /// `no BluetoothHFP input yet` (the spike's negative result).
  func startAudioOnly() {
    guard !audioOnlyActive, streamingStatus != .streaming else { return }
    AudioPublisher.configureAudioSession()
    if wiredMode {
      // Air-gapped USB path: listen for the Mac (it dials in over the cable via usbmux).
      print("[StreamSession] AUDIO-ONLY WIRED (usbmux) → NWListener :\(wiredListenPort)")
      audioUplink.start(port: wiredListenPort)
    } else {
      let audioURL = publishURL(path: "/publish-audio?agent=1")
      print("[StreamSession] AUDIO-ONLY (camera-free) → \(audioURL)")
      audioPublisher.start(url: audioURL)
    }
    audioOnlyActive = true
  }

  func stopAudioOnly() {
    guard audioOnlyActive else { return }
    audioPublisher.stop()
    audioUplink.stop()
    audioOnlyActive = false
  }

  /// Stops both the stream and the underlying device session. Call in test tearDown.
  func endSession() {
    stream = nil
    clearListeners()
    publisher.stop()
    audioPublisher.stop()
    audioUplink.stop()
    videoUplink.stop()
    agentPlayer.stop()
    streamingStatus = .stopped
    isPaused = false
    currentVideoFrame = nil
    hasReceivedFirstFrame = false
    sessionManager.cleanup()
  }

  func capturePhoto() {
    guard !isCapturingPhoto, streamingStatus == .streaming else {
      showPhotoCaptureError = true
      return
    }
    isCapturingPhoto = true
    let success = stream?.capturePhoto(format: .jpeg) ?? false
    if !success {
      isCapturingPhoto = false
      showPhotoCaptureError = true
    }
  }

  /// The server asked for a high-res still over the publish WebSocket. Mark the
  /// request_id so the next PhotoData callback uploads instead of showing
  /// the local preview, then trigger the same SDK call as the UI button.
  private func captureRemotePhoto(requestId: String) {
    guard !isCapturingPhoto, streamingStatus == .streaming else {
      print("[StreamSession] ignoring capture_photo request \(requestId) — not streaming or already capturing")
      return
    }
    pendingRemotePhotoRequestId = requestId
    isCapturingPhoto = true
    let success = stream?.capturePhoto(format: .jpeg) ?? false
    if !success {
      isCapturingPhoto = false
      pendingRemotePhotoRequestId = nil
      print("[StreamSession] capturePhoto SDK call returned false for request \(requestId)")
    }
  }

  func dismissError() {
    showError = false
    errorMessage = ""
  }

  func dismissPhotoCaptureError() {
    showPhotoCaptureError = false
  }

  func dismissPhotoPreview() {
    showPhotoPreview = false
    capturedPhoto = nil
  }

  // MARK: - Private

  private func startSession() async {
    let deviceSession: DeviceSession
    do {
      deviceSession = try await sessionManager.getSession()
      requiresDATAppUpdate = false
    } catch DeviceSessionError.datAppOnTheGlassesUpdateRequired {
      requiresDATAppUpdate = true
      showError(DeviceSessionError.datAppOnTheGlassesUpdateRequired.localizedDescription)
      return
    } catch {
      showError("Failed to start session: \(error.localizedDescription)")
      return
    }

    guard deviceSession.state == .started else {
      showError("Device session is not ready. Please try again.")
      return
    }

    let config = StreamConfiguration(
      videoCodec: VideoCodec.raw,
      resolution: StreamingResolution.high,
      frameRate: 24
    )

    // Add the camera stream BEFORE activating HFP. On this hardware, an
    // already-active HFP voice channel prevents addStream from succeeding
    // (Meta DAT's "HFP first" guidance only applies to streams that carry
    // audio — our camera stream doesn't).
    guard let newStream = try? deviceSession.addStream(config: config) else {
      print("[StreamSession] addStream returned nil")
      showError("Could not add camera stream. Try again.")
      return
    }
    stream = newStream
    streamingStatus = .waiting
    setupListeners(for: newStream)
    if wiredMode {
      // Air-gapped USB (Stage 5): camera JPEG + mic over NWListeners (the Mac dials in via
      // usbmux). Forwarders: `pymobiledevice3 usbmux forward 8767 8767` (video) + `8766 8766`.
      print("[StreamSession] CAMERA WIRED (usbmux) → video :\(wiredVideoPort), audio :\(wiredListenPort)")
      videoUplink.start(port: wiredVideoPort)
      await newStream.start()
      AudioPublisher.configureAudioSession()
      audioUplink.start(port: wiredListenPort)
    } else {
      let videoURL = publishURL(path: "/publish")
      print("[StreamSession] starting publisher → \(videoURL)")
      publisher.start(url: videoURL)
      await newStream.start()

      // Now bring up HFP and the audio publisher. The camera stream's BT
      // Classic link is already established and won't be disturbed.
      AudioPublisher.configureAudioSession()
      let audioURL = publishURL(path: "/publish-audio?agent=1")
      print("[StreamSession] starting audio publisher → \(audioURL)")
      audioPublisher.start(url: audioURL)

      let agentURL = publishURL(path: "/agent-audio")
      print("[StreamSession] starting agent player → \(agentURL)")
      agentPlayer.start(url: agentURL)
    }
  }

  private func setupListeners(for stream: MWDATCamera.Stream) {
    stateListenerToken = stream.statePublisher.listen { [weak self] state in
      Task { @MainActor in self?.handleStateChange(state) }
    }

    videoFrameListenerToken = stream.videoFramePublisher.listen { [weak self] frame in
      Task { @MainActor in self?.handleVideoFrame(frame) }
    }

    errorListenerToken = stream.errorPublisher.listen { [weak self] error in
      Task { @MainActor in self?.handleError(error) }
    }

    photoDataListenerToken = stream.photoDataPublisher.listen { [weak self] data in
      Task { @MainActor in self?.handlePhotoData(data) }
    }
  }

  private func clearListeners() {
    stateListenerToken = nil
    videoFrameListenerToken = nil
    errorListenerToken = nil
    photoDataListenerToken = nil
  }

  private func handleStateChange(_ state: StreamState) {
    switch state {
    case .stopped:
      currentVideoFrame = nil
      streamingStatus = .stopped
    case .waitingForDevice, .starting, .stopping, .paused:
      streamingStatus = .waiting
    case .streaming:
      streamingStatus = .streaming
    }
  }

  private func handleVideoFrame(_ frame: VideoFrame) {
    if let image = frame.makeUIImage() {
      currentVideoFrame = image
      if !hasReceivedFirstFrame {
        hasReceivedFirstFrame = true
      }
      if videoEnabled, let data = image.jpegData(compressionQuality: 0.5) {
        if wiredMode { videoUplink.send(data) } else { publisher.send(data) }
      }
    }
  }

  private func handleError(_ error: StreamError) {
    let message = error.localizedDescription
    if message != errorMessage {
      showError(message)
    }
  }

  private func handlePhotoData(_ data: PhotoData) {
    isCapturingPhoto = false
    if let requestId = pendingRemotePhotoRequestId {
      pendingRemotePhotoRequestId = nil
      uploadCapturedPhoto(data.data, requestId: requestId)
      return
    }
    if let image = UIImage(data: data.data) {
      capturedPhoto = image
      showPhotoPreview = true
    }
  }

  private func uploadCapturedPhoto(_ jpeg: Data, requestId: String) {
    var req = URLRequest(url: publishHTTPURL(path: "/publish/photo"))
    req.httpMethod = "POST"
    req.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
    req.setValue(requestId, forHTTPHeaderField: "X-Request-Id")
    req.httpBody = jpeg
    URLSession.shared.dataTask(with: req) { _, resp, err in
      if let err {
        print("[StreamSession] photo upload \(requestId) failed: \(err)")
        return
      }
      let status = (resp as? HTTPURLResponse)?.statusCode ?? -1
      print("[StreamSession] photo upload \(requestId) status=\(status) bytes=\(jpeg.count)")
    }.resume()
  }

  private func showError(_ message: String) {
    errorMessage = message
    showError = true
  }

}
