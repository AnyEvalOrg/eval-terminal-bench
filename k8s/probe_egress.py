"""Run inside the isolated probe pod. Emit command intent and measured results only."""
import http.client
import json
import socket
import ssl
import struct
import sys
from pathlib import Path

proxy = sys.argv[1]
results = []


def check(command, action, expected):
    try:
        value = action()
    except Exception as exc:
        value = {"error_type": type(exc).__name__, "error": str(exc)}
    try:
        passed = expected(value)
    except (TypeError, AttributeError, KeyError):
        passed = False
    row = {"command": command, "output": value, "passed": passed}
    results.append(row)
    print(json.dumps(row), flush=True)


def https(host, path):
    c = http.client.HTTPSConnection(proxy, 8080, timeout=20, context=ssl.create_default_context())
    c.set_tunnel(host, 443)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return {"http_status": r.status}
    finally:
        c.close()


def tcp(host, port):
    with socket.create_connection((host, port), timeout=5):
        return "connected"


def dns(host, use_tcp=False):
    packet = struct.pack("!HHHHHH", 1234, 0x100, 1, 0, 0, 0) + b"\x04pypi\x03org\0" + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if use_tcp else socket.SOCK_DGRAM) as s:
        s.settimeout(5)
        s.connect((host, 53))
        if use_tcp:
            s.sendall(struct.pack("!H", len(packet)) + packet)
            header = s.recv(2)
            if len(header) != 2:
                raise RuntimeError("DNS TCP response missing length")
            size = struct.unpack("!H", header)[0]
            response = b""
            while len(response) < size:
                chunk = s.recv(size - len(response))
                if not chunk:
                    raise RuntimeError("Truncated DNS TCP response")
                response += chunk
        else:
            s.send(packet)
            response = s.recv(4096)
        txid, flags, _, answers, _, _ = struct.unpack("!HHHHHH", response[:12])
        return {"server": host, "answers": answers, "valid_response": txid == 1234 and bool(flags & 0x8000) and flags & 15 == 0,
                "contains_proxy_ip": socket.inet_aton(proxy) in response}


def timeout(value):
    return isinstance(value, dict) and value.get("error_type") in ("TimeoutError", "timeout")


check("HTTPS CONNECT pypi.org:443; GET /simple/", lambda: https("pypi.org", "/simple/"), lambda v: v.get("http_status") == 200)
check("HTTPS CONNECT example.com:443 (off-list, WARN mode); GET /", lambda: https("example.com", "/"), lambda v: v.get("http_status") == 200)
check("raw TCP 1.1.1.1:443, bypass all proxy env", lambda: tcp("1.1.1.1", 443), timeout)
check("raw TCP 8.8.8.8:53", lambda: tcp("8.8.8.8", 53), timeout)
for server in ("8.8.8.8", "1.1.1.1"):
    check(f"DNS A pypi.org via UDP {server}:53", lambda server=server: dns(server), timeout)
check("read /etc/resolv.conf", lambda: Path("/etc/resolv.conf").read_text(),
      lambda v: [line.split()[1] for line in v.splitlines() if line.startswith("nameserver")] == [proxy])
# iron-proxy 0.49.0's interception DNS answers over UDP only (TCP 53 is refused); a TCP
# refusal is therefore expected, not a bypass. The task pod's resolver uses UDP.
check(f"DNS A pypi.org via UDP proxy {proxy}:53", lambda: dns(proxy, False),
      lambda v: v.get("valid_response") is True and v.get("contains_proxy_ip") is True and v.get("answers", 0) > 0)
check("system resolver: socket.gethostbyname('pypi.org')", lambda: socket.gethostbyname("pypi.org"), lambda v: v == proxy)
print(json.dumps({"all_passed": all(r["passed"] for r in results), "checks": len(results)}), flush=True)
sys.exit(0 if all(r["passed"] for r in results) else 1)
