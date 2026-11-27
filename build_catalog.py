#!/usr/bin/env python3
"""Build catalog.json from the switch's own menu JavaScript.

The GS108Tv2 web UI ships /base/js/tabs_Layer2.js. Two functions in it define
the whole navigation:

  Loading()     maps each menu section id to its top tab and display label
                (System / Management, Switching / Ports, ...)
  PaintSubTree() lists the pages inside each section as SecLvl / FrthLvl calls

Parsing both gives every page path with its tab, section, group and title,
without guessing URLs.
"""
import argparse
import hashlib
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MENU = os.path.join(HERE, "_capture", "menu.js")

# endpoint sensitivity, used by the CLI, README and OpenAPI x-risk
DESTRUCTIVE = {
    "base/system/sys_reset.html": "reboots the switch",
    "base/system/reset_cfg.html": "restores factory defaults",
    "base/system/file_upload.html": "writes a file/firmware to flash over TFTP",
    "base/system/http_file_upload.html": "writes a file/firmware to flash over HTTP",
    "base/system/dual_image_cfg.html": "changes the active firmware image",
    "base/system/change_passwd.html": "changes the admin password",
}
ACTION = {
    "base/system/ping.html", "base/system/traceroute.html",
    "base/system/port/cableTest.html", "base/system/log/inmemory_cfg.html",
    "base/system/log/persistent_cfg.html", "base/system/event_log.html",
    "base/support.html", "base/prodRegistration/registration.html",
}

# the menu ids are terse; these are the labels the UI shows
PRETTY = {
    "managementsecurity": "Management Security",
    "trafficcontrol": "Traffic Control",
    "saveconfig": "Save Configuration",
    "onlinehelp": "Online Help",
    "multicast": "Multicast",
    "dot1as": "802.1AS",
    "PortAuthentication": "Port Authentication",
    "Diffserv": "DiffServ",
    "AddressTable": "Address Table",
    "RouterDiscovery": "Router Discovery",
    "RoutingTable": "Routing Table",
    "Filemanagement": "File Management",
    "AutoVoIP": "Auto VoIP",
    "Deviceview": "Device View",
}

CASE_RE = re.compile(r'^\s*case\s+"([^"]+)"\s*:')
FRST_RE = re.compile(r'FrstLvl\(\s*"[^"]*"\s*,\s*"([^"]*)"\s*,\s*"[^"]*"')
LVL_RE = re.compile(
    r'(SecLvl|FrthLvl)\(\s*"[^"]*"\s*,\s*"([^"]*)"\s*,\s*"([^"]+\.html)"')
NAME_RE = re.compile(r'currName\+=space\(\)\+"([^"]+)"')
SUB_RE = re.compile(r'SubTree\+="([^"]+)"')


def classify(path, title):
    if path in DESTRUCTIVE:
        return "destructive"
    if path in ACTION:
        return "action"
    low = (path + " " + title).lower()
    if any(k in low for k in ("upload", "download", "reset", "reboot", "license")):
        return "action"
    if any(k in low for k in ("_cfg", "config", "wizard", "setup", "membership",
                              "registration", "change_passwd")):
        return "write"
    return "read"


def slug(path):
    s = re.sub(r"\.html$", "", path)
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def clean(s):
    return re.sub(r"\s+", " ", s.replace("<br />", " ")).strip()


def parse_section_map(src):
    """id -> (top tab, section label) from the Loading() function."""
    start = src.index("function Loading(")
    body = src[start:src.index("\nfunction ", start + 20)]
    out, top = {}, ""
    for line in body.splitlines():
        m = CASE_RE.match(line)
        if m and "currName" not in line:
            top = m.group(1)
            continue
        if "currName+=" not in line or "SubTree+=" not in line:
            continue
        name = NAME_RE.search(line)
        sub = SUB_RE.search(line)
        if not (name and sub):
            continue
        for sid in sub.group(1).split("~"):
            sid = sid.strip()
            if sid:
                label = PRETTY.get(clean(name.group(1)), clean(name.group(1)))
                out.setdefault(sid, (top, label))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Build catalog.json from a captured tabs_Layer2.js")
    ap.add_argument("--menu", default=MENU,
                    help=f"menu JavaScript captured from the switch "
                         f"(default {os.path.relpath(MENU, HERE)})")
    ap.add_argument("--out", default=os.path.join(HERE, "catalog.json"))
    args = ap.parse_args()

    try:
        src = open(args.menu, encoding="latin-1").read()
    except FileNotFoundError:
        raise SystemExit(
            f"missing {args.menu}\n"
            "Capture it from your own switch first:\n"
            "  mkdir -p _capture\n"
            "  curl -o _capture/menu.js http://<switch>/base/js/tabs_Layer2.js")
    section_map = parse_section_map(src)

    tab = group = ""
    pages, seen = [], set()
    for line in src.splitlines():
        m = CASE_RE.match(line)
        if m:
            tab, group = m.group(1), ""
            continue
        m = FRST_RE.search(line)
        if m:
            group = clean(m.group(1))
            continue
        m = LVL_RE.search(line)
        if not m:
            continue
        _kind, title, path = m.groups()
        title, path = clean(title), path.lstrip("/")
        if path in seen:
            continue
        seen.add(path)
        top, section = section_map.get(tab, ("Other", tab))
        pages.append({
            "id": slug(path),
            "path": path,
            "title": title,
            "top": top,
            "section": section,
            "group": group,
            "risk": classify(path, title),
            "url": "/" + path,
        })

    out = {
        "source": "/base/js/tabs_Layer2.js",
        "menu_sha": hashlib.sha256(open(args.menu, "rb").read()).hexdigest()[:16],
        "count": len(pages),
        "pages": pages,
    }
    dest = args.out
    with open(dest, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {dest}: {len(pages)} pages, "
          f"{len({p['top'] for p in pages})} tabs, "
          f"{len({p['section'] for p in pages})} sections")
    for top in dict.fromkeys(p["top"] for p in pages):
        n = sum(1 for p in pages if p["top"] == top)
        print(f"  {top:14} {n:3}")
    unmapped = sorted({p["section"] for p in pages if p["section"] in
                       {q for q in section_map.values()}} - set())
    if unmapped:
        print("  unmapped sections:", unmapped)


if __name__ == "__main__":
    main()
