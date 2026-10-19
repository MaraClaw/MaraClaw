import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from deploy.autoscale_development import prepare
from deploy.autoscale_docker import Docker, run
from deploy.autoscale_policy import Policy
from deploy import starter_service
from deploy.starter_host import execute, prerequisites, private_directory


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Compose startup with honest capacity admission")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--compose-file", action="append", type=Path, default=[])
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--foreground", action="store_true", help="supervise here without installing a service")
    parser.add_argument("--no-install", action="store_true", help="start and record only; never touch host services")
    parser.add_argument("--no-build", action="store_true", help="require already cached images instead of building")
    arguments = parser.parse_args()
    try:
        os.umask(0o077)
        root = arguments.root.resolve()
        darwin = os.uname().sysname == "Darwin"
        prerequisites(darwin)
        install = not (arguments.no_install or arguments.foreground)
        if install and not darwin and (os.getuid() != 0 or not Path("/run/systemd/system").is_dir()):
            raise ValueError("persistent Linux startup requires root and systemd")
        env_file = (arguments.env_file or root / ("deploy/.env.development" if arguments.development else "deploy/.env")).resolve()
        os.environ["MARACLAW_ENV_FILE"] = str(env_file)
        extras: list[Path] = []
        if darwin:
            extras.append(root / "deploy/compose.macos.yaml")
        extras.extend(path.resolve() for path in arguments.compose_file)
        files = (env_file, root / "compose.yaml", *extras)
        initial = Docker(root, files)
        state = (arguments.state_dir or (Path.home() / "Library/Application Support/MaraClaw/autoscaler"
                 if darwin else initial.state_dir)).absolute()
        private_directory(state)
        with (state / "controller.lock").open("a") as lock:
            if install:
                starter_service.stop(darwin, root, state)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if arguments.development:
                existing = initial.containers()
                override = state / "development.json"
                if not override.exists() and any(item.project == initial.project and not item.oneoff for item in existing):
                    raise ValueError("development cannot adopt an existing project without its effective state")
                environment = dict(line.split("=", 1) for line in run(initial.compose + ["config", "--environment"]).splitlines()
                                   if "=" in line)
                override = prepare(files, state, environment)
                files = (env_file, root / "compose.yaml", override, *extras)
            docker = Docker(root, files, state)
            if not darwin:
                model = run(docker.compose + ["config", "--format", "json", "--no-env-resolution"])
                mount = next(item for item in json.loads(model)["services"]["ingress"]["volumes"]
                             if item["target"] == "/run/maraclaw-autoscaler")
                if mount["source"] != str(state):
                    raise ValueError("host state must match ingress socket bind")
            policy_path = (arguments.policy or root / "deploy/autoscaler.toml").resolve()
            policy = Policy.load(policy_path)
            if arguments.no_build:
                run(["docker", "image", "inspect", *sorted(set(docker.images.values()))])
            else:
                execute(docker.compose + ["build"], 1800)
            counts = docker.startup(policy)
            options = [word for name, count in counts.items() for word in ("--scale", f"{name}={count}")]
            if docker.startup_reusable:
                options += ["--no-deps", *(name for name in docker.services if name != "bootstrap")]
            execute(docker.compose + ["up", "-d", "--no-build", "--pull", "never", "--wait",
                                     "--wait-timeout", "240", *options], 1800)
            docker.remember_startup()
            command = [str(Path(sys.executable).absolute()), "-m", "deploy.autoscale", "--root", str(root),
                       "--env-file", str(env_file), "--state-dir", str(state), "--policy", str(policy_path),
                       "--docker-host", os.environ["DOCKER_HOST"],
                       "--docker-binary", os.environ["MARACLAW_DOCKER_BINARY"],
                       "--socket-transport", "exec" if darwin else "unix",
                       *(word for source in files[1:] for word in ("--compose-file", str(source)))]
            print(f"startup recorded project={docker.project} counts={counts} state={state}", flush=True)
        if install:
            starter_service.install(command, root, state)
        elif arguments.foreground:
            os.chdir(root)
            os.execv(command[0], command)
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration,
            subprocess.SubprocessError) as error:
        print(f"startup refused ({type(error).__name__}): {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
