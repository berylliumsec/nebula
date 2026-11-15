import Foundation
import Network

/// Serves the UI bundled in the app on a fixed loopback origin and tunnels
/// Core's API (HTTP and WebSocket) to the selected server. Only this device can
/// connect, and only with the in-app Host header. Credentials stay in WebKit's
/// cookie jar; this server never stores them.
final class LocalUIServer {
    static let port: UInt16 = 48173
    static let origin = URL(string: "http://127.0.0.1:\(port)/")!
    private static let localAuthority = "127.0.0.1:\(port)"
    private static let localOrigin = "http://127.0.0.1:\(port)"

    private let root: URL
    private let queue = DispatchQueue(label: "com.berylliumsec.nebula.local-ui")
    private var listener: NWListener?
    private var target: URL?

    init?(bundle: Bundle = .main) {
        guard let root = bundle.url(forResource: "WebUI", withExtension: nil),
              FileManager.default.fileExists(atPath: root.appendingPathComponent("index.html").path) else { return nil }
        self.root = root.standardizedFileURL
    }

    /// The server Core requests go to. Set before loading the page.
    var upstream: URL? {
        get { queue.sync { target } }
        set { queue.sync { target = newValue } }
    }

    var isRunning: Bool { queue.sync { listenerReady } }
    private var listenerReady: Bool {
        if case .ready? = listener?.state { return true }
        return false
    }

    /// Starts (or restarts after iOS suspended it) the listener; reports readiness on the main queue.
    func start(_ completion: @escaping (Bool) -> Void) {
        queue.async {
            if self.listenerReady { DispatchQueue.main.async { completion(true) }; return }
            self.listener?.cancel()
            let parameters = NWParameters.tcp
            parameters.acceptLocalOnly = true
            parameters.allowLocalEndpointReuse = true
            parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: Self.port)!)
            guard let listener = try? NWListener(using: parameters) else {
                DispatchQueue.main.async { completion(false) }
                return
            }
            var reported = false
            let report: (Bool) -> Void = { ready in
                guard !reported else { return }
                reported = true
                DispatchQueue.main.async { completion(ready) }
            }
            listener.stateUpdateHandler = { state in
                switch state {
                case .ready: report(true)
                case .failed, .cancelled: report(false)
                default: break
                }
            }
            listener.newConnectionHandler = { [weak self] connection in self?.accept(connection) }
            self.listener = listener
            listener.start(queue: self.queue)
        }
    }

    // MARK: Connections

    private func accept(_ client: NWConnection) {
        client.start(queue: queue)
        readHead(client, buffer: Data()) { [weak self] head, rest in
            guard let self, let lines = ProxyRewrite.lines(head),
                  let request = ProxyRewrite.requestLine(lines[0]),
                  ProxyRewrite.hostIsLocal(lines, local: Self.localAuthority) else {
                self?.respond(client, status: "400 Bad Request", type: "text/plain; charset=utf-8", body: Data("Bad request".utf8))
                return
            }
            let (method, target) = request
            if ProxyRewrite.isCorePath(target) {
                guard let upstream = self.target else {
                    self.respond(client, status: "503 Service Unavailable", type: "application/json", body: Data(#"{"detail":"Choose a Nebula server in App connection."}"#.utf8))
                    return
                }
                self.tunnel(client, lines: lines, rest: rest, upstream: upstream)
            } else if method == "GET" || method == "HEAD" {
                self.serveFile(client, target: target, head: method == "HEAD")
            } else {
                self.respond(client, status: "405 Method Not Allowed", type: "text/plain; charset=utf-8", body: Data())
            }
        }
    }

    private func readHead(_ connection: NWConnection, buffer: Data, completion: @escaping (Data, Data) -> Void) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { [weak self] data, _, complete, error in
            var buffer = buffer
            if let data { buffer.append(data) }
            if let end = buffer.range(of: ProxyRewrite.headTerminator) {
                completion(Data(buffer[..<end.upperBound]), Data(buffer[end.upperBound...]))
            } else if complete || error != nil || buffer.count > 65_536 {
                connection.cancel()
            } else {
                self?.readHead(connection, buffer: buffer, completion: completion)
            }
        }
    }

    // MARK: Bundled UI

    private func serveFile(_ client: NWConnection, target: String, head: Bool) {
        var name = ProxyRewrite.staticFile(target)
        var file = root.appendingPathComponent(name).standardizedFileURL
        var isDirectory: ObjCBool = false
        if !file.path.hasPrefix(root.path + "/") || !FileManager.default.fileExists(atPath: file.path, isDirectory: &isDirectory) || isDirectory.boolValue {
            name = "index.html"
            file = root.appendingPathComponent(name)
        }
        guard let body = try? Data(contentsOf: file) else {
            respond(client, status: "404 Not Found", type: "text/plain; charset=utf-8", body: Data())
            return
        }
        // Hashed assets never change; the shell and service worker always revalidate.
        let cache = name.hasPrefix("assets/") ? "public, max-age=31536000, immutable" : "no-cache"
        respond(client, status: "200 OK", type: ProxyRewrite.contentType(name), body: body, cache: cache, omitBody: head)
    }

    private func respond(_ client: NWConnection, status: String, type: String, body: Data, cache: String = "no-store", omitBody: Bool = false) {
        var head = "HTTP/1.1 \(status)\r\nContent-Type: \(type)\r\nContent-Length: \(body.count)\r\nCache-Control: \(cache)\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n"
        if status.hasPrefix("200"), type.hasPrefix("text/html") { head = head.replacingOccurrences(of: "Connection: close", with: "Referrer-Policy: same-origin\r\nConnection: close") }
        var payload = Data(head.utf8)
        if !omitBody { payload.append(body) }
        client.send(content: payload, completion: .contentProcessed { _ in client.cancel() })
    }

    // MARK: Core tunnel

    private func tunnel(_ client: NWConnection, lines: [String], rest: Data, upstream: URL) {
        guard let host = upstream.host,
              let port = NWEndpoint.Port(rawValue: UInt16(upstream.port ?? (upstream.scheme == "https" ? 443 : 80))) else {
            respond(client, status: "502 Bad Gateway", type: "text/plain; charset=utf-8", body: Data())
            return
        }
        let server = NWConnection(host: NWEndpoint.Host(host), port: port, using: upstream.scheme == "https" ? .tls : .tcp)
        var opened = false
        server.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready where !opened:
                opened = true
                var first = ProxyRewrite.request(lines, upstream: upstream, localOrigin: Self.localOrigin)
                first.append(rest)
                server.send(content: first, completion: .contentProcessed { error in
                    if error != nil { client.cancel(); server.cancel(); return }
                    self.pump(from: client, to: server)
                })
                self.readHead(server, buffer: Data()) { head, body in
                    guard let lines = ProxyRewrite.lines(head) else { client.cancel(); server.cancel(); return }
                    var out = ProxyRewrite.response(lines, upstream: upstream, localOrigin: Self.localOrigin)
                    out.append(body)
                    client.send(content: out, completion: .contentProcessed { error in
                        if error != nil { client.cancel(); server.cancel(); return }
                        self.pump(from: server, to: client)
                    })
                }
            case .waiting, .failed:
                guard !opened else { client.cancel(); server.cancel(); return }
                opened = true
                server.cancel()
                self.respond(client, status: "502 Bad Gateway", type: "application/json", body: Data(#"{"detail":"The Nebula server could not be reached."}"#.utf8))
            default: break
            }
        }
        server.start(queue: queue)
    }

    /// Copies bytes until either side ends, then closes both after the last write.
    private func pump(from source: NWConnection, to sink: NWConnection) {
        source.receive(minimumIncompleteLength: 1, maximumLength: 262_144) { [weak self] data, _, complete, error in
            let finish = complete || error != nil
            guard let data, !data.isEmpty else {
                if finish { sink.cancel(); source.cancel() } else { self?.pump(from: source, to: sink) }
                return
            }
            sink.send(content: data, completion: .contentProcessed { sendError in
                if sendError != nil || finish { sink.cancel(); source.cancel(); return }
                self?.pump(from: source, to: sink)
            })
        }
    }
}
