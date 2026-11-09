import SwiftUI

/// The dashboard's palette (smitty/dashboard/index.html :root), navy + cream with coral
/// reserved for "needs attention" and always paired with a label or icon.
enum Theme {
    static let navy950 = Color(hex: 0x060D20)
    static let navy900 = Color(hex: 0x0A1532)
    static let navy800 = Color(hex: 0x0F1F47)
    static let navy700 = Color(hex: 0x17306A)
    static let tint = Color(hex: 0x9DB4F2)          // listening / thinking
    static let cream = Color(hex: 0xF6ECD9)
    static let cream2 = cream.opacity(0.72)
    static let cream3 = cream.opacity(0.46)
    static let cream4 = cream.opacity(0.22)
    static let hair = cream.opacity(0.13)
    static let hair2 = cream.opacity(0.07)
    static let coral = Color(hex: 0xFF7A66)
    static let coralSoft = coral.opacity(0.16)
    static let glassTop = cream.opacity(0.095)
    static let glassBottom = cream.opacity(0.028)
    static let radius: CGFloat = 24

    static var background: some View {
        LinearGradient(colors: [navy950, navy900], startPoint: .top, endPoint: .bottom)
            .ignoresSafeArea()
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}

/// Frosted card, as on the dashboard.
struct Glass: ViewModifier {
    var radius: CGFloat = Theme.radius
    var padding: CGFloat = 14
    func body(content: Content) -> some View {
        content
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(LinearGradient(colors: [Theme.glassTop, Theme.glassBottom], startPoint: .topLeading, endPoint: .bottomTrailing))
            )
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).stroke(Theme.hair, lineWidth: 1))
    }
}

extension View {
    func glass(radius: CGFloat = Theme.radius, padding: CGFloat = 14) -> some View {
        modifier(Glass(radius: radius, padding: padding))
    }
}

/// Small uppercase label used for section headers and meta text.
struct Eyebrow: View {
    let text: String
    var color: Color = Theme.cream3
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 11, weight: .semibold))
            .tracking(1.2)
            .foregroundStyle(color)
    }
}

/// A rounded pill: the state indicator and tags.
struct Pill: View {
    let text: String
    var icon: String? = nil
    var color: Color = Theme.cream2
    var fill: Color = Theme.hair2
    var body: some View {
        HStack(spacing: 5) {
            if let icon { Image(systemName: icon).font(.system(size: 11, weight: .semibold)) }
            Text(text).font(.system(size: 12, weight: .semibold))
        }
        .foregroundStyle(color)
        .padding(.horizontal, 10).padding(.vertical, 5)
        .background(Capsule().fill(fill))
        .overlay(Capsule().stroke(Theme.hair, lineWidth: 1))
    }
}

/// Primary / secondary / attention button styles.
struct PillButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, attention }
    var kind: Kind = .secondary
    func makeBody(configuration: Configuration) -> some View {
        let (fg, bg): (Color, Color) = switch kind {
        case .primary: (Theme.navy950, Theme.cream)
        case .secondary: (Theme.cream2, Theme.hair2)
        case .attention: (Theme.coral, Theme.coralSoft)
        }
        configuration.label
            .font(.system(size: 14, weight: .semibold))
            .foregroundStyle(fg)
            .padding(.horizontal, 14).padding(.vertical, 9)
            .frame(maxWidth: .infinity)
            .background(Capsule().fill(bg))
            .overlay(Capsule().stroke(kind == .primary ? .clear : Theme.hair, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.7 : 1)
    }
}

/// How Smitty's state reads on the pill at the top.
struct SmittyState {
    let raw: String
    var label: String {
        switch raw {
        case "idle": "Idle"
        case "listening": "Listening"
        case "thinking": "Thinking"
        case "speaking": "Speaking"
        case "needs_confirmation": "Needs attention"
        case "error": "Error"
        case "offline": "Offline"
        default: raw.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    var color: Color {
        switch raw {
        case "needs_confirmation", "error": Theme.coral
        case "listening", "thinking", "speaking": Theme.tint
        case "offline": Theme.cream4
        default: Theme.cream2
        }
    }
    var icon: String {
        switch raw {
        case "listening": "waveform"
        case "thinking": "sparkles"
        case "speaking": "speaker.wave.2"
        case "needs_confirmation": "exclamationmark.circle"
        case "error": "xmark.octagon"
        case "offline": "moon.zzz"
        default: "circle.dotted"
        }
    }
}

enum Fmt {
    static let time: DateFormatter = {
        let f = DateFormatter(); f.dateFormat = "HH:mm"; return f
    }()
    static func ago(_ date: Date) -> String {
        let s = max(0, Int(Date().timeIntervalSince(date)))
        if s < 60 { return "just now" }
        if s < 3600 { return "\(s / 60) min ago" }
        if s < 86400 { return "\(s / 3600) h ago" }
        return "\(s / 86400) d ago"
    }
    static func duration(_ s: Int) -> String {
        if s < 60 { return "\(s)s" }
        if s < 3600 { return "\(s / 60)m \(s % 60)s" }
        return "\(s / 3600)h \((s % 3600) / 60)m"
    }
}
