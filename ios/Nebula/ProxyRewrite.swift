import Foundation

/// Pure rules for the in-app UI server: which requests go to Core, and how the
/// HTTP heads are rewritten so Core sees the same origin it would see from a
/// browser loaded directly from the server.
enum ProxyRewrite {
    static let headTerminator = Data("\r\n\r\n".utf8)

    /// Only Core's API leaves the device; everything else is the bundled UI.
    static func isCorePath(_ target: String) -> Bool {
        let path = target.split(separator: "?", maxSplits: 1).first.map(String.init) ?? target
        return path == "/api" || path.hasPrefix("/api/")
    }

    /// `scheme://host[:port]` with the default port left out, as a browser writes `Origin`.
    static func origin(_ url: URL) -> String {
        let scheme = url.scheme?.lowercased() ?? "http"
        return "\(scheme)://\(authority(url))"
    }

    static func authority(_ url: URL) -> String {
        let host = url.host ?? ""
        let shown = host.contains(":") ? "[\(host)]" : host
        let defaultPort = url.scheme?.lowercased() == "https" ? 443 : 80
        if let port = url.port, port != defaultPort { return "\(shown):\(port)" }
        return shown
    }

    /// Splits a head into lines; returns nil unless it is complete and ASCII/UTF-8.
    static func lines(_ head: Data) -> [String]? {
        guard head.count >= headTerminator.count, head.suffix(headTerminator.count) == headTerminator,
              let text = String(data: head.dropLast(headTerminator.count), encoding: .utf8) else { return nil }
        let lines = text.components(separatedBy: "\r\n")
        return lines.first?.isEmpty == false ? lines : nil
    }

    static func name(_ line: String) -> String {
        line.split(separator: ":", maxSplits: 1).first.map { $0.trimmingCharacters(in: .whitespaces).lowercased() } ?? ""
    }

    /// Request line parts: method, target, version.
    static func requestLine(_ line: String) -> (method: String, target: String)? {
        let parts = line.split(separator: " ")
        guard parts.count == 3, parts[2].hasPrefix("HTTP/1."), parts[1].hasPrefix("/") else { return nil }
        return (String(parts[0]), String(parts[1]))
    }

    /// The page must have been loaded from the in-app origin (blocks DNS rebinding).
    static func hostIsLocal(_ lines: [String], local: String) -> Bool {
        lines.dropFirst().contains { name($0) == "host" && value($0) == local }
    }

    static func value(_ line: String) -> String {
        guard let colon = line.firstIndex(of: ":") else { return "" }
        return line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
    }

    /// Request to Core: Host/Origin/Referer name the server; one request per
    /// connection unless it is a WebSocket upgrade.
    static func request(_ lines: [String], upstream: URL, localOrigin: String) -> Data {
        let upgrade = lines.dropFirst().contains { name($0) == "upgrade" }
        let serverOrigin = origin(upstream)
        var out = [lines[0]]
        for line in lines.dropFirst() {
            switch name(line) {
            case "host": out.append("Host: \(authority(upstream))")
            case "origin": out.append("Origin: \(serverOrigin)")
            case "referer":
                let referer = value(line)
                out.append("Referer: " + (referer.hasPrefix(localOrigin) ? serverOrigin + referer.dropFirst(localOrigin.count) : referer))
            case "connection", "keep-alive", "proxy-connection":
                if upgrade && name(line) == "connection" { out.append(line) }
            default: out.append(line)
            }
        }
        if !upgrade { out.append("Connection: close") }
        return Data((out.joined(separator: "\r\n") + "\r\n\r\n").utf8)
    }

    /// Response from Core: cookies bind to the in-app origin (plain HTTP on
    /// loopback, so `Secure` and `Domain` are dropped) and redirects stay in-app.
    static func response(_ lines: [String], upstream: URL, localOrigin: String) -> Data {
        let serverOrigin = origin(upstream)
        var out = [lines[0]]
        for line in lines.dropFirst() {
            switch name(line) {
            case "set-cookie":
                let attributes = value(line).components(separatedBy: ";").map { $0.trimmingCharacters(in: .whitespaces) }
                let kept = attributes.enumerated().filter { index, attribute in
                    let key = attribute.split(separator: "=", maxSplits: 1).first.map { $0.lowercased() } ?? ""
                    return index == 0 || (key != "secure" && key != "domain")
                }.map(\.element)
                out.append("Set-Cookie: " + kept.joined(separator: "; "))
            case "location":
                let location = value(line)
                out.append("Location: " + (location.hasPrefix(serverOrigin) ? localOrigin + location.dropFirst(serverOrigin.count) : location))
            case "strict-transport-security", "alt-svc": continue
            default: out.append(line)
            }
        }
        return Data((out.joined(separator: "\r\n") + "\r\n\r\n").utf8)
    }

    /// Bundled file for a UI path; unknown routes get the SPA shell.
    static func staticFile(_ target: String) -> String {
        let path = target.split(separator: "?", maxSplits: 1).first.map(String.init) ?? "/"
        let decoded = (path.removingPercentEncoding ?? path).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let parts = decoded.split(separator: "/")
        guard !decoded.isEmpty, !parts.contains(where: { $0 == ".." || $0 == "." || $0.hasPrefix(".") }),
              !decoded.contains("\\"), !decoded.contains("\0") else { return "index.html" }
        return decoded
    }

    static func contentType(_ file: String) -> String {
        switch (file as NSString).pathExtension.lowercased() {
        case "html": return "text/html; charset=utf-8"
        case "js", "mjs": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json": return "application/json"
        case "webmanifest": return "application/manifest+json"
        case "svg": return "image/svg+xml"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "webp": return "image/webp"
        case "woff2": return "font/woff2"
        case "woff": return "font/woff"
        case "ttf": return "font/ttf"
        case "wasm": return "application/wasm"
        case "map", "txt": return "text/plain; charset=utf-8"
        default: return "application/octet-stream"
        }
    }
}
