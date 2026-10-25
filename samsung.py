"""Samsung TV volume for the remote server.

The TV's speakers can't be driven over HDMI-CEC, so volume keys go to the TV
itself over its local (Tizen) remote WebSocket and the level is read back over
UPnP. The first connection makes the TV ask for permission on screen; the
token it hands out is kept in the data folder and reused afterwards.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import pathlib
import re
from collections.abc import Callable
from urllib.parse import quote

import aiohttp
from aiohttp import WSMsgType

log = logging.getLogger("tv-remote")

REMOTE_NAME = "Pi TV Remote"
REMOTE_PATH = "/api/v2/channels/samsung.remote.control"
UPNP_PATH = "/upnp/control/RenderingControl1"
UPNP_SERVICE = "urn:schemas-upnp-org:service:RenderingControl:1"

CONNECT_TIMEOUT_S = 6.0       # TCP + TLS + WebSocket upgrade
FIRST_MSG_TIMEOUT_S = 35.0    # the person has about 30 s to press "Allow"
RETRY_COOLDOWN_S = 3.0        # after a failed connect, fail fast instead of re-prompting
REFRESH_DELAY_S = 0.4         # let the TV apply a key before reading the level back
POLL_S = 4.0
UPNP_TIMEOUT_S = 1.5

_UNSET = object()  # nothing reported yet, so the first reading always goes out

MSG_ALLOW = "На телевизоре появится запрос — нажми «Разрешить»"
MSG_DENIED = "Телевизор не разрешил управление"

SOAP = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    f'<s:Body><u:{{action}} xmlns:u="{UPNP_SERVICE}">'
    "<InstanceID>0</InstanceID><Channel>Master</Channel>"
    "</u:{action}></s:Body></s:Envelope>"
)


def _why(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class SamsungTV:
    """Sends volume keys to a Samsung TV and reports its volume and mute state."""

    KEYS = {"VOLUME_UP": "KEY_VOLUP", "VOLUME_DOWN": "KEY_VOLDOWN", "VOLUME_MUTE": "KEY_MUTE"}

    def __init__(
        self,
        host: str,
        data_dir: pathlib.Path,
        on_volume: Callable[[dict | None], None],
        notify: Callable[[str], None],
        *,
        remote_url: str | None = None,
        upnp_url: str | None = None,
    ) -> None:
        self._remote_url = remote_url or f"wss://{host}:8002{REMOTE_PATH}"
        self._upnp_url = upnp_url or f"http://{host}:9197{UPNP_PATH}"
        self._token_file = pathlib.Path(data_dir) / "samsung_token"
        self._on_volume = on_volume
        self._notify = notify
        self._token = self._load_token()
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()       # connect + send
        self._vol_lock = asyncio.Lock()   # one UPnP read at a time, so results arrive in order
        self._failed_at = float("-inf")
        self._refresh_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()
        self._reported: object = _UNSET

    def _load_token(self) -> str:
        try:
            return self._token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _save_token(self, token: object) -> None:
        if not isinstance(token, str) or not token.strip() or token.strip() == self._token:
            return
        self._token = token.strip()
        try:
            self._token_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._token_file.with_name(self._token_file.name + ".tmp")
            tmp.write_text(self._token, encoding="utf-8")
            tmp.replace(self._token_file)
        except OSError as exc:
            log.warning("Samsung: can't save the token: %s", exc)

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # The TV drops idle connections; a fresh one per request avoids stale-socket errors.
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True))
        return self._session

    # --- remote channel (keys) ---------------------------------------------

    def _alive(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def _await_connect(self, ws: aiohttp.ClientWebSocketResponse) -> str:
        """Wait for the TV's verdict: "ok", "denied", "timeout" or "closed"."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIRST_MSG_TIMEOUT_S
        while (left := deadline - loop.time()) > 0:
            try:
                msg = await ws.receive(timeout=left)
            except asyncio.TimeoutError:
                break
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                return "closed"
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            event = data.get("event") if isinstance(data, dict) else None
            if event == "ms.channel.connect":
                payload = data.get("data")
                self._save_token(payload.get("token") if isinstance(payload, dict) else None)
                return "ok"
            if event in ("ms.channel.unauthorized", "ms.channel.timeout"):
                return "denied"
        return "timeout"

    async def _connect(self) -> bool:
        """Open the remote channel. The caller holds the lock."""
        loop = asyncio.get_running_loop()
        if loop.time() - self._failed_at < RETRY_COOLDOWN_S:
            return False
        name = base64.b64encode(REMOTE_NAME.encode()).decode()
        url = f"{self._remote_url}?name={name}"
        asked = not self._token
        if asked:
            self._notify(MSG_ALLOW)
        else:
            url += f"&token={quote(self._token, safe='')}"
        ws = None
        verdict = "closed"
        try:
            ws = await asyncio.wait_for(
                self._http().ws_connect(url, ssl=False, heartbeat=20), CONNECT_TIMEOUT_S)
            verdict = await self._await_connect(ws)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            log.info("Samsung: can't reach the TV (%s)", _why(exc))
        finally:
            if ws is not None and verdict != "ok":
                with contextlib.suppress(Exception):
                    await ws.close()
        if verdict == "ok":
            self._ws = ws
            self._reader = asyncio.create_task(self._drain(ws))
            self._failed_at = float("-inf")
            log.info("Samsung: remote channel open")
            return True
        self._failed_at = loop.time()
        # Silence while waiting for "Allow" means nobody pressed it.
        if verdict == "denied" or (verdict == "timeout" and asked):
            log.warning("Samsung: the TV refused the connection (%s)", verdict)
            self._notify(MSG_DENIED)
        return False

    async def _drain(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Read and discard the TV's chatter so the socket stays healthy; forget it when it closes."""
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        except Exception:  # noqa: BLE001 - any failure just means the socket is gone
            pass
        finally:
            if self._ws is ws:
                self._ws = None
            with contextlib.suppress(Exception):
                await ws.close()

    async def _drop(self) -> None:
        ws, self._ws = self._ws, None
        reader, self._reader = self._reader, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2)
        if reader is not None and not reader.done():
            reader.cancel()

    async def _send_key(self, tv_key: str) -> bool:
        payload = {
            "method": "ms.remote.control",
            "params": {"Cmd": "Click", "DataOfCmd": tv_key, "Option": "false", "TypeOfRemote": "SendRemoteKey"},
        }
        for _ in range(2):  # a socket the TV closed quietly: reconnect once and resend
            if not self._alive() and not await self._connect():
                return False
            try:
                await self._ws.send_json(payload)
                return True
            except (aiohttp.ClientError, OSError, RuntimeError, asyncio.TimeoutError) as exc:
                log.info("Samsung: send failed (%s), reconnecting", _why(exc))
                await self._drop()
        return False

    async def key(self, code: str) -> bool:
        """Press a volume key. False if the TV is unreachable or refused us."""
        tv_key = self.KEYS.get(code)
        if tv_key is None:
            return False
        async with self._lock:
            ok = await self._send_key(tv_key)
        if ok:
            self._schedule_refresh()
        return ok

    # --- UPnP (level) ------------------------------------------------------

    async def _upnp(self, action: str, tag: str) -> str:
        headers = {"SOAPAction": f'"{UPNP_SERVICE}#{action}"', "Content-Type": 'text/xml; charset=utf-8'}
        async with self._http().post(
            self._upnp_url, data=SOAP.format(action=action).encode(), headers=headers,
            timeout=aiohttp.ClientTimeout(total=UPNP_TIMEOUT_S),
        ) as resp:
            resp.raise_for_status()
            body = (await resp.read()).decode("utf-8", errors="replace")
        found = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", body)
        if found is None:
            raise ValueError(f"no {tag} in the reply")
        return found.group(1)

    async def _read_volume(self) -> dict:
        level = int(await self._upnp("GetVolume", "CurrentVolume"))
        muted = (await self._upnp("GetMute", "CurrentMute")).lower() in ("1", "true", "yes")
        return {"level": max(0, min(100, level)), "max": 100, "muted": muted}

    async def refresh_volume(self) -> None:
        """Read level and mute from the TV and report them if they changed (None if unreadable)."""
        async with self._vol_lock:
            state = None
            for _ in range(2):  # one quick retry so a hiccup doesn't blank the UI
                try:
                    state = await self._read_volume()
                    break
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as exc:
                    log.debug("Samsung: volume read failed: %s", _why(exc))
            if self._reported is _UNSET or state != self._reported:
                self._reported = state
                self._on_volume(dict(state) if state else None)

    def _schedule_refresh(self) -> None:
        """One refresh shortly after the last key of a burst."""
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
        self._refresh_timer = asyncio.get_running_loop().call_later(REFRESH_DELAY_S, self._start_refresh)

    def _start_refresh(self) -> None:
        self._refresh_timer = None
        task = asyncio.create_task(self.refresh_volume())
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Samsung volume refresh failed", exc_info=task.exception())

    async def run(self, should_poll: Callable[[], bool]) -> None:
        """Poll the volume every few seconds while should_poll() says someone is looking."""
        while True:
            await asyncio.sleep(POLL_S)
            try:
                if should_poll():
                    await self.refresh_volume()
            except Exception:  # noqa: BLE001 - keep polling whatever a callback does
                log.exception("Samsung poll failed")

    async def close(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None
        for task in list(self._tasks):
            task.cancel()
        await self._drop()
        if self._session is not None:
            await self._session.close()
            self._session = None

