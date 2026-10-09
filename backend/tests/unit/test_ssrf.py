import pytest

from competitor_moves.core.ssrf import BlockedURL, check_url, is_public_ip


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                                "100.64.0.1", "::1", "fe80::1%eth0", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"])
def test_non_public_ips_are_rejected(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", ["93.184.215.14", "8.8.8.8", "2606:4700::1111"])
def test_public_ips_pass(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "http:///nohost", "http://user:pw@example.com/",
                                 "http://127.0.0.1/", "http://localhost:8000/", "http://[::1]/", "http://169.254.169.254/latest",
                                 "http://10.0.0.5/admin", "http://example.com:99999/"])
def test_blocked_urls(url):
    with pytest.raises(BlockedURL):
        check_url(url)


def test_allow_local_is_exact_host_and_port(monkeypatch):
    from competitor_moves.core import ssrf
    monkeypatch.setattr(ssrf, "ALLOW_LOCAL", {"127.0.0.1:8765"})
    check_url("http://127.0.0.1:8765/x")
    with pytest.raises(BlockedURL):
        check_url("http://127.0.0.1:8766/x")
