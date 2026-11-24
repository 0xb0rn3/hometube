# HomeTube

YouTube for the TV, served by a small board on your home network.

HomeTube is one Python program. It serves a 10-foot page you open in the TV's own browser, looks
videos up, and relays the streams, so the TV only ever talks to the board. Subscriptions, history and
Watch Later are kept on the board. It runs well on a 4-core ARM board with 4 GB of RAM (developed on a
KickPi K2B, Allwinner H618, Armbian on Debian 13). There are no Shorts anywhere.

## What it does

- Home rows you choose, subscriptions, a channel's latest videos, search, History, Watch Later,
  Continue watching (resumes where you stopped)
- Player: previous, back 10 s, play/pause, forward 10 s, next, full screen or windowed, quality (it
  starts at the highest), speed, subtitles, chapters
- Queue: the row, search or list you opened a video from plays in order, shown as "Row · 5 of 16".
  Mix turns the current video into YouTube's mix (about 25 videos). Up next shows YouTube's
  suggestions. When a video ends the next one starts after 5 seconds (Back cancels)
- Remote control: arrows, OK and Back, plus the media keys (play, pause, fast forward, rewind,
  next/previous track) and channel up/down for next/previous video
- Optional YouTube account (your subscriptions and recommendations from YouTube itself)

## Install

On a Debian, Ubuntu, Armbian or Raspberry Pi OS board with systemd:

```sh
git clone https://github.com/0xb0rn3/hometube
cd hometube
sudo ./install.sh
```

Then open `http://<board address>:8890/` in the TV's browser and keep it as a bookmark. The installer
puts the program in `/opt/hometube`, yt-dlp in its own environment there, and the latest deno (checked
against its published SHA-256). It adds a service and a daily yt-dlp update (YouTube changes often).
Running it again updates the program and keeps your settings and everything saved.

## Through a VPN

HomeTube's own traffic can be pinned to a VPN tunnel's routing table, so YouTube sees the tunnel's
address, not your home's:

```sh
sudo ./install.sh --route-table 200 --route-dev wg0 --after wg-quick@wg0.service
```

- `--route-table` adds a rule for HomeTube's account only (`ip rule ... uidrange <uid> lookup 200`).
  Give that table an unreachable fallback route so it fails closed when the tunnel is down.
- `--route-dev` makes HomeTube ask the kernel, before every request to YouTube, which way its traffic
  would leave. If it is not `wg0` (the rule was removed, the account changed, the tunnel is gone),
  HomeTube sends nothing and the TV shows why.
- With a routing table the service gets no IPv6 sockets at all, so an IPv6 route cannot take it
  around the (IPv4) rule.

Why this matters with a relay: the TV plays whatever the board hands it, so a board going out the
wrong way would still play. The route check is what turns that into a visible stop.

To see the state, on the board:

```sh
curl -s http://127.0.0.1:8890/api/tv/status
# {"route_dev": "wg0", "route_ok": true, "route_problem": null, "youtube_saw": "<address>", ...}
```

`youtube_saw` is the address YouTube put in the last stream it handed out: the address it saw.

## Settings

`/etc/hometube/config.json`; every key is optional and checked when the service starts.

| key | default | meaning |
|---|---|---|
| `bind` | `0.0.0.0` (installer), `127.0.0.1` (program) | address the page and API listen on |
| `port` | `8890` (installer), `8891` (program) | port |
| `route_table` | none | routing table for HomeTube's traffic |
| `route_pref` | `91` | priority of that rule |
| `route_dev` | none | interface the traffic must leave by (fails closed) |
| `after_unit` | none | start after this unit |
| `label` | none | shown after "HomeTube" in the page title |
| `rows` | a few general rows | home rows: `[["Title", "search words"], ...]` |

After changing it: `sudo systemctl restart hometube`.

## YouTube account (optional)

Export `cookies.txt` from youtube.com while signed in (a private window, then close it without
signing out), copy it to the board and run:

```sh
sudo python3 /opt/hometube/hometube.py cookies cookies.txt     # --clear to disconnect
```

Only YouTube and Google cookies are kept, readable by HomeTube's account alone. The page on the
network cannot set or read them.

## How it works

- A lookup runs yt-dlp with deno, which works through YouTube's JavaScript check. On the H618 that
  takes 15 to 18 seconds, most of it the check itself. To hide it, a card you stay on for about a
  second is looked up in the background, at a lower priority, so the video is ready when you press OK.
- YouTube's stream servers do not let a page from another address read their streams, so the board
  relays them: the TV gets a playlist with only H.264 variants (what every TV browser decodes), and
  every address in it is a signed link back to the board. The relay fetches only what it signed and
  only from YouTube's stream hosts.
- Connections to YouTube are kept open and the next two segments are fetched while the TV plays the
  current one. Measured on the H618 through a VPN exit 267 ms away: segments after the first arrive in
  0.12 to 0.22 s, about three times faster than they play at 1080p.

## Security notes

- The page has no sign-in (it is for a TV). Each address on the network gets a limited number of
  requests a minute, changes are only accepted from the page's own origin, and the account cookies
  and the status can only be reached from the board itself.
- The service runs as its own unprivileged account with a read-only system (`ProtectSystem=strict`),
  no access to home folders, and a 768 MB memory cap.
- Plain HTTP on your home network: keep the port off your router's port forwarding.

## With SBC Control Center

[SBC Control Center](https://github.com/0xb0rn3/sbc-control-center) offers HomeTube during its install.
It then runs HomeTube on `127.0.0.1:8891`, pinned to the board's VPN tunnel, and serves it at `/tv`
on its own portal, which also connects the YouTube account for a signed-in administrator.

## Tests

```sh
python3 -m unittest discover -s tests
```

They need no network.

## License

MIT, see [LICENSE](LICENSE). The bundled hls.js 1.7.4 is Apache-2.0 ([web/hls.LICENSE](web/hls.LICENSE)).

HomeTube is a Stingray Labs project.
