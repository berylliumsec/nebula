import Foundation

enum ServerAddress {
    static func parse(_ input: String) -> URL? {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !text.contains(where: { $0.isWhitespace }),
              var parts = URLComponents(string: text.contains("://") ? text : "http://" + text),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              parts.port == nil || (1...65535).contains(parts.port!) else { return nil }
        parts.scheme = parts.scheme?.lowercased()
        parts.path = "/"
        return parts.url
    }

    static func pairingURL(_ input: URL, server: URL) -> URL? {
        let candidate: URL
        if input.scheme == "nebula", input.host == "pair",
           let parts = URLComponents(url: input, resolvingAgainstBaseURL: false),
           let value = parts.queryItems?.first(where: { $0.name == "url" })?.value,
           let nested = URL(string: value) { candidate = nested }
        else { candidate = input }
        guard sameOrigin(candidate, server), candidate.user == nil, candidate.password == nil,
              candidate.path == "/", candidate.query == nil,
              let fragment = candidate.fragment,
              let items = URLComponents(string: "?" + fragment)?.queryItems,
              items.count == 2, Set(items.map(\.name)) == Set(["pair", "code"]),
              let secret = items.first(where: { $0.name == "pair" })?.value,
              (32...256).contains(secret.count),
              let code = items.first(where: { $0.name == "code" })?.value,
              code.count == 6, code.allSatisfy({ $0.isASCII && $0.isNumber }) else { return nil }
        return candidate
    }

    static func sameOrigin(_ url: URL, _ server: URL) -> Bool {
        func port(_ url: URL) -> Int { url.port ?? (url.scheme == "https" ? 443 : 80) }
        return url.scheme?.lowercased() == server.scheme?.lowercased()
            && url.host?.lowercased() == server.host?.lowercased()
            && port(url) == port(server)
    }
}
