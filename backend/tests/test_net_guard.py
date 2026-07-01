"""Tests del guard anti-SSRF."""

import pytest

from app.net_guard import SsrfBlocked, assert_public_url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",  # metadata cloud
    "http://localhost:8000/",
    "http://127.0.0.1/",
    "http://10.0.0.5/x",
    "http://192.168.1.1/",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "",
    "http:///nohost",
])
def test_blocks_private_and_bad_schemes(url):
    with pytest.raises(SsrfBlocked):
        assert_public_url(url)


def test_allows_public_https():
    # dominio público real y estable; resuelve a IP pública
    assert_public_url("https://example.com/image.jpg")
