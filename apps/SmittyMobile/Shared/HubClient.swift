import Foundation

/// Plain-URLSession client for the Supabase hub: GoTrue for auth, PostgREST for rows, Storage for screenshots.
actor HubClient {
    enum Failure: LocalizedError {
        case notConfigured, notSignedIn, http(Int, String), decoding(String)
        var errorDescription: String? {
            switch self {
            case .notConfigured: "Enter the hub URL and anon key first."
            case .notSignedIn: "Sign in to the hub first."
            case .http(let code, let msg): "Hub error \(code): \(msg)"
            case .decoding(let what): "Could not read \(what) from the hub."
            }
        }
    }

    private(set) var config: HubConfig
    private let session: URLSession
    var onConfigChange: (@Sendable (HubConfig) -> Void)?

    init(config: HubConfig) {
        self.config = config
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = false
        session = URLSession(configuration: c)
    }

    func update(config: HubConfig) { self.config = config }
    func setConfigObserver(_ f: @escaping @Sendable (HubConfig) -> Void) { onConfigChange = f }

    private func persist() {
        Keychain.save(config)
        onConfigChange?(config)
    }

    // MARK: auth

    private struct TokenResponse: Decodable {
        let access_token: String
        let refresh_token: String
        let expires_in: Double?
        let user: UserInfo?
        struct UserInfo: Decodable { let id: String?; let email: String? }
    }

    /// `path` is "token?grant_type=password" style: the query must stay a query (appendingPathComponent would encode the "?").
    private func authRequest(path: String, body: [String: Any]) async throws -> TokenResponse {
        guard let base = config.baseURL, !config.anonKey.isEmpty else { throw Failure.notConfigured }
        let parts = path.split(separator: "?", maxSplits: 1).map(String.init)
        var comps = URLComponents(url: base.appendingPathComponent("auth/v1/\(parts[0])"), resolvingAgainstBaseURL: false)!
        if parts.count == 2 { comps.percentEncodedQuery = parts[1] }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(config.anonKey, forHTTPHeaderField: "apikey")
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw Failure.http(code, Self.message(data)) }
        return try JSONDecoder().decode(TokenResponse.self, from: data)
    }

    private func apply(_ t: TokenResponse) {
        config.accessToken = t.access_token
        config.refreshToken = t.refresh_token
        config.expiresAt = Date().addingTimeInterval(t.expires_in ?? 3600)
        if let u = t.user { config.userId = u.id ?? config.userId; config.email = u.email ?? config.email }
        persist()
    }

    func signIn(email: String, password: String) async throws {
        apply(try await authRequest(path: "token?grant_type=password", body: ["email": email, "password": password]))
    }

    func signOut() {
        config.accessToken = nil; config.refreshToken = nil; config.expiresAt = nil; config.email = nil; config.userId = nil
        persist()
    }

    /// A valid access token, refreshing when it is within a minute of expiring.
    private func bearer() async throws -> String {
        guard config.isConfigured else { throw Failure.notConfigured }
        guard let refresh = config.refreshToken else { throw Failure.notSignedIn }
        if let t = config.accessToken, let exp = config.expiresAt, exp.timeIntervalSinceNow > 60 { return t }
        do {
            apply(try await authRequest(path: "token?grant_type=refresh_token", body: ["refresh_token": refresh]))
        } catch Failure.http(let code, _) where code == 400 || code == 401 {
            signOut()                           // the refresh token is dead: back to the sign-in screen
            throw Failure.notSignedIn
        }
        return config.accessToken ?? ""
    }

    // MARK: rest

    private func request(_ method: String, _ path: String, query: [URLQueryItem] = [], body: Data? = nil,
                         prefer: String? = nil) async throws -> Data {
        guard let base = config.baseURL else { throw Failure.notConfigured }
        let token = try await bearer()
        var comps = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.setValue(config.anonKey, forHTTPHeaderField: "apikey")
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let prefer { req.setValue(prefer, forHTTPHeaderField: "Prefer") }
        req.httpBody = body
        let (data, resp) = try await session.data(for: req)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw Failure.http(code, Self.message(data)) }
        return data
    }

    private func rows<T: Decodable>(_ table: String, _ query: [URLQueryItem], as: T.Type = T.self) async throws -> [T] {
        let data = try await request("GET", "rest/v1/\(table)", query: query)
        do { return try JSONDecoder().decode([T].self, from: data) }
        catch { throw Failure.decoding("\(table) (\(error))") }
    }

    private static func message(_ data: Data) -> String {
        if let o = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for k in ["msg", "message", "error_description", "error", "hint"] { if let s = o[k] as? String { return s } }
        }
        return String(data: data.prefix(200), encoding: .utf8) ?? ""
    }

    // MARK: reads

    func snapshot() async throws -> HubSnapshot {
        HubSnapshot(rows: try await rows("hub_state", [.init(name: "select", value: "*")]))
    }

    func notices(limit: Int = 50) async throws -> [Notice] {
        try await rows("notices", [.init(name: "select", value: "*"), .init(name: "order", value: "id.desc"),
                                   .init(name: "limit", value: String(limit))])
    }

    func chat(limit: Int = 60) async throws -> [ChatMessage] {
        let m: [ChatMessage] = try await rows("chat_messages", [.init(name: "select", value: "*"),
                                                                .init(name: "order", value: "id.desc"),
                                                                .init(name: "limit", value: String(limit))])
        return m.reversed()
    }

    func steps(session key: String, after: Int64 = 0, limit: Int = 200) async throws -> [Step] {
        var q: [URLQueryItem] = [.init(name: "select", value: "*"), .init(name: "session", value: "eq.\(key)"),
                                 .init(name: "limit", value: String(limit))]
        if after > 0 {
            q += [.init(name: "id", value: "gt.\(after)"), .init(name: "order", value: "id.asc")]
            return try await rows("steps", q)
        }
        q.append(.init(name: "order", value: "id.desc"))
        let s: [Step] = try await rows("steps", q)
        return s.reversed()
    }

    func commands(ids: [UUID]) async throws -> [Command] {
        guard !ids.isEmpty else { return [] }
        let list = ids.map { $0.uuidString.lowercased() }.joined(separator: ",")
        return try await rows("commands", [.init(name: "select", value: "*"), .init(name: "id", value: "in.(\(list))")])
    }

    // MARK: writes

    @discardableResult
    func send(_ kind: String, _ body: [String: Any], device: String) async throws -> Command {
        let payload: [String: Any] = ["kind": kind, "body": body, "device": device]
        let data = try await request("POST", "rest/v1/commands", body: try JSONSerialization.data(withJSONObject: payload),
                                     prefer: "return=representation")
        guard let c = try JSONDecoder().decode([Command].self, from: data).first else { throw Failure.decoding("command") }
        return c
    }

    // MARK: files

    func artifact(_ path: String) async throws -> Data {
        try await request("GET", "storage/v1/object/authenticated/artifacts/\(path)")
    }
}
