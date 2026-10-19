from pathlib import Path
import socket
import tempfile
import threading
import unittest

from deploy.autoscale_proxy import Proxy, parse_addresses, parse_stats

CSV = ("# pxname,svname,req_tot,scur,qcur,addr,status,idle_conn_cur,safe_conn_cur,used_conn_cur\n"
       "engine_pool,BACKEND,123,2,1,,UP,,,\n"
       "engine_pool,engine1,,0,0,10.0.0.2:8000,UP,0,1,0\n")
STATE = ("1\n# be_id be_name srv_id srv_name srv_addr\n"
         "1 engine_pool 1 engine1 10.0.0.2\n\n")


class ProxyParsingTests(unittest.TestCase):
    def test_counters_when_actual_csv_uses_backend_request_totals(self) -> None:
        rows = parse_stats(CSV)
        self.assertEqual((rows[("engine_pool", "BACKEND")].requests,
                          rows[("engine_pool", "BACKEND")].queued), (123, 1))
        self.assertFalse(rows[("engine_pool", "engine1")].quiet)

    def test_malformed_when_required_counter_is_empty(self) -> None:
        with self.assertRaises(ValueError):
            parse_stats(CSV.replace("BACKEND,123", "BACKEND,"))

    def test_mapping_when_state_contains_address(self) -> None:
        self.assertEqual(parse_addresses(STATE), {("engine_pool", "engine1"): "10.0.0.2"})

    def test_socket_when_stats_arrive_in_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.settimeout(5)
                server.bind(str(path))
                server.listen(1)
                def serve() -> None:
                    with server.accept()[0] as connection:
                        connection.settimeout(5)
                        self.assertEqual(connection.recv(1024), b"show stat\n")
                        self.assertEqual(connection.recv(1), b"")
                        connection.sendall(CSV[:30].encode())
                        connection.sendall(CSV[30:].encode())
                worker = threading.Thread(target=serve)
                worker.start()
                rows = parse_stats(Proxy(path).command("show stat"))
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(rows[("engine_pool", "BACKEND")].requests, 123)

    def test_release_when_leaving_maintenance_blocks_assignment_until_health_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.sock"
            commands: list[str] = []
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.settimeout(5)
                server.bind(str(path))
                server.listen(4)
                def serve() -> None:
                    for _ in range(4):
                        with server.accept()[0] as connection:
                            connection.settimeout(5)
                            commands.append(connection.recv(1024).decode().strip())
                            self.assertEqual(connection.recv(1), b"")
                worker = threading.Thread(target=serve)
                worker.start()
                Proxy(path).release("engine_pool", "engine2")
                worker.join(timeout=5)
            self.assertEqual(commands, ["set server engine_pool/engine2 " + setting for setting in
                ("weight 0", "state ready", "health down", "weight 100%")])
