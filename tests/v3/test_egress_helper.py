import ipaddress
import struct

from nebula.v3 import egress_helper


def _dns_name(value: str) -> bytes:
    return (
        b"".join(
            bytes([len(label)]) + label.encode("ascii") for label in value.split(".")
        )
        + b"\0"
    )


def _query(name: str) -> bytes:
    return (
        struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        + _dns_name(name)
        + struct.pack("!HH", 1, 1)
    )


def _response(name: str, address: str, *, cname: str | None = None) -> bytes:
    question = _dns_name(name) + struct.pack("!HH", 1, 1)
    answers = b""
    if cname is not None:
        encoded = _dns_name(cname)
        answers += b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 60, len(encoded)) + encoded
        owner = _dns_name(cname)
    else:
        owner = b"\xc0\x0c"
    packed = ipaddress.ip_address(address).packed
    answers += owner + struct.pack("!HHIH", 1, 1, 60, len(packed)) + packed
    return (
        struct.pack("!HHHHHH", 0x1234, 0x8180, 1, 2 if cname else 1, 0, 0)
        + question
        + answers
    )


def _rcode(packet: bytes) -> int:
    return struct.unpack("!H", packet[2:4])[0] & 0xF


def _resolver(monkeypatch, *, domains, networks=(), enabled=True):
    monkeypatch.setattr(egress_helper, "_upstream_resolvers", lambda: ["8.8.8.8"])
    return egress_helper.PolicyResolver(
        domains=list(domains),
        ports=[443],
        explicit_networks=list(networks),
        enabled=enabled,
    )


def test_policy_dns_enforces_exact_and_wildcard_domain_boundaries(monkeypatch):
    resolver = _resolver(
        monkeypatch,
        domains=["api.example.test", "*.services.example.test"],
    )

    assert resolver._domain_allowed("api.example.test") is True
    assert resolver._domain_allowed("a.services.example.test") is True
    assert resolver._domain_allowed("services.example.test") is False
    assert resolver._domain_allowed("example.test") is False

    refused = resolver.resolve(_query("outside.example.test"))
    assert _rcode(refused) == 5


def test_policy_dns_opens_only_configured_ports_for_public_answers(monkeypatch):
    resolver = _resolver(monkeypatch, domains=["api.example.test"])
    response = _response("api.example.test", "8.8.4.4")
    resolver._forward = lambda _request: response  # type: ignore[method-assign]
    installed = []
    monkeypatch.setattr(
        egress_helper,
        "_install_rule",
        lambda network, port: installed.append((str(network), port)),
    )

    result = resolver.resolve(_query("api.example.test"))

    assert result == response
    assert installed == [("8.8.4.4/32", "443")]
    resolver.resolve(_query("api.example.test"))
    assert installed == [("8.8.4.4/32", "443")]


def test_policy_dns_blocks_private_rebinding_unless_cidr_is_explicit(monkeypatch):
    private = _response("api.example.test", "10.20.30.40", cname="edge.cdn.example")
    denied = _resolver(monkeypatch, domains=["api.example.test"])
    denied._forward = lambda _request: private  # type: ignore[method-assign]
    monkeypatch.setattr(egress_helper, "_install_rule", lambda *_args: None)
    assert _rcode(denied.resolve(_query("api.example.test"))) == 2

    allowed = _resolver(
        monkeypatch,
        domains=["api.example.test"],
        networks=[ipaddress.ip_network("10.20.30.0/24")],
    )
    allowed._forward = lambda _request: private  # type: ignore[method-assign]
    installed = []
    monkeypatch.setattr(
        egress_helper,
        "_install_rule",
        lambda network, port: installed.append((str(network), port)),
    )
    assert allowed.resolve(_query("api.example.test")) == private
    assert installed == [("10.20.30.40/32", "443")]


def test_policy_dns_stays_closed_until_the_session_grant_is_enabled(monkeypatch):
    resolver = _resolver(monkeypatch, domains=["api.example.test"], enabled=False)
    assert _rcode(resolver.resolve(_query("api.example.test"))) == 5

    resolver.enable()
    response = _response("api.example.test", "1.1.1.1")
    resolver._forward = lambda _request: response  # type: ignore[method-assign]
    monkeypatch.setattr(egress_helper, "_install_rule", lambda *_args: None)
    assert resolver.resolve(_query("api.example.test")) == response
