/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

import MWDATCore
import Observation
import SwiftUI

/// Manages DeviceSession lifecycle with 1:1 device-to-session mapping.
/// Monitors device availability and creates sessions on demand via `getSession()`.
@Observable
@MainActor
final class DeviceSessionManager {
  private(set) var isReady: Bool = false
  private(set) var hasActiveDevice: Bool = false

  private let wearables: WearablesInterface
  private let deviceSelector: AutoDeviceSelector
  private var deviceSession: DeviceSession?
  @ObservationIgnored private var deviceMonitorTask: Task<Void, Never>?
  @ObservationIgnored private var stateObserverTask: Task<Void, Never>?

  init(wearables: WearablesInterface) {
    self.wearables = wearables
    self.deviceSelector = AutoDeviceSelector(wearables: wearables)
    startDeviceMonitoring()
  }

  isolated deinit {
    deviceMonitorTask?.cancel()
    stateObserverTask?.cancel()
    deviceSession?.stop()
  }

  /// Stops the device session and cancels monitoring. Call before releasing.
  /// The stateObserverTask handles cleanup when .stopped arrives.
  func cleanup() {
    deviceMonitorTask?.cancel()
    deviceMonitorTask = nil
    deviceSession?.stop()
  }

  /// Stops only the device session, leaving device-availability monitoring
  /// active so the next getSession() can build a fresh session. MWDAT 0.7
  /// requires a new DeviceSession per streaming run (no removeStream API).
  ///
  /// Awaits the .stopped transition before returning. Without this, a quick
  /// Start→Stop→Start cycle hits ActivityManagerError 11 on the second start
  /// because the glasses-side activity manager hasn't released the previous
  /// session by the time wearables.createSession() runs.
  func stopDeviceSession() async {
    guard let session = deviceSession else { return }

    // Take over teardown from the state observer so we own the .stopped wait.
    stateObserverTask?.cancel()
    stateObserverTask = nil

    if session.state == .stopped {
      deviceSession = nil
      isReady = false
      return
    }

    // Subscribe BEFORE calling stop — stateStream doesn't buffer past events.
    let stateStream = session.stateStream()
    session.stop()
    for await state in stateStream where state == .stopped { break }
    deviceSession = nil
    isReady = false
  }

  /// Returns a ready DeviceSession, creating one if needed.
  /// Waits for the session to reach .started state before returning.
  func getSession() async throws(DeviceSessionError) -> DeviceSession {
    if let session = deviceSession, session.state == .started {
      isReady = true
      return session
    }

    if deviceSession?.state == .stopped {
      deviceSession = nil
    }

    // Wait for an in-progress session to finish starting
    if let session = deviceSession {
      // The session may have already transitioned to .started before the
      // for-await loop begins iterating (stateStream doesn't buffer past events).
      if session.state == .started {
        isReady = true
        startStateObserver(for: session)
        return session
      }

      try await waitForSessionStart(
        stateStream: session.stateStream(),
        errorStream: session.errorStream()
      )
      isReady = true
      startStateObserver(for: session)
      return session
    }

    // Create a new session
    do {
      let session = try wearables.createSession(deviceSelector: deviceSelector)
      deviceSession = session

      let stateStream = session.stateStream()
      let errorStream = session.errorStream()
      try session.start()

      // The session may have already transitioned to .started before the
      // for-await loop begins iterating (the state change is delivered on
      // another thread and the stream does not buffer past events).
      if session.state == .started {
        isReady = true
        startStateObserver(for: session)
        return session
      }

      try await waitForSessionStart(stateStream: stateStream, errorStream: errorStream)
      isReady = true
      startStateObserver(for: session)
      return session
    } catch {
      isReady = false
      deviceSession = nil
      throw error
    }
  }

  // MARK: - Private

  private func waitForSessionStart(
    stateStream: AsyncStream<DeviceSessionState>,
    errorStream: AsyncStream<DeviceSessionError>
  ) async throws(DeviceSessionError) {
    do {
      try await withThrowingTaskGroup(of: Void.self) { group in
        group.addTask {
          for await state in stateStream {
            if state == .started {
              return
            }
            if state == .stopped {
              throw DeviceSessionError.unexpectedError(description: "The session failed to start")
            }
          }
          guard !Task.isCancelled else {
            return
          }
          throw DeviceSessionError.unexpectedError(description: "The session failed to start")
        }

        group.addTask {
          for await error in errorStream {
            throw error
          }
          guard !Task.isCancelled else {
            return
          }
          throw DeviceSessionError.unexpectedError(description: "The session failed to start")
        }

        guard try await group.next() != nil else {
          throw DeviceSessionError.unexpectedError(description: "The session failed to start")
        }
        group.cancelAll()
      }
    } catch let error as DeviceSessionError {
      throw error
    } catch {
      throw .unexpectedError(description: error.localizedDescription)
    }
  }

  /// Monitors device availability only — does NOT create sessions.
  /// Session creation is deferred to `getSession()` to avoid races.
  private func startDeviceMonitoring() {
    deviceMonitorTask = Task { [weak self] in
      guard let self else { return }
      for await device in deviceSelector.activeDeviceStream() {
        hasActiveDevice = device != nil
      }
    }
  }

  private func startStateObserver(for session: DeviceSession) {
    stateObserverTask?.cancel()
    stateObserverTask = Task { [weak self] in
      for await state in session.stateStream() {
        guard let self else { return }
        if state == .started {
          isReady = true
        } else if state == .stopped {
          isReady = false
          deviceSession = nil
          stateObserverTask = nil
          return
        }
      }
    }
  }
}
