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
