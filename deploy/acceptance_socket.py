from pathlib import Path
import socketserver
import subprocess
import threading

SCRIPT = ("import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(3);"
          "s.connect('/run/maraclaw-autoscaler/admin.sock');"
          "s.sendall(sys.stdin.buffer.read());s.shutdown(socket.SHUT_WR);"
          "data=bytearray();\n"
          "while chunk:=s.recv(65536): data.extend(chunk)\n"
          "sys.stdout.buffer.write(data);s.close()")


class Relay(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, helper: str):
        self.helper = helper
        super().__init__(str(path), Handler)


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(5)
        command = self.rfile.read(65536)
        server = self.server
        if not isinstance(server, Relay):
            raise TypeError("expected acceptance relay")
        result = subprocess.run(["docker", "exec", "-i", server.helper, "python", "-c", SCRIPT],
                                input=command, capture_output=True, timeout=5, check=True)
        self.wfile.write(result.stdout)


def start(path: Path, helper: str) -> tuple[Relay, threading.Thread]:
    relay = Relay(path, helper)
    worker = threading.Thread(target=relay.serve_forever, daemon=True)
    worker.start()
    return relay, worker
