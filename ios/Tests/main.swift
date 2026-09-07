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
