from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import signal
import time


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/api/ready"}:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        identity = socket.gethostname().encode()
        self.send_response(200)
        self.send_header("X-Backend", identity.decode())
        if self.path == "/stream":
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for _ in range(120):
                    self.wfile.write(b"stream\n")
                    self.wfile.flush()
                    time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(identity)))
            self.end_headers()
            self.wfile.write(identity)

    def log_message(self, format: str, *args: object) -> None:
        return None


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _number, _frame: exit(0))
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
