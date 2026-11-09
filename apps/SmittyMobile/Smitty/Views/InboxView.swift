import SwiftUI

struct InboxView: View {
    @Environment(HubStore.self) private var store

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 12) {
                if store.approvals.isEmpty && store.notices.isEmpty {
                    EmptyNote(icon: "tray", title: "Nothing waiting", text: "Finished sessions, questions and approvals show up here.")
                }
                ForEach(store.approvals) { a in ApprovalCard(approval: a) }
                ForEach(store.notices) { n in NoticeCard(notice: n) }
            }
            .padding(.horizontal, 14).padding(.vertical, 6)
        }
        .refreshable { await store.refresh() }
    }
}

/// A pending tool approval: the whole question, then one tap per answer.
struct ApprovalCard: View {
    @Environment(HubStore.self) private var store
    let approval: Approval
    @State private var answered: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Pill(text: "Needs approval", icon: "exclamationmark.circle", color: Theme.coral, fill: Theme.coralSoft)
                Spacer()
                Text(Fmt.ago(approval.askedAt)).font(.system(size: 11)).foregroundStyle(Theme.cream4)
            }
            if let s = approval.session ?? approval.project {
                Eyebrow(text: s)
            }
            Text(approval.question).font(.system(size: 15)).foregroundStyle(Theme.cream)
            if let action = approval.action, !action.isEmpty {
                Text(action)
                    .font(.system(size: 12, design: .monospaced))
                    .foregroundStyle(Theme.cream2)
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Theme.navy950.opacity(0.6)))
            }
            if let answered {
                Text("Sent: \(answered)").font(.system(size: 12)).foregroundStyle(Theme.cream3)
            } else {
                HStack(spacing: 8) {
                    Button("Allow") { answer("yes") }.buttonStyle(PillButtonStyle(kind: .primary))
                    Button("Deny") { answer("no") }.buttonStyle(PillButtonStyle(kind: .secondary))
                    Button("Allow all") { answer("all") }.buttonStyle(PillButtonStyle(kind: .secondary))
                }
            }
        }
        .glass()
        .overlay(RoundedRectangle(cornerRadius: Theme.radius, style: .continuous).stroke(Theme.coral.opacity(0.35), lineWidth: 1))
    }

    private func answer(_ a: String) {
        answered = ["yes": "Allow", "no": "Deny", "all": "Allow all"][a]
        Task { await store.answer(approval, a) }
    }
}

struct NoticeCard: View {
    @Environment(HubStore.self) private var store
    let notice: Notice
    @State private var reply = ""
    @State private var sent: String? = nil
    @State private var showImage = false
    @State private var imageData: Data? = nil

    private var isAttention: Bool { notice.urgent && !notice.isRead }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 8) {
                HStack(spacing: 6) {
                    if isAttention {
                        Image(systemName: "exclamationmark.circle.fill").font(.system(size: 12)).foregroundStyle(Theme.coral)
                    } else if !notice.isRead {
                        Circle().fill(Theme.tint).frame(width: 7, height: 7)      // unread
                    }
                    Text(notice.headline).font(.system(size: 11, weight: .semibold)).tracking(1).textCase(.uppercase)
                        .foregroundStyle(isAttention ? Theme.coral : notice.isRead ? Theme.cream4 : Theme.cream3)
                }
                Spacer()
                Text(Fmt.ago(notice.ts)).font(.system(size: 11)).foregroundStyle(Theme.cream4)
                if !notice.isRead {
                    Button { withAnimation { store.markRead([notice.id]) } } label: {
                        Label("Mark read", systemImage: "checkmark").font(.system(size: 11, weight: .semibold))
                    }
                    .buttonStyle(SmallPillButtonStyle())
                }
            }
            Text(notice.subject).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.cream)
            if !notice.body.isEmpty {
                ExpandableText(notice.body, font: .system(size: 14), color: Theme.cream2, lines: 5, threshold: 280)
            }
            if let path = notice.image {
                screenshot(path)
            }
            if let sent {
                Text("Sent: \(sent)").font(.system(size: 12)).foregroundStyle(Theme.cream3)
            } else {
                replyRow
            }
        }
        .glass()
        .fullScreenCover(isPresented: $showImage) {
            ImageViewer(data: imageData, title: notice.subject) { text in send(text) }
        }
    }

    @ViewBuilder private func screenshot(_ path: String) -> some View {
        Group {
            if let d = imageData, let ui = UIImage(data: d) {
                Image(uiImage: ui).resizable().scaledToFit()
                    .frame(maxHeight: 220)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .onTapGesture { showImage = true }
            } else {
                HStack { ProgressView().tint(Theme.cream3); Text("Loading screenshot…").font(.system(size: 12)).foregroundStyle(Theme.cream3) }
                    .frame(height: 60)
            }
        }
        .task { if imageData == nil { imageData = await store.image(path) } }
        if sent == nil {
            HStack(spacing: 8) {
                Button("Approve") { send("Approved") }.buttonStyle(PillButtonStyle(kind: .primary))
                Button("Request changes") { showImage = true }.buttonStyle(PillButtonStyle(kind: .secondary))
            }
        }
    }

    private var replyRow: some View {
        HStack(spacing: 8) {
            TextField(notice.canReplyToSession ? "Reply to this session…" : "Reply to Smitty…", text: $reply, axis: .vertical)
                .lineLimit(1...4)
                .font(.system(size: 14)).foregroundStyle(Theme.cream)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Capsule().fill(Theme.hair2))
                .overlay(Capsule().stroke(Theme.hair, lineWidth: 1))
            Button {
                let t = reply.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !t.isEmpty else { return }
                send(t)
            } label: {
                Image(systemName: "arrow.up").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.navy950)
                    .frame(width: 32, height: 32).background(Circle().fill(reply.isEmpty ? Theme.cream4 : Theme.cream))
            }
            .disabled(reply.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private func send(_ text: String) {
        sent = text
        reply = ""
        showImage = false
        store.markRead([notice.id])
        Task { await store.reply(notice, text) }
    }
}

/// Compact capsule for in-card actions like "Mark read".
struct SmallPillButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(Theme.cream2)
            .padding(.horizontal, 9).padding(.vertical, 5)
            .background(Capsule().fill(Theme.hair2))
            .overlay(Capsule().stroke(Theme.hair, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.6 : 1)
    }
}

/// Full-screen screenshot with "Approve" / "Request changes".
struct ImageViewer: View {
    let data: Data?
    let title: String
    let onSend: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var changes = ""
    @State private var scale: CGFloat = 1

    var body: some View {
        ZStack {
            Theme.navy950.ignoresSafeArea()
            VStack(spacing: 12) {
                HStack {
                    Text(title).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.cream).lineLimit(1)
                    Spacer()
                    Button { dismiss() } label: { Image(systemName: "xmark").foregroundStyle(Theme.cream2) }
                }
                .padding(.horizontal, 16)
                if let data, let ui = UIImage(data: data) {
                    ScrollView([.horizontal, .vertical]) {
                        Image(uiImage: ui).resizable().scaledToFit()
                            .scaleEffect(scale)
                            .gesture(MagnifyGesture().onChanged { v in scale = max(1, min(4, v.magnification)) })
                    }
                } else {
                    Spacer(); Text("No image").foregroundStyle(Theme.cream3); Spacer()
                }
                VStack(spacing: 8) {
                    TextField("What should change?", text: $changes, axis: .vertical)
                        .lineLimit(1...4)
                        .font(.system(size: 14)).foregroundStyle(Theme.cream)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(RoundedRectangle(cornerRadius: 14).fill(Theme.hair2))
                        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Theme.hair, lineWidth: 1))
                    HStack(spacing: 8) {
                        Button("Approve") { onSend("Approved") }.buttonStyle(PillButtonStyle(kind: .primary))
                        Button("Request changes") {
                            let t = changes.trimmingCharacters(in: .whitespacesAndNewlines)
                            guard !t.isEmpty else { return }
                            onSend("Please change: \(t)")
                        }
                        .buttonStyle(PillButtonStyle(kind: .secondary))
                        .disabled(changes.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                }
                .padding(.horizontal, 16).padding(.bottom, 8)
            }
            .padding(.top, 12)
        }
        .preferredColorScheme(.dark)
    }
}
