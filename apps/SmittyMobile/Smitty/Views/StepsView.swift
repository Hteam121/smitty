import SwiftUI

/// What one session did, step by step (the dashboard's "View steps" drawer).
struct StepsView: View {
    @Environment(HubStore.self) private var store
    @Environment(\.dismiss) private var dismiss
    let session: Session
    @State private var steps: [Step] = []
    @State private var error: String? = nil

    private var live: Session? { store.snapshot.sessions.first { $0.key == session.key } }

    var body: some View {
        ZStack {
            Theme.background
            VStack(spacing: 0) {
                HStack(spacing: 10) {
                    Button { dismiss() } label: { Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(Theme.cream2) }
                    VStack(alignment: .leading, spacing: 2) {
                        Text((live ?? session).title).font(.system(size: 16, weight: .semibold)).foregroundStyle(Theme.cream).lineLimit(1)
                        Text("\(session.projectName) · \((live ?? session).status.replacingOccurrences(of: "_", with: " "))")
                            .font(.system(size: 12)).foregroundStyle(Theme.cream3)
                    }
                    Spacer()
                    if live?.isBusy == true { ProgressView().tint(Theme.tint) }
                }
                .padding(.horizontal, 16).padding(.vertical, 10)
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            if steps.isEmpty {
                                Text(error ?? "No steps recorded yet.").font(.system(size: 13)).foregroundStyle(Theme.cream3).padding(.top, 30)
                                    .frame(maxWidth: .infinity)
                            }
                            ForEach(steps) { StepRow(step: $0).id($0.id) }
                            Color.clear.frame(height: 1).id("end")
                        }
                        .padding(.horizontal, 14).padding(.bottom, 20)
                    }
                    .onChange(of: steps.count) { _, _ in proxy.scrollTo("end") }
                }
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .task { await poll() }
    }

    private func poll() async {
        while !Task.isCancelled {
            do {
                let after = steps.last?.id ?? 0
                let fresh = try await store.client.steps(session: session.key, after: after)
                if after == 0 { steps = fresh } else if !fresh.isEmpty { steps += fresh }
                error = nil
            } catch { self.error = error.localizedDescription }
            try? await Task.sleep(for: .seconds(2))
        }
    }
}

struct StepRow: View {
    let step: Step

    private var label: String {
        switch step.kind {
        case "prompt": "Prompt"
        case "thinking": "Thinking"
        case "text": "Said"
        case "tool": step.tool ?? "Tool"
        case "tool_result": "Result"
        case "agent": "Subagent"
        case "model": "Model"
        case "result": step.isError ? "Failed" : "Finished"
        case "error": "Error"
        default: step.kind
        }
    }
    private var mono: Bool { step.kind == "tool" || step.kind == "tool_result" }
    private var color: Color {
        switch step.kind {
        case "error": Theme.coral
        case "result": step.isError ? Theme.coral : Theme.cream
        case "thinking": Theme.cream3
        case "tool": Theme.tint
        default: Theme.cream2
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(label.uppercased()).font(.system(size: 10, weight: .bold)).tracking(1).foregroundStyle(color)
                if let a = step.agent { Text("· \(a)").font(.system(size: 10)).foregroundStyle(Theme.cream4).lineLimit(1) }
                Spacer()
                Text(Fmt.time.string(from: step.ts)).font(.system(size: 10)).foregroundStyle(Theme.cream4)
            }
            Text(step.text)
                .font(mono ? .system(size: 12, design: .monospaced) : .system(size: 14))
                .foregroundStyle(step.kind == "thinking" ? Theme.cream3 : Theme.cream2)
                .lineLimit(step.kind == "tool_result" || step.kind == "thinking" ? 8 : nil)
                .textSelection(.enabled)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(step.kind == "prompt" ? Theme.cream.opacity(0.06) : Theme.navy950.opacity(0.35)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Theme.hair2, lineWidth: 1))
    }
}
