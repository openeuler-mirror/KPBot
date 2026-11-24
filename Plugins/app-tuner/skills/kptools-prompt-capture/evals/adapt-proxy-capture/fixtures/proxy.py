"""Uninstrumented loopback proxy for the adaptation case."""
import argparse
import asyncio


async def run(backend_port):
    async def forward(reader, writer):
        upstream = None
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            fields = dict(line.split(b":", 1) for line in headers.lower().split(b"\r\n")[1:-2])
            body = await reader.readexactly(int(fields.get(b"content-length", b"0")))
            peer, upstream = await asyncio.open_connection("127.0.0.1", backend_port)
            upstream.write(headers + body)
            await upstream.drain()
            while chunk := await peer.read(4096):
                writer.write(chunk)
                await writer.drain()
        finally:
            if upstream:
                upstream.close()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(forward, "127.0.0.1", 0)
    print(server.sockets[0].getsockname()[1], flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-port", type=int, required=True)
    asyncio.run(run(parser.parse_args().backend_port))
