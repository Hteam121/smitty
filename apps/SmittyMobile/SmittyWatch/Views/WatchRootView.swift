import SwiftUI

/// "Tell Smitty" on top (one tap opens dictation), then approvals, then the inbox.
struct WatchRootView: View {
    @Environment(HubStore.self) private var store
    @State private var sentNote: String? = nil

    private var unread: [Notice] { store.notices.filter { !$0.isRead } }

    var body: some View {
        NavigationStack {
            Group {
                if !store.isSignedIn {
                    VStack(spacing: 8) {
                        Image(systemName: "iphone.and.apple.watch").font(.system(size: 26)).foregroundStyle(Theme.cream3)
                        Text("Sign in on the iPhone app").font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.cream)
                        Text("The sign-in is shared with the watch automatically.").font(.system(size: 12)).foregroundStyle(Theme.cream3)
                            .multilineTextAlignment(.center)
                    }
                    .padding()
                } else {
                    List {
                        Section {
                            header
                                .listRowInsets(EdgeInsets(top: 0, leading: 6, bottom: 2, trailing: 6))
                            tellSmitty
                                .listRowInsets(EdgeInsets(top: 2, leading: 0, bottom: 2, trailing: 0))
                        }
                        .listRowBackground(Color.clear)
                        if !store.approvals.isEmpty {
                            Section("Approvals") {
                                ForEach(store.approvals) { a in
                                    NavigationLink(value: a) { ApprovalRow(approval: a) }
                                        .listRowBackground(RoundedRectangle(cornerRadius: 12).fill(Theme.coralSoft))
                                }
                            }
                        }
                        Section {
                            if unread.isEmpty && store.notices.isEmpty {
                                Text("Nothing waiting").font(.system(size: 13)).foregroundStyle(Theme.cream3)
                            }
                            ForEach(unread.isEmpty ? Array(store.notices.prefix(5)) : unread) { n in
                                NavigationLink(value: n) { NoticeRow(notice: n) }
                                    .listRowBackground(RoundedRectangle(cornerRadius: 12).fill(Theme.hair2))
                                    .swipeActions(edge: .leading, allowsFullSwipe: true) {
                                        if !n.isRead {
                                            Button { store.markRead([n.id]) } label: { Label("Read", systemImage: "checkmark") }.tint(.green)
                                        }
                                    }
                            }
                            if unread.count > 1 {
                                Button { store.markAllRead() } label: {
                                    WatchPill(title: "Mark all \(unread.count) read", icon: "checkmark.circle", kind: .secondary)
                                }
                                .buttonStyle(.plain)
                                .listRowBackground(Color.clear)
                            }
                        } header: {
                            Text(unread.isEmpty ? "Inbox" : "Unread · \(unread.count)")
                        }
                    }
                    .navigationDestination(for: Approval.self) { ApprovalDetail(approval: $0) }
                    .navigationDestination(for: Notice.self) { NoticeDetail(notice: $0) }
                    .toolbar(.hidden, for: .navigationBar)      // our own left-aligned header instead of the system title
                    .environment(\.defaultMinListRowHeight, 1)
                    .padding(.top, -18)                          // pull the list up into the space the hidden bar leaves
                }
            }
            .navigationTitle("Smitty")
            .background(Theme.background)
        }
    }

    /// "Smitty" and the state, left-aligned under the clock; a second line only when something is off.
    private var header: some View {
        let s = store.snapshot.state
        return VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text("Smitty").font(.system(size: 20, weight: .bold)).foregroundStyle(Theme.cream)
                Image(systemName: s.icon).font(.system(size: 14, weight: .semibold)).foregroundStyle(s.color)
                Text(s.label).font(.system(size: 12, weight: .semibold)).foregroundStyle(s.color)
                Spacer(minLength: 0)
            }
            if let note = store.offlineNote {
                Label(note, systemImage: "moon.zzz").font(.system(size: 11)).foregroundStyle(Theme.cream3)
            } else if let e = store.lastError {
                Text(e).font(.system(size: 10)).foregroundStyle(Theme.coral).lineLimit(2)
            }
        }
        .padding(.top, 2)
    }

    /// One tap straight into the watch's dictation / scribble input; the text goes to Smitty as a chat line.
    private var tellSmitty: some View {
        VStack(spacing: 4) {
            TextFieldLink(prompt: Text("Tell Smitty…")) {
                WatchPill(title: "Tell Smitty", icon: "mic.fill", kind: .primary)
            } onSubmit: { text in
                let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { return }
                sentNote = store.macOffline ? "Queued until the Mac is back" : "Sent to Smitty"
                Task {
                    await store.say(t)
                    try? await Task.sleep(for: .seconds(2.5))
                    sentNote = nil
                }
            }
            .buttonStyle(.plain)
            if let sentNote {
                Text(sentNote).font(.system(size: 11)).foregroundStyle(Theme.cream3)
            }
        }
    }
}

struct ApprovalRow: View {
    let approval: Approval
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: "exclamationmark.circle").font(.system(size: 10, weight: .bold))
                Text("Needs approval").font(.system(size: 10, weight: .bold))
            }
            .foregroundStyle(Theme.coral)
            Text(approval.session ?? approval.project ?? "Session").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.cream).lineLimit(1)
            Text(approval.reason ?? approval.action ?? approval.question).font(.system(size: 11)).foregroundStyle(Theme.cream2).lineLimit(2)
        }
    }
}

struct NoticeRow: View {
    let notice: Notice
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(notice.headline).font(.system(size: 10, weight: .bold)).foregroundStyle(notice.urgent && !notice.isRead ? Theme.coral : Theme.cream3)
            Text(notice.subject).font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.cream).lineLimit(2)
            if !notice.body.isEmpty {
                Text(notice.body).font(.system(size: 11)).foregroundStyle(Theme.cream2).lineLimit(2)
            }
        }
    }
}

/// The whole question, then one big tap per answer. No swipe actions on purpose.
struct ApprovalDetail: View {
    @Environment(HubStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let approval: Approval
    @State private var sent: String? = nil

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                if let s = approval.session ?? approval.project {
                    Text(s).font(.system(size: 11, weight: .semibold)).foregroundStyle(Theme.cream3)
                }
                Text(approval.question).font(.system(size: 14)).foregroundStyle(Theme.cream)
                if let action = approval.action, !action.isEmpty {
                    Text(action).font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.cream2)
                        .padding(6).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.navy950.opacity(0.6)))
                }
                if let sent {
                    Text("Sent: \(sent)").font(.system(size: 12)).foregroundStyle(Theme.cream3)
                } else {
                    VStack(spacing: 8) {
                        Button { answer("yes") } label: { WatchPill(title: "Allow", icon: "checkmark", kind: .primary) }.buttonStyle(.plain)
                        Button { answer("no") } label: { WatchPill(title: "Deny", icon: "xmark", kind: .attention) }.buttonStyle(.plain)
                        Button { answer("all") } label: { WatchPill(title: "Allow all for this session", icon: "checkmark.circle", kind: .secondary) }.buttonStyle(.plain)
                    }
                    .padding(.top, 6)
                }
            }
            .padding(.horizontal, 4)
        }
        .navigationTitle("Approval")
        .background(Theme.background)
    }

    private func answer(_ a: String) {
        sent = ["yes": "Allow", "no": "Deny", "all": "Allow all"][a]
        Task {
            await store.answer(approval, a)
            try? await Task.sleep(for: .seconds(0.8))
            dismiss()
        }
    }
}

/// Opening an item does not mark it read on the watch: that is an explicit tap (or a reply).
struct NoticeDetail: View {
    @Environment(HubStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let notice: Notice
    @State private var sent: String? = nil

    private var isRead: Bool { store.notices.first { $0.id == notice.id }?.isRead ?? notice.isRead }

    private func send(_ text: String) {
        sent = text
        store.markRead([notice.id])
        Task { await store.reply(notice, text) }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                Text(notice.headline).font(.system(size: 10, weight: .bold)).foregroundStyle(notice.urgent ? Theme.coral : Theme.cream3)
                Text(notice.subject).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.cream)
                if !notice.body.isEmpty { Text(notice.body).font(.system(size: 12)).foregroundStyle(Theme.cream2) }
                if notice.image != nil {
                    Text("Has a screenshot — open the iPhone app to see it.").font(.system(size: 11)).foregroundStyle(Theme.cream3)
                }
                if let sent {
                    Text("Sent: \(sent)").font(.system(size: 12)).foregroundStyle(Theme.cream3)
                } else {
                    VStack(spacing: 8) {
                        if notice.image != nil {
                            Button { send("Approved") } label: { WatchPill(title: "Looks good", icon: "checkmark", kind: .primary) }
                                .buttonStyle(.plain)
                        }
                        // One tap straight into dictation; the spoken reply goes to the session as-is.
                        TextFieldLink(prompt: Text("Reply…")) {
                            WatchPill(title: "Reply by voice", icon: "mic.fill", kind: notice.image != nil ? .secondary : .primary)
                        } onSubmit: { text in
                            let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
                            guard !t.isEmpty else { return }
                            send(t)
                        }
                        .buttonStyle(.plain)
                        if !isRead {
                            Button {
                                store.markRead([notice.id])
                                dismiss()
                            } label: { WatchPill(title: "Mark as read", icon: "checkmark.circle", kind: .secondary) }
                                .buttonStyle(.plain)
                        }
                    }
                    .padding(.top, 6)
                }
            }
            .padding(.horizontal, 4)
        }
        .navigationTitle("Inbox")
        .background(Theme.background)
    }
}

/// The watch's one button shape: full width, 40pt tall, cream (primary) or glass (secondary).
struct WatchPill: View {
    enum Kind { case primary, secondary, attention }
    let title: String
    let icon: String
    let kind: Kind

    private var colors: (text: Color, fill: Color, stroke: Color) {
        switch kind {
        case .primary: (Theme.navy950, Theme.cream, .clear)
        case .secondary: (Theme.cream, Theme.hair2, Theme.hair)
        case .attention: (Theme.coral, Theme.coralSoft, Theme.coral.opacity(0.35))
        }
    }

    var body: some View {
        let c = colors
        Label(title, systemImage: icon)
            .font(.system(size: 14, weight: .semibold))
            .foregroundStyle(c.text)
            .lineLimit(1).minimumScaleFactor(0.8)
            .frame(maxWidth: .infinity, minHeight: 40)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(c.fill))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(c.stroke, lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}
