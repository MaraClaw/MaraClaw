import fcntl
from multiprocessing.connection import Connection
from pathlib import Path


def late_writer(root: str, agent_id: str, channel: Connection) -> None:
    directory = Path(root) / agent_id
    locks = Path(root) / "_setup_locks"
    locks.mkdir(exist_ok=True)
    with (locks / f"{agent_id}.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        directory.mkdir()
        channel.send("writing")
        assert channel.poll(10), "parent did not release the writer"
        assert channel.recv() == "finish"
        (directory / "late.md").write_bytes(b"late thread output")
        fcntl.flock(lock, fcntl.LOCK_UN)
    channel.close()
