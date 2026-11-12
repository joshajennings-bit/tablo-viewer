"""
Runs mpv embedded inside a window of this app and controls it over mpv's
JSON IPC (a named pipe on Windows, a Unix socket elsewhere).

Two IPC connections are used on purpose: Windows named pipes opened as plain
files can't read and write at the same time from different threads, so one
connection only listens for events and the other only sends commands.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("tablo.mpv")

IS_WIN = sys.platform == "win32"

# Keys pressed while the video has focus. Anything sent as script-message
# comes back to the app as a "client-message" event.
INPUT_CONF = """\
# Tablo Viewer key bindings for the embedded mpv player
f               script-message tv fullscreen
MBTN_LEFT_DBL   script-message tv fullscreen
ESC             script-message tv exit-fullscreen
UP              script-message tv channel-up
DOWN            script-message tv channel-down
PGUP            script-message tv channel-up
PGDWN           script-message tv channel-down
c               script-message tv captions
C               script-message tv captions
j               script-message tv captions
J               script-message tv captions
v               script-message tv captions
LEFT            seek -10
RIGHT           seek 30
WHEEL_UP        add volume 5
WHEEL_DOWN      add volume -5
q               ignore
Q               ignore
CLOSE_WIN       ignore
"""

OBSERVED = ["pause", "volume", "mute", "paused-for-cache", "idle-active",
            "video-params/w", "video-params/h", "demuxer-cache-duration", "sid"]


class _Conn:
    """One line-oriented IPC connection."""

    def __init__(self, address: str):
        self._buf = b""
        if IS_WIN:
            self._f = open(address, "r+b", buffering=0)  # noqa: SIM115
            self._sock = None
        else:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(address)
            self._f = None

    def send(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        if self._f:
            self._f.write(data)
        else:
            self._sock.sendall(data)

    def readline(self) -> dict | None:
        while b"\n" not in self._buf:
            chunk = self._f.read(65536) if self._f else self._sock.recv(65536)
            if not chunk:
                return None
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except ValueError:
            return {}

    def close(self) -> None:
        try:
            (self._f or self._sock).close()
        except Exception:  # noqa: BLE001
            pass


class MpvPlayer:
    def __init__(self, mpv_path: str, wid: int, data_dir: Path, on_event, volume: int = 80):
        self.mpv_path = mpv_path
        self.wid = wid
        self.data_dir = data_dir
        self.on_event = on_event  # called from a background thread: on_event(dict)
        self.initial_volume = volume
        self.proc: subprocess.Popen | None = None
        self._cmd_q: queue.Queue = queue.Queue()
        self._req = 0
        self._alive = False
        if IS_WIN:
            self.address = rf"\\.\pipe\tabloviewer-{os.getpid()}"
        else:
            self.address = str(data_dir / f"mpv-{os.getpid()}.sock")

    # ---------------- lifecycle ----------------

    def start(self) -> None:
        conf = self.data_dir / "input.conf"
        conf.write_text(INPUT_CONF, encoding="utf-8")
        args = [
            self.mpv_path,
            f"--wid={self.wid}",
            "--idle=yes",
            "--force-window=no",
            f"--input-ipc-server={self.address}",
            f"--input-conf={conf}",
            "--input-default-bindings=yes",
            "--keep-open=no",
            "--deinterlace=yes",
            "--sid=no",  # closed captions start off; the app turns them on when asked
            "--hwdec=auto-safe",
            "--cache=yes",
            "--demuxer-max-bytes=150MiB",
            "--demuxer-max-back-bytes=200MiB",
            f"--volume={self.initial_volume}",
            "--volume-max=130",
            "--osd-level=1",
            "--osd-font-size=34",
            "--cursor-autohide=1500",
            "--script-opts=osc-visibility=never",
            "--msg-level=all=warn",
            f"--log-file={self.data_dir / 'mpv.log'}",
            "--terminal=no",
        ]
        if not IS_WIN and os.path.exists(self.address):
            os.unlink(self.address)
        log.info("starting mpv: %s", self.mpv_path)
        flags = subprocess.CREATE_NO_WINDOW if IS_WIN else 0
        self.proc = subprocess.Popen(args, creationflags=flags,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        evt = self._connect()
        cmd = self._connect()
        self._alive = True
        threading.Thread(target=self._event_loop, args=(evt,), daemon=True).start()
        threading.Thread(target=self._command_loop, args=(cmd,), daemon=True).start()

    def _connect(self) -> _Conn:
        deadline = time.monotonic() + 8
        last = None
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"mpv exited immediately (code {self.proc.returncode}). "
                                   f"See mpv.log in {self.data_dir}.")
            try:
                return _Conn(self.address)
            except OSError as e:
                last = e
                time.sleep(0.1)
        raise RuntimeError(f"Couldn't connect to mpv: {last}")

    @property
    def running(self) -> bool:
        return self._alive and self.proc is not None and self.proc.poll() is None

    def quit(self) -> None:
        self._alive = False
        if self.proc and self.proc.poll() is None:
            try:
                self._cmd_q.put(["quit"])
                self.proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                self.proc.kill()

    # ---------------- commands ----------------

    def command(self, *args) -> None:
        if self.running:
            self._cmd_q.put(list(args))

    def play(self, url: str, title: str = "", captions: bool = False) -> None:
        if title:
            self.command("set_property", "force-media-title", title)
        self.set_captions(captions)  # applies to the stream about to load
        self.command("loadfile", url, "replace")
        self.command("set_property", "pause", False)

    def stop(self) -> None:
        self.command("stop")

    def set_volume(self, v: float) -> None:
        self.command("set_property", "volume", max(0, min(130, round(v))))

    def set_captions(self, on: bool) -> None:
        """Closed captions on ("auto" picks the caption track) or off."""
        self.command("set_property", "sid", "auto" if on else "no")

    def set_osc(self, visible: bool) -> None:
        self.command("script-message", "osc-visibility", "auto" if visible else "never", "no_osd")

    def show_text(self, text: str, ms: int = 1500) -> None:
        self.command("show-text", text, ms)

    # ---------------- threads ----------------

    def _command_loop(self, conn: _Conn) -> None:
        try:
            conn.send({"command": ["disable_event", "all"], "request_id": 0})
            conn.readline()
            while self._alive:
                args = self._cmd_q.get()
                self._req += 1
                rid = self._req
                conn.send({"command": args, "request_id": rid})
                if args and args[0] == "quit":
                    break
                while True:  # wait for this command's reply so the pipe never fills up
                    msg = conn.readline()
                    if msg is None:
                        return
                    if msg.get("request_id") == rid:
                        if msg.get("error") not in (None, "success"):
                            log.info("mpv %s -> %s", args[0], msg.get("error"))
                        break
        except Exception as e:  # noqa: BLE001
            log.warning("mpv command connection closed: %s", e)
        finally:
            conn.close()

    def _event_loop(self, conn: _Conn) -> None:
        try:
            for i, name in enumerate(OBSERVED, start=1):
                conn.send({"command": ["observe_property", i, name]})
            while True:
                msg = conn.readline()
                if msg is None:
                    break
                if "event" in msg:
                    try:
                        self.on_event(msg)
                    except Exception:  # noqa: BLE001
                        log.exception("mpv event handler failed")
        except Exception as e:  # noqa: BLE001
            log.warning("mpv event connection closed: %s", e)
        finally:
            conn.close()
            if self._alive:
                self._alive = False
                self.on_event({"event": "player-exited"})
