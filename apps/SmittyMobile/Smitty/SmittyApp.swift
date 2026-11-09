import SwiftUI

@main
struct SmittyApp: App {
    @State private var store = HubStore(device: "iphone")
    @State private var phone = PhoneConnectivity()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(store)
                .environment(phone)
                .preferredColorScheme(.dark)
                .tint(Theme.cream)
                .onAppear {
                    phone.activate()
                    phone.share(store.config)
                }
                .onChange(of: phase) { _, p in
                    if p == .active { store.startPolling() } else { store.stopPolling() }
                }
                .onChange(of: store.config) { _, cfg in phone.share(cfg) }
        }
    }
}
