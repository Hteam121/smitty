import SwiftUI

/// Hub URL + anon key, then sign in with the hub's one email + password account.
struct SettingsView: View {
    @Environment(HubStore.self) private var store
    @Environment(PhoneConnectivity.self) private var phone
    @Environment(\.dismiss) private var dismiss
    let isSheet: Bool

    @State private var url = ""
    @State private var anonKey = ""
    @State private var email = ""
    @State private var password = ""
    @State private var busy = false
    @State private var message: String? = nil

    var body: some View {
        ZStack {
            Theme.background
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Smitty").font(.system(size: 28, weight: .bold)).foregroundStyle(Theme.cream)
                            Text(store.isSignedIn ? "Signed in as \(store.config.email ?? "you")" : "Connect to your hub")
                                .font(.system(size: 14)).foregroundStyle(Theme.cream3)
                        }
                        Spacer()
                        if isSheet { Button { dismiss() } label: { Image(systemName: "xmark").foregroundStyle(Theme.cream2) } }
                    }
                    .padding(.top, 10)

                    VStack(alignment: .leading, spacing: 10) {
                        Eyebrow(text: "Hub (Supabase project)")
                        field("https://xxxx.supabase.co", text: $url, keyboard: .URL)
                        field("publishable key (sb_publishable_…)", text: $anonKey, secure: true)
                        Text("From the project's Settings → API Keys. The publishable key is safe to keep on the phone; the secret key never leaves the Mac.")
                            .font(.system(size: 12)).foregroundStyle(Theme.cream4)
                    }
                    .glass()

                    if store.isSignedIn {
                        VStack(alignment: .leading, spacing: 10) {
                            Eyebrow(text: "Watch")
                            Text(phone.watchAppInstalled ? "Sign-in shared with the Watch app." :
                                 phone.watchPaired ? "Watch paired; install the Smitty watch app to use it." : "No Watch paired.")
                                .font(.system(size: 13)).foregroundStyle(Theme.cream2)
                            Button("Share sign-in with Watch again") { phone.share(store.config) }
                                .buttonStyle(PillButtonStyle(kind: .secondary))
                        }
                        .glass()
                        Button("Sign out") { Task { await store.signOut() } }.buttonStyle(PillButtonStyle(kind: .attention))
                    } else {
                        VStack(alignment: .leading, spacing: 10) {
                            Eyebrow(text: "Sign in")
                            Text("The email and password of the hub's one user (Supabase → Authentication → Users).")
                                .font(.system(size: 12)).foregroundStyle(Theme.cream4)
                            field("email", text: $email, keyboard: .emailAddress)
                            field("password", text: $password, secure: true)
                            Button(busy ? "Signing in…" : "Sign in") { Task { await emailSignIn() } }
                                .buttonStyle(PillButtonStyle(kind: .primary))
                                .disabled(busy || !canSignIn || email.isEmpty || password.isEmpty)
                        }
                        .glass()
                    }

                    if let message {
                        HStack(spacing: 6) {
                            Image(systemName: "exclamationmark.circle").foregroundStyle(Theme.coral)
                            Text(message).font(.system(size: 13)).foregroundStyle(Theme.cream2)
                        }
                    }
                    if let sync = store.lastSync {
                        Text("Last sync \(Fmt.ago(sync))").font(.system(size: 12)).foregroundStyle(Theme.cream4)
                    }
                }
                .padding(.horizontal, 18).padding(.bottom, 30)
            }
        }
        .onAppear { url = store.config.url; anonKey = store.config.anonKey }
        .preferredColorScheme(.dark)
    }

    private var canSignIn: Bool { !url.isEmpty && !anonKey.isEmpty }

    private func field(_ placeholder: String, text: Binding<String>, keyboard: UIKeyboardType = .default, secure: Bool = false) -> some View {
        Group {
            if secure { SecureField(placeholder, text: text) } else { TextField(placeholder, text: text).keyboardType(keyboard) }
        }
        .textInputAutocapitalization(.never).autocorrectionDisabled()
        .font(.system(size: 15)).foregroundStyle(Theme.cream)
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Theme.navy950.opacity(0.5)))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.hair, lineWidth: 1))
    }

    private func emailSignIn() async {
        busy = true; message = nil
        await store.configure(url: url, anonKey: anonKey)
        do {
            try await store.signIn(email: email, password: password)
            password = ""
            if isSheet { dismiss() }
        } catch { message = error.localizedDescription }
        busy = false
    }
}
