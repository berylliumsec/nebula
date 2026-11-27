import Foundation

/// Pure rules for Butler push notifications, kept apart from UIKit so the
/// policy checks in Tests/main.swift can compile them with `swiftc`.
enum PushPolicy {
    /// APNs device tokens are sent to Core as lowercase hex.
    static func hex(_ token: Data) -> String {
        token.map { String(format: "%02x", $0) }.joined()
    }

    /// The APNs environment this signed build talks to. Development-signed
    /// builds embed a provisioning profile whose entitlements say
    /// `aps-environment = development`; App Store and TestFlight builds have
    /// no embedded profile and use production.
    static func environment(provisioningProfile: Data?) -> String {
        guard let profile = provisioningProfile,
              let text = String(data: profile, encoding: .isoLatin1),
              let key = text.range(of: "<key>aps-environment</key>") else { return "production" }
        let rest = text[key.upperBound...].prefix(200)
        return rest.contains("<string>development</string>") ? "development" : "production"
    }

    /// Where a tapped notification may take the page. Core sends a path inside
    /// Butler mode; anything else (another origin, a scheme, a non-Butler route)
    /// opens Butler mode's landing instead.
    static func butlerPath(_ value: Any?) -> String {
        guard let raw = value as? String, raw.count <= 512,
              raw.hasPrefix("/butler"), !raw.hasPrefix("//"),
              let parts = URLComponents(string: raw),
              parts.scheme == nil, parts.host == nil, parts.path == "/butler" else { return "/" }
        return raw
    }

    /// The script that tells the page this phone's push state. The page owns
    /// registering it with Core through its paired session; the script only
    /// sets a value and announces it, so no page script can call native code.
    static func pageScript(token: String?, environment: String, authorization: String, bundleId: String) -> String {
        let state: [String: Any] = [
            "token": token ?? NSNull(),
            "environment": environment,
            "authorization": authorization,
            "bundleId": bundleId,
        ]
        let json = (try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "null"
        return "window.__nebulaShellPush = \(json); window.dispatchEvent(new CustomEvent('nebula-shell-push'));"
    }
}
