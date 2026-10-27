"""An offline development stack for the YouTube flow: core, fetcher and a proxy, all on loopback.

    python -I tools/dev_fetch_stack.py [--port 8080] [--audio FILE] [--ffmpeg PATH] [--timeout SECONDS]

Then open http://127.0.0.1:8080/ (or http://127.0.0.1:8080/#yt=<URL-encoded
link>) in a browser on this machine. Three servers run in this one process:

- the core from the checkout, with FETCH_ENABLED=true, on 127.0.0.1 and a
  free port. It renders with the stub ffmpeg (tests/stubs/ffmpeg_stub.py)
  unless --ffmpeg names a local one, so the downloaded tone is a placeholder;
- the fetcher from the checkout, on 127.0.0.1 and a free port, running the
  stub yt-dlp (tests/stubs/ytdlp_stub.py) instead of yt-dlp. Nothing ever
  talks to YouTube. The stub hands back --audio, or a generated 60-second
  WAV, as the "m4a" (the browser's decoder goes by content, not by name), with
  a heatmap that peaks about 60 % of the way in;
- a small HTTP/1.0 proxy on 127.0.0.1:--port that forwards /fetch/ paths to
  the fetcher and everything else to the core, unchanged, as the
  deployment's reverse proxy does. PUBLIC_ORIGIN is http://127.0.0.1:<port>.

Any 11-character video ID fetches the audio. The stub's fixed IDs show each
failure instead: tooLong____, blocked____, notFound___, failing____,
oversize___, noAudio____, hanging____ (a timeout after --timeout seconds),
ctrlTitle__, hugeHeatmap and badHeatmap_ (see the stub's docstring).

Development only: it binds loopback addresses and nothing else, and it is
never part of an image.
"""
from __future__ import annotations

import argparse
import math
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# -I keeps the checkout off sys.path; both packages come from it explicitly.
for _path in (ROOT / "fetcher", ROOT / "core"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from strange_ringtone.config import Settings as CoreSettings  # noqa: E402
from strange_ringtone.render import Renderer  # noqa: E402
from strange_ringtone.server import RingtoneServer  # noqa: E402
from strange_ringtone_fetch.config import Settings as FetchSettings  # noqa: E402
from strange_ringtone_fetch.server import FetchServer  # noqa: E402
from strange_ringtone_fetch.ytdlp import YtDlp  # noqa: E402

LOOPBACK = "127.0.0.1"
FFMPEG_STUB = ROOT / "tests" / "stubs" / "ffmpeg_stub.py"
YTDLP_STUB = ROOT / "tests" / "stubs" / "ytdlp_stub.py"
RATE = 44100
SECONDS = 60
MAX_HEAD_BYTES = 65536


class StubYtDlp(YtDlp):
    """The fetcher's real runner with the stub as its program and the audio it should hand back."""

    def __init__(self, audio: Path, duration: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.audio = audio
        self.duration = duration

    def child_env(self, job_dir: Path) -> dict[str, str]:
        env = super().child_env(job_dir)
        env["STRANGE_RINGTONE_YTDLP_STUB_AUDIO"] = str(self.audio)
        env["STRANGE_RINGTONE_YTDLP_STUB_DURATION"] = f"{self.duration:g}"
        return env


def make_audio(path: Path) -> float:
    """A 60-second mono WAV: quiet notes, with a louder stretch around 60 %."""
    frames = bytearray()
    notes = (261.63, 329.63, 392.0, 440.0, 349.23, 293.66)
    for i in range(RATE * SECONDS):
        t = i / RATE
        note = notes[int(t) % len(notes)]
        loud = 0.6 if 33 <= t < 45 else 0.15
        envelope = math.exp(-3 * (t % 1.0))
        sample = loud * envelope * math.sin(2 * math.pi * note * t)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(frames))
    return float(SECONDS)


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as source:
            return source.getnframes() / source.getframerate()
    except (wave.Error, EOFError, OSError):
        # Not a WAV (an m4a, say): the stub's default duration then applies.
        return 215.5


class ProxyHandler(socketserver.StreamRequestHandler):
    server: Proxy

    def handle(self) -> None:
        self.request.settimeout(30)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(65536)
            if not chunk or len(head) > MAX_HEAD_BYTES:
                return
            head += chunk
        block, _, body = head.partition(b"\r\n\r\n")
        lines = block.split(b"\r\n")
        fields = lines[0].split(b" ")
        target = fields[1] if len(fields) == 3 else b"/"
        port = self.server.fetch_port if target.startswith(b"/fetch/") else self.server.core_port
        length = 0
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length" and value.strip().isdigit():
                length = int(value.strip())
        while len(body) < length:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            body += chunk
        # Passed on as received, Origin included; both backends answer one
        # request per connection and then close it.
        with socket.create_connection((LOOPBACK, port), timeout=30) as upstream:
            upstream.sendall(block + b"\r\n\r\n" + body)
            while chunk := upstream.recv(65536):
                self.request.sendall(chunk)


class Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, core_port: int, fetch_port: int) -> None:
        self.core_port = core_port
        self.fetch_port = fetch_port
        super().__init__((LOOPBACK, port), ProxyHandler)


def serve(server: socketserver.BaseServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline core + fetcher + proxy on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=8080, help="the proxy's loopback port (default 8080)")
    parser.add_argument("--audio", type=Path, help="a local audio file the stub returns (default: a generated WAV)")
    parser.add_argument("--ffmpeg", help="a local ffmpeg for real renders (default: the stub ffmpeg)")
    parser.add_argument("--timeout", type=int, default=10, help="FETCH_TIMEOUT_SECONDS for the stub (default 10)")
    args = parser.parse_args(argv)
    origin = f"http://{LOOPBACK}:{args.port}"
    with tempfile.TemporaryDirectory(prefix="strange_ringtone_dev_", ignore_cleanup_errors=True) as temp:
        work = Path(temp)
        if args.audio is not None:
            audio = args.audio.resolve()
            duration = wav_seconds(audio)
        else:
            audio = work / "audio.wav"
            duration = make_audio(audio)
        core_settings = CoreSettings(public_origin=origin, fetch_enabled=True, scratch_dir=work / "core")
        command = (args.ffmpeg,) if args.ffmpeg else (sys.executable, "-I", str(FFMPEG_STUB))
        core = RingtoneServer((LOOPBACK, 0), core_settings,
                              renderer=Renderer(command, core_settings.render_timeout_seconds))
        fetch_settings = FetchSettings(public_origin=origin, fetch_timeout_seconds=args.timeout,
                                       scratch_dir=work / "fetch")
        runner = StubYtDlp(
            audio,
            duration,
            max_seconds=fetch_settings.fetch_max_seconds,
            max_bytes=fetch_settings.fetch_max_bytes,
            timeout_seconds=fetch_settings.fetch_timeout_seconds,
            command=(sys.executable, "-I", str(YTDLP_STUB)),
        )
        fetcher = FetchServer((LOOPBACK, 0), fetch_settings, runner)
        proxy = Proxy(args.port, core.server_address[1], fetcher.server_address[1])
        for server in (core, fetcher, proxy):
            serve(server)
        print(f"dev stack: open {origin}/ (core :{core.server_address[1]}, fetcher :{fetcher.server_address[1]})",
              file=sys.stderr)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            for server in (proxy, fetcher, core):
                server.shutdown()
                server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
