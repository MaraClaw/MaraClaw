import json
import os
from pathlib import Path
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
            return 0
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
