# netgear

An unofficial HTTP client and OpenAPI 3.1 description for the **NETGEAR GS108Tv2**
ProSAFE 8-port smart switch, with notes on the GS110TPv2 that shares its
firmware.

The switch has **no REST or JSON API**. `/base/web_main.html` is a shell over 163
server-rendered HTML pages:

- **Read** = `GET /base/<page>.html` and parse the tables.
- **Write** = `POST` the page's own form. Most forms post back to the same path;
  15 post to a separate handler (`*_rw.html` and friends), which is why the spec
  documents those as their own paths. Every POST is read-modify-write: the
  browser sends every hidden field, checkbox, and select on the page, not just
  the value you changed.
- **Auth** = `POST /base/main_login.html` with `pwd=<password>`. This firmware
  answers **`200` with `Set-Cookie: SID=...`**, not a redirect; a bad password
  re-renders the login page with `err_flag=1`. There is no token, no versioned
  API, and no CORS.

So the practical "API" is a thin, stateful wrapper over that form contract. That
is what `gs108t.py` provides, and `openapi.yaml` describes the same pages as HTTP
paths.

## Files

| File | Purpose |
|---|---|
| `gs108t.py` | client and CLI: session, login, page fetch, form introspection, form apply, authenticated crawl |
| `gen_openapi.py` | turns `endpoints.json` (or `catalog.json`) into `openapi.yaml` and `openapi.json` |
| `build_catalog.py` | parses a captured `tabs_Layer2.js` into `catalog.json` |
| `selftest.py` | offline checks, plus `--live` unauthenticated checks against a device |
| `catalog.json` | 163 pages with tab, section, group, title, and risk |
| `openapi.yaml` / `openapi.json` | OpenAPI 3.1: 179 paths, 300 operations, 136 typed form bodies |
| `endpoints.json` | produced by `discover`; stays local because it records one device's addresses and identity |

Everything uses only the Python standard library, so there is no `requests` and
no PyYAML dependency.

## Quick start

```bash
git clone https://github.com/Netquor/netgear.git
cd netgear

export GS108T_HOST=http://192.0.2.10   # your switch
export GS108T_PASSWORD='...'           # or pass --password / --password-file

python3 gs108t.py login                # verifies credentials, prints model and firmware
python3 gs108t.py summary              # System Information as key/value lines

python3 gs108t.py pages --grep 'vlan|port config'
python3 gs108t.py fields /base/system/port/port_cfg.html
python3 gs108t.py get /base/system/stats/stats_switch.html --out stats.html
```

Change one value without touching the rest of the form:

```bash
# 1. see the exact field names first
python3 gs108t.py fields /base/system/port/port_cfg.html

# 2. preview the exact POST body without sending it
python3 gs108t.py set /base/system/port/port_cfg.html --field 01=2 --dry-run

# 3. set only what you mean; everything else is resubmitted as rendered
python3 gs108t.py set /base/system/port/port_cfg.html --field 01=2

# 4. commit the running configuration to NVRAM
python3 gs108t.py save
```

`set` follows the form's own `action`, so pages that post to a `*_rw.html`
handler work without extra flags. Pages with more than one form (for example,
VLAN membership add and delete) take `--form N`.

## Install

The CLI is a single standard-library script, so a symlink on your `PATH` is
enough. It resolves symlinks to find `catalog.json` next to the real file, so
keep the checkout and link to `gs108t.py` itself:

```bash
chmod +x gs108t.py

# pick a directory that is already on PATH
ln -s "$PWD/gs108t.py" /opt/homebrew/bin/gs108t    # Apple Silicon Homebrew
# ln -s "$PWD/gs108t.py" ~/.local/bin/gs108t       # if ~/.local/bin is on PATH

gs108t --help
```

Then `gs108t summary` works from any directory. Updating the checkout updates the
command; nothing is copied.

## Generate the spec with real field schemas

The catalog already lists every path. Form bodies require one authenticated crawl:

```bash
python3 gs108t.py discover --out endpoints.json --keep-html html/ --verbose
python3 gen_openapi.py --out openapi.yaml
```

`discover` walks all 163 pages at four requests per second (a 0.25 s delay),
records each form's action, method, fields, select options, defaults, and enabled
state, and recovers when the switch drops the session. On a GS108Tv2 that
recovery is needed: the PoE and SNMP pages abort or stall, and the switch forgets
the session when they do. `gen_openapi` then emits:

- `GET` to `text/html` for every page
- `POST` to `application/x-www-form-urlencoded` with typed properties, enums from
  `<select>` options, defaults, and a `required` list of what the browser sends
- a separate `POST` path for the 15 forms whose action is another handler
  (`*_rw.html`, `*_cfg1.html`, `lag_configuration.html`)
- `x-unsupported: true` and `deprecated: true` for menu entries this model does
  not serve

Until the crawl runs, `openapi.yaml` contains every path, but only the login body
is fully typed. The spec says so in `info.description`.

Default values are scrubbed before publication: the capture's IP addresses, MAC,
and long system strings are not copied into the spec, and those fields are marked
`x-echo` so a caller knows to resend what the page rendered.

Checks that need no credentials:

```bash
python3 selftest.py --live
```

Rebuild the catalog from your own device's menu script:

```bash
mkdir -p _capture
curl -o _capture/menu.js http://192.0.2.10/base/js/tabs_Layer2.js
python3 build_catalog.py
```

## Coverage

163 menu pages across eight tabs. Risk is classified from the path and title:

| Risk | Count | Meaning |
|---|---|---|
| read | 47 | tables, statistics, logs, status |
| write | 97 | configuration forms |
| action | 13 | ping, traceroute, cable test, log clear, registration |
| destructive | 6 | reboot, factory default, TFTP or HTTP upload, dual image, password |

| Tab | Pages |
|---|---|
| System | 34 |
| Switching | 46 |
| Routing | 10 |
| QoS | 11 |
| Security | 36 |
| Monitoring | 11 |
| Maintenance | 12 |
| Help | 3 |

On the crawled GS108Tv2 (software 5.4.2.30), 139 pages serve real content and 24
are listed in the menu but not usable:

- 20 return `404`: the Routing tab, MRP, and 802.1AS pages, which belong to the
  GS110TPv2 build
- 3 PoE pages return a 1–2 byte body and drop the session
- 1 PoE timer page stalls mid-chunked-response; after that crawl the captured
  unit reported six minutes of uptime instead of nineteen days, so treat that
  request as hazardous and do not retry it in a loop

The spec keeps every path so a client can discover support at runtime rather than
guessing from the menu.

Destructive endpoints are marked `x-risk: destructive` in the spec. The CLI
refuses `reboot` without `--yes`. There is deliberately no factory-reset command:
the endpoint is documented, not wired.

## Verified and derived

Verified against a live GS108Tv2 running software 5.4.2.30:

- `GET /` returns the login page (`iso-8859-1`, `Server: Web Server`,
  `Cache-Control: no-cache`). The form posts to `/base/main_login.html` with
  `pwd`, `err_flag`, `err_msg`, and an image button named `login`.
- A correct password returns **`200`, not a redirect**, with
  `Set-Cookie: SID=<token>;PATH=/;HttpOnly`. A wrong password re-renders the
  login page with `err_flag=1` and `err_msg=Login failure`. Check the cookie,
  not the status code.
- The login handler wants the request a browser sends, including
  `Referer: <base>/`; without it the POST can be rejected.
- `/base/web_main.html` answers `200` plus the authenticated shell once a
  session exists, and `302` to `/` when it does not.
- An unauthenticated `GET` of any content page is a `302` to `/`.
- `tabs_Layer2.js`, `help.html`, and `style.css` are served without
  authentication. The 163-page map comes from the menu script.
- The device clock reports January 1970 in `Date`, so its timestamps are
  unreliable.
- Every page was fetched with a session (`discover`); the per-page form bodies
  in `endpoints.json` are real captures, not guesses.

Not established:

- the session lifetime, and whether the switch allows more than one admin
  session at a time
- behavior after a rejected form POST beyond "the page re-renders with an error
  banner"

## Quirks

- Every response is HTML; the device serves no JSON.
- Writes are per page and not atomic. A rejected POST re-renders the page with an
  error banner and keeps your values; it does not roll back.
- Settings live in the running configuration until `save_changes.html` is posted.
- Many pages carry hidden fields whose values are the device's current state
  (IP, MAC, port table dumps). They must be echoed back unchanged; the spec marks
  them `x-echo`.
- The UI uses latin-1, not UTF-8.
- Exports and print views go through `file_download.html` and
  `http_file_download.html`.
- The device map is a signed Java applet (`MapApplet`, `NETGEARC.RSA`), not HTML.
- Some pages are unsafe to request at all. `base/poe/timer_glob_cfg.html` stalls
  on a GS108Tv2, and the switch's uptime reset after the crawl that touched it.
  `discover` records it as a failure and moves on; it does not keep retrying.
- SNMP is the other management surface, and it is often not exposed.

## Firmware

`GS108Tv2_GS110TPv2_V5.4.2.36.stk` is not a flat image. It starts with a 172-byte
NETGEAR header (`f2 13 aa 55`), and at offset `0xac` a 7-Zip archive begins. That
archive contains a single entry, `switchdrvr.bin`: a 16 MB statically linked,
stripped 32-bit MIPS ELF. The HTML pages are embedded in that binary in a
compressed filesystem image, so the live crawl is still the practical way to get
the forms.

The separately published `GS108Tv1` firmware (`3.0.4.10`) is a different,
smaller UI and does not match a v2 switch.

## Safety

This targets one device, sequentially, at four requests per second. It contains no
credential guessing, no host lists, and no parallel fan-out. An admin password
combined with a factory-reset endpoint does not belong on a network range.

If your switch is reachable from the internet, put it behind a VPN or an access
list, use HTTPS, and change the default password.

Not affiliated with or endorsed by NETGEAR.

## License

MIT. See `LICENSE`.
