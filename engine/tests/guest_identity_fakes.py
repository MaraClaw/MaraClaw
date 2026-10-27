from dataclasses import dataclass
from types import SimpleNamespace

from pywhales.exceptions import NoSuchContainer


@dataclass(slots=True)
class Guest:
    id: str
    name: str
    owner: str | None
    published_port: str | None = None


class Containers:
    def __init__(self, *guests: Guest) -> None:
        self.guests = {guest.id: guest for guest in guests}
        self.calls: list[tuple[str, str]] = []
        self.threads: list[int] = []

    def inspect(self, reference: str) -> SimpleNamespace:
        import threading

        self.threads.append(threading.get_ident())
        for guest in self.guests.values():
            if reference in (guest.id, guest.name):
                labels = {"maraclaw.agent_id": guest.owner} if guest.owner is not None else {}
                return SimpleNamespace(
                    id=guest.id,
                    name=guest.name,
                    config=SimpleNamespace(labels=labels),
                    host_config=SimpleNamespace(
                        port_bindings={"18789/tcp": [SimpleNamespace(host_port=guest.published_port)]}
                    ),
                )
        raise NoSuchContainer(["docker", "inspect", reference], 1)

    def stop(self, reference: str, *, time: int) -> None:
        import threading

        assert time == 10
        self.threads.append(threading.get_ident())
        self.calls.append(("stop", reference))

    def remove(self, references: list[str]) -> None:
        import threading

        self.threads.append(threading.get_ident())
        for reference in references:
            self.calls.append(("remove", reference))
            container = self.inspect(reference)
            del self.guests[container.id]

    def execute(self, reference: str, command: list[str], *, stream: bool = False) -> str:
        self.calls.append(("execute", reference))
        return ""


class Docker:
    def __init__(self, *guests: Guest) -> None:
        self.container = Containers(*guests)
