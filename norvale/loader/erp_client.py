"""Minimal Frappe REST client: one identity (API token or Administrator session) per instance."""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

q = lambda s: urllib.parse.quote(str(s), safe="")


class ErpError(Exception):
    def __init__(self, status, exc_type, message):
        super().__init__(f"HTTP {status} {exc_type or ''}: {message}".strip())
        self.status, self.exc_type, self.message = status, exc_type, message


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    @staticmethod
    def origin(url):
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        if port is None:
            port = {"http": 80, "https": 443}.get(parts.scheme)
        return parts.scheme, parts.hostname, port

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self.origin(req.full_url) != self.origin(newurl):
            raise ErpError(code, "Redirect", "ERP refused a cross-origin redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ErpClient:
    def __init__(self, base, site, token=None):
        self.base, self.site, self.token = base, site, token
        self.opener = urllib.request.build_opener(SameOriginRedirect(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None, timeout=300, *, response_key=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "X-Frappe-Site-Name": self.site})
        if self.token:
            req.add_header("Authorization", "token " + self.token)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise _error(e) from None
        if response_key is not None:
            return payload[response_key]
        return payload.get("data", payload.get("message"))

    def login(self, user, password):
        return self.request("POST", "/api/method/login", {"usr": user, "pwd": password})

    # convenience used by loader and agent tools
    def get(self, doctype, name):
        return self.request("GET", f"/api/resource/{q(doctype)}/{q(name)}")

    def list(self, doctype, filters=None, fields=None, limit=20, order_by=None):
        params = {"limit_page_length": limit, "fields": json.dumps(fields or ["name"])}
        if filters:
            params["filters"] = json.dumps(filters)
        if order_by:
            params["order_by"] = order_by
        return self.request("GET", f"/api/resource/{q(doctype)}?" + urllib.parse.urlencode(params))

    def metadata(self, doctype):
        docs = self.request("POST", "/api/method/frappe.desk.form.load.getdoctype",
                            {"doctype": doctype}, response_key="docs")
        return next(doc for doc in docs if doc["name"] == doctype)

    def create(self, doctype, doc):
        return self.request("POST", f"/api/resource/{q(doctype)}", doc)

    def update(self, doctype, name, changes):
        return self.request("PUT", f"/api/resource/{q(doctype)}/{q(name)}", changes)

    def call(self, method, args=None):
        return self.request("POST", f"/api/method/{method}", args or {})


def _error(e):
    body = e.read().decode(errors="replace")
    try:
        j = json.loads(body)
    except ValueError:
        return ErpError(e.code, None, body[:300])
    msg = j.get("_server_messages")
    if msg:
        try:
            msg = " | ".join(json.loads(m).get("message", m) for m in json.loads(msg))
        except (ValueError, AttributeError, TypeError):
            pass
    else:
        msg = (j.get("exception") or "").strip().split("\n")[-1] or body[:300]
    return ErpError(e.code, j.get("exc_type"), str(msg)[:600])
