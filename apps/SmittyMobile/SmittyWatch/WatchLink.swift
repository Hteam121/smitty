import Foundation
import Observation
import WatchConnectivity

/// Receives the hub sign-in from the phone (application context) so the watch can poll the hub itself.
@MainActor @Observable
final class WatchLink: NSObject, WCSessionDelegate {
    var onConfig: ((HubConfig) -> Void)?
    private(set) var phoneReachable = false

    func activate() {
        guard WCSession.isSupported() else { return }
        let s = WCSession.default
        s.delegate = self
        s.activate()
    }

    private func take(_ ctx: [String: Any]) {
        guard let cfg = HubConfig(handoff: ctx) else { return }
        let current = Keychain.load()
        // Keep our own access token when nothing that matters changed.
        if current.url == cfg.url && current.anonKey == cfg.anonKey && current.refreshToken == cfg.refreshToken { return }
        onConfig?(cfg)
    }

    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        let ctx = session.receivedApplicationContext
        let reachable = session.isReachable
        Task { @MainActor in
            self.phoneReachable = reachable
            if !ctx.isEmpty { self.take(ctx) }
        }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        Task { @MainActor in self.take(applicationContext) }
    }

    nonisolated func sessionReachabilityDidChange(_ session: WCSession) {
        let r = session.isReachable
        Task { @MainActor in self.phoneReachable = r }
    }
}
