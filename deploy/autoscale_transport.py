import os
from pathlib import Path
import subprocess

from deploy.autoscale_docker import Docker, environment
from deploy.autoscale_proxy import Proxy, Snapshot


RELAY = """import socket,sys,time
command=sys.stdin.buffer.read(4097)
if len(command)>4096: raise ValueError('command too large')
deadline=time.monotonic()+4
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
 connection.settimeout(4)
 connection.connect('/run/maraclaw-autoscaler/admin.sock')
 connection.sendall(command)
 connection.shutdown(socket.SHUT_WR)
 chunks=[]
 size=0
 while True:
  connection.settimeout(max(0.001,deadline-time.monotonic()))
  chunk=connection.recv(65536)
  if not chunk: break
  size+=len(chunk)
  if size>8388608: raise ValueError('response too large')
  chunks.append(chunk)
 sys.stdout.buffer.write(b''.join(chunks))
"""


class ExecProxy(Proxy):
    def __init__(self, docker: Docker):
        super().__init__(Path("/run/maraclaw-autoscaler/admin.sock"))
        self.docker = docker
        self.ingress: str | None = None

    def snapshot(self, ingress: str) -> Snapshot:
        self.ingress = ingress
        return super().snapshot(ingress)

    def command(self, command: str) -> str:
        containers = self.docker.containers()
        background = self.docker.owned(containers, "background")
        ingress = self.docker.owned(containers, "ingress")
        if (len(background) != 1 or len(ingress) != 1
                or not all(item.running and item.healthy for item in (*background, *ingress))):
            raise ValueError("socket transport requires healthy owned singletons")
        if self.ingress is not None and self.ingress != ingress[0].identity + ":" + ingress[0].started:
            raise ValueError("ingress changed during socket operation")
        data = (command + "\n").encode("ascii")
        if len(data) > 4096 or "\n" in command:
            raise ValueError("invalid socket command")
        result = subprocess.run([os.environ.get("MARACLAW_DOCKER_BINARY", "docker"), "exec", "-i",
            background[0].identity, "python", "-c", RELAY], input=data,
            capture_output=True, timeout=8, check=False, env=environment())
        if result.returncode or len(result.stdout) > 8 * 1024**2:
            raise RuntimeError("socket transport unavailable")
        return result.stdout.decode("utf-8")
