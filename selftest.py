#!/usr/bin/env python3
"""Offline + unauthenticated checks for the GS108T client and spec.

  python3 selftest.py          # offline only
  python3 selftest.py --live   # also GET / against the switch (no auth)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gs108t  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((PASS if cond else FAIL, name, detail))
    print(f"{PASS if cond else FAIL:4}  {name}{('  — ' + detail) if detail else ''}")


def main():
    live = "--live" in sys.argv

    cat = json.load(open(os.path.join(HERE, "catalog.json")))
    pages = cat["pages"]
    check("catalog has 163 pages", cat["count"] == 163 == len(pages),
          str(cat["count"]))
    check("paths are unique", len({p["path"] for p in pages}) == len(pages))
    check("every path ends in .html",
          all(p["path"].endswith(".html") for p in pages))
    check("every page has a tab", all(p["top"] for p in pages))

    login_html = open(os.path.join(HERE, "_capture", "login.html"),
                      encoding="latin-1").read()
    p = gs108t.FormParser()
    p.feed(login_html)
    check("login page parses to one form", len(p.forms) == 1)
    if p.forms:
        names = p.forms[0].field_names()
        check("login form targets main_login.html",
              p.forms[0].action == "/base/main_login.html", p.forms[0].action)
        check("login form carries pwd", "pwd" in names, ",".join(sorted(names)))

    spec = json.load(open(os.path.join(HERE, "openapi.json")))
    check("openapi.json is 3.1", spec.get("openapi") == "3.1.0")
    check("spec has 164 paths", len(spec["paths"]) == 164, str(len(spec["paths"])))
    check("every spec path is absolute",
          all(k.startswith("/") for k in spec["paths"]))
    check("login is unauthenticated",
          spec["paths"]["/base/main_login.html"]["post"].get("security") == [])

    yaml_path = os.path.join(HERE, "openapi.yaml")
    yaml_head = open(yaml_path).read(80)
    check("openapi.yaml exists and is a YAML mapping",
          yaml_head.startswith("openapi:"))

    # the crawler, the generator and the catalog must agree on the page set
    check("catalog and spec cover the same pages",
          {"/" + p["path"] for p in pages} == set(spec["paths"]) - {"/base/main_login.html"})

    if live:
        host = os.environ.get("GS108T_HOST")
        if not host:
            print("SKIP  live checks (set GS108T_HOST to your switch)")
        else:
            c = gs108t.Client(host)
            r = c.get("/")
            check("GET / answers 200", r.status == 200, str(r.status))
            check("GET / is the login page", "main_login.html" in r.text)
            check("no session before login", c.logged_in() is False)
            r = c.request("GET", "/base/web_main.html", follow=True)
            check("web_main redirects to login when unauthenticated",
                  "main_login.html" in r.text or r.status == 200)

    bad = [r for r in results if r[0] == FAIL]
    print(f"\n{len(results) - len(bad)}/{len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
