"""Public-only SMTP dials pinned to validated DNS answers (not HTTP URLs)."""

import smtplib
import socket

from app.core.outbound_policy import OutboundPolicyError, _public_address


def _public_socket(host: str, port: int, timeout: float | None) -> socket.socket:
    # Validate every answer before connecting. Dial the exact sockaddr, never re-resolve.
    if host.rstrip(".").lower() == "localhost" or host.rstrip(".").lower().endswith(".localhost"):
        raise OutboundPolicyError("Local SMTP destinations are blocked")
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for answer in answers:
        _public_address(str(answer[4][0]))
    if not answers:
        raise OutboundPolicyError("SMTP host has no public addresses")
    last_error: OSError | None = None
    for family, kind, protocol, _name, address in answers:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            sock.connect(address)
            return sock
        except OSError as exc:
            sock.close()
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OutboundPolicyError("SMTP host has no usable addresses")


class PublicSMTP(smtplib.SMTP):
    def _get_socket(self, host: str, port: int, timeout: float | None) -> socket.socket:
        return _public_socket(host, port, timeout)


class PublicSMTPSSL(smtplib.SMTP_SSL):
    def _get_socket(self, host: str, port: int, timeout: float | None) -> socket.socket:
        sock = _public_socket(host, port, timeout)
        try:
            return self.context.wrap_socket(sock, server_hostname=host)
        except BaseException:
            sock.close()
            raise
