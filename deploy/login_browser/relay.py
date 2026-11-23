"""Binary stdio <-> loopback VNC. Invoked only through authenticated docker exec."""
import os
import selectors
import socket
import sys

with socket.create_connection(('127.0.0.1', 5900), timeout=5) as peer:
    peer.settimeout(None)
    selector = selectors.DefaultSelector()
    selector.register(peer, selectors.EVENT_READ)
    selector.register(sys.stdin.buffer, selectors.EVENT_READ)
    while True:
        for key, _ in selector.select():
            if key.fileobj is peer:
                data = peer.recv(65536)
                if not data:
                    raise SystemExit(0)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            else:
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    raise SystemExit(0)
                peer.sendall(data)
