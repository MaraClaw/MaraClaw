from configparser import ConfigParser
from dataclasses import dataclass
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess

from deploy.starter_host import execute, private_directory


LABEL = "com.maraclaw.autoscaler"
UNIT = Path("/etc/systemd/system/maraclaw-autoscaler.service")


@dataclass(frozen=True, slots=True)
class Ownership:
    root: Path
    state: Path
    endpoint: str

    def check(self, command: list[str], directory: str) -> None:
        if len(command) < 3 or command[1:3] != ["-m", "deploy.autoscale"] or directory != str(self.root):
            raise ValueError("foreign persistent controller")
        for flag, expected in (("--root", str(self.root)), ("--state-dir", str(self.state)),
                               ("--docker-host", self.endpoint)):
            aliases = [word for word in command if word.startswith("--")
                       and flag.startswith(word.split("=", 1)[0])]
            if aliases != [flag]:
                raise ValueError("persistent controller ownership is unproven")
            index = command.index(flag) + 1
            if index >= len(command) or command[index] != expected:
                raise ValueError("foreign persistent controller")

    def plist(self, path: Path) -> None:
        with path.open("rb") as stream:
            model = plistlib.load(stream)
        if model["Label"] != LABEL or model["EnvironmentVariables"]["DOCKER_HOST"] != self.endpoint:
            raise ValueError("foreign persistent controller")
        self.check(model["ProgramArguments"], model["WorkingDirectory"])

    def unit(self, path: Path) -> None:
        model = ConfigParser(interpolation=None)
        model.read_string(path.read_text())
        self.check(shlex.split(model["Service"]["ExecStart"]), model["Service"]["WorkingDirectory"])


def verify(darwin: bool, owner: Ownership) -> bool:
    if darwin:
        path = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
        if path.exists():
            owner.plist(path)
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                                capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            return False
        arguments = re.search(r"^\s*arguments = \{\n(.*?)^\s*\}", result.stdout, re.M | re.S)
        directory = re.search(r"^\s*working directory = (.+)$", result.stdout, re.M)
        if arguments is None or directory is None:
            raise ValueError("loaded controller ownership is unproven")
        owner.check([line.strip() for line in arguments[1].splitlines() if line.strip()], directory[1])
        return True
    if UNIT.exists():
        owner.unit(UNIT)
    result = subprocess.run(["systemctl", "show", "maraclaw-autoscaler.service",
                             "--property=LoadState,WorkingDirectory,ExecStart,FragmentPath"],
                            capture_output=True, text=True, timeout=30, check=True)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        return False
    command = re.search(r"argv\[\]=(.*?) ;", values.get("ExecStart", ""))
    if command is None:
        raise ValueError("loaded controller ownership is unproven")
    owner.check(shlex.split(command[1]), values.get("WorkingDirectory", ""))
    fragment = values.get("FragmentPath", "")
    if fragment:
        owner.unit(Path(fragment))
    return True


def launchagent(command: list[str], root: Path, state: Path) -> Path:
    owner = Ownership(root, state, os.environ["DOCKER_HOST"])
    owner.check(command, str(root))
    verify(True, owner)
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (LABEL + ".plist")
    model = {"Label": LABEL, "ProgramArguments": command, "WorkingDirectory": str(root),
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
        "Umask": 0o077, "ProcessType": "Background",
        "EnvironmentVariables": {"PATH": os.environ["PATH"], "HOME": str(Path.home()),
                                 "DOCKER_HOST": os.environ["DOCKER_HOST"]},
        "StandardOutPath": str(state / "controller.log"), "StandardErrorPath": str(state / "controller.log")}
    temporary = directory / (LABEL + ".plist.tmp")
    with temporary.open("xb") as output:
        os.chmod(temporary, 0o600)
        plistlib.dump(model, output)
    temporary.replace(path)
    return path


def stop(darwin: bool, root: Path, state: Path) -> None:
    if not verify(darwin, Ownership(root, state, os.environ["DOCKER_HOST"])):
        return
    if darwin:
        target = f"gui/{os.getuid()}/{LABEL}"
        execute(["launchctl", "bootout", target], 260)
    else:
        execute(["systemctl", "stop", "maraclaw-autoscaler.service"], 260)


def install(command: list[str], root: Path, state: Path) -> None:
    private_directory(state)
    if os.uname().sysname == "Darwin":
        path = launchagent(command, root, state)
        execute(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
    else:
        owner = Ownership(root, state, os.environ["DOCKER_HOST"])
        owner.check(command, str(root))
        verify(False, owner)
        if any(any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-=:"
                   for character in word) for word in command):
            raise ValueError("systemd arguments contain unsupported metacharacters")
        text = (root / "deploy/maraclaw-autoscaler.service").read_text()
        text = text.replace("@ROOT@", str(root)).replace("@PYTHON@", command[0])
        text = "\n".join("ExecStart=" + " ".join(command) if line.startswith("ExecStart=") else line
                         for line in text.splitlines()) + "\n"
        UNIT.write_text(text)
        execute(["systemctl", "daemon-reload"])
        execute(["systemctl", "enable", "--now", "maraclaw-autoscaler.service"])
