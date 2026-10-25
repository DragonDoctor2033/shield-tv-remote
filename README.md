# tv-remote

A LAN-only remote control for an NVIDIA SHIELD TV, served as a web page from a Raspberry Pi (or any always-on Linux box). Phones open the page and add it to the home screen, where it runs as a standalone app: no App Store app, no account, and several people can use it at the same time. Optionally it also controls the volume of a Samsung Tizen TV whose speakers you listen through.

Tested with a SHIELD TV Pro (2019) on Android 11 and a 2020 Samsung Tizen TV. The interface text is in Russian (see [Files](#files) for where the strings live).

[Русская версия](README.ru.md)

## Features

- D-pad ring with auto-repeat, or a swipe touchpad mode: swipe for arrows, tap for OK, hold for a long OK (context menus). The chosen mode is remembered on the phone.
- Back, Home, rewind, play/pause, fast-forward.
- Text entry: type on the phone, the text appears in the field that is focused on the TV.
- Power: wake or put the Shield to sleep, including while it is asleep.
- App tiles: launch any installed app by its package name (you define the tiles in `config.json`).
- Status line: connected, sleeping, or which app is in the foreground.
- Volume down, mute, up, with a level readout (Samsung only).
- Pair once for the whole server, not once per phone. The server reconnects on its own when the Shield or a phone drops off.

## How it works

- **Keys, swipes, long-press OK and text** go to the Shield over the Android TV Remote v2 protocol, the same one the Google TV remote app uses, through the [`androidtvremote2`](https://github.com/tronikos/androidtvremote2) library. It connects to a fixed IP address, so there is no discovery that could lose the TV. Pairing is a one-time step: the page shows a pairing screen and the TV shows a 6-character code.
- **App launches and power** go over adb (`cmd activity start-activity`; `input keyevent KEYCODE_WAKEUP` or `KEYCODE_SLEEP` after reading `mWakefulness`). Reasons, found on a real Shield:
  - the remote protocol can only open app links, the Shield has no handler for `market://launch`, and it drops the connection after a failed link;
  - power keys sent over the remote protocol did not reach a sleeping Shield, while adb still answers during sleep.
- **Why not adb for everything:** `adb shell input keyevent` takes about 1.3 s per key on Android 11, far too slow for navigation. Regular keys therefore use the remote protocol.
- **Volume:** many TVs, this Samsung included, ignore HDMI-CEC volume commands from a source device, and the Shield's own HDMI output is fixed at full volume. So when a Samsung is configured, the volume keys go straight to it over its local remote API (`wss://TV_IP:8002`; the TV asks for a one-time "Allow" and the token is stored in `data/samsung_token`), and the current level is read back over UPnP RenderingControl (port 9197). Without a Samsung configured, volume keys go to the Shield.
- **`tv-remote-mdns.service`** publishes a friendly `.local` name (default `tv.local`) through avahi, so phones do not need the Pi's IP address.

## Requirements

- A Raspberry Pi or any always-on Linux machine with systemd, Python 3.11+ and avahi. `deploy/install.sh` uses `apt-get`, so it targets Raspberry Pi OS, Debian and Ubuntu; on other distributions install `adb` and avahi yourself and adapt the script.
- An NVIDIA SHIELD TV with "Network debugging" enabled in Developer options.
- Optional: a Samsung Tizen TV, 2016 or newer, on the same network.
- A phone. The page was developed for Safari on iPhone; other mobile browsers should be able to open it.

Give the Shield, the Samsung and the Pi fixed addresses (a DHCP reservation on the router is enough). The remote connects to these addresses directly.

## Setup

**1. Shield.** Enable Developer options (Settings → Device Preferences → About, press "Build" several times), then turn on Developer options → Network debugging.

**2. Server.** On the Pi:

```sh
sudo apt-get update
sudo apt-get install -y git python3-venv adb
git clone https://github.com/DragonDoctor2033/shield-tv-remote.git tv-remote
cd tv-remote
cp config.example.json config.json
```

Edit `config.json`: set `"shield"` to the Shield's IP address, and adjust the app tiles. See [Configuration](#configuration).

**3. Authorize adb once.** Do this as the same user that will run `install.sh`, because the service runs as that user and adb keeps its key in that user's home folder:

```sh
adb connect SHIELD_IP:5555
```

The TV shows "Allow USB debugging?". Tick "Always allow from this computer" and confirm. Running the command again should print `already connected`, and `adb devices` should list the Shield as `device`, not `unauthorized`.

**4. Install the services:**

```sh
deploy/install.sh
```

The script (uses `sudo`; safe to run again) installs avahi (`avahi-daemon`, `avahi-utils`) and `adb` if missing, creates `.venv`, installs `requirements.txt`, writes `tv-remote.service` and `tv-remote-mdns.service` to `/etc/systemd/system/` with your user, folder and mDNS name filled in, then enables and restarts both.

**5. Phone.** Open `http://tv.local` in Safari (if `.local` names do not resolve on your phone or network, use `http://PI_IP`), then Share → Add to Home Screen, and start the remote from the new icon. The first time, it shows a pairing screen: tap the button, a 6-character code appears on the TV, type it in. Pairing is stored on the server, so other phones skip this step.

**6. Samsung volume (optional).** Set `"samsung"` to the TV's IP address in `config.json` and restart:

```sh
sudo systemctl restart tv-remote
```

On the first volume press the TV shows an "Allow" prompt; accept it within about 30 seconds. The token is saved to `data/samsung_token` and reused afterwards.

Tip for privacy-minded owners: the Samsung can stay offline. Give it a manual IP address with no gateway (or one that is unreachable) and turn IPv6 off in its network settings. The local API keeps working.

## Configuration

`config.json` is read once at startup. After editing it, run `sudo systemctl restart tv-remote`. It is listed in `.gitignore`, so your addresses never end up in the repository.

| Key | Default | Meaning |
| --- | --- | --- |
| `shield` | none, required | IP address of the Shield. |
| `samsung` | `null` | IP address of the Samsung TV. `null` means no Samsung: volume keys go to the Shield. |
| `port` | `80` | Port the web page is served on. Port 80 works without root because the service has `CAP_NET_BIND_SERVICE`. |
| `mdns_name` | `tv.local` | Name published through avahi. Read only by `deploy/install.sh`, so re-run the script after changing it. |
| `apps` | see example | List of launch tiles, in display order. Each entry has `id` (Android package name), `name` (label on the tile) and `color` (any CSS colour). |
| `names` | see example | Map from package name to a friendly name shown in the status line when that app is in the foreground. Apps listed in `apps` already use their tile name. Unknown packages are shown by package name. |

To find a package name, run `adb -s SHIELD_IP:5555 shell pm list packages -3` and look for the app.

### Command-line flags

Flags override the config. The service starts `server.py` without flags, so the config is what applies there.

| Flag | Meaning |
| --- | --- |
| `--config PATH` | Config file. Default: `config.json` next to `server.py`. |
| `--tv IP` | Shield address (overrides `shield`). |
| `--samsung IP` | Samsung address (overrides `samsung`). |
| `--port N` | Port (overrides `port`; 80 if neither is set). |
| `--bind ADDR` | Address to listen on. Default: all interfaces, IPv4 and IPv6. |
| `--data DIR` | Where the pairing certificate and Samsung token are kept. Default: `data/` next to `server.py`. |
| `--fake` | No TV: commands are only logged and the state is simulated. Nothing is contacted (a Samsung address is ignored too), and `config.example.json` is used if there is no `config.json`. |

`--fake` is handy for working on the page without a TV, on any machine:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py --fake --port 8765
```

Then open `http://localhost:8765`. On Windows the interpreter is `.venv\Scripts\python`.

## Updating

```sh
git pull && deploy/install.sh
```

`config.json` and `data/` are not tracked by git, so an update leaves them alone.

To follow the logs: `journalctl -u tv-remote -f`.

## Troubleshooting

- **"No connection with the Pi" on the phone:** the phone is not on the same network, or the `.local` name does not resolve. Try the Pi's IP address.
- **The status line says the TV is not responding:** check the Shield's IP in `config.json`, that Network debugging is on, and the logs.
- **App tiles or power do nothing:** adb is probably not authorized for the service's user. Run `adb devices` as that user; `unauthorized` means the prompt on the TV was not accepted.
- **Pairing screen appears again:** the Shield forgot the certificate in `data/`. Pair again from the page.

## Security

- There is no login and no encryption: the page and its WebSocket are plain HTTP on your LAN. Anyone who can reach the server can control the TV, like the stock remote. A web page opened in a browser on your network can reach it too. If guests use your Wi-Fi, put them on a guest network.
- Never expose it to the internet: no port forwarding, no public reverse proxy.
- `data/` holds the pairing certificate and private key (`cert.pem`, `key.pem`) and the Samsung token (`samsung_token`). Whoever has them can control your TV. They are in `.gitignore`; keep them private.
- Network debugging means any host you approved with "Always allow" can run adb commands on the Shield. You can revoke those approvals in the Shield's Developer options.
- The connection to the Samsung uses TLS without verifying the TV's self-signed certificate.

## Files

- `server.py`: the aiohttp server. Serves the page, the `/ws` WebSocket and `/api/apps`; keeps one supervised connection to the Shield; runs adb for launches and power; has the `--fake` mode. Error messages shown on the phone are here.
- `samsung.py`: Samsung volume. Keys over the TV's remote API, level over UPnP.
- `config.example.json`: template for `config.json`.
- `requirements.txt`: Python dependencies.
- `static/`: the web page (`index.html`, `style.css`, `app.js`, `manifest.webmanifest`, icons). The Russian UI text is in `index.html` and `app.js`.
- `deploy/`: `install.sh` and the systemd unit templates (`*.service.in`).
- `tools_make_icons.py`: redraws the home-screen icons. Needs Pillow; run it from the project folder.
- `data/`: created at runtime; pairing certificate and Samsung token.

## Credits

- [androidtvremote2](https://github.com/tronikos/androidtvremote2) by tronikos (Apache-2.0): the Android TV Remote v2 client.
- [aiohttp](https://github.com/aio-libs/aiohttp) (Apache-2.0): web server and Samsung client.

This project is not affiliated with or endorsed by NVIDIA, Samsung or Google. All product names are trademarks of their respective owners.

## License

[MIT](LICENSE)
