#!/usr/bin/env python3
import os
import selectors
import socket
import subprocess
import threading


LISTEN_HOST = os.environ.get("IMAP_PROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("IMAP_PROXY_LISTEN_PORT", "1993"))
DOCKER_CONTAINER = os.environ.get("IMAP_PROXY_CONTAINER", "mailserver")
TARGET_HOST = os.environ.get("IMAP_PROXY_TARGET_HOST", "127.0.0.1")
TARGET_PORT = os.environ.get("IMAP_PROXY_TARGET_PORT", "993")


def relay(client: socket.socket, backend_in, backend_out) -> None:
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, "client")
    selector.register(backend_out, selectors.EVENT_READ, "backend")
    try:
        while True:
            events = selector.select(timeout=60)
            if not events:
                continue
            for key, _ in events:
                if key.data == "client":
                    chunk = client.recv(65536)
                    if not chunk:
                        return
                    backend_in.write(chunk)
                    backend_in.flush()
                else:
                    chunk = backend_out.read(65536)
                    if not chunk:
                        return
                    client.sendall(chunk)
    finally:
        try:
            selector.close()
        except Exception:
            pass


def handle_client(client: socket.socket) -> None:
    proc = None
    try:
        proc = subprocess.Popen(
            ["docker", "exec", "-i", DOCKER_CONTAINER, "nc", TARGET_HOST, TARGET_PORT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        relay(client, proc.stdin, proc.stdout)
    finally:
        try:
            client.close()
        except Exception:
            pass
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def main() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    try:
        while True:
            client, _ = srv.accept()
            t = threading.Thread(target=handle_client, args=(client,), daemon=True)
            t.start()
    finally:
        srv.close()


if __name__ == "__main__":
    main()
