import SwiftUI

@main
struct SmittyWatchApp: App {
    @State private var store = HubStore(device: "watch")
    @State private var link = WatchLink()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            WatchRootView()
                .environment(store)
                .environment(link)
                .tint(Theme.cream)
                .onAppear {
                    link.activate()
                    link.onConfig = { cfg in Task { await store.adopt(cfg) } }
                }
                .onChange(of: phase) { _, p in
                    if p == .active { store.startPolling(every: 4) } else { store.stopPolling() }
                }
        }
    }
}
