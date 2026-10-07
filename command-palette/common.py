#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import socket


def send_herdr_request(request_id: str, method: str, params: dict) -> dict:
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if not socket_path:
        raise RuntimeError("HERDR_SOCKET_PATH is not set")
    payload = json.dumps({"id": request_id, "method": method, "params": params})
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(payload.encode() + b"\n")
        response = b""
        while b"\n" not in response:
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    if not response:
        raise RuntimeError(f"Herdr returned no response for {method}")
    decoded = json.loads(response.splitlines()[0])
    if "error" in decoded:
        error = decoded["error"]
        raise RuntimeError(error.get("message") or error.get("code") or str(error))
    return decoded["result"]
