import os
import shutil
import stat
from contextlib import ExitStack, suppress
from pathlib import Path, PurePosixPath
from uuid import uuid4


def publish_complete_file(root: Path, relative: str, data: bytes) -> os.stat_result | None:
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        path = PurePosixPath(relative)
        for part in path.parts[:-1]:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o755, dir_fd=directory)
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        temporary = f".storage-create-{uuid4().hex}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                info = os.fstat(stream.fileno())
            try:
                os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                return None
            os.fsync(directory)
            return info
        finally:
            os.unlink(temporary, dir_fd=directory)


def delete_contained_entry(root: Path, relative: str) -> None:
    with ExitStack() as stack:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, directory)
        path = PurePosixPath(relative)
        for part in path.parts[:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            stack.callback(os.close, directory)
        if stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
            shutil.rmtree(path.name, dir_fd=directory)
        else:
            os.unlink(path.name, dir_fd=directory)
