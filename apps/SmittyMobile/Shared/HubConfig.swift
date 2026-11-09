import Foundation
import Security

/// Where the hub is and who we are, kept in the Keychain (the anon key is public, the tokens are not).
struct HubConfig: Codable, Equatable {
    var url: String = ""              // https://xxxx.supabase.co
    var anonKey: String = ""
    var accessToken: String? = nil
    var refreshToken: String? = nil
    var expiresAt: Date? = nil
    var email: String? = nil
    var userId: String? = nil

    var isConfigured: Bool { !url.isEmpty && !anonKey.isEmpty }
    var isSignedIn: Bool { isConfigured && refreshToken != nil }
    var baseURL: URL? { URL(string: url.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))) }

    /// What the phone hands to the watch: enough to get its own access token.
    var handoff: [String: Any] {
        ["url": url, "anonKey": anonKey, "refreshToken": refreshToken ?? "", "email": email ?? ""]
    }
    init() {}
    init?(handoff: [String: Any]) {
        guard let u = handoff["url"] as? String, let k = handoff["anonKey"] as? String, !u.isEmpty, !k.isEmpty else { return nil }
        url = u; anonKey = k
        let r = handoff["refreshToken"] as? String ?? ""
        refreshToken = r.isEmpty ? nil : r
        email = (handoff["email"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }
}

/// Keychain-backed. An unsigned build (CODE_SIGNING_ALLOWED=NO in the simulator) has no keychain
/// access group, so the keychain refuses it; then the config falls back to a file in Application
/// Support so the simulator still remembers the sign-in. Signed builds never touch the file.
enum Keychain {
    static let service = (Bundle.main.bundleIdentifier ?? "smitty") + ".hub"
    static let account = "config"

    private static var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
    }

    private static var fallbackURL: URL? {
        guard let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else { return nil }
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("hub.json")
    }

    static func load() -> HubConfig {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        var data: Data? = nil
        if SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess { data = out as? Data }
        if data == nil, let url = fallbackURL { data = try? Data(contentsOf: url) }
        guard let data, let cfg = try? JSONDecoder().decode(HubConfig.self, from: data) else { return HubConfig() }
        return cfg
    }

    static func save(_ cfg: HubConfig) {
        guard let data = try? JSONEncoder().encode(cfg) else { return }
        let attrs: [String: Any] = [kSecValueData as String: data,
                                    kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock]
        var status = SecItemUpdate(query as CFDictionary, attrs as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attrs) { $1 } as CFDictionary, nil)
        }
        if status != errSecSuccess, let url = fallbackURL {
            NSLog("Smitty: keychain unavailable (%d); keeping the hub config in a file", status)
            try? data.write(to: url, options: [.atomic, .completeFileProtection])
        }
    }

    static func clear() {
        SecItemDelete(query as CFDictionary)
        if let url = fallbackURL { try? FileManager.default.removeItem(at: url) }
    }
}
