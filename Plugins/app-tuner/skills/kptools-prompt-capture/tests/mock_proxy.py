"""Loopback-only mock backend and proxy; no third-party dependencies."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import capture_runtime as pc


async def read_request(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    headers = [tuple(line.split(b":", 1)) for line in lines[1:] if b":" in line]
    headers = [(key.lower(), value.strip()) for key, value in headers]
    size = int(dict(headers).get(b"content-length", b"0"))
    body = await reader.readexactly(size)
    return head, headers, body


async def backend(reader, writer):
    try:
        _, _, raw = await read_request(reader)
        body = json.loads(raw)
        if body.get("stream"):
            data = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            data += b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":1}}\n\n'
            data += b'data: [DONE]\n\n'
            kind = b"text/event-stream"
        else:
            data = b'{"choices":[{"message":{"content":"hello"}}],"usage":{"prompt_tokens":7}}'
            kind = b"application/json"
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + kind +
                     b"\r\nContent-Length: " + str(len(data)).encode() +
                     b"\r\nConnection: close\r\n\r\n" + data)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def serve(args):
    if args.capture_config:
        pc.start(args.capture_config)

    async def forward(reader, writer):
        ctx = None
        upstream = None
        try:
            arrival = time.time()
            head, headers, raw = await read_request(reader)
            endpoint = head.split(b" ")[1].decode().removeprefix("/v1")
            ctx = pc.begin(endpoint, json.loads(raw), raw, headers, arrival)
            peer, upstream = await asyncio.open_connection("127.0.0.1", args.backend_port)
            upstream.write(head + raw)
            await upstream.drain()
            response_head = await peer.readuntil(b"\r\n\r\n")
            writer.write(response_head)
            while chunk := await peer.read(31):
                if ctx:
                    ctx.response_chunk(chunk)
                writer.write(chunk)
                await writer.drain()
            if ctx:
                ctx.finish("ok")
        except (ConnectionError, asyncio.IncompleteReadError):
            if ctx:
                ctx.finish("client_disconnect")
        except asyncio.CancelledError:
            if ctx:
                ctx.finish("cancelled")
            raise
        except Exception as exc:
            if ctx:
                ctx.finish("error:" + type(exc).__name__)
        finally:
            if upstream:
                upstream.close()
                await upstream.wait_closed()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(backend if args.backend else forward, "127.0.0.1", args.port)
    print(server.sockets[0].getsockname()[1], flush=True)
    try:
        async with server:
            await server.serve_forever()
    finally:
        pc.capture_shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--backend", action="store_true")
    parser.add_argument("--backend-port", type=int)
    parser.add_argument("--capture-config")
    asyncio.run(serve(parser.parse_args()))
