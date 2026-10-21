import json
import os
from pathlib import Path
import plistlib
import sys


def main() -> int:
    root = Path(__file__).parent.parent
    name, arguments = Path(sys.argv[0]).name, sys.argv[1:]
    fixture = json.loads((root / "fixture.json").read_text())
    with (root / "events.jsonl").open("a") as events:
        events.write(json.dumps([name, arguments]) + "\n")
    match name:
        case "python3":
            os.execv(sys.executable, [sys.executable, *arguments])
        case "systemctl":
            unit = Path("/etc/systemd/system/maraclaw-autoscaler.service")
            if arguments[0] == "show":
                if unit.exists():
                    fields = dict(line.split("=", 1) for line in unit.read_text().splitlines() if "=" in line)
                    print("LoadState=loaded\nFragmentPath=" + str(unit))
                    print("WorkingDirectory=" + fields["WorkingDirectory"])
                    print("ExecStart={ argv[]=" + fields["ExecStart"] + " ; }")
                else:
                    print("LoadState=not-found")
            if arguments[0] == "enable":
                (root / "controller.service").write_text(unit.read_text())
            return 0
        case "launchctl":
            loaded = root / "loaded.plist"
            if arguments[0] == "print":
                if not loaded.exists():
                    return 1
                model = plistlib.loads(loaded.read_bytes())
                print("job = {\n arguments = {\n" + "\n".join("  " + word for word in model["ProgramArguments"])
                      + "\n }\n working directory = " + model["WorkingDirectory"] + "\n}")
            elif arguments[0] == "bootout":
                loaded.unlink()
            elif arguments[0] == "bootstrap":
                loaded.write_bytes(Path(arguments[2]).read_bytes())
            else:
                raise ValueError("unexpected fixture LaunchAgent command")
        case "docker":
            if arguments[:2] == ["compose", "version"]:
                print("5.6.0")
            elif arguments[:2] == ["context", "inspect"]:
                print("unix:///var/run/docker.sock")
            elif arguments[0] == "info":
                print("linux" if "{{.OSType}}" in arguments else fixture["memory"])
            elif arguments[0] == "ps":
                print("\n".join(item["Id"] for item in fixture["containers"]))
            elif arguments[0] == "inspect":
                print(json.dumps(fixture["containers"]))
            elif arguments[:2] == ["image", "inspect"]:
                print("fixture-image")
            elif "config" in arguments:
                if "--hash" in arguments:
                    print("\n".join(f"{service} unchanged" for service in fixture["model"]["services"]))
                elif "--quiet" not in arguments:
                    print(json.dumps(fixture["model"]))
            elif "up" in arguments and fixture["fail_up"]:
                return 12
            elif "up" not in arguments and "build" not in arguments:
                raise ValueError("unexpected fixture Docker command")
        case _:
            raise ValueError("unexpected fixture command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
