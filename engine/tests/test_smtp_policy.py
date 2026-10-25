import socket
from unittest.mock import Mock

import pytest

from app.core import smtp_policy
from app.core.outbound_policy import OutboundPolicyError


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1", "::ffff:127.0.0.1"])
def test_rebinding_to_private_address_is_blocked_before_connect(monkeypatch, address):
    # Given DNS has changed to a private answer since configuration validation.
    monkeypatch.setattr(
        socket, "getaddrinfo", Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 25))])
    )
    create = Mock()
    monkeypatch.setattr(socket, "socket", create)
    # When delivery resolves again, then no socket is opened to the private network.
    with pytest.raises(OutboundPolicyError):
        smtp_policy._public_socket("smtp.example.com", 25, 15)
    create.assert_not_called()


def test_public_delivery_pins_sockaddr_without_second_lookup(monkeypatch):
    # Given a public DNS result which would rebind if resolved a second time.
    resolve = Mock(
        side_effect=[[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 2525))], AssertionError("re-resolved")]
    )
    sock = Mock()
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket, "socket", Mock(return_value=sock))
    # When the SMTP adapter opens its socket, then it dials the exact validated address.
    assert smtp_policy._public_socket("smtp.example.com", 2525, 15) is sock
    sock.connect.assert_called_once_with(("8.8.8.8", 2525))
    assert resolve.call_count == 1


def test_mixed_public_and_private_dns_answers_fail_closed(monkeypatch):
    # Given one valid DNS answer and one metadata destination.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 25)) for address in ("8.8.8.8", "169.254.169.254")
            ]
        ),
    )
    create = Mock()
    monkeypatch.setattr(socket, "socket", create)
    # When delivery validates the whole set, then it rejects before trying even the public answer.
    with pytest.raises(OutboundPolicyError):
        smtp_policy._public_socket("smtp.example.com", 25, 15)
    create.assert_not_called()
