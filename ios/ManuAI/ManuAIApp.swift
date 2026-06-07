/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

//
// ManuAIApp.swift
//
// Main entry point for the ManuAI sample app demonstrating the Meta Wearables DAT SDK.
// This app shows how to connect to wearable devices (like Ray-Ban Meta smart glasses),
// stream live video from their cameras, and capture photos. It provides a complete example
// of DAT SDK integration including device registration, permissions, and media streaming.
//

import Foundation
import MWDATCore
import Network
import SwiftUI

#if DEBUG
import MWDATMockDevice
#endif

@main
struct ManuAIApp: App {
  #if DEBUG
  // Debug menu for simulating device connections during development
  @State private var debugMenuViewModel = DebugMenuViewModel(mockDeviceKit: MockDeviceKit.shared)
  #endif
  private let wearables: WearablesInterface
  @State private var wearablesViewModel: WearablesViewModel

  init() {
    do {
      try Wearables.configure()
    } catch {
      #if DEBUG
      NSLog("[ManuAI] Failed to configure Wearables SDK: \(error)")
      #endif
    }

    Self.triggerLocalNetworkPrompt()

    #if DEBUG
    // Start the test server when launched by XCUITests so tests can control
    // mock device setup via HTTP commands from the test process.
    if ProcessInfo.processInfo.arguments.contains("--ui-testing") {
      MockDeviceKit.shared.enable(config: MockDeviceKitConfig(initiallyRegistered: false))

      let portFilePath = ProcessInfo.processInfo.environment["MWDAT_TEST_SERVER_PORT_FILE"]
      Task {
        try await MockDeviceKit.shared.startTestServer(portFilePath: portFilePath)
      }
    }
    #endif

    let wearables = Wearables.shared
    self.wearables = wearables
    self._wearablesViewModel = State(wrappedValue: WearablesViewModel(wearables: wearables))
  }

  /// iOS only prompts for Local Network permission when the app first touches
  /// something the OS classifies as a local-network operation. URLSession to
  /// a LAN IP literal *should* qualify, but in practice the prompt often
  /// doesn't fire — the connect is silently cancelled (NSURLErrorCancelled).
  /// Starting a short-lived NWBrowser for our declared Bonjour service trips
  /// the permission flow reliably. Keep the browser alive long enough for
  /// the prompt to actually appear, then cancel — we don't need any results.
  private static func triggerLocalNetworkPrompt() {
    let browser = NWBrowser(
      for: .bonjour(type: "_bonjour._tcp", domain: nil),
      using: .init()
    )
    browser.start(queue: .main)
    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
      browser.cancel()
    }
  }

  var body: some Scene {
    WindowGroup {
      // Main app view with access to the shared Wearables SDK instance
      // The Wearables.shared singleton provides the core DAT API
      MainAppView(wearables: Wearables.shared, viewModel: wearablesViewModel)
        // Show error alerts for view model failures
        .alert("Error", isPresented: $wearablesViewModel.showError) {
          Button("OK") {
            wearablesViewModel.dismissError()
          }
        } message: {
          Text(wearablesViewModel.errorMessage)
        }
        #if DEBUG
      .sheet(isPresented: $debugMenuViewModel.showDebugMenu) {
        MockDeviceKitView(viewModel: debugMenuViewModel.mockDeviceKitViewModel)
      }
      .overlay {
        DebugMenuView(debugMenuViewModel: debugMenuViewModel)
      }
        #endif

      // Registration view handles the flow for connecting to the glasses via Meta AI
      RegistrationView(viewModel: wearablesViewModel)
    }
  }
}
