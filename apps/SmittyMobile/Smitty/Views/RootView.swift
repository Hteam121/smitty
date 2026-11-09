import SwiftUI

enum Tab: Hashable { case chat, inbox, sessions }

struct RootView: View {
    @Environment(HubStore.self) private var store
    @State private var tab: Tab = .chat
    @State private var showSettings = false
    @State private var chatDraft = ""

    var body: some View {
        ZStack {
            Theme.background
            if store.isSignedIn {
                VStack(spacing: 0) {
                    header
                    content
                    tabBar
                }
            } else {
                SettingsView(isSheet: false)
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView(isSheet: true) }
        .onAppear { store.startPolling() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            StatePill()
            Spacer()
            if let err = store.lastError {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(Theme.coral)
                    .help(err)
            }
            if tab == .inbox && store.unreadCount > 0 {
                Button { withAnimation { store.markAllRead() } } label: {
                    Label("Mark all read", systemImage: "checkmark.circle").font(.system(size: 12, weight: .semibold))
                }
                .buttonStyle(SmallPillButtonStyle())
            }
            Button { showSettings = true } label: {
                Image(systemName: "gearshape").font(.system(size: 17, weight: .medium)).foregroundStyle(Theme.cream2)
            }
        }
        .padding(.horizontal, 18).padding(.top, 8).padding(.bottom, 6)
    }

    @ViewBuilder private var content: some View {
        switch tab {
        case .chat: ChatView(draft: $chatDraft)
        case .inbox: InboxView()
        case .sessions: SessionsView(onNewTask: { project in
            chatDraft = "In \(project), "
            tab = .chat
        })
        }
    }

    private var tabBar: some View {
        HStack {
            tabButton(.chat, "Chat", "bubble.left.and.text.bubble.right", badge: 0)
            tabButton(.inbox, "Inbox", "tray", badge: store.unreadCount + store.approvals.count, attention: !store.approvals.isEmpty)
            tabButton(.sessions, "Sessions", "square.stack.3d.up", badge: store.snapshot.sessions.filter(\.isBusy).count)
        }
        .padding(.horizontal, 10).padding(.top, 8).padding(.bottom, 4)
        .background(Theme.navy950.opacity(0.85).ignoresSafeArea(edges: .bottom))
        .overlay(Rectangle().fill(Theme.hair2).frame(height: 1), alignment: .top)
    }

    private func tabButton(_ t: Tab, _ label: String, _ icon: String, badge: Int, attention: Bool = false) -> some View {
        Button { tab = t } label: {
            VStack(spacing: 3) {
                ZStack(alignment: .topTrailing) {
                    Image(systemName: icon).font(.system(size: 20, weight: .medium))
                    if badge > 0 {
                        Text("\(badge)")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(attention ? Theme.navy950 : Theme.cream)
                            .padding(.horizontal, 5).padding(.vertical, 1)
                            .background(Capsule().fill(attention ? Theme.coral : Theme.navy700))
                            .offset(x: 10, y: -6)
                    }
                }
                Text(label).font(.system(size: 11, weight: .medium))
            }
            .foregroundStyle(tab == t ? Theme.cream : Theme.cream3)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 4)
        }
    }
}

/// Smitty's state, and the Mac's reachability, in one pill.
struct StatePill: View {
    @Environment(HubStore.self) private var store
    var body: some View {
        let s = store.snapshot.state
        HStack(spacing: 8) {
            Pill(text: s.label, icon: s.icon, color: s.color, fill: s.raw == "needs_confirmation" ? Theme.coralSoft : Theme.hair2)
            if let note = store.offlineNote {
                Text(note).font(.system(size: 12)).foregroundStyle(Theme.cream3)
            } else if store.snapshot.voice {
                Image(systemName: "mic").font(.system(size: 11)).foregroundStyle(Theme.cream4)
            }
        }
    }
}

/// Empty-state copy for a tab.
struct EmptyNote: View {
    let icon: String, title: String, text: String
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: icon).font(.system(size: 28, weight: .light)).foregroundStyle(Theme.cream4)
            Text(title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.cream2)
            Text(text).font(.system(size: 13)).foregroundStyle(Theme.cream3).multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.top, 60).padding(.horizontal, 30)
    }
}
