from dataclasses import dataclass
from pathlib import Path
import re
import shlex


TEMPLATE = """[Unit]
Description=MaraClaw single-host request autoscaler
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=simple
User=root
Environment=DOCKER_HOST=unix:///var/run/docker.sock
WorkingDirectory=@ROOT@
ExecStart=@PYTHON@ -m deploy.autoscale --root @ROOT@
Restart=on-failure
RestartSec=10
TimeoutStopSec=240
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


@dataclass(frozen=True, slots=True)
class Legacy:
    root: Path
    state: Path
    endpoint: str

    def verify(self, unit: Path, values: dict[str, str]) -> None:
        text = unit.read_text()
        match = re.search(r"^ExecStart=(/[^\s]+) -m deploy.autoscale --root (.+)$", text, re.M)
        if (match is None or self.state != Path("/var/lib/maraclaw-autoscaler")
                or self.endpoint != "unix:///var/run/docker.sock"
                or text != TEMPLATE.replace("@ROOT@", str(self.root)).replace("@PYTHON@", match[1])):
            raise ValueError("unrecognized legacy controller definition")
        expected = {"LoadState": "loaded", "FragmentPath": str(unit), "DropInPaths": "",
                    "NeedDaemonReload": "no", "WorkingDirectory": str(self.root),
                    "User": "root", "Type": "simple", "Environment": "DOCKER_HOST=" + self.endpoint,
                    "EnvironmentFiles": "", "ExecStartPre": "", "ExecStartPost": "", "ExecStop": "",
                    "ExecStopPost": "", "RootDirectory": "", "Restart": "on-failure",
                    "RestartUSec": "10s", "TimeoutStopUSec": "4min", "UMask": "0077",
                    "NoNewPrivileges": "yes", "PrivateTmp": "yes"}
        commands = re.findall(r"argv\[\]=(.*?) ;", values.get("ExecStart", ""))
        executables = re.findall(r"path=(.*?) ;", values.get("ExecStart", ""))
        if (any(values.get(key) != value for key, value in expected.items()) or len(commands) != 1
                or executables != [match[1]]
                or shlex.split(commands[0]) != [match[1], "-m", "deploy.autoscale", "--root", str(self.root)]):
            raise ValueError("loaded legacy controller definition differs from installed definition")
