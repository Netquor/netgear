# netgear

An unofficial HTTP client and OpenAPI 3.1 description for the **NETGEAR GS108Tv2**
ProSAFE 8-port smart switch.

The switch has **no REST or JSON API**. `/base/web_main.html` is a frameset over
163 server-rendered HTML pages:

- **Read** = `GET /base/<page>.html` and parse the tables.
- **Write** = `POST` the page's own form back to the same path. Every POST is
  read-modify-write: the browser sends every hidden field, checkbox, and select
  on the page, not just the value you changed.
- **Auth** = `POST /base/main_login.html` with `pwd=<password>`. Success is a
  `302` to `/base/web_main.html`; failure re-renders the login page with
  `err_flag=1` and an error message. The session is a cookie. There is no token,
  no versioned API, and no CORS.

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
| `openapi.yaml` / `openapi.json` | OpenAPI 3.1, 164 paths (163 pages plus login) |
| `endpoints.json` | produced by `discover`; real forms and field schemas |

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

# 2. set only what you mean; everything else is resubmitted as rendered
python3 gs108t.py set /base/system/port/port_cfg.html --field 01=2

# 3. commit the running configuration to NVRAM
python3 gs108t.py save
```

Pages with more than one form (for example, VLAN membership add and delete) take
`--form N`.

## Generate the spec with real field schemas

The catalog already lists every path. Form bodies require one authenticated crawl:

```bash
python3 gs108t.py discover --out endpoints.json --keep-html html/ --verbose
python3 gen_openapi.py --out openapi.yaml
```

`discover` walks all 163 pages at five requests per second (a 0.2 s delay),
records each form's action, method, fields, select options, defaults, and enabled
state, and aborts if the session is lost. `gen_openapi` then emits, for each page:

- `GET` to `text/html`
- `POST` to `application/x-www-form-urlencoded` with typed properties, enums from
  `<select>` options, defaults, and a `required` list of what the browser sends.

Until the crawl runs, `openapi.yaml` contains all 164 paths, but only the login
body is fully typed. The spec says so in `info.description`.

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

163 pages across eight tabs. Risk is classified from the path and title:

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

Destructive endpoints are marked `x-risk: destructive` in the spec. The CLI
refuses `reboot` without `--yes`. There is deliberately no factory-reset command:
the endpoint is documented, not wired.

## Verified and derived

Verified against a live device without authentication:

- `GET /` returns the login page (`iso-8859-1`, `Server: Web Server`,
  `Cache-Control: no-cache`). The form posts to `/base/main_login.html` with
  `pwd`, `err_flag`, `err_msg`, and an image button named `login`.
- `/base/web_main.html` answers `302` to `/` without a session.
- `tabs_Layer2.js`, `help.html`, and `style.css` are served without
  authentication. The 163-page map comes from the menu script.
- The device clock reports January 1970 in `Date`, so its timestamps are
  unreliable.

Derived, not verified:

- the exact success redirect from `main_login.html`
- the session cookie name and lifetime
- per-page form bodies, which appear after `discover` runs

## Quirks

- Every response is HTML; the device serves no JSON.
- Writes are per page and not atomic. A rejected POST re-renders the page with an
  error banner and keeps your values; it does not roll back.
- Settings live in the running configuration until `save_changes.html` is posted.
- The UI uses latin-1, not UTF-8.
- Exports and print views go through `file_download.html` and
  `http_file_download.html`.
- SNMP is the other management surface, and it is often not exposed.

## Safety

This targets one device, sequentially, at five requests per second. It contains no
credential guessing, no host lists, and no parallel fan-out. An admin password
combined with a factory-reset endpoint does not belong on a network range.

If your switch is reachable from the internet, put it behind a VPN or an access
list, use HTTPS, and change the default password.

Not affiliated with or endorsed by NETGEAR.

## License

MIT. See `LICENSE`.
