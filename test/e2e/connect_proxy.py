"""A minimal HTTP CONNECT proxy for the e2e `worker` suite: the only way out of the
remote worker's isolated Docker network. It tunnels CONNECT requests to the allowed
host:port pairs only and refuses everything else (other hosts, other ports, plain HTTP),
logging one line per request (ALLOW / DENY) to stdout.

Usage: python3 connect_proxy.py <listen port> <host:port> [<host:port> ...]"""
import socket
import sys
import threading

ALLOWED = set(sys.argv[2:])


def log(line):
    print(line, flush=True)


def pipe(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for s in (source, target):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client, address):
    try:
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 65536:
            chunk = client.recv(4096)
            if not chunk:
                return
            head += chunk
        request_line = head.split(b'\r\n', 1)[0].decode('latin-1')
        parts = request_line.split()
        method, target = (parts[0], parts[1]) if len(parts) >= 2 else ('?', '?')
        if method != 'CONNECT' or target not in ALLOWED:
            log('DENY {} {} from {}'.format(method, target, address[0]))
            client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            return
        host, port = target.rsplit(':', 1)
        upstream = socket.create_connection((host, int(port)), timeout=10)
        upstream.settimeout(None)
        log('ALLOW CONNECT {} from {}'.format(target, address[0]))
        client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        rest = head.split(b'\r\n\r\n', 1)[1]
        if rest:
            upstream.sendall(rest)
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()
        pipe(client, upstream)
    except OSError as e:
        log('ERROR {} from {}'.format(e, address[0]))
    finally:
        client.close()


def main():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('0.0.0.0', int(sys.argv[1])))
    server.listen(64)
    log('proxy listening on :{}, allowed: {}'.format(sys.argv[1], ' '.join(sorted(ALLOWED))))
    while True:
        client, address = server.accept()
        threading.Thread(target=handle, args=(client, address), daemon=True).start()


if __name__ == '__main__':
    main()
