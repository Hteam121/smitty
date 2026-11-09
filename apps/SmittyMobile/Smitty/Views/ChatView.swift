import SwiftUI

struct ChatView: View {
    @Environment(HubStore.self) private var store
    @Binding var draft: String
    @FocusState private var focused: Bool
    @State private var dictation = Dictation()
    @State private var dictBase = ""            // what was in the field before the mic started

    private var draftEmpty: Bool { draft.trimmingCharacters(in: .whitespaces).isEmpty }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 10) {
                        if store.messages.isEmpty && store.pendingChat.isEmpty {
                            EmptyNote(icon: "bubble.left.and.text.bubble.right", title: "Talk to Smitty",
                                      text: "Ask what's running, start work in a project, or just say hi.")
                        }
                        ForEach(store.messages) { m in
                            Bubble(text: m.text, isUser: m.isUser, time: Fmt.time.string(from: m.ts), about: m.about)
                                .id(m.id)
                        }
                        ForEach(store.pendingChat) { c in
                            Bubble(text: c.body["text"]?.string ?? "", isUser: true, time: nil, about: nil,
                                   status: c.status == "running" ? "Smitty is on it" :
                                           (store.macOffline ? "Queued, Mac offline" : "Sending…"))
                                .id(c.id?.uuidString ?? "")
                        }
                        if store.snapshot.chatBusy { TypingDots().id("typing") }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(.horizontal, 14).padding(.top, 6)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: store.messages.count) { _, _ in withAnimation { proxy.scrollTo("bottom") } }
                .onChange(of: store.pendingChat.count) { _, _ in withAnimation { proxy.scrollTo("bottom") } }
                .onAppear { proxy.scrollTo("bottom") }
            }
            composer
        }
    }

    /// Text field with a mic: tap the mic to dictate (the words land in the field live), tap the
    /// field to go back to typing, send with the arrow either way.
    private var composer: some View {
        VStack(spacing: 6) {
            if dictation.isListening {
                HStack(spacing: 8) {
                    Circle().fill(Theme.coral)
                        .frame(width: 8, height: 8)
                        .scaleEffect(1 + CGFloat(dictation.level) * 1.6)
                        .animation(.easeOut(duration: 0.12), value: dictation.level)
                    Text(dictation.state == .starting ? "Starting the mic…" :
                         dictation.transcript.isEmpty ? "Listening… say something" : "Listening… tap the field to type instead")
                        .font(.system(size: 12)).foregroundStyle(Theme.cream3)
                    Spacer()
                }
                .padding(.horizontal, 6)
            } else if let why = dictation.problem {
                HStack(spacing: 6) {
                    Image(systemName: "mic.slash").font(.system(size: 11)).foregroundStyle(Theme.coral)
                    Text(why).font(.system(size: 12)).foregroundStyle(Theme.cream3)
                    Spacer()
                }
                .padding(.horizontal, 6)
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField(dictation.isListening ? "Listening…" : "Tell Smitty…", text: $draft, axis: .vertical)
                    .lineLimit(1...5)
                    .focused($focused)
                    .font(.system(size: 15))
                    .foregroundStyle(Theme.cream)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(Capsule().fill(Theme.hair2))
                    .overlay(Capsule().stroke(dictation.isListening ? Theme.coral.opacity(0.6) : Theme.hair, lineWidth: 1))
                    .onSubmit(send)
                if dictation.isListening {
                    roundButton("stop.fill", fill: Theme.coral, tint: Theme.navy950) { dictation.stop() }
                } else if draftEmpty {
                    roundButton("mic.fill", fill: Theme.hair2, tint: Theme.cream) { startDictation() }
                } else {
                    roundButton("arrow.up", fill: Theme.cream, tint: Theme.navy950, action: send)
                }
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .onChange(of: dictation.transcript) { _, t in
            if !t.isEmpty { draft = dictBase + t }
        }
        .onChange(of: focused) { _, f in
            if f, dictation.isListening { dictation.stop() }      // tapping the field = switch to typing
        }
    }

    private func roundButton(_ icon: String, fill: Color, tint: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 16, weight: .bold))
                .foregroundStyle(tint)
                .frame(width: 38, height: 38)
                .background(Circle().fill(fill))
                .overlay(Circle().stroke(Theme.hair, lineWidth: fill == Theme.hair2 ? 1 : 0))
        }
    }

    private func startDictation() {
        focused = false
        let base = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        dictBase = base.isEmpty ? "" : base + " "
        Task { await dictation.start() }
    }

    private func send() {
        if dictation.isListening { dictation.stop() }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        dictBase = ""
        Task { await store.say(text) }
    }
}

struct Bubble: View {
    let text: String
    let isUser: Bool
    let time: String?
    let about: String?
    var status: String? = nil

    var body: some View {
        HStack {
            if isUser { Spacer(minLength: 50) }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 4) {
                if let about, !about.isEmpty {
                    Eyebrow(text: "about \(about)")
                }
                ExpandableText(text, font: .system(size: 15), color: isUser ? Theme.navy950 : Theme.cream,
                               moreColor: isUser ? Theme.navy700 : Theme.tint)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .fill(isUser ? AnyShapeStyle(Theme.cream) :
                                  AnyShapeStyle(LinearGradient(colors: [Theme.glassTop, Theme.glassBottom], startPoint: .topLeading, endPoint: .bottomTrailing)))
                    )
                    .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(isUser ? .clear : Theme.hair, lineWidth: 1))
                    .textSelection(.enabled)
                if let status {
                    HStack(spacing: 4) {
                        Image(systemName: status.hasPrefix("Queued") ? "clock" : "ellipsis").font(.system(size: 10))
                        Text(status)
                    }
                    .font(.system(size: 11)).foregroundStyle(Theme.cream3)
                } else if let time {
                    Text(time).font(.system(size: 11)).foregroundStyle(Theme.cream4)
                }
            }
            if !isUser { Spacer(minLength: 50) }
        }
    }
}

/// Long text starts folded to a few lines with a "More" link; tap to open, "Less" to fold again.
struct ExpandableText: View {
    let text: String
    var font: Font = .system(size: 15)
    var color: Color = Theme.cream
    var moreColor: Color = Theme.tint
    var lines: Int = 6
    var threshold: Int = 320          // characters before we bother folding
    @State private var expanded = false

    init(_ text: String, font: Font = .system(size: 15), color: Color = Theme.cream, moreColor: Color = Theme.tint,
         lines: Int = 6, threshold: Int = 320) {
        self.text = text; self.font = font; self.color = color; self.moreColor = moreColor
        self.lines = lines; self.threshold = threshold
    }

    private var foldable: Bool { text.count > threshold || text.filter { $0 == "\n" }.count >= lines }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(text)
                .font(font)
                .foregroundStyle(color)
                .lineLimit(foldable && !expanded ? lines : nil)
                .textSelection(.enabled)
            if foldable {
                Button { withAnimation(.easeInOut(duration: 0.2)) { expanded.toggle() } } label: {
                    HStack(spacing: 3) {
                        Text(expanded ? "Less" : "More")
                        Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.system(size: 10, weight: .bold))
                    }
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(moreColor)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

struct TypingDots: View {
    @State private var on = false
    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { i in
                Circle().fill(Theme.tint).frame(width: 7, height: 7)
                    .opacity(on ? 1 : 0.25)
                    .animation(.easeInOut(duration: 0.6).repeatForever().delay(Double(i) * 0.2), value: on)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .glass(radius: 18, padding: 0)
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear { on = true }
    }
}
