# tv-remote

A LAN-only remote control for an NVIDIA SHIELD TV, served as a web page from a Raspberry Pi (or any always-on Linux box). Phones open the page and add it to the home screen, where it runs as a standalone app: no App Store app, no account, and several people can use it at the same time. Optionally it also controls the volume of a Samsung Tizen TV whose speakers you listen through.

Tested with a SHIELD TV Pro (2019) on Android 11 and a 2020 Samsung Tizen TV. The interface text is in Russian (see [Files](#files) for where the strings live).

[Русская версия](README.ru.md)

## Features

- D-pad ring with auto-repeat, or a swipe touchpad mode: swipe for arrows, tap for OK, hold for a long OK (context menus). The chosen mode is remembered on the phone.
- Back, Home, rewind, play/pause, fast-forward.
- Text entry: type on the phone, the text appears in the field that is focused on the TV.
- Voice input: the mic button in the header sends your voice to the SHIELD as a voice search or an Assistant query. Needs the remote to be opened over HTTPS, see [Voice input and HTTPS](#voice-input-and-https).
- Haptic feedback on presses: vibration on Android, the system tick on iPhone (iOS 18 and later).
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
- **Voice** is streamed over the same WebSocket as everything else. The page records the microphone, converts it to 8 kHz 16-bit mono PCM and sends it as binary messages; the server passes it to the SHIELD through a voice session of the remote protocol, in 8 KB chunks (about half a second of audio each). The SHIELD treats it as a voice search or an Assistant query. One phone can use voice at a time. The page stops after 12 s; a session that was forgotten is cut by the server after 15 s.
- **Haptics:** Android phones vibrate through the browser's vibration API. Safari on iPhone has no such API, but iOS plays a system tick when a real tap toggles a `switch` checkbox (iOS 18 and later). Since iOS 26 a script that clicks the switch no longer counts, only a real tap does, so every control carries an invisible label for one hidden switch and the finger toggles it itself. The tick comes when the finger lifts; swipes and long presses may not tick.
- **`tv-remote-mdns.service`** publishes a friendly `.local` name (default `tv.local`) through avahi, so phones do not need the Pi's IP address.

## Requirements

- A Raspberry Pi or any always-on Linux machine with systemd, Python 3.11+ and avahi. `deploy/install.sh` uses `apt-get`, so it targets Raspberry Pi OS, Debian and Ubuntu; on other distributions install `adb` and avahi yourself and adapt the script.
- An NVIDIA SHIELD TV with "Network debugging" enabled in Developer options.
- Optional: a Samsung Tizen TV, 2016 or newer, on the same network.
- A phone. The page was developed for Safari on iPhone; other mobile browsers should be able to open it.
- Optional, for voice input only: an HTTPS address for the remote. That takes a domain name you control (for a real certificate), a reverse proxy such as nginx on the Pi, and a way to override DNS on your home network (the router, or a home resolver such as AdGuard Home or Pi-hole). See [Voice input and HTTPS](#voice-input-and-https).

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

**5. Phone.** Open `http://tv.local` in Safari (if `.local` names do not resolve on your phone or network, use `http://PI_IP`), then Share → Add to Home Screen, and start the remote from the new icon. The first time, it shows a pairing screen: tap the button, a 6-character code appears on the TV, type it in. Pairing is stored on the server, so other phones skip this step. The mic button needs an HTTPS address, see [Voice input and HTTPS](#voice-input-and-https); everything else works over plain `http`.

**6. Samsung volume (optional).** Set `"samsung"` to the TV's IP address in `config.json` and restart:

```sh
sudo systemctl restart tv-remote
```

On the first volume press the TV shows an "Allow" prompt; accept it within about 30 seconds. The token is saved to `data/samsung_token` and reused afterwards.

Tip for privacy-minded owners: the Samsung can stay offline. Give it a manual IP address with no gateway (or one that is unreachable) and turn IPv6 off in its network settings. The local API keeps working.

## Voice input and HTTPS

The mic button in the header streams the phone's microphone to the SHIELD as a voice search or an Assistant query. Tap it to start (the button pulses and the status line says "Listening…"), speak, tap again to stop. It stops by itself after 12 seconds. Only one phone can use voice at a time.

Browsers give the microphone only to a secure page, so voice works only when the remote is opened over HTTPS. Over plain `http` the button shows a hint instead. The server itself stays plain HTTP on the Pi; HTTPS comes from a reverse proxy in front of it. This whole section is optional: without it the remote works as before, just without the mic.

Without HTTPS you can still dictate: use the dictation button of the phone's own keyboard in the text field (the keyboard button in the header). The recognized text is sent to the TV like typed text.

**1. A name.** Pick a name in a domain you own, for example `tv.example.com` (used as a placeholder below). It does not have to point anywhere on the public internet.

**2. A certificate.** Get one from Let's Encrypt with the DNS-01 challenge: certbot with a plugin for your DNS provider, or acme.sh, or lego. DNS-01 proves ownership with a TXT record at your DNS provider, so no port is opened and nothing is reachable from outside. Use a real certificate: phones do not trust a self-signed one, and the reachability check below would fail. Let's Encrypt certificates are logged publicly (Certificate Transparency), so the name itself becomes public; the address it resolves to does not.

**3. Local DNS.** Make `tv.example.com` resolve to the Pi's LAN address (for example `192.168.1.10`) inside your home network only. Add a DNS rewrite or a local DNS record in the router or in a home resolver such as AdGuard Home or Pi-hole. The phones must use that resolver (usually the router hands it out through DHCP).

**4. A reverse proxy.** On the Pi, `sudo apt-get install -y nginx`, then add a server block (for example in `/etc/nginx/conf.d/tv-remote.conf`), fix the names, paths and subnet, and reload nginx:

```nginx
# If you already have "map $http_upgrade $connection_upgrade { ... }" in your nginx
# config, you can use $connection_upgrade below instead of "upgrade".
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name tv.example.com;

    ssl_certificate     /etc/letsencrypt/live/tv.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tv.example.com/privkey.pem;

    # LAN only. Use your own subnet.
    allow 192.168.1.0/24;
    deny all;

    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1d;   # the WebSocket stays open while the remote is
    }
}
```

Notes:

- The server checks the `Origin` of every WebSocket against the `Host` and answers 403 if they differ ("refused a WebSocket" in the log). So the proxy must pass the original `Host`, as above; do not let it replace it with `127.0.0.1`.
- Use your `port` instead of `80` if you changed it. If nginx runs on another machine, use the Pi's address instead of `127.0.0.1`.
- Phones outside the `allow` range (a guest network, a VPN) get a 403 page if they open the https address directly; the automatic move from `http` skips them (see below).

**5. Tell the remote about it.** Set `"https_url": "https://tv.example.com"` in `config.json` and run `sudo systemctl restart tv-remote`.

**6. Phone.** Open `https://tv.example.com`, add it to the home screen again and tap the mic. The first time, the browser asks for microphone access.

With `https_url` set, a page opened over plain `http` first checks, from that phone, that the https address answers (it waits 2.5 s), and then moves there. A phone that cannot reach it stays on `http` and keeps a working remote without voice. Typical reasons: its DNS bypasses your home resolver (private or encrypted DNS), or iCloud Private Relay is on. The check asks the remote's own `/api/ping` (readable cross-origin), so a proxy's refusal such as a 403 from `deny all` does not count, and those phones stay on `http` too.

Things to know after the move:

- A home-screen icon saved from the old `http` address may open the new address with a small browser bar on iPhone. Remove the icon and add it again from the https address.
- The remembered mode (buttons or swipes) is stored per address, so choose it once more.

## Configuration

`config.json` is read once at startup. After editing it, run `sudo systemctl restart tv-remote`. It is listed in `.gitignore`, so your addresses never end up in the repository.

| Key | Default | Meaning |
| --- | --- | --- |
| `shield` | none, required | IP address of the Shield. |
| `samsung` | `null` | IP address of the Samsung TV. `null` means no Samsung: volume keys go to the Shield. |
| `port` | `80` | Port the web page is served on. Port 80 works without root because the service has `CAP_NET_BIND_SERVICE`. |
| `mdns_name` | `tv.local` | Name published through avahi. Read only by `deploy/install.sh`, so re-run the script after changing it. |
| `https_url` | `null` | Address of the HTTPS version of the page, for example `"https://tv.example.com"`. When set, a page opened over plain `http` checks from that phone that this address answers and moves there. `null` means no move. See [Voice input and HTTPS](#voice-input-and-https). |
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
- **The mic button says voice works only at the https address:** the page is open over plain `http`. Set up HTTPS and `https_url` as described above, or dictate with the phone keyboard. If `https_url` is already set, this phone could not reach the https address (its DNS bypasses the home resolver, or iCloud Private Relay is on); open the https address in the browser to see what happens.
- **"No access to the microphone":** the browser's permission was denied. Allow the microphone for the site in the browser's settings (on iPhone, in the Safari settings), then reload the page. "The microphone does not respond" means the browser gave no audio for 2.5 seconds; tap the mic again.
- **Voice says it is busy, or that the TV does not support it or did not answer:** "busy" means another phone is using voice, wait for it to finish. For the other two, check that the SHIELD is awake and connected (status line) and try again.
- **"No connection with the Pi" only at the https address:** the proxy does not pass the WebSocket. If the log (`journalctl -u tv-remote`) says "refused a WebSocket from ...", the proxy is not passing the original `Host`; otherwise check the `Upgrade` and `Connection` headers and the `allow` range.
- **No tick on iPhone:** System Haptics must be on (Settings → Sounds & Haptics). Swipes and long presses may not tick at all.

## Security

- There is no login. The server itself speaks plain HTTP on your LAN, without encryption (an HTTPS proxy in front of it encrypts only the leg between the phone and the proxy). Anyone who can reach the server can control the TV, like the stock remote. The WebSocket refuses a browser page from another site (it compares `Origin` with `Host`), but any program on your network that does not send an `Origin` is let in. If guests use your Wi-Fi, put them on a guest network.
- Never expose it to the internet: no port forwarding, no public reverse proxy. The HTTPS setup above needs none of that: the proxy only accepts the LAN (`allow` / `deny`) and the certificate comes from a DNS challenge.
- Voice: the audio goes only from the phone to the Pi to the SHIELD, inside your LAN. The Pi forwards it and does not record or store it. The microphone is on only while the button is active, and the page lets go of it when you stop, switch to another app or lock the phone. What the SHIELD does with the audio afterwards (a voice search or an Assistant query is handled by the SHIELD and Google) is outside this project.
- Keep the certificate's private key (`privkey.pem`) private, like `data/`.
- `data/` holds the pairing certificate and private key (`cert.pem`, `key.pem`) and the Samsung token (`samsung_token`). Whoever has them can control your TV. They are in `.gitignore`; keep them private.
- Network debugging means any host you approved with "Always allow" can run adb commands on the Shield. You can revoke those approvals in the Shield's Developer options.
- The connection to the Samsung uses TLS without verifying the TV's self-signed certificate.

## Files

- `server.py`: the aiohttp server. Serves the page, the `/ws` WebSocket and `/api/apps`; keeps one supervised connection to the Shield; runs adb for launches and power; owns the voice session; has the `--fake` mode (voice only counts the bytes). Error messages shown on the phone are here.
- `samsung.py`: Samsung volume. Keys over the TV's remote API, level over UPnP.
- `config.example.json`: template for `config.json`.
- `requirements.txt`: Python dependencies.
- `static/`: the web page (`index.html`, `style.css`, `app.js`, `manifest.webmanifest`, icons). The Russian UI text is in `index.html` and `app.js`; the microphone capture, the resampling to 8 kHz, the haptics and the move to `https_url` are in `app.js`.
- `deploy/`: `install.sh` and the systemd unit templates (`*.service.in`).
- `tools_make_icons.py`: redraws the home-screen icons. Needs Pillow; run it from the project folder.
- `data/`: created at runtime; pairing certificate and Samsung token.

## Credits

- [androidtvremote2](https://github.com/tronikos/androidtvremote2) by tronikos (Apache-2.0): the Android TV Remote v2 client.
- [aiohttp](https://github.com/aio-libs/aiohttp) (Apache-2.0): web server and Samsung client.

This project is not affiliated with or endorsed by NVIDIA, Samsung or Google. All product names are trademarks of their respective owners.

## License

[MIT](LICENSE)
