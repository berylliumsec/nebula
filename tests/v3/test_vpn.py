import pytest

from nebula.v3.vpn import VpnProfileError, parse_openvpn_profile


BASE = """client
dev tun
proto udp
remote vpn.example.com 1194
remote-cert-tls server
<ca>
certificate
</ca>
"""


def test_admits_inline_tun_profile_and_forces_full_tunnel():
    parsed = parse_openvpn_profile(BASE)

    assert parsed.remote_host == "vpn.example.com"
    assert parsed.remote_port == 1194
    assert parsed.protocol == "udp"
    assert "dev tun0" in parsed.config
    assert "redirect-gateway def1 ipv6" in parsed.config


def test_admits_ping_timer_rem_without_arguments():
    parsed = parse_openvpn_profile(BASE + "ping 10\nping-restart 60\nping-timer-rem\n")

    assert "ping-timer-rem\n" in parsed.config
    with pytest.raises(VpnProfileError, match="does not accept arguments"):
        parse_openvpn_profile(BASE + "ping-timer-rem unexpected\n")


def test_common_safe_client_timing_options_are_admitted():
    parsed = parse_openvpn_profile(
        BASE
        + "reneg-sec 0\nreneg-bytes 0\nreneg-pkts 0\n"
        + "remote-random\nfast-io\nmute-replay-warnings\nsuppress-timestamps\n"
    )

    assert "reneg-sec 0\n" in parsed.config
    assert "remote-random\n" in parsed.config


@pytest.mark.parametrize("value", ["-1", "forever", "1 2"])
def test_renegotiation_options_reject_invalid_values(value):
    with pytest.raises(VpnProfileError, match="reneg-sec requires"):
        parse_openvpn_profile(BASE + f"reneg-sec {value}\n")


@pytest.mark.parametrize(
    "directive",
    [
        "up /tmp/script",
        "plugin /tmp/plugin.so",
        "route-nopull",
        "route 10.0.0.0 255.0.0.0",
        "management 0.0.0.0 7505",
        "config nested.ovpn",
        "dev tap",
    ],
)
def test_rejects_profile_escape_hatches(directive: str):
    with pytest.raises(VpnProfileError):
        parse_openvpn_profile(BASE.replace("dev tun", directive))


def test_requires_credentials_without_accepting_a_file_path():
    profile = BASE + "auth-user-pass\n"
    parsed = parse_openvpn_profile(profile, username="operator", password="secret")
    assert "<auth-user-pass>\noperator\nsecret\n</auth-user-pass>" in parsed.config

    with pytest.raises(VpnProfileError, match="requires"):
        parse_openvpn_profile(profile)
    with pytest.raises(VpnProfileError, match="file paths"):
        parse_openvpn_profile(BASE + "auth-user-pass credentials.txt\n")


def test_requires_server_identity_verification_and_one_remote():
    with pytest.raises(VpnProfileError, match="verify"):
        parse_openvpn_profile(BASE.replace("remote-cert-tls server\n", ""))
    with pytest.raises(VpnProfileError, match="exactly one remote"):
        parse_openvpn_profile(BASE + "remote backup.example.com 1194\n")
