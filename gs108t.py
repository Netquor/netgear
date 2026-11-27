#!/usr/bin/env python3
"""Client and CLI for the NETGEAR GS108Tv2 ProSAFE web UI.

The switch has no JSON API. Its UI is a set of server-rendered HTML pages where
every change is a form POST back to the same page, guarded by a session cookie
set by POST /base/main_login.html. This module wraps that contract:

  * one HTTP session (cookies, redirects, timeouts) with no third-party deps
  * login and session checks
  * page fetch, form introspection, and form apply (read-modify-write)
  * authenticated crawl that turns every page's forms into endpoints.json,
    which gen_openapi.py turns into openapi.yaml

Nothing here guesses credentials. The password comes from --password, the
GS108T_PASSWORD environment variable, or an interactive prompt.

Usage:
  export GS108T_HOST=http://192.0.2.10
  python3 gs108t.py login
  python3 gs108t.py summary
  python3 gs108t.py pages --grep vlan
  python3 gs108t.py fields /base/system/port/port_cfg.html
  python3 gs108t.py set /base/system/port/port_cfg.html --field 01=2
  python3 gs108t.py discover --out endpoints.json --keep-html html/
  python3 gs108t.py save
"""
from __future__ import annotations

import argparse
import difflib
import getpass
import html as htmlmod
import http.client
import json
import os
import re
import ssl
import sys
import time
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

HERE = os.path.dirname(os.path.realpath(__file__))
# Documentation address (RFC 5737). Point GS108T_HOST at your own switch.
DEFAULT_HOST = os.environ.get("GS108T_HOST", "http://192.0.2.10")
LOGIN_PATH = "/base/main_login.html"
MAIN_PATH = "/base/web_main.html"
DEBUG = bool(os.environ.get("GS108T_DEBUG"))


class Response:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = headers          # list of (name, value), original case
        self.body = body                # bytes
        self.url = url

    @property
    def text(self):
        charset = "iso-8859-1"
        m = re.search(r"charset=([\w-]+)", self.header("content-type") or "")
        if m:
            charset = m.group(1)
        return self.body.decode(charset, "replace")

    def header(self, name):
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def __repr__(self):
        return f"<Response {self.status} {self.url} {len(self.body)}B>"


def unknown_field_message(path, unknown, known):
    """Explain a --field name the form does not have.

    The usual cause is not a typo but a damaged argv: a Ctrl-C or a paste from
    a transcript leaves a control character (or its caret notation) inside the
    name, which is invisible in a terminal and produces a baffling error.
    """
    hints = []
    for name in sorted(unknown):
        ctl = [c for c in name if ord(c) < 32 or ord(c) == 127]
        near = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        if ctl:
            hint = (f"{name!r} contains a control character "
                    f"0x{ord(ctl[0]):02x}; it came from the shell or a paste, "
                    f"not from this page")
            if near:
                hint += f" (did you mean {near[0]!r}?)"
            hints.append(hint)
        elif near:
            hints.append(f"{name!r}: did you mean {near[0]!r}?")
        else:
            hints.append(repr(name))
    return (f"{path}: unknown field(s): " + "; ".join(hints) +
            "\nknown: " + ", ".join(known))


class Client:
    def __init__(self, host=DEFAULT_HOST, password=None, timeout=10.0,
                 verify_tls=False):
        if "://" not in host:
            host = "http://" + host
        parts = urlsplit(host)
        self.scheme = parts.scheme or "http"
        self.host = parts.hostname or parts.netloc
        default_port = 443 if self.scheme == "https" else 80
        self.port = parts.port or default_port
        self.base = f"{self.scheme}://{parts.netloc}"
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.password = password
        self.cookies = {}
        self.user_agent = "gs108t-client/1.0"

    # --- transport -------------------------------------------------------
    def _connect(self):
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            if not self.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            # timeout is keyword-only on HTTPSConnection; the third positional
            # argument is the TLS context, which would shadow the timeout
            return http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout,
                                               context=ctx)
        return http.client.HTTPConnection(self.host, self.port, self.timeout)

    def _store_cookies(self, headers):
        for k, v in headers:
            if k.lower() != "set-cookie":
                continue
            pair = v.split(";", 1)[0].strip()
            if "=" in pair:
                name, val = pair.split("=", 1)
                self.cookies[name.strip()] = val.strip()

    def request(self, method, path, data=None, headers=None, follow=True,
                _depth=0):
        if path.startswith("http://") or path.startswith("https://"):
            target = urlsplit(path)
            path = target.path + (("?" + target.query) if target.query else "")
        body = None
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if data is not None:
            body = urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs.update(headers or {})

        conn = self._connect()
        try:
            conn.request(method, path, body=body, headers=hdrs)
            raw = conn.getresponse()
            payload = raw.read()
            resp = Response(raw.status, raw.getheaders(), payload,
                            urljoin(self.base, path))
        finally:
            conn.close()

        if DEBUG:
            print(f"{method:4} {path:60} -> {resp.status}", file=sys.stderr)
        self._store_cookies(resp.headers)
        if follow and resp.status in (301, 302, 303, 307, 308):
            loc = resp.header("location")
            if loc and _depth < 5:
                # 302 after a POST becomes a GET, as in a browser
                return self.request("GET", urljoin(self.base, loc), None,
                                    follow=True, _depth=_depth + 1)
        return resp

    # --- session ---------------------------------------------------------
    def logged_in(self):
        """True when a session cookie opens the main shell.

        This firmware answers 200 with the shell instead of redirecting, so
        look for shell-only markers (the login page has none of them).
        """
        r = self.request("GET", MAIN_PATH, follow=False)
        if r.status != 200:
            return False
        body = r.body.lower()
        return b"headdiv" in body or b"<iframe" in body or b"waitpreloadpage" in body

    def login(self):
        if not self.password:
            raise RuntimeError("no password supplied (GS108T_PASSWORD / --password)")
        # the login handler on this firmware expects the same request a browser
        # makes, including the referer from the login page
        self.request("GET", "/", follow=False)      # pick up the pre-session cookie
        fields = {"pwd": self.password, "err_flag": "0", "err_msg": "",
                  "login.x": "10", "login.y": "10"}
        headers = {
            "Referer": self.base + "/",
            "Origin": self.base,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r = self.request("POST", LOGIN_PATH, fields, headers=headers,
                         follow=False)
        loc = r.header("location") or ""
        ok = "SID" in self.cookies
        if not ok:
            ok = r.status in (301, 302, 303) and MAIN_PATH in loc
        if not ok:
            ok = self.logged_in()
        if not ok:
            msg = "login rejected"
            m = re.search(r'name="err_msg"[^>]*VALUE="([^"]*)"', r.text, re.I)
            if m and m.group(1).strip():
                msg += f": {m.group(1).strip()}"
            raise RuntimeError(msg)
        return self

    def logout(self):
        self.cookies.clear()

    # --- pages -----------------------------------------------------------
    def get(self, path):
        return self.request("GET", path)

    def post(self, path, fields):
        return self.request("POST", path, fields)

    def forms(self, path, html=None):
        html = html if html is not None else self.get(path).text
        p = FormParser()
        p.feed(html)
        return p.forms

    def prepare(self, path, values, form_index=0, submit=None):
        """Build the exact payload a browser would submit for this form."""
        html = self.get(path).text
        forms = self.forms(path, html)
        if not forms:
            raise RuntimeError(f"{path}: no form found (login expired?)")
        form = forms[form_index]
        unknown = set(values) - form.field_names()
        if unknown:
            raise RuntimeError(unknown_field_message(
                path, unknown, sorted(form.field_names())))
        data = form.defaults()
        data.update({k: str(v) for k, v in values.items()})
        if submit:
            data[submit] = "Apply"
        elif "submt" in form.field_names():
            # the UI posts 0x10 when Apply is pressed; without it the device
            # accepts the POST but changes nothing
            data["submt"] = "16"
        # form actions may be relative to the page's directory
        action = urljoin(self.base + path, form.action or path)
        return action, data

    def apply(self, path, values, form_index=0, submit=None):
        """Read the page, fill the chosen form, POST it back.

        Values are matched by input name. Anything not supplied keeps the value
        the page rendered (checked boxes, selected options, hidden tokens).
        """
        action, data = self.prepare(path, values, form_index, submit)
        return self.post(action, data)


# --- HTML form model -----------------------------------------------------
class Field:
    def __init__(self, name, ftype="text", value="", options=None,
                 checked=False, disabled=False):
        self.name = name
        self.type = ftype
        self.value = value
        self.options = options or []      # [{value, text, selected}]
        self.checked = checked
        self.disabled = disabled

    def to_dict(self):
        d = {"name": self.name, "type": self.type}
        if self.disabled:
            d["disabled"] = True
        if self.options:
            d["options"] = [{"value": o["value"],
                             "text": re.sub(r"\s+", " ", o["text"]).strip()}
                            for o in self.options]
            sel = [o["value"] for o in self.options if o["selected"]]
            if sel:
                d["selected"] = sel[0]
        elif self.type == "checkbox":
            d["checked"] = self.checked
            d["value"] = self.value or "on"
        else:
            if self.value:
                d["value"] = self.value
        return d


class Form:
    def __init__(self, action, method):
        self.action = action
        self.method = (method or "get").lower()
        self.fields = []

    def field_names(self):
        return {f.name for f in self.fields if f.name}

    def defaults(self):
        data = {}
        for f in self.fields:
            if not f.name or f.disabled:
                continue
            if f.type in ("submit", "image", "button", "reset"):
                continue
            if f.type == "checkbox":
                if f.checked:
                    data[f.name] = f.value or "on"
            elif f.type == "radio":
                if f.checked:
                    data[f.name] = f.value
            elif f.type == "select":
                sel = [o for o in f.options if o["selected"]]
                data[f.name] = (sel[0]["value"] if sel
                                else (f.options[0]["value"] if f.options else ""))
            else:
                data[f.name] = f.value
        return data

    def to_dict(self):
        return {"action": self.action, "method": self.method,
                "fields": [f.to_dict() for f in self.fields]}


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self._form = None
        self._select = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self._form = Form(a.get("action"), a.get("method"))
            self.forms.append(self._form)
            return
        if self._form is None:
            return
        if tag == "input":
            self._form.fields.append(Field(
                a.get("name", ""), a.get("type", "text").lower(),
                a.get("value", ""),
                checked="checked" in a, disabled="disabled" in a))
        elif tag == "select":
            self._select = Field(a.get("name", ""), "select", "",
                                 disabled="disabled" in a)
            self._form.fields.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value", ""), "text": "",
                            "selected": "selected" in a}
            self._select.options.append(self._option)
        elif tag == "textarea":
            self._form.fields.append(Field(a.get("name", ""), "textarea", "",
                                           disabled="disabled" in a))
        elif tag == "button" and a.get("name"):
            self._form.fields.append(Field(a["name"], "button", a.get("value", "")))

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data

    def handle_endtag(self, tag):
        if tag == "select":
            # <option> end tags are optional; stop collecting on the select
            self._option = None
            self._select = None
        elif tag == "option":
            self._option = None
        elif tag == "form":
            self._form = None


# --- HTML reading helpers ------------------------------------------------
class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
            self.rows.append(self._row)
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
                self.rows.append(self._row)
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr":
            self._row = None


def text_of(fragment):
    fragment = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?s)</(tr|p|div|h\d|li)>", "\n", fragment)
    fragment = re.sub(r"(?s)</t[dh]>", " | ", fragment)
    fragment = re.sub(r"(?s)<[^>]+>", "", fragment)
    return re.sub(r"[ \t]+", " ", htmlmod.unescape(fragment)).strip()


def parse_table_rows(html):
    p = TableParser()
    p.feed(html)
    return [r for r in p.rows if any(c for c in r)]


def kv_pairs(html):
    """Collapse label/value table rows into a dict (best effort)."""
    out = {}
    rows = parse_table_rows(html)
    for i, r in enumerate(rows):
        cells = [c for c in r if c]
        if len(cells) == 2:
            out.setdefault(cells[0].rstrip(":"), cells[1])
        # header row followed by a value row, e.g. Model Name / Boot / Software
        if len(r) >= 2 and r[0].strip() == "Model Name" and i + 1 < len(rows):
            for key, value in zip(r, rows[i + 1]):
                if key and value:
                    out[key] = value
    return out


def load_catalog():
    with open(os.path.join(HERE, "catalog.json")) as f:
        return json.load(f)


# --- commands ------------------------------------------------------------
def cmd_login(c, args):
    c.login()
    r = c.get("/base/system/management/sysInfo.html")
    info = kv_pairs(r.text)
    print(f"logged in to {c.base}")
    for k in ("Model Name", "Software Version", "Boot Version", "Boot",
              "Base MAC Address", "Serial Number", "System Up Time"):
        if k in info:
            print(f"  {k:16} {info[k]}")


def cmd_pages(args):
    cat = load_catalog()
    rx = re.compile(args.grep, re.I) if args.grep else None
    for p in cat["pages"]:
        if rx and not rx.search(p["title"] + " " + p["path"]):
            continue
        print(f"{p['risk']:11} {p['path']:56} {p['title']}")


def cmd_get(c, args):
    r = c.get(args.path)
    if args.out:
        with open(args.out, "wb") as f:
            f.write(r.body)
        print(f"{r.status} {len(r.body)}B -> {args.out}")
    else:
        print(r.text)


def cmd_fields(c, args):
    forms = c.forms(args.path)
    if not forms:
        print("no forms")
        return
    wanted = [args.form] if args.form is not None else range(len(forms))
    for i in wanted:
        if i >= len(forms):
            sys.exit(f"no form[{i}]; page has {len(forms)} form(s)")
        f = forms[i]
        print(f"form[{i}] {f.method.upper()} {f.action or args.path}")
        for fld in f.fields:
            if fld.options:
                extra = " options=" + ",".join(
                    f"{o['value']}{'*' if o['selected'] else ''}"
                    for o in fld.options[:12])
                if len(fld.options) > 12:
                    extra += ",..."
            elif fld.type == "checkbox":
                extra = f" checked={fld.checked}"
            elif fld.value:
                extra = f" value={fld.value!r}"
            else:
                extra = ""
            print(f"  {fld.name:24} {fld.type:9}{extra}")


def cmd_set(c, args):
    values = {}
    for item in args.field:
        if "=" not in item:
            sys.exit(f"bad --field {item!r}, expected name=value")
        k, v = item.split("=", 1)
        values[k] = v
    if args.dry_run:
        action, data = c.prepare(args.path, values, form_index=args.form)
        print(f"POST {action}")
        for k, v in data.items():
            mark = "*" if k in values else " "
            print(f" {mark} {k}={v}")
        return
    r = c.apply(args.path, values, form_index=args.form)
    print(f"{r.status} {r.url} ({len(r.body)}B)")
    if args.show:
        print(r.text)


def cmd_summary(c, args):
    r = c.get("/base/system/management/sysInfo.html")
    info = kv_pairs(r.text)
    if not info:
        print(text_of(r.text)[:2000])
        return
    width = max(len(k) for k in info)
    for k, v in info.items():
        print(f"{k:<{width}}  {v}")


def cmd_save(c, args):
    # the Save page needs the SaveChanges box ticked plus the Apply flag
    r = c.post("/base/system/save_changes.html",
               {"SaveChanges": "checkbox", "submt": "16"})
    print(f"{r.status} save requested")


def cmd_reboot(c, args):
    if not args.yes:
        sys.exit("reboot is disruptive: pass --yes to confirm")
    # the Reboot page requires its confirmation box and the Apply flag
    r = c.post("/base/system/sys_reset.html", {"CBox_2": "0", "submt": "16"})
    print(f"{r.status} reboot requested")


def fetch(c, url, retries):
    last = None
    for attempt in range(retries + 1):
        try:
            return c.get(url)
        except (OSError, TimeoutError) as e:
            last = e
            if attempt < retries:
                time.sleep(1.5)
                # slow pages on this firmware need more than the default
                c.timeout = max(c.timeout, 30)
    raise last


def reauth(c):
    """Log in again; some pages (PoE, SNMP) end the session on this model."""
    c.request("GET", "/", follow=False)
    return c.login()


def is_login_page(r):
    return b'name="pwd"' in r.body and b"main_login.html" in r.body


def cmd_discover(c, args):
    cat = load_catalog()
    out = {"id": "netgear-gs108t-webui", "host": c.base,
           "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "pages": []}
    keep = args.keep_html
    if keep:
        os.makedirs(keep, exist_ok=True)
    failures = []
    relogins = 0

    def get_ok(url):
        """Fetch a page, restoring the session if the switch drops it."""
        nonlocal relogins
        r = fetch(c, url, args.retries)
        if is_login_page(r) or r.status in (301, 302):
            reauth(c)
            relogins += 1
            r = fetch(c, url, args.retries)
        return r

    for n, p in enumerate(cat["pages"], 1):
        entry = dict(p)
        try:
            r = get_ok(p["url"])
        except Exception as e:                      # noqa: BLE001
            failures.append({"path": p["path"], "error": repr(e)})
            entry.update({
                "status": 0,
                "unsupported": True,
                "error": str(e) or type(e).__name__,
                "bytes": 0,
                "html_title": "",
                "forms": [],
                "tables": 0,
            })
            out["pages"].append(entry)
            # a stalled transfer usually leaves the HTTP server wedged, so
            # start a fresh session before the next page
            try:
                reauth(c)
                relogins += 1
            except Exception:                       # noqa: BLE001
                pass
            if args.verbose:
                print(f"{n:3}/{cat['count']} ERR {p['path']}", flush=True)
            time.sleep(args.delay)
            continue
        if is_login_page(r):
            failures.append({"path": p["path"], "error": "login page returned"})
            entry.update({"status": r.status, "unsupported": True,
                          "error": "login page returned", "bytes": len(r.body),
                          "html_title": "", "forms": [], "tables": 0})
            out["pages"].append(entry)
            continue
        parser = FormParser()
        parser.feed(r.text)
        title_m = re.search(r"(?is)<title>(.*?)</title>", r.text)
        # a sub-100-byte body means the switch aborted the request; the
        # session is usually gone too, so log in again before the next page
        empty = r.status == 200 and len(r.body) < 100
        entry.update({
            "status": r.status,
            "unsupported": r.status == 404 or empty,
            "empty": empty,
            "bytes": len(r.body),
            "html_title": text_of(title_m.group(1)) if title_m else "",
            "forms": [f.to_dict() for f in parser.forms],
            "tables": len(parse_table_rows(r.text)),
        })
        out["pages"].append(entry)
        if empty:
            reauth(c)
            relogins += 1
        if keep:
            name = re.sub(r"[^a-z0-9]+", "_", p["path"].lower()).strip("_")
            with open(os.path.join(keep, name + ".html"), "wb") as f:
                f.write(r.body)
        if args.verbose:
            print(f"{n:3}/{cat['count']} {r.status} {p['path']}", flush=True)
        time.sleep(args.delay)
    out["failures"] = failures
    out["stats"] = {
        "pages": len(out["pages"]),
        "supported": sum(1 for p in out["pages"] if p["status"] == 200
                         and not p.get("empty")),
        "unsupported": sum(1 for p in out["pages"] if p.get("unsupported")),
        "empty": sum(1 for p in out["pages"] if p.get("empty")),
        "with_forms": sum(1 for p in out["pages"] if p["forms"]),
        "forms": sum(len(p["forms"]) for p in out["pages"]),
        "fields": sum(len(f["fields"]) for p in out["pages"] for f in p["forms"]),
        "relogins": relogins,
        "failures": len(failures),
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {args.out}: {out['stats']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="NETGEAR GS108Tv2 web-UI client")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"switch base URL (default {DEFAULT_HOST}, env GS108T_HOST)")
    ap.add_argument("--password", help="admin password (prefer GS108T_PASSWORD)")
    ap.add_argument("--password-file")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (self-signed switch certs)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("login", help="verify credentials and print device info")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("pages", help="list pages from the menu catalog")
    p.add_argument("--grep", help="filter on title or path")
    p.set_defaults(func=cmd_pages)

    p = sub.add_parser("get", help="fetch one page (HTML)")
    p.add_argument("path")
    p.add_argument("--out")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("fields", help="show the form fields of one page")
    p.add_argument("path")
    p.add_argument("--form", type=int, help="only this form index")
    p.set_defaults(func=cmd_fields)

    p = sub.add_parser("set", help="apply values to a page's first form")
    p.add_argument("path")
    p.add_argument("--field", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--form", type=int, default=0, help="form index (default 0)")
    p.add_argument("--show", action="store_true", help="print the response HTML")
    p.add_argument("--dry-run", action="store_true",
                   help="print the exact POST body without sending it")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("summary", help="read system information")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("save", help="commit running config to NVRAM")
    p.set_defaults(func=cmd_save)

    p = sub.add_parser("reboot", help="restart the switch")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reboot)

    p = sub.add_parser("discover", help="crawl all pages and record their forms")
    p.add_argument("--out", default="endpoints.json")
    p.add_argument("--keep-html", metavar="DIR")
    p.add_argument("--delay", type=float, default=0.2,
                   help="seconds between requests (default 0.2)")
    p.add_argument("--retries", type=int, default=1,
                   help="retries per page on timeout (default 1)")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_discover)

    args = ap.parse_args(argv)
    if args.command == "pages":
        cmd_pages(args)
        return

    try:
        c = make_client(args)
        if args.command != "login":
            c.login()
        args.func(c, args)
    except RuntimeError as e:
        sys.exit(f"error: {e}")
    except OSError as e:
        sys.exit(f"error: cannot reach {args.host}: {e}")


def make_client(args):
    pw = args.password or os.environ.get("GS108T_PASSWORD")
    if args.password_file:
        pw = open(args.password_file).read().strip()
    if pw is None and sys.stdin.isatty():
        pw = getpass.getpass(f"password for {args.host}: ")
    return Client(args.host, pw, timeout=args.timeout,
                  verify_tls=not args.insecure)


if __name__ == "__main__":
    main()
