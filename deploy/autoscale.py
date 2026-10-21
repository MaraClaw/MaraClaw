import argparse
import csv
import fcntl
import logging
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from .autoscale_controller import Controller
from .autoscale_docker import Docker
from .autoscale_policy import Policy, StartupMemoryError
from .autoscale_proxy import Proxy
from .autoscale_transport import ExecProxy


def main() -> int:
    parser = argparse.ArgumentParser(description="Single-host request-driven Compose autoscaler")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--compose-file", type=Path, action="append")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--state-dir", type=Path, help="private host checkpoint and controller state directory")
    parser.add_argument("--socket-transport", choices=("unix", "exec"), default="unix")
    parser.add_argument("--docker-host")
    parser.add_argument("--docker-binary", type=Path)
    startup = parser.add_mutually_exclusive_group()
    startup.add_argument("--check-startup", action="store_true", help="admit startup; print preserved Compose arguments")
    startup.add_argument("--record-startup", action="store_true", help="record a successful starter deployment")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger = logging.getLogger("maraclaw-autoscaler")
    try:
        os.umask(0o077)
        root = arguments.root.resolve()
        os.environ["MARACLAW_ENV_FILE"] = str(arguments.env_file or root / "deploy/.env")
        if arguments.docker_host:
            os.environ["DOCKER_HOST"] = arguments.docker_host
            os.environ.pop("DOCKER_CONTEXT", None)
        if arguments.docker_binary:
            os.environ["MARACLAW_DOCKER_BINARY"] = str(arguments.docker_binary)
        policy = Policy.load(arguments.policy or root / "deploy/autoscaler.toml")
        docker = Docker(root, (arguments.env_file or root / "deploy/.env",
                               *(arguments.compose_file or [root / "compose.yaml"])), arguments.state_dir)
        directory = arguments.state_dir or docker.state_dir
        with (directory / "controller.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if arguments.check_startup:
                counts = docker.startup(policy)
                options = " ".join(f"--scale {name}={count}" for name, count in counts.items())
                if docker.startup_reusable:
                    options += " --no-deps " + " ".join(name for name in docker.services if name != "bootstrap")
                print(options)
                return 0
            if arguments.record_startup:
                docker.remember_startup(policy)
                return 0
            proxy = ExecProxy(docker) if arguments.socket_transport == "exec" else Proxy(directory / "admin.sock")
            controller = Controller(docker, proxy, policy, directory)
            controller.retirement.recover()
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda _number, _frame: stop.set())
            signal.signal(signal.SIGINT, lambda _number, _frame: stop.set())
            try:
                while not stop.is_set():
                    try:
                        event = controller.tick(time.monotonic())
                        if event not in {"steady", "cooldown", "retiring"}:
                            logger.info("decision %s", event)
                    except (OSError, ValueError, KeyError, TypeError, csv.Error, RuntimeError,
                            subprocess.TimeoutExpired) as error:
                        logger.warning("sample_unavailable scaling_suspended=true reason=%s", type(error).__name__)
                        controller.invalidate()
                    stop.wait(policy.sample_seconds)
            finally:
                controller.retirement.cancel()
    except StartupMemoryError as error:
        logger.error("%s", error)
        return 1
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration, subprocess.TimeoutExpired):
        logger.error("controller_refused inspect_local_configuration=true")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
