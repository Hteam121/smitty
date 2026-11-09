import SwiftUI

struct SessionsView: View {
    @Environment(HubStore.self) private var store
    let onNewTask: (String) -> Void
    @State private var selected: Session? = nil

    private var groups: [(project: String, path: String, sessions: [Session])] {
        var byPath: [String: [Session]] = [:]
        for s in store.snapshot.sessions { byPath[s.project, default: []].append(s) }
        return byPath.keys.sorted().map { p in
            (project: (p as NSString).lastPathComponent, path: p, sessions: byPath[p]!)
        }
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Theme.background
                ScrollView {
                LazyVStack(spacing: 14) {
                    if !store.snapshot.models.choices.isEmpty {
                        let m = store.snapshot.models
                        HStack {
                            Text("New sessions run on").font(.system(size: 13)).foregroundStyle(Theme.cream2)
                            Spacer()
                            ModelMenu(target: "default", current: m.worker == m.workerDefault ? nil : m.worker,
                                      defaultLabel: m.workerDefault.map { m.label($0) + " (configured)" } ?? "Claude Code's default")
                        }
                        .glass(padding: 12)
                    }
                    if groups.isEmpty {
                        EmptyNote(icon: "square.stack.3d.up", title: "No sessions open",
                                  text: store.macOffline ? "The Mac is offline; this is the last known state." :
                                        "Ask Smitty to start work in a project from the Chat tab.")
                    }
                    ForEach(groups, id: \.path) { g in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Eyebrow(text: g.project, color: Theme.cream2)
                                Spacer()
                                Button { onNewTask(g.project) } label: {
                                    HStack(spacing: 4) { Image(systemName: "plus"); Text("New task here") }
                                        .font(.system(size: 12, weight: .semibold)).foregroundStyle(Theme.cream2)
                                }
                            }
                            ForEach(g.sessions) { s in
                                NavigationLink(value: s) { SessionRow(session: s) }
                                    .buttonStyle(.plain)
                            }
                        }
                        .glass()
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 6)
                }
                .refreshable { await store.refresh() }
            }
            .navigationDestination(for: Session.self) { s in StepsView(session: s) }
            .toolbar(.hidden, for: .navigationBar)
        }
    }
}

struct SessionRow: View {
    @Environment(HubStore.self) private var store
    let session: Session
    @State private var sendText = ""
    @State private var showSend = false

    private var statusColor: Color {
        switch session.status {
        case "awaiting_approval", "error": Theme.coral
        case "running", "starting": Theme.tint
        default: Theme.cream3
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Circle().fill(statusColor).frame(width: 8, height: 8)
                Text(session.title).font(.system(size: 15, weight: .semibold)).foregroundStyle(Theme.cream).lineLimit(2)
                Spacer()
                Text(session.status.replacingOccurrences(of: "_", with: " "))
                    .font(.system(size: 11, weight: .semibold)).foregroundStyle(statusColor)
            }
            HStack(spacing: 10) {
                if session.scheduled { Pill(text: "scheduled", icon: "clock") }
                if session.autoApprove { Pill(text: "auto-approve", icon: "checkmark.seal") }
                if !session.lastTool.isEmpty {
                    Text(session.lastTool).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.cream3).lineLimit(1)
                }
                Spacer()
                Text(Fmt.duration(session.runningFor)).font(.system(size: 11)).foregroundStyle(Theme.cream4)
            }
            if !session.lastResult.isEmpty && !session.isBusy {
                Text(session.lastResult).font(.system(size: 13)).foregroundStyle(Theme.cream2).lineLimit(3)
            }
            ForEach(session.subagents, id: \.self) { a in
                HStack(spacing: 6) {
                    Image(systemName: "arrow.turn.down.right").font(.system(size: 10)).foregroundStyle(Theme.cream4)
                    Text(a.description.isEmpty ? a.type : a.description).font(.system(size: 12)).foregroundStyle(Theme.cream2).lineLimit(1)
                    Spacer()
                    Text(a.status).font(.system(size: 11)).foregroundStyle(a.status == "running" ? Theme.tint : Theme.cream4)
                }
                .padding(.leading, 6)
            }
            HStack(spacing: 8) {
                if session.isBusy {
                    Button("Stop") { Task { await store.session(session, "stop") } }.buttonStyle(PillButtonStyle(kind: .attention))
                } else {
                    Button("Send") { showSend.toggle() }.buttonStyle(PillButtonStyle(kind: .primary))
                    Button("Close") { Task { await store.session(session, "close") } }.buttonStyle(PillButtonStyle(kind: .secondary))
                }
                Spacer()
                if !store.snapshot.models.choices.isEmpty {
                    let m = store.snapshot.models
                    ModelMenu(target: session.key, current: session.model,
                              defaultLabel: session.model == nil && !session.modelUsed.isEmpty ? "Default · \(m.label(session.modelUsed))" : "Default")
                }
            }
            if showSend {
                HStack(spacing: 8) {
                    TextField("Tell this session…", text: $sendText, axis: .vertical)
                        .lineLimit(1...4).font(.system(size: 14)).foregroundStyle(Theme.cream)
                        .padding(.horizontal, 12).padding(.vertical, 8)
                        .background(Capsule().fill(Theme.hair2)).overlay(Capsule().stroke(Theme.hair, lineWidth: 1))
                    Button {
                        let t = sendText.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard !t.isEmpty else { return }
                        sendText = ""; showSend = false
                        Task { await store.session(session, "send", text: t) }
                    } label: {
                        Image(systemName: "arrow.up").font(.system(size: 14, weight: .bold)).foregroundStyle(Theme.navy950)
                            .frame(width: 32, height: 32).background(Circle().fill(Theme.cream))
                    }
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Theme.navy950.opacity(0.35)))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Theme.hair2, lineWidth: 1))
    }
}

/// A model picker: a pill showing the current model that opens the list the Mac's Claude Code offers.
/// A session that is in the middle of a turn switches at its next step.
struct ModelMenu: View {
    @Environment(HubStore.self) private var store
    let target: String          // a session key, or "default" for new sessions
    let current: String?        // the chosen model id; nil = the default
    let defaultLabel: String

    var body: some View {
        let m = store.snapshot.models
        Menu {
            Button { pick("") } label: { row(defaultLabel, on: current == nil) }
            ForEach(m.choices.filter { !$0.older }) { c in
                Button { pick(c.id) } label: { row(c.label, on: c.id == current) }
            }
            if m.choices.contains(where: \.older) {
                Menu("Earlier models") {
                    ForEach(m.choices.filter(\.older)) { c in
                        Button { pick(c.id) } label: { row(c.label, on: c.id == current) }
                    }
                }
            }
        } label: {
            Pill(text: current.map(m.label) ?? defaultLabel, icon: "cpu")
        }
        .disabled(store.macOffline)
        .accessibilityLabel(target == "default" ? "The model new sessions start on" : "The model this session runs on")
    }

    @ViewBuilder private func row(_ text: String, on: Bool) -> some View {
        if on { Label(text, systemImage: "checkmark") } else { Text(text) }
    }

    private func pick(_ id: String) {
        guard id != (current ?? "") else { return }
        Task { await store.setModel(target, id) }
    }
}
