import Foundation

// MARK: - JSON

/// Any JSON value (Postgres jsonb columns and loosely typed snapshots).
enum JSONValue: Codable, Equatable, Hashable {
    case string(String), number(Double), bool(Bool), null
    case array([JSONValue]), object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else { throw DecodingError.dataCorruptedError(in: c, debugDescription: "not JSON") }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .null: try c.encodeNil()
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }
    var string: String? { if case .string(let s) = self { return s }; return nil }
    var double: Double? {
        switch self { case .number(let n): return n; case .string(let s): return Double(s); default: return nil }
    }
    var int: Int? { double.map { Int($0) } }
    var bool: Bool? {
        switch self { case .bool(let b): return b; case .number(let n): return n != 0; default: return nil }
    }
    var array: [JSONValue]? { if case .array(let a) = self { return a }; return nil }
    var object: [String: JSONValue]? { if case .object(let o) = self { return o }; return nil }
    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(n)
        case .bool(let b): return b ? "true" : "false"
        case .null: return ""
        default: return (try? String(data: JSONEncoder().encode(self), encoding: .utf8))?.flatMap { $0 } ?? ""
        }
    }

    static func from(_ any: Any) -> JSONValue {
        switch any {
        case let s as String: return .string(s)
        case let b as Bool: return .bool(b)
        case let n as NSNumber: return .number(n.doubleValue)
        case let n as Int: return .number(Double(n))
        case let n as Double: return .number(n)
        case let a as [Any]: return .array(a.map(from))
        case let o as [String: Any]: return .object(o.mapValues(from))
        default: return .null
        }
    }
}

// MARK: - Dates

enum HubDate {
    private static let frac: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
    private static let plain: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime]; return f
    }()
    /// Postgres gives "2026-11-06T18:02:11.123456+00:00"; ISO8601DateFormatter wants at most 3 fraction digits.
    static func parse(_ s: String) -> Date? {
        if let d = frac.date(from: s) ?? plain.date(from: s) { return d }
        var t = s
        if let dot = t.firstIndex(of: "."), let tz = t[dot...].firstIndex(where: { $0 == "+" || $0 == "-" || $0 == "Z" }) {
            let digits = t[t.index(after: dot)..<tz].prefix(3)
            t = String(t[..<dot]) + "." + digits + String(t[tz...])
        }
        if t.hasSuffix("+00") { t += ":00" }
        return frac.date(from: t) ?? plain.date(from: t)
    }
    static func string(_ d: Date) -> String { frac.string(from: d) }
}

/// Decodes a timestamptz column.
@propertyWrapper
struct HubTimestamp: Codable, Equatable, Hashable {
    var wrappedValue: Date
    init(wrappedValue: Date) { self.wrappedValue = wrappedValue }
    init(from decoder: Decoder) throws {
        let s = try decoder.singleValueContainer().decode(String.self)
        guard let d = HubDate.parse(s) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "bad timestamp \(s)"))
        }
        wrappedValue = d
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer(); try c.encode(HubDate.string(wrappedValue))
    }
}

@propertyWrapper
struct HubTimestampOptional: Codable, Equatable, Hashable {
    var wrappedValue: Date?
    init(wrappedValue: Date?) { self.wrappedValue = wrappedValue }
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { wrappedValue = nil; return }
        wrappedValue = HubDate.parse(try c.decode(String.self))
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        if let d = wrappedValue { try c.encode(HubDate.string(d)) } else { try c.encodeNil() }
    }
}

extension KeyedDecodingContainer {
    /// Missing optional timestamps decode as nil instead of failing.
    func decode(_ type: HubTimestampOptional.Type, forKey key: Key) throws -> HubTimestampOptional {
        try decodeIfPresent(type, forKey: key) ?? HubTimestampOptional(wrappedValue: nil)
    }
}

// MARK: - Rows

struct StateRow: Codable {
    let key: String
    let value: JSONValue
    @HubTimestamp var updated: Date
}

struct Notice: Codable, Identifiable, Hashable {
    let id: Int64
    @HubTimestamp var ts: Date
    let kind: String
    let title: String
    let body: String
    let session: String?
    let urgent: Bool
    @HubTimestampOptional var read: Date?
    let detail: JSONValue
    let image: String?

    var isRead: Bool { read != nil }
    var project: String? { detail["project"]?.string }
    var headline: String {
        // "Finished: Dashboard polish" -> ("Finished", "Dashboard polish")
        title.split(separator: ":", maxSplits: 1).first.map(String.init) ?? title
    }
    var subject: String {
        let parts = title.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        return parts.count > 1 ? parts[1].trimmingCharacters(in: .whitespaces) : title
    }
    /// A notice from one of Smitty's own sessions can be continued with a reply.
    var canReplyToSession: Bool { session != nil && (detail["own"]?.bool ?? true) }
}

struct ChatMessage: Codable, Identifiable, Hashable {
    let id: Int64
    @HubTimestamp var ts: Date
    let role: String        // heard | said
    let text: String
    let detail: JSONValue
    var isUser: Bool { role == "heard" }
    var about: String? { detail["about"]?.string }
}

struct Step: Codable, Identifiable, Hashable {
    let id: Int64
    @HubTimestamp var ts: Date
    let session: String
    let kind: String        // prompt | thinking | text | tool | tool_result | agent | result | error
    let text: String
    let detail: JSONValue
    var tool: String? { detail["tool"]?.string }
    var agent: String? { detail["agent"]?.string }
    var isError: Bool { kind == "error" || (detail["error"]?.bool ?? false) }
}

struct Command: Codable, Identifiable, Hashable {
    var id: UUID?
    @HubTimestampOptional var created: Date?
    var device: String?
    var kind: String
    var body: JSONValue
    var status: String?
    var result: JSONValue?

    var isSettled: Bool { status == "done" || status == "failed" }
    var error: String? { status == "failed" ? (result?["error"]?.string ?? "failed") : nil }
}

// MARK: - Snapshot

struct Subagent: Hashable {
    let type: String, description: String, status: String, lastTool: String, seconds: Int
    init(_ j: JSONValue) {
        type = j["type"]?.string ?? ""; description = j["description"]?.string ?? ""
        status = j["status"]?.string ?? ""; lastTool = j["last_tool"]?.string ?? ""; seconds = j["seconds"]?.int ?? 0
    }
}

struct Session: Identifiable, Hashable {
    let key: String, sessionId: String?, title: String, scheduled: Bool, turns: Int
    let subagents: [Subagent], project: String, status: String, autoApprove: Bool
    let lastTool: String, lastResult: String, runningFor: Int
    /// The model this session was told to use (nil = the default) and the one its last reply came from.
    let model: String?, modelUsed: String
    var id: String { key }
    var projectName: String { (project as NSString).lastPathComponent }
    var isBusy: Bool { status == "running" || status == "starting" }
    init(_ j: JSONValue) {
        key = j["key"]?.string ?? UUID().uuidString
        sessionId = j["session_id"]?.string
        title = j["title"]?.string ?? "Session"
        scheduled = j["scheduled"]?.bool ?? false
        turns = j["turns"]?.int ?? 0
        subagents = (j["subagents"]?.array ?? []).map(Subagent.init)
        project = j["project"]?.string ?? ""
        status = j["status"]?.string ?? ""
        autoApprove = j["auto_approve"]?.bool ?? false
        lastTool = j["last_tool"]?.string ?? ""
        lastResult = j["last_result"]?.string ?? ""
        runningFor = j["running_for_s"]?.int ?? 0
        model = j["model"]?.string
        modelUsed = j["model_used"]?.string ?? ""
    }
}

/// One model the Mac's Claude Code can run a session on.
struct ModelChoice: Identifiable, Hashable {
    let id: String, label: String, older: Bool
    init(_ j: JSONValue) {
        id = j["id"]?.string ?? ""
        label = j["label"]?.string ?? ModelSettings.name(id)
        older = j["older"]?.bool ?? false
    }
}

/// The model pickers' state: what can be chosen, and what new sessions run on.
struct ModelSettings {
    var choices: [ModelChoice] = []
    var worker: String? = nil, workerDefault: String? = nil

    init() {}
    init(_ j: JSONValue) {
        choices = (j["choices"]?.array ?? []).map(ModelChoice.init).filter { !$0.id.isEmpty }
        worker = j["worker"]?.string
        workerDefault = j["worker_default"]?.string
    }

    func label(_ id: String) -> String { choices.first { $0.id == id }?.label ?? Self.name(id) }

    /// claude-opus-5-5 -> Opus 5.5 (a trailing date stamp is dropped).
    static func name(_ id: String) -> String {
        let parts = id.replacingOccurrences(of: "claude-", with: "").split(separator: "-").map(String.init)
            .filter { !($0.count == 8 && Int($0) != nil) }
        let words = parts.filter { Int($0) == nil }.map(\.capitalized).joined(separator: " ")
        let nums = parts.filter { Int($0) != nil }.joined(separator: ".")
        let out = [words, nums].filter { !$0.isEmpty }.joined(separator: " ")
        return out.isEmpty ? id : out
    }
}

struct Approval: Identifiable, Hashable {
    let id: String, question: String, askedAt: Date, expiresAt: Date?
    let session: String?, key: String?, reason: String?, action: String?, project: String?
    init(_ j: JSONValue) {
        id = j["id"]?.string ?? ""
        question = j["question"]?.string ?? ""
        askedAt = Date(timeIntervalSince1970: j["asked_at"]?.double ?? 0)
        expiresAt = j["expires_at"]?.double.map { Date(timeIntervalSince1970: $0) }
        session = j["session"]?.string; key = j["key"]?.string; reason = j["reason"]?.string
        action = j["action"]?.string; project = j["project"]?.string
    }
}

/// Everything in hub_state, decoded once.
struct HubSnapshot {
    var state = SmittyState(raw: "offline")
    var sessions: [Session] = []
    var approvals: [Approval] = []
    var models = ModelSettings()
    var chatBusy = false
    var attention = "active"
    var macOnline = false
    var macSeen: Date? = nil
    var mode: String? = nil
    var voice = false
    var fetchedAt = Date.distantPast

    static let staleAfter: TimeInterval = 20

    init() {}
    init(rows: [StateRow]) {
        for r in rows {
            switch r.key {
            case "state": state = SmittyState(raw: r.value.string ?? "offline")
            case "sessions": sessions = (r.value.array ?? []).map(Session.init)
            case "approvals": approvals = (r.value.array ?? []).map(Approval.init).filter { !$0.id.isEmpty }
            case "models": models = ModelSettings(r.value)
            case "chat": chatBusy = r.value["busy"]?.bool ?? false
            case "attention": attention = r.value.string ?? "active"
            case "status": mode = r.value["mode"]?.string; voice = r.value["voice"]?.bool ?? false
            case "mac":
                let seen = r.value["seen"]?.string.flatMap(HubDate.parse) ?? r.updated
                macSeen = seen
                macOnline = (r.value["online"]?.bool ?? false) && Date().timeIntervalSince(seen) < Self.staleAfter
            default: break
            }
        }
        fetchedAt = Date()
        if !macOnline { state = SmittyState(raw: "offline") }
        else if !approvals.isEmpty { state = SmittyState(raw: "needs_confirmation") }
    }
}
