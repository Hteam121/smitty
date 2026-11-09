import Foundation
import Observation

/// What the screens read. Polls the hub every few seconds while the app is in front.
@MainActor @Observable
final class HubStore {
    let device: String
    private(set) var config: HubConfig
    let client: HubClient

    var snapshot = HubSnapshot()
    var notices: [Notice] = []
    var messages: [ChatMessage] = []
    var pending: [Command] = []           // commands we sent that the Mac has not settled yet
    var lastError: String? = nil
    var lastSync: Date? = nil
    var polling = false

    private var pollTask: Task<Void, Never>?
    private var imageCache: [String: Data] = [:]
    private var locallyRead: Set<Int64> = []      // read on this device, not yet confirmed by the Mac
    private var readQueue: Set<Int64> = []        // ids waiting to go out in one mark_read command
    private var readFlush: Task<Void, Never>?

    init(device: String) {
        self.device = device
        let cfg = Keychain.load()
        config = cfg
        client = HubClient(config: cfg)
        Task { [client] in
            await client.setConfigObserver { [weak self] cfg in
                Task { @MainActor in self?.config = cfg }
            }
        }
    }

    var isSignedIn: Bool { config.isSignedIn }
    var unreadCount: Int { notices.filter { !$0.isRead }.count }
    var approvals: [Approval] { snapshot.approvals }
    var macOffline: Bool { !snapshot.macOnline }

    /// "Mac offline since 14:02" or nil when it is online.
    var offlineNote: String? {
        guard macOffline else { return nil }
        guard let seen = snapshot.macSeen else { return "Mac offline" }
        return "Mac offline since \(Fmt.time.string(from: seen))"
    }

    // MARK: config

    func configure(url: String, anonKey: String) async {
        var cfg = config
        cfg.url = url.trimmingCharacters(in: .whitespacesAndNewlines)
        cfg.anonKey = anonKey.trimmingCharacters(in: .whitespacesAndNewlines)
        if cfg.url != config.url { cfg.refreshToken = nil; cfg.accessToken = nil }
        config = cfg
        Keychain.save(cfg)
        await client.update(config: cfg)
    }

    func adopt(_ cfg: HubConfig) async {
        config = cfg
        Keychain.save(cfg)
        await client.update(config: cfg)
        await refresh()
    }

    func signIn(email: String, password: String) async throws {
        try await client.signIn(email: email, password: password)
        config = await client.config
        await refresh()
    }

    func signOut() async {
        await client.signOut()
        config = await client.config
        snapshot = HubSnapshot(); notices = []; messages = []; pending = []
    }

    // MARK: polling

    func startPolling(every seconds: Double = 3) {
        stopPolling()
        polling = true
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel(); pollTask = nil; polling = false
    }

    func refresh() async {
        guard config.isSignedIn else { return }
        do {
            async let s = client.snapshot()
            async let n = client.notices()
            async let m = client.chat()
            let (snap, notes, msgs) = try await (s, n, m)
            snapshot = snap; messages = msgs
            notices = notes.map { n in
                if n.isRead { locallyRead.remove(n.id); return n }
                guard locallyRead.contains(n.id) else { return n }
                var m = n; m.read = Date(); return m       // keep it read until the Mac catches up
            }
            try await settlePending()
            lastError = nil
            lastSync = Date()
        } catch {
            lastError = error.localizedDescription
            config = await client.config          // sign-out on a dead refresh token shows up here
        }
    }

    private func settlePending() async throws {
        guard !pending.isEmpty else { return }
        let ids = pending.compactMap(\.id)
        let fresh = try await client.commands(ids: ids)
        var keep: [Command] = []
        for p in pending {
            if let f = fresh.first(where: { $0.id == p.id }) {
                if f.status == "failed" { lastError = f.error }
                if !f.isSettled { keep.append(f) }
            } else { keep.append(p) }
        }
        pending = keep
    }

    // MARK: actions

    @discardableResult
    func send(_ kind: String, _ body: [String: Any]) async -> Command? {
        do {
            let c = try await client.send(kind, body, device: device)
            pending.append(c)
            lastError = nil
            return c
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    func say(_ text: String) async { await send("chat", ["text": text]) }
    func answer(_ approval: Approval, _ answer: String) async { await send("approval", ["id": approval.id, "answer": answer]) }
    func reply(_ notice: Notice, _ text: String) async { await send("notice_reply", ["id": notice.id, "text": text]) }
    func session(_ s: Session, _ action: String, text: String? = nil) async {
        var b: [String: Any] = ["key": s.key, "action": action]
        if let text { b["text"] = text }
        await send("session", b)
    }
    /// Shows the notices read right away and sends one mark_read for the whole burst (cards marking
    /// themselves as they scroll into view, or "Mark all read") instead of one command per card.
    func markRead(_ ids: [Int64]) {
        let unread = ids.filter { id in notices.first { $0.id == id }?.isRead == false }
        guard !unread.isEmpty else { return }
        notices = notices.map { n in
            guard unread.contains(n.id) else { return n }
            var m = n; m.read = Date(); return m
        }
        locallyRead.formUnion(unread)
        readQueue.formUnion(unread)
        readFlush?.cancel()
        readFlush = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(700))
            guard !Task.isCancelled, let self else { return }
            let batch = Array(self.readQueue).sorted()
            self.readQueue = []
            guard !batch.isEmpty else { return }
            await self.send("mark_read", ["ids": batch])
        }
    }

    func markAllRead() { markRead(notices.filter { !$0.isRead }.map(\.id)) }

    func image(_ path: String) async -> Data? {
        if let d = imageCache[path] { return d }
        guard let d = try? await client.artifact(path) else { return nil }
        imageCache[path] = d
        return d
    }

    /// The pending chat commands, shown as bubbles until the Mac has answered.
    var pendingChat: [Command] { pending.filter { $0.kind == "chat" } }
}
