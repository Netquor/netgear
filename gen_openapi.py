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
def operation_id(method, page_id):
    return re.sub(r"-+", "-", f"{method}-{page_id}")


def field_schema(fields):
    """Merge form fields into an object schema (browser submission shape)."""
    props, required = {}, []
    by_name = {}
    for f in fields:
        name = f.get("name")
        if not name or f.get("type") in ("submit", "image", "button", "reset"):
            continue
        if name in by_name:                    # radio groups
            prev = by_name[name]
            vals = prev.setdefault("enum", [])
            if f.get("value") and f["value"] not in vals:
                vals.append(f["value"])
            continue
        prop = {"type": "string"}
        htype = f.get("type", "text")
        if f.get("options"):
            prop["enum"] = [o["value"] for o in f["options"]]
            prop["x-html-type"] = "select"
        elif htype == "checkbox":
            prop["default"] = f.get("value") or "on"
            prop["x-html-type"] = "checkbox"
        elif htype == "radio":
            prop["enum"] = [f.get("value")] if f.get("value") else []
            prop["x-html-type"] = "radio"
        elif f.get("value"):
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


def build_from_endpoints(ep):
    pages = ep["pages"]
    generated = ep.get("generated") or ""
    if generated and not generated.startswith("not yet"):
        capture_note = (f"Field schemas were captured from a live device on "
                        f"{generated}.")
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
                "returns page HTML, and POST submits the page's own form back "
                "to the same path. POSTs are read-modify-write, so fetch the "
                "page, change the fields you care about, and submit everything "
                "else as rendered. " + capture_note + " "
                "Operations marked destructive reboot the switch, restore "
                "factory defaults, or write firmware; call them deliberately. "
                "Not affiliated with NETGEAR."
            ),
        },
        "servers": [{"url": ep.get("host", "")}],
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
                                    "/base/main_login.html. The client stores "
                                    "it automatically; its name is chosen by "
                                    "the device."),
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
        base_op = {
            "tags": [tag],
            "summary": p.get("title") or p.get("html_title") or p["path"],
            "description": (f"GS108Tv2 page `{p['path']}`"
                            + (f" ({where})" if where else "")
                            + f". Risk: {p.get('risk')}."),
            "x-risk": p.get("risk"),
            "x-section": p.get("section"),
            "responses": {
                "200": {
                    "description": "Page HTML.",
                    "content": {"text/html": {
                        "schema": {"$ref": "#/components/schemas/HtmlPage"}}},
                }
            },
        }
        ops = {"get": dict(base_op,
                           operationId=operation_id("get", p["id"]))}
        forms = p.get("forms") or []
        if forms:
            form = forms[0]
            schema = drop_none(field_schema(form["fields"]))
            post = dict(base_op,
                        operationId=operation_id("post", p["id"]),
                        description=(f"Submit the form on `{p['path']}` "
                                     f"(action `{form.get('action') or p['path']}`, "
                                     f"method {form.get('method', 'post').upper()})."),
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
            ops["post"] = post
        doc["paths"][path] = ops

    # login is the one unauthenticated call
    login = {
        "post": {
            "tags": ["Login"],
            "summary": "Log in",
            "description": ("Sets the session cookie. On success the device "
                            "returns 302 to /base/web_main.html; on failure it "
                            "returns the login page with err_flag=1."),
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
                "302": {"description": "Login accepted.",
                        "headers": {"Location": {"schema": {"type": "string"}}}},
                "200": {"description": "Login rejected; HTML carries err_msg."},
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
    ap.add_argument("--host", default="http://192.0.2.10")
    ap.add_argument("--out", default=os.path.join(HERE, "openapi.yaml"))
    args = ap.parse_args()

    if os.path.exists(args.endpoints):
        with open(args.endpoints) as f:
            ep = json.load(f)
        doc = build_from_endpoints(ep)
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
