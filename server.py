"""Shield TV remote: a small web server for phones on the home Wi-Fi.

Phones open the page and talk to this server over a WebSocket. The server
keeps one Android TV Remote (v2) connection to the Shield, the same protocol
the Google TV remote uses, and forwards keys and text to it. App launches and
power go over adb; volume goes to the Samsung TV itself (samsung.py).
Nothing here is meant to be reachable from outside the home network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pathlib
import shlex
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

ROOT = pathlib.Path(__file__).resolve().parent
STATIC = ROOT / "static"

log = logging.getLogger("tv-remote")

# Long presses (hold OK for a context menu) are released by the page, but a
# phone can drop off Wi-Fi mid-press, so the server lets go on its own too.
LONG_PRESS_MAX_S = 5.0

# Voice search streams PCM (16-bit LE, mono, 8 kHz). The Shield wants messages of at least 8 KB
# and the library pads shorter ones with silence, so audio goes out in exact 0.5 s chunks. The
# page stops after 12 s; the server stops a forgotten session on its own a little later.
VOICE_CHUNK = 8192
VOICE_MAX_S = 15.0

KEYS = {
    "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT", "DPAD_CENTER",
    "BACK", "HOME", "MENU", "SETTINGS", "SEARCH", "ENTER", "DEL",
    "MEDIA_PLAY_PAUSE", "MEDIA_REWIND", "MEDIA_FAST_FORWARD",
    "MEDIA_PREVIOUS", "MEDIA_NEXT", "CAPTIONS",
    "VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE",
    "POWER", "WAKEUP", "SLEEP",
}

# The Samsung ignores CEC volume from the Shield, so these go to the TV directly.
VOLUME_KEYS = {"VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE"}


def load_config(path: pathlib.Path) -> dict:
    """Addresses and the app tiles; see config.example.json."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Adb:
    """Starts apps and handles power over adb.

    The remote protocol can only open app links, the Shield has no handler for
    market://launch, and a link it can't open makes it drop the connection.
    adb also answers while the Shield sleeps, when remote keys don't get through.
    """

    LAUNCH_CATEGORIES = ("android.intent.category.LEANBACK_LAUNCHER", "android.intent.category.LAUNCHER")

    def __init__(self, host: str):
        self.serial = f"{host}:5555"
        self._components: dict[str, str] = {}

    async def _adb(self, *args: str, timeout: float = 10) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "adb", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "timeout"
        return proc.returncode, out.decode(errors="replace")

    async def shell(self, cmd: str) -> tuple[int, str]:
        rc, out = await self._adb("-s", self.serial, "shell", cmd)
        if rc != 0:  # adb server restarted or the Shield dropped us: reconnect once
            await self._adb("connect", self.serial)
            rc, out = await self._adb("-s", self.serial, "shell", cmd)
        return rc, out

    async def _component(self, package: str) -> str | None:
        if package not in self._components:
            for category in self.LAUNCH_CATEGORIES:
                rc, out = await self.shell(
                    "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                    f"-c {category} {shlex.quote(package)}")
                lines = out.strip().splitlines()
                if rc == 0 and lines and "/" in lines[-1]:
                    self._components[package] = lines[-1].strip()
                    break
        return self._components.get(package)

    async def wakefulness(self) -> str | None:
        """Awake, Dreaming (screensaver), Dozing or Asleep; None if adb can't tell."""
        rc, out = await self.shell("dumpsys power | grep mWakefulness=")
        line = out.strip().splitlines()[0] if out.strip() else ""
        if rc != 0 or not line.strip().startswith("mWakefulness="):
            return None
        return line.split("=", 1)[1].strip()

    async def keyevent(self, code: str) -> bool:
        rc, _ = await self.shell(f"input keyevent {shlex.quote(code)}")
        return rc == 0

    async def launch(self, package: str) -> bool:
        component = await self._component(package)
        if component is None:
            log.warning("adb: no launcher activity for %s", package)
            return False
        # The launcher's own flags: bring the app's task to the front instead of a fresh copy.
        rc, out = await self.shell(
            "cmd activity start-activity -a android.intent.action.MAIN "
            f"-c android.intent.category.LEANBACK_LAUNCHER -f 0x10200000 -n {shlex.quote(component)}")
        ok = rc == 0 and "Error" not in out
        if not ok:
            log.warning("adb: launching %s failed: %s", component, out.strip())
        return ok


class Shield:
    """One supervised remote connection to the TV, plus the state the page shows."""

    def __init__(self, host: str, data_dir: pathlib.Path, on_change):
        from androidtvremote2 import AndroidTVRemote

        data_dir.mkdir(parents=True, exist_ok=True)
        self._remote = AndroidTVRemote(
            client_name="Pi TV Remote",  # goes into the certificate's DNS name, so ASCII only
            certfile=str(data_dir / "cert.pem"),
            keyfile=str(data_dir / "key.pem"),
            host=host,
            enable_voice=True,
        )
        self._remote.add_is_on_updated_callback(lambda v: self._set(on=v))
        self._remote.add_current_app_updated_callback(lambda v: self._set(app=v))
        # The Shield reports 0/0 because the TV owns the volume; the Samsung module fills it in.
        self._remote.add_volume_info_updated_callback(
            lambda v: self._set(volume=dict(v)) if v.get("max") else None)
        self._on_change = on_change
        self._adb = Adb(host)
        self._wake = asyncio.Event()
        self._connected_evt = asyncio.Event()
        self._waiting = False
        self._voice = None  # the open VoiceStream, if any
        self._voice_buf = bytearray()
        # Optimistic: the first connect attempt tells us whether the certificate is paired.
        self.state = {
            "connected": False,
            "paired": True,
            "pairing": False,
            "on": None,
            "app": None,
            "volume": None,
        }

    def _set(self, **changes) -> None:
        changed = {k: v for k, v in changes.items() if self.state.get(k) != v}
        if not changed:
            return
        self.state.update(changed)
        if "connected" in changed:
            (self._connected_evt.set if changed["connected"] else self._connected_evt.clear)()
        self._on_change()

    async def _wait_wake(self, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def run(self) -> None:
        from androidtvremote2 import CannotConnect, ConnectionClosed, InvalidAuth

        await self._remote.async_generate_cert_if_missing()
        backoff = 1.0
        while True:
            if self.state["pairing"] or not self.state["paired"]:
                await self._wait_wake(None)
                continue
            try:
                await asyncio.wait_for(self._remote.async_connect(), 10)
            except InvalidAuth:
                log.warning("Shield rejected our certificate, pairing needed")
                self._remote.disconnect()
                self._set(paired=False, connected=False)
                continue
            except (CannotConnect, ConnectionClosed, asyncio.TimeoutError, OSError) as exc:
                log.info("Can't reach the Shield (%s), retry in %.0fs", exc, backoff)
                self._remote.disconnect()
                self._set(connected=False)
                await self._wait_wake(backoff)
                backoff = min(backoff * 2, 15)
                continue
            backoff = 1.0
            log.info("Connected to the Shield")
            self._set(connected=True, on=self._remote.is_on, app=self._remote.current_app)
            proto = self._remote._remote_message_protocol  # no public "connection lost" hook
            if proto is not None:
                try:
                    await proto.on_con_lost
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - any loss just means reconnect
                    pass
            log.info("Lost the connection to the Shield")
            if not self.state["pairing"]:  # pairing closes the remote link on purpose
                self._remote.disconnect()
            self._set(connected=False)

    def kick(self) -> None:
        """Retry a lost connection now instead of after the backoff."""
        if not self.state["connected"]:
            self._wake.set()

    async def _ready(self) -> bool:
        """Wait briefly for a connection, nudging the supervisor to retry right away."""
        if self.state["connected"]:
            return True
        if self._waiting:
            return False
        self._waiting = True
        self._wake.set()
        try:
            await asyncio.wait_for(self._connected_evt.wait(), 4)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._waiting = False

    async def key(self, code: str, direction: str = "SHORT") -> bool:
        if not await self._ready():
            return False
        self._remote.send_key_command(code, direction)
        return True

    async def text(self, value: str) -> bool:
        if not await self._ready():
            return False
        if value:
            self._remote.send_text(value)
        else:
            self._remote.send_key_command("DEL")
        return True

    async def power(self, code: str) -> bool:
        """WAKEUP only wakes; POWER toggles by what the Shield really is, not what we last heard."""
        now = await self._adb.wakefulness()
        if now is None:
            log.warning("power: adb can't see the Shield, falling back to the remote key")
            return await self.key(code)
        asleep = now in ("Asleep", "Dozing")
        if not asleep and code == "WAKEUP":
            return True
        event = "KEYCODE_WAKEUP" if asleep else "KEYCODE_SLEEP"
        log.info("power: Shield is %s, sending %s", now, event)
        ok = await self._adb.keyevent(event)
        if asleep:
            self._wake.set()  # the remote link may have dropped while it slept
        return ok

    async def launch(self, package: str) -> bool:
        if await self._adb.wakefulness() in ("Asleep", "Dozing"):
            await self._adb.keyevent("KEYCODE_WAKEUP")
        return await self._adb.launch(package)

    async def voice_start(self) -> str | None:
        """Open a voice session; None once the TV listens, else what to tell the user."""
        from androidtvremote2 import ConnectionClosed, VoiceSessionInProgress

        if not await self._ready():
            return "Нет связи с телевизором"
        if not self._remote.is_voice_enabled:
            return "Телевизор не поддерживает голосовой ввод"
        self._voice_buf.clear()
        try:
            self._voice = await self._remote.start_voice()
        except VoiceSessionInProgress:
            return "Голосовой ввод уже занят"
        except asyncio.TimeoutError:
            return "Телевизор не ответил на голосовой запрос"
        except ConnectionClosed:
            return "Нет связи с телевизором"
        return None

    async def voice_audio(self, data: bytes) -> bool:
        """Buffer audio and send it in whole chunks; False if the TV connection is gone."""
        from androidtvremote2 import ConnectionClosed

        if self._voice is None:
            return False
        self._voice_buf += data
        try:
            while len(self._voice_buf) >= VOICE_CHUNK:
                self._voice.send_chunk(bytes(self._voice_buf[:VOICE_CHUNK]))
                del self._voice_buf[:VOICE_CHUNK]
        except ConnectionClosed:
            return False
        return True

    async def voice_end(self) -> None:
        """Send what is left (the library pads it to a full chunk) and close the session."""
        from androidtvremote2 import ConnectionClosed

        voice, self._voice = self._voice, None
        rest = bytes(self._voice_buf[:len(self._voice_buf) & ~1])  # whole samples only
        self._voice_buf.clear()
        if voice is None:
            return
        try:
            if rest:
                voice.send_chunk(rest)
        except ConnectionClosed:
            pass
        voice.end()

    async def pair_start(self) -> None:
        self._set(pairing=True, connected=False)
        try:
            await self._remote.async_start_pairing()
        except Exception:
            self._set(pairing=False)
            self._wake.set()
            raise

    async def pair_finish(self, code: str) -> None:
        try:
            await self._remote.async_finish_pairing(code.strip().upper())
        finally:
            self._set(pairing=False)
            self._wake.set()
        self._set(paired=True)
        self._wake.set()

    def pair_cancel(self) -> None:
        self._remote.disconnect()
        self._set(pairing=False)
        self._wake.set()


class FakeShield:
    """Stand-in for UI work away from the TV: logs commands, fakes state."""

    def __init__(self, on_change):
        self._on_change = on_change
        self.state = {
            "connected": True, "paired": True, "pairing": False, "on": True,
            "app": "com.google.android.tvlauncher",
            "volume": {"level": 12, "max": 100, "muted": False},
        }
        self._voice_bytes = 0

    def _changed(self) -> None:
        self._on_change()

    async def run(self) -> None:
        await asyncio.Event().wait()

    def kick(self) -> None:
        pass

    async def key(self, code: str, direction: str = "SHORT") -> bool:
        log.info("fake key %s %s", code, direction)
        vol = self.state["volume"]
        if code == "POWER":
            self.state["on"] = not self.state["on"]
        elif code == "WAKEUP":
            self.state["on"] = True
        elif code == "HOME":
            self.state["app"] = "com.google.android.tvlauncher"
        elif code == "VOLUME_UP":
            vol["level"] = min(vol["max"], vol["level"] + 2)
        elif code == "VOLUME_DOWN":
            vol["level"] = max(0, vol["level"] - 2)
        elif code == "VOLUME_MUTE":
            vol["muted"] = not vol["muted"]
        self._changed()
        return True

    async def power(self, code: str) -> bool:
        return await self.key(code)

    async def text(self, value: str) -> bool:
        log.info("fake text %r", value)
        return True

    async def launch(self, package: str) -> bool:
        log.info("fake launch %s", package)
        self.state["app"] = package
        self._changed()
        return True

    async def voice_start(self) -> str | None:
        log.info("fake voice start")
        self._voice_bytes = 0
        return None

    async def voice_audio(self, data: bytes) -> bool:
        self._voice_bytes += len(data)
        return True

    async def voice_end(self) -> None:
        log.info("fake voice end, %d bytes (%.2f s)", self._voice_bytes, self._voice_bytes / 16000)

    async def pair_start(self) -> None:
        self.state["pairing"] = True
        self._changed()

    async def pair_finish(self, code: str) -> None:
        await asyncio.sleep(0.5)
        self.state.update(pairing=False, paired=True)
        self._changed()

    def pair_cancel(self) -> None:
        self.state["pairing"] = False
        self._changed()


class Server:
    def __init__(self, args, config: dict):
        # The page reads this shape from /api/apps: tiles to launch, friendly package names, and
        # the https address to move to (the microphone needs a secure page).
        self.apps = {"launch": config.get("apps", []), "names": config.get("names", {}),
                     "https_url": config.get("https_url")}
        self.clients: set[web.WebSocketResponse] = set()
        self._held: dict[str, asyncio.TimerHandle] = {}
        self._voice_ws: web.WebSocketResponse | None = None  # the phone that owns the voice session
        self._voice_timer: asyncio.TimerHandle | None = None
        if args.fake:
            self.tv = FakeShield(self.broadcast_state)
        else:
            self.tv = Shield(args.tv, pathlib.Path(args.data), self.broadcast_state)
        self.samsung = None
        if args.samsung and not args.fake:
            from samsung import SamsungTV

            self.samsung = SamsungTV(args.samsung, pathlib.Path(args.data), self._on_tv_volume, self._notify)

    def _on_tv_volume(self, volume: dict | None) -> None:
        self.tv.state["volume"] = volume
        self.broadcast_state()

    def _notify(self, text: str) -> None:
        msg = json.dumps({"t": "info", "msg": text}, ensure_ascii=False)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, msg))

    def state_msg(self) -> str:
        return json.dumps({"t": "state", **self.tv.state}, ensure_ascii=False)

    def broadcast_state(self) -> None:
        msg = self.state_msg()
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, msg))

    @staticmethod
    async def _send(ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_str(msg)
        except (ConnectionError, RuntimeError):
            pass

    async def _tell(self, ws: web.WebSocketResponse, **msg) -> None:
        await self._send(ws, json.dumps(msg, ensure_ascii=False))

    async def _voice_start(self, ws: web.WebSocketResponse) -> None:
        await self._voice_stop(ws)  # a second start from the same phone replaces its old session
        if self._voice_ws is not None:
            await self._tell(ws, t="error", msg="Голосовой ввод уже занят")
            return
        self._voice_ws = ws  # reserved while the TV answers (up to ~2 s)
        err = await self.tv.voice_start()
        if err:
            self._voice_ws = None
            await self._tell(ws, t="error", msg=err)
            return
        loop = asyncio.get_running_loop()
        self._voice_timer = loop.call_later(
            VOICE_MAX_S, lambda: asyncio.ensure_future(self._voice_stop(ws)))
        await self._tell(ws, t="voice", state="listening")

    async def _voice_stop(self, ws: web.WebSocketResponse) -> None:
        """End the voice session if this phone owns it."""
        if self._voice_ws is not ws:
            return
        self._voice_ws = None
        if self._voice_timer is not None:
            self._voice_timer.cancel()
            self._voice_timer = None
        await self.tv.voice_end()
        await self._tell(ws, t="voice", state="done")

    async def _release(self, code: str) -> None:
        handle = self._held.pop(code, None)
        if handle is not None:
            handle.cancel()
            await self.tv.key(code, "END_LONG")

    async def handle(self, ws: web.WebSocketResponse, msg: dict) -> None:
        kind = msg.get("t")
        if kind == "key" and msg.get("k") in KEYS:
            code = msg["k"]
            if code in VOLUME_KEYS and self.samsung is not None:
                if not await self.samsung.key(code):
                    await ws.send_json({"t": "error", "msg": "Телевизор Samsung не отвечает"})
                return
            if code in ("POWER", "WAKEUP"):
                ok = await self.tv.power(code)
            else:
                ok = await self.tv.key(code)
            if not ok:
                await ws.send_json({"t": "error", "msg": "Нет связи с телевизором"})
        elif kind == "hold" and msg.get("k") in KEYS:
            code = msg["k"]
            if msg.get("d") == "start" and code not in self._held:
                if await self.tv.key(code, "START_LONG"):
                    loop = asyncio.get_running_loop()
                    self._held[code] = loop.call_later(
                        LONG_PRESS_MAX_S, lambda: asyncio.ensure_future(self._release(code)))
            elif msg.get("d") == "end":
                await self._release(code)
        elif kind == "text" and isinstance(msg.get("v"), str):
            await self.tv.text(msg["v"][:500])
        elif kind == "app" and msg.get("id") in {a["id"] for a in self.apps["launch"]}:
            if not await self.tv.launch(msg["id"]):
                await ws.send_json({"t": "error", "msg": "Не получилось открыть приложение"})
        elif kind == "voice" and msg.get("d") in ("start", "end"):
            if msg["d"] == "start":
                await self._voice_start(ws)
            else:
                await self._voice_stop(ws)
        elif kind == "pair_start":
            try:
                await self.tv.pair_start()
            except Exception as exc:  # noqa: BLE001
                log.warning("pairing start failed: %s", exc)
                await ws.send_json({"t": "error", "msg": "Телевизор не отвечает. Он включён?"})
        elif kind == "pair_finish" and isinstance(msg.get("code"), str):
            try:
                await self.tv.pair_finish(msg["code"])
            except Exception as exc:  # noqa: BLE001
                log.warning("pairing finish failed: %s", exc)
                await ws.send_json({"t": "error", "msg": "Код не подошёл, попробуй ещё раз"})
        elif kind == "pair_cancel":
            self.tv.pair_cancel()

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        # Browsers send Origin: without this any web page open on the LAN could press keys.
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            log.warning("refused a WebSocket from %s", origin)
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.clients.add(ws)
        self.tv.kick()
        if self.samsung is not None:
            asyncio.ensure_future(self.samsung.refresh_volume())
        await ws.send_str(self.state_msg())
        try:
            async for raw in ws:
                if raw.type == WSMsgType.BINARY:  # voice audio; only the session's owner counts
                    if ws is self._voice_ws and not await self.tv.voice_audio(raw.data):
                        await self._tell(ws, t="error", msg="Нет связи с телевизором")
                        await self._voice_stop(ws)
                    continue
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(raw.data)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self.handle(ws, msg)
        finally:
            self.clients.discard(ws)
            await self._voice_stop(ws)
            for code in list(self._held):
                await self._release(code)
        return ws

    async def apps_handler(self, request: web.Request) -> web.Response:
        return web.json_response(self.apps)

    async def ping_handler(self, request: web.Request) -> web.Response:
        # Readable cross-origin on purpose: the http page asks the https address before moving
        # there, and a proxy's own refusal (403 without this header) must not count as an answer.
        return web.json_response({"ok": True}, headers={"Access-Control-Allow-Origin": "*"})

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/ws", self.ws_handler)
        app.router.add_get("/api/apps", self.apps_handler)
        app.router.add_get("/api/ping", self.ping_handler)
        app.router.add_static("/", STATIC)

        def tv_done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception():
                log.error("TV connection loop died", exc_info=task.exception())

        async def start_tv(app):
            app["tv_task"] = asyncio.create_task(self.tv.run())
            app["tv_task"].add_done_callback(tv_done)
            if self.samsung is not None:
                # Poll the TV's volume only while someone has the remote open.
                app["samsung_task"] = asyncio.create_task(self.samsung.run(lambda: bool(self.clients)))
                app["samsung_task"].add_done_callback(tv_done)

        async def stop_tv(app):
            app["tv_task"].cancel()
            if self.samsung is not None:
                app["samsung_task"].cancel()
                await self.samsung.close()

        app.on_startup.append(start_tv)
        app.on_cleanup.append(stop_tv)

        @web.middleware
        async def no_cache(request, handler):
            resp = await handler(request)
            # The page is small and served on the LAN; always fresh beats stale after an update.
            resp.headers.setdefault("Cache-Control", "no-cache")
            return resp

        app.middlewares.append(no_cache)
        return app


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default=str(ROOT / "config.json"), help="addresses and app tiles")
    p.add_argument("--tv", help="Shield IP address (overrides the config)")
    p.add_argument("--samsung", help="Samsung TV IP address, for volume (overrides the config)")
    p.add_argument("--bind", default=None, help="default: every interface, IPv4 and IPv6")
    p.add_argument("--port", type=int, help="default: the config's port, else 80")
    p.add_argument("--data", default=str(ROOT / "data"), help="where the pairing certificate lives")
    p.add_argument("--fake", action="store_true", help="no TV: log commands, fake state")
    args = p.parse_args()
    config_path = pathlib.Path(args.config)
    if not config_path.exists() and args.fake:
        config_path = ROOT / "config.example.json"
    if not config_path.exists():
        p.error(f"{config_path} not found: copy config.example.json to config.json and fill it in")
    config = load_config(config_path)
    args.tv = args.tv or config.get("shield")
    args.samsung = args.samsung or config.get("samsung")
    args.port = args.port or config.get("port") or 80
    if not args.tv and not args.fake:
        p.error("no Shield address: set \"shield\" in the config or pass --tv")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def build() -> web.Application:
        # The remote library binds to the running loop, so build inside it.
        return Server(args, config).make_app()

    web.run_app(build(), host=args.bind, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
