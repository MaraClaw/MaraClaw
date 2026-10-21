import fcntl
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest

from deploy.starter_handoff import handoff


class ForegroundHandoffTests(unittest.TestCase):
    def test_foreground_controller_acquires_lock_and_receives_terminal_shutdown(self) -> None:
        child = """
import fcntl, signal, sys
from pathlib import Path
with (Path(sys.argv[1]) / 'controller.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_args: sys.exit(0))
    print('controller-ready', flush=True)
    sys.stdin.read()
"""
        launcher = """
from pathlib import Path
import sys
from deploy.starter_handoff import handoff, foreground
state = Path(sys.argv[1])
with handoff(state, False):
    raise SystemExit(foreground([sys.executable, '-B', '-c', sys.argv[2], str(state)], state))
"""
        for number in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=number), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                with subprocess.Popen([sys.executable, "-B", "-c", launcher, str(state), child],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as process:
                    try:
                        assert process.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(timeout=10), "controller did not become ready")
                        self.assertEqual(process.stdout.readline().strip(), "controller-ready")
                        with self.assertRaises(BlockingIOError), handoff(state, False):
                            self.fail("foreground handoff was released before controller shutdown")
                        with (state / "controller.lock").open("a") as lock, self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        process.send_signal(number)
                        process.wait(timeout=10)
                        stdout, stderr = process.communicate(timeout=10)
                        self.assertEqual(process.returncode, 0, stdout + stderr)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)
                with handoff(state, False), (state / "controller.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
