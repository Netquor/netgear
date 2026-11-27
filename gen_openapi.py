#!/usr/bin/env python3
"""Generate openapi.yaml for the GS108Tv2 web UI.

Two sources, in order of preference:

  endpoints.json   produced by `gs108t.py discover` against the live switch.
                   Every form and field comes from the real HTML.
  catalog.json   the menu-derived page list. Used when no crawl has run yet;
                   paths are real but form bodies are described generically.

Run:  python3 gen_openapi.py [--endpoints endpoints.json] [--out openapi.yaml]
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
# RFC 5737 documentation address; the spec never names a real switch
DOC_HOST = "http://192.0.2.10"


# --- tiny YAML writer (JSON-safe scalars, readable structure) -------------
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
SCALARS = (str, int, float, bool, type(None))


def _key(k):
    return k if KEY_RE.match(str(k)) else json.dumps(str(k))


def _scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def _emit(obj, indent=0):
    pad = " " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                if v:
                    lines.append(f"{pad}{_key(k)}:")
                    lines.extend(_emit(v, indent + 2))
                else:
                    lines.append(f"{pad}{_key(k)}: {{}}")
            elif isinstance(v, list):
                if v:
                    lines.append(f"{pad}{_key(k)}:")
                    lines.extend(_emit(v, indent + 2))
                else:
                    lines.append(f"{pad}{_key(k)}: []")
            else:
                lines.append(f"{pad}{_key(k)}: {_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict) and item:
                body = _emit(item, indent + 2)
                first = body[0].lstrip()
                lines.append(f"{pad}- {first}")
                lines.extend(body[1:])
            elif isinstance(item, list) and item:
                lines.append(f"{pad}-")
                lines.extend(_emit(item, indent + 2))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(obj)}")
    return lines


def dump_yaml(obj):
    return "\n".join(_emit(obj)) + "\n"


# --- schema helpers ------------------------------------------------------
def slug(path):
    s = re.sub(r"\.html$", "", path.lstrip("/"))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def operation_id(method, page_id):
    return re.sub(r"-+", "-", f"{method}-{page_id}")


IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}\b")
MAC = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
ADDRESS_FIELD = re.compile(
    r"(^|_)(ip|ipaddr|ipv6|gateway|subnet|mask|dns|server|host|ntp|syslog)", re.I)


def is_device_value(name, value):
    """True when a captured default identifies the device rather than the form.

    A published spec must not carry one switch's address, MAC, or system dump,
    and a placeholder would be worse: it invites clients to send made-up
    values. These fields are marked x-echo so callers know to resend what the
    page rendered.
    """
    if not isinstance(value, str) or not value:
        return False
    if ADDRESS_FIELD.search(name) and (IPV4.search(value) or IPV6.search(value)):
        return True
    if MAC.search(value):
        return True
    return len(value) > 64


def field_schema(fields):
    """Merge form fields into an object schema (browser submission shape)."""
    props, required = {}, []
    by_name = {}
    for f in fields:
        name = f.get("name")
        if not name or f.get("type") in ("submit", "image", "button", "reset"):
            continue
        htype = f.get("type", "text")
        if name in by_name:                    # repeated name: radio group or checkbox
            prev = by_name[name]
            value = f.get("value")
            if prev.get("x-html-type") == "checkbox" or htype == "checkbox":
                if f.get("checked") and not prev.get("default"):
                    prev["default"] = value or "on"
                continue
            if is_device_value(name, value):
                prev.pop("enum", None)
                prev["x-echo"] = True
                continue
            vals = prev.setdefault("enum", [])
            if value and value not in vals:
                vals.append(value)
            if not vals:
                prev.pop("enum", None)
            continue
        prop = {"type": "string"}
        if f.get("options"):
            prop["enum"] = [o["value"] for o in f["options"]]
            prop["x-html-type"] = "select"
        elif htype == "checkbox":
            if f.get("checked"):
                prop["default"] = f.get("value") or "on"
            prop["x-html-type"] = "checkbox"
        elif htype == "radio":
            value = f.get("value")
            if is_device_value(name, value):
                prop["x-echo"] = True
            else:
                prop["enum"] = [value] if value else []
            prop["x-html-type"] = "radio"
        elif name == "submt":
            # the page renders it empty, but the UI fills 0x10 before submit
            prop["default"] = "16"
            prop["description"] = ("Apply flag. The web UI posts `16` (0x10) "
                                   "when Apply is pressed; an empty value is "
                                   "accepted but changes nothing.")
        elif f.get("value"):
            if is_device_value(name, f["value"]):
                prop["x-echo"] = True
                prop["description"] = ("Resend the value the page rendered; "
                                       "it belongs to the device, not to the "
                                       "form.")
            else:
                prop["default"] = f["value"]
        if f.get("disabled"):
            prop["readOnly"] = True
        props[name] = prop
        by_name[name] = prop
        if not f.get("disabled"):
            required.append(name)
    return {"type": "object", "properties": props,
            "required": required or None}


def drop_none(d):
    if isinstance(d, dict):
        return {k: drop_none(v) for k, v in d.items() if v is not None}
    if isinstance(d, list):
        return [drop_none(v) for v in d]
    return d


def build_from_endpoints(ep, host=None):
    pages = ep["pages"]
    stats = ep.get("stats") or {}
    generated = ep.get("generated") or ""
    # never publish the address of the switch the capture came from
    host = host or DOC_HOST
    if generated and not generated.startswith("not yet"):
        capture_note = (f"Field schemas were captured from a live device on "
                        f"{generated}.")
        if stats:
            capture_note += (f" Of {stats.get('pages')} menu pages, "
                             f"{stats.get('supported')} answered with real "
                             f"content, {stats.get('with_forms')} expose forms "
                             f"({stats.get('fields')} fields), and "
                             f"{stats.get('unsupported')} were not usable on "
                             f"that model.")
    else:
        capture_note = ("Page paths come from the switch's own menu JavaScript; "
                        "form bodies have not been captured yet. Run `discover` "
                        "against your device, then regenerate, to fill in the "
                        "field schemas.")
    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "NETGEAR GS108Tv2 web UI",
            "version": "1.0.0",
            "description": (
                "Unofficial description of the NETGEAR GS108Tv2 ProSAFE web "
                "UI as an HTTP contract. The switch has no JSON API: GET "
                "returns page HTML, and POST submits a form. Most forms post "
                "back to the page that rendered them; the rest post to their "
                "own `*_rw.html` handler, which appears here as a separate "
                "path. POSTs are read-modify-write, so fetch the page, change "
                "the fields you care about, and submit everything else as "
                "rendered. Fields marked `x-echo` carry the device's current "
                "address or identity and must be resent unchanged. " +
                capture_note + " "
                "Paths marked `x-unsupported` are in the menu but are not "
                "served by every model. Operations marked destructive reboot "
                "the switch, restore factory defaults, or write firmware; call "
                "them deliberately. Not affiliated with NETGEAR."
            ),
        },
        "servers": [{"url": host}],
        "security": [{"cookieAuth": []}],
        "tags": [],
        "paths": {},
        "components": {
            "securitySchemes": {
                "cookieAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Cookie",
                    "description": ("Session cookie issued by POST "
                                    "/base/main_login.html. On this firmware it "
                                    "is `SID`, scoped to `/` and HttpOnly."),
                }
            },
            "schemas": {
                "HtmlPage": {"type": "string",
                             "description": "Server-rendered HTML page."},
            },
        },
    }
    seen_tags = []
    for p in pages:
        path = "/" + p["path"]
        tag = p.get("top") or "General"
        if tag not in seen_tags:
            seen_tags.append(tag)
        where = " · ".join(x for x in (p.get("section"), p.get("group")) if x)
        status = p.get("status")
        unsupported = bool(p.get("unsupported")) or (status not in (None, 200))
        if unsupported:
            if p.get("empty"):
                why = ("This model aborted the request and dropped the "
                       "session, so the page is not usable here.")
            else:
                why = f"Not present on the crawled model (HTTP {status})."
            detail = (f"GS108Tv2 page `{p['path']}`"
                      + (f" ({where})" if where else "") + f". {why}")
        else:
            detail = (f"GS108Tv2 page `{p['path']}`"
                      + (f" ({where})" if where else "")
                      + f". Risk: {p.get('risk')}.")
        base_op = {
            "tags": [tag],
            "summary": p.get("title") or p.get("html_title") or p["path"],
            "description": detail,
            "x-risk": p.get("risk"),
            "x-section": p.get("section"),
        }
        if unsupported:
            # the menu lists it, but this firmware/model does not serve it
            base_op.update({
                "deprecated": True,
                "x-unsupported": True,
                "responses": {
                    "404": {"description": "Page not present on this model."},
                },
            })
            doc["paths"].setdefault(path, {}).update({
                "get": dict(base_op,
                            operationId=operation_id("get", p["id"]))})
            continue

        base_op["responses"] = {
            "200": {
                "description": "Page HTML.",
                "content": {"text/html": {
                    "schema": {"$ref": "#/components/schemas/HtmlPage"}}},
            }
        }
        ops = {"get": dict(base_op,
                           operationId=operation_id("get", p["id"]))}
        forms = p.get("forms") or []
        if forms:
            form = forms[0]
            schema = drop_none(field_schema(form["fields"]))
            action = (form.get("action") or "").lstrip("/") or p["path"]
            target = "/" + action
            if target != path:
                desc = (f"Apply the form shown on `{p['path']}`. This firmware "
                        f"posts edits to a separate `*_rw` handler, so the "
                        f"write target is `{action}` rather than the page.")
            else:
                desc = (f"Submit a form back to `{p['path']}` and receive the "
                        f"re-rendered page. Send every field the browser would "
                        f"send; omitted fields revert to defaults.")
            post = dict(base_op,
                        operationId=operation_id("post", slug(action)),
                        summary=(p.get("title") or p["path"])
                                + (" (apply)" if target != path else ""),
                        description=desc,
                        requestBody={"content": {
                            "application/x-www-form-urlencoded": {
                                "schema": schema}}},
                        responses={
                            "200": {"description": "Resulting page HTML.",
                                    "content": {"text/html": {"schema": {
                                        "$ref": "#/components/schemas/HtmlPage"}}}},
                            "302": {"description": "Redirect back to the page "
                                                   "or to the login screen."},
                        })
            if schema.get("properties") == {}:
                post.pop("requestBody")
            if target == path:
                ops["post"] = post
            else:
                doc["paths"].setdefault(target, {})["post"] = post
        doc["paths"].setdefault(path, {}).update(ops)

    # login is the one unauthenticated call
    login = {
        "post": {
            "tags": ["Login"],
            "summary": "Log in",
            "description": ("Sets the session cookie. This firmware answers 200 "
                            "with Set-Cookie: SID on success and re-renders the "
                            "login page with err_flag=1 on failure, so check the "
                            "cookie rather than the status code. Send Referer: <base>/."),
            "security": [],
            "operationId": "post-login",
            "requestBody": {"content": {"application/x-www-form-urlencoded": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "pwd": {"type": "string", "description": "Admin password."},
                        "err_flag": {"type": "string", "default": "0"},
                        "err_msg": {"type": "string", "default": ""},
                        "login.x": {"type": "string", "default": "10"},
                        "login.y": {"type": "string", "default": "10"},
                    },
                    "required": ["pwd"],
                }}}},
            "responses": {
                "200": {"description": "Login accepted when Set-Cookie carries "
                                       "SID; otherwise the login HTML with "
                                       "err_msg is returned.",
                        "headers": {"Set-Cookie": {"schema": {"type": "string"}}}},
            },
        },
        "get": {
            "tags": ["Login"],
            "summary": "Login page",
            "security": [],
            "operationId": "get-login-page",
            "responses": {"200": {"description": "Login HTML.",
                                  "content": {"text/html": {"schema": {
                                      "$ref": "#/components/schemas/HtmlPage"}}}}},
        },
    }
    doc["paths"]["/base/main_login.html"] = login
    doc["tags"] = [{"name": t} for t in ["Login"] + seen_tags]
    return doc


def build_from_catalog(cat, host):
    pages = []
    for p in cat["pages"]:
        pages.append(dict(p, forms=[], html_title=""))
    return build_from_endpoints(
        {"host": host, "generated": "not yet crawled (catalog only)",
         "pages": pages})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoints", default=os.path.join(HERE, "endpoints.json"))
    ap.add_argument("--catalog", default=os.path.join(HERE, "catalog.json"))
    ap.add_argument("--host", default=DOC_HOST)
    ap.add_argument("--out", default=os.path.join(HERE, "openapi.yaml"))
    args = ap.parse_args()

    if os.path.exists(args.endpoints):
        with open(args.endpoints) as f:
            ep = json.load(f)
        doc = build_from_endpoints(ep, args.host)
        source = args.endpoints
        crawled = {p["path"]: p for p in ep["pages"]}
    else:
        with open(args.catalog) as f:
            cat = json.load(f)
        doc = build_from_catalog(cat, args.host)
        source = args.catalog + " (no crawl yet)"
        crawled = {}

    with open(args.out, "w") as f:
        f.write(dump_yaml(doc))
    with open(os.path.splitext(args.out)[0] + ".json", "w") as f:
        json.dump(doc, f, indent=1)

    ops = sum(len(v) for v in doc["paths"].values())
    with_bodies = sum(1 for v in doc["paths"].values() if "post" in v)
    print(f"{args.out}: {len(doc['paths'])} paths, {ops} operations, "
          f"{with_bodies} with form bodies  <- {source}")


if __name__ == "__main__":
    main()
