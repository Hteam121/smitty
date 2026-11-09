import Foundation
import Observation
import WatchConnectivity

/// Hands the hub sign-in to the Watch so it can talk to the hub on its own.
@MainActor @Observable
final class PhoneConnectivity: NSObject, WCSessionDelegate {
    private(set) var watchPaired = false
    private(set) var watchAppInstalled = false
    private var lastShared: [String: Any] = [:]

    func activate() {
        guard WCSession.isSupported() else { return }
        let s = WCSession.default
        s.delegate = self
        s.activate()
    }

    func share(_ cfg: HubConfig) {
        guard WCSession.isSupported(), WCSession.default.activationState == .activated else { return }
        let ctx = cfg.handoff
        if NSDictionary(dictionary: ctx).isEqual(to: lastShared) { return }
        do {
            try WCSession.default.updateApplicationContext(ctx)
            lastShared = ctx
        } catch {
            // Not paired or no watch app: nothing to do.
        }
    }

    // MARK: WCSessionDelegate

    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        let paired = session.isPaired, installed = session.isWatchAppInstalled
        Task { @MainActor in
            self.watchPaired = paired
            self.watchAppInstalled = installed
            self.lastShared = [:]            // re-send on the next share()
        }
    }

    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) { session.activate() }
    nonisolated func sessionWatchStateDidChange(_ session: WCSession) {
        let paired = session.isPaired, installed = session.isWatchAppInstalled
        Task { @MainActor in self.watchPaired = paired; self.watchAppInstalled = installed }
    }
}
