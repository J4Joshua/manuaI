/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * All rights reserved.
 *
 * This source code is licensed under the license found in the
 * LICENSE file in the root directory of this source tree.
 */

//
// MockDeviceViewModel.swift
//
// View model for individual mock devices used in development and testing of DAT SDK features.
// This controls mock device behaviors like power states, physical states (folded/unfolded),
// and media content (camera feeds and captured images).
//

#if DEBUG

import AVFoundation
import Foundation
import MWDATMockDevice
import Observation
import UIKit

extension MockDeviceCardView {
  @Observable
  @MainActor
  final class ViewModel {
    let device: MockDevice
    var hasCameraFeed: Bool = false
    var hasCapturedImage: Bool = false
    var cameraSource: CameraFacing?
    var isPoweredOn: Bool = false
    var isDonned: Bool = false
    var isUnfolded: Bool = false
    var showCameraPermissionAlert: Bool = false

    init(device: MockDevice, hasCameraFeed: Bool = false, hasCapturedImage: Bool = false) {
      self.device = device
      self.hasCameraFeed = hasCameraFeed
      self.hasCapturedImage = hasCapturedImage
    }

    var id: String { device.deviceIdentifier }

    // Display name for the mock device in the UI
    var deviceName: String {
      if device is MockRaybanMeta {
        return "RayBan Meta Glasses"
      }
      return "Device"
    }

    func powerOn() {
      device.powerOn()
      isPoweredOn = true
    }

    func powerOff() {
      device.powerOff()
      isPoweredOn = false
      isDonned = false
      isUnfolded = false
    }

    func don() {
      device.don()
      isDonned = true
      isUnfolded = true
    }

    func doff() {
      device.doff()
      isDonned = false
    }

    func unfold() {
      if let rayBanDevice = device as? MockDisplaylessGlasses {
        rayBanDevice.unfold()
        isUnfolded = true
      }
    }

    func fold() {
      if let rayBanDevice = device as? MockDisplaylessGlasses {
        rayBanDevice.fold()
        isUnfolded = false
        isDonned = false
      }
    }

    func captouchTap() {
      (device as? MockDisplaylessGlasses)?.services.captouch.tap()
    }

    func captouchTapAndHold() {
      (device as? MockDisplaylessGlasses)?.services.captouch.tapAndHold()
    }

    func setCameraFeed(_ facing: CameraFacing) {
      if let cameraKit = (device as? MockDisplaylessGlasses)?.services.camera {
        Task {
          let status = AVCaptureDevice.authorizationStatus(for: .video)
          if status == .denied || status == .restricted {
            self.showCameraPermissionAlert = true
            return
          }
          let granted = await AVCaptureDevice.requestAccess(for: .video)
          guard granted else {
            self.showCameraPermissionAlert = true
            return
          }
          await cameraKit.setCameraFeed(cameraFacing: facing)
          self.cameraSource = facing
          self.hasCameraFeed = false
        }
      }
    }

    func openSettings() {
      if let url = URL(string: UIApplication.openSettingsURLString) {
        UIApplication.shared.open(url)
      }
    }

    // Load mock video content
    func selectVideo(from url: URL) {
      if let cameraKit = (device as? MockDisplaylessGlasses)?.services.camera {
        cameraKit.setCameraFeed(fileURL: url)
        hasCameraFeed = true
        cameraSource = nil
      }
    }

    // Load mock image content
    func selectImage(from url: URL) {
      if let cameraKit = (device as? MockDisplaylessGlasses)?.services.camera {
        cameraKit.setCapturedImage(fileURL: url)
        hasCapturedImage = true
      }
    }
  }
}

#endif
