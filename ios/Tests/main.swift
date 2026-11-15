import Foundation

func check(_ condition: @autoclosure () -> Bool, _ label: String) {
    guard condition() else { fatalError(label) }
}
for bad in ["", "javascript:alert(1)", "file:///etc/passwd", "https://u:p@host", "https://host/path", "https://host?token=x", "https://host/#token", "http://host:0", "http://host:65536", "a b"] {
    check(ServerAddress.parse(bad) == nil, "Accepted invalid address: \(bad)")
}
let server = ServerAddress.parse("192.168.1.155:8000")!
check(server.absoluteString == "http://192.168.1.155:8000/", "Normalize LAN address")
check(ServerAddress.sameOrigin(URL(string: "http://192.168.1.155:8000/chat?id=1")!, server), "Allow local routes")
check(!ServerAddress.sameOrigin(URL(string: "http://192.168.1.155:8001/")!, server), "Reject different port")
check(!ServerAddress.sameOrigin(URL(string: "https://192.168.1.155:8000/")!, server), "Reject different scheme")
check(!ServerAddress.sameOrigin(URL(string: "http://example.com:8000/")!, server), "Reject different host")
check(ServerAddress.sameOrigin(URL(string: "https://EXAMPLE.com:443/chat")!, URL(string: "https://example.com/")!), "Normalize default port")
print("16 server address and navigation policy checks passed")
let pairing = URL(string: server.absoluteString + "#pair=" + String(repeating: "a", count: 43) + "&code=123456")!
check(ServerAddress.pairingURL(pairing, server: server) != nil, "Accept valid pairing link")
check(ServerAddress.pairingURL(pairing, server: URL(string: "http://other:8000")!) == nil, "Reject pairing for another server")
check(ServerAddress.pairingURL(URL(string: server.absoluteString + "#pair=x&code=123456")!, server: server) == nil, "Reject short secret")
check(ServerAddress.pairingURL(URL(string: pairing.absoluteString + "&code=123456")!, server: server) == nil, "Reject duplicate code")
var link = URLComponents(string: "nebula://pair")!
link.queryItems = [URLQueryItem(name: "url", value: pairing.absoluteString)]
check(ServerAddress.pairingURL(link.url!, server: server) == pairing, "Accept native pairing link")
print("5 pairing-link checks passed")
let vpn = ServerAddress.parse("http://10.10.0.2:8000")!
let lan = ServerAddress.parse("192.168.1.155:8000")!
var history = ServerHistory.remember(vpn, in: [])
history = ServerHistory.remember(lan, in: history)
check(history == [lan.absoluteString, vpn.absoluteString], "Most recent server first")
history = ServerHistory.remember(URL(string: "http://10.10.0.2:8000/")!, in: history)
check(history == [vpn.absoluteString, lan.absoluteString], "One entry per origin")
check(ServerHistory.remember(ServerAddress.parse("c:1")!, in: ServerHistory.remember(ServerAddress.parse("d:2")!, in: history)).count == ServerHistory.limit, "History stays bounded")
check(ServerHistory.alternates(to: vpn, in: history) == [lan], "Offer the other saved server")
check(ServerHistory.alternates(to: nil, in: ["not a url", vpn.absoluteString]) == [vpn], "Ignore invalid saved entries")
check(ServerAddress.label(lan) == "192.168.1.155:8000", "Label keeps a custom port")
check(ServerAddress.label(URL(string: "https://nebula.example/")!) == "nebula.example", "Label drops a default port")
check(ServerAddress.isSettingsLink(URL(string: "nebula://settings")!), "Recognize the settings link")
check(!ServerAddress.isSettingsLink(URL(string: "https://settings/")!), "Only the nebula scheme opens settings")
print("9 saved-server and settings-link checks passed")
let core = URL(string: "https://simulation.local:8443/")!
let local = "http://127.0.0.1:48173"
check(ProxyRewrite.isCorePath("/api/v1/chat?x=1") && ProxyRewrite.isCorePath("/api"), "API goes to Core")
check(!ProxyRewrite.isCorePath("/projects/a/butler") && !ProxyRewrite.isCorePath("/apiary"), "UI routes stay in the app")
check(ProxyRewrite.origin(core) == "https://simulation.local:8443" && ProxyRewrite.origin(URL(string: "https://h:443/")!) == "https://h", "Origin drops the default port")
let head = Data("POST /api/v1/chat HTTP/1.1\r\nHost: 127.0.0.1:48173\r\nOrigin: http://127.0.0.1:48173\r\nReferer: http://127.0.0.1:48173/projects\r\nConnection: keep-alive\r\nX-Nebula-CSRF: t\r\n\r\n".utf8)
let requestLines = ProxyRewrite.lines(head)!
check(ProxyRewrite.hostIsLocal(requestLines, local: "127.0.0.1:48173"), "Accept the in-app Host")
check(!ProxyRewrite.hostIsLocal(ProxyRewrite.lines(Data("GET / HTTP/1.1\r\nHost: evil.test:48173\r\n\r\n".utf8))!, local: "127.0.0.1:48173"), "Reject a rebound Host")
let rewritten = String(data: ProxyRewrite.request(requestLines, upstream: core, localOrigin: local), encoding: .utf8)!
check(rewritten.contains("Host: simulation.local:8443\r\n") && rewritten.contains("Origin: https://simulation.local:8443\r\n"), "Core sees its own Host and Origin")
check(rewritten.contains("Referer: https://simulation.local:8443/projects\r\n") && rewritten.contains("X-Nebula-CSRF: t\r\n"), "Referer rewritten, CSRF kept")
check(rewritten.hasSuffix("Connection: close\r\n\r\n") && !rewritten.contains("keep-alive"), "One request per tunnel")
let upgrade = String(data: ProxyRewrite.request(ProxyRewrite.lines(Data("GET /api/v1/events/ws HTTP/1.1\r\nHost: 127.0.0.1:48173\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n".utf8))!, upstream: core, localOrigin: local), encoding: .utf8)!
check(upgrade.contains("Connection: Upgrade\r\n") && !upgrade.contains("Connection: close"), "WebSocket upgrade passes")
let reply = String(data: ProxyRewrite.response(ProxyRewrite.lines(Data("HTTP/1.1 200 OK\r\nset-cookie: nebula_device=x; Domain=simulation.local; HttpOnly; Path=/; SameSite=strict; Secure\r\nLocation: https://simulation.local:8443/x\r\nStrict-Transport-Security: max-age=1\r\n\r\n".utf8))!, upstream: core, localOrigin: local), encoding: .utf8)!
check(reply.contains("Set-Cookie: nebula_device=x; HttpOnly; Path=/; SameSite=strict\r\n"), "Cookie binds to the in-app origin")
check(reply.contains("Location: http://127.0.0.1:48173/x\r\n") && !reply.contains("Strict-Transport"), "Redirect and HSTS stay in-app")
check(ProxyRewrite.staticFile("/assets/index-1.js?v=2") == "assets/index-1.js" && ProxyRewrite.staticFile("/projects/a/butler") == "projects/a/butler", "Static paths")
check(ProxyRewrite.staticFile("/../etc/passwd") == "index.html" && ProxyRewrite.staticFile("/assets/%2e%2e/x") == "index.html" && ProxyRewrite.staticFile("/.git/config") == "index.html" && ProxyRewrite.staticFile("/") == "index.html", "Reject traversal and hidden files")
check(ProxyRewrite.contentType("assets/a.js").hasPrefix("text/javascript") && ProxyRewrite.contentType("index.html").hasPrefix("text/html"), "Content types")
print("13 in-app UI server checks passed")
