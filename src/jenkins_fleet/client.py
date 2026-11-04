"""Minimal Jenkins REST client built on the standard library.

Only what jenkins-fleet needs: JSON GETs against ``/api/json`` endpoints and
POSTing Groovy to the script console. Handles basic auth (user + API token)
and CSRF crumbs for instances that still require them.
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class JenkinsError(RuntimeError):
    """Raised when Jenkins returns an error or an unexpected payload."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class JenkinsClient:
    def __init__(
        self,
        url: str,
        user: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        ca_bundle: str | None = None,
    ):
        if not url:
            raise ValueError("Jenkins URL is required (use --url or JENKINS_URL)")
        self.base_url = url.rstrip("/") + "/"
        self.timeout = timeout
        self._auth_header: str | None = None
        if user and token:
            raw = f"{user}:{token}".encode()
            self._auth_header = "Basic " + base64.b64encode(raw).decode("ascii")
        elif user or token:
            raise ValueError("--user and --token must be given together")

        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        ]
        if ca_bundle:
            ctx = ssl.create_default_context(cafile=ca_bundle)
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)
        self._crumb: tuple[str, str] | None = None
        self._crumb_fetched = False

    # ------------------------------------------------------------------ HTTP
    def url_for(self, path: str) -> str:
        return urllib.parse.urljoin(self.base_url, path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        url = self.url_for(path)
        shown = url  # error messages omit the (long, encoded) query string
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, method=method)
        if self._auth_header:
            req.add_header("Authorization", self._auth_header)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            hint = ""
            if exc.code in (401, 403):
                hint = " (check --user/--token and that the account has the needed permission)"
            raise JenkinsError(
                f"{method} {shown} -> HTTP {exc.code} {exc.reason}{hint}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise JenkinsError(f"{method} {shown} failed: {exc.reason}") from exc

    def get_json(self, path: str, tree: str | None = None, depth: int | None = None) -> Any:
        if not path.rstrip("/").endswith("api/json"):
            path = path.rstrip("/") + "/api/json"
        params: dict[str, str] = {}
        if tree:
            params["tree"] = tree
        if depth is not None:
            params["depth"] = str(depth)
        body = self._request("GET", path, params=params or None)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise JenkinsError(f"GET {path}: response is not JSON") from exc

    # ----------------------------------------------------------------- CSRF
    def _get_crumb(self) -> tuple[str, str] | None:
        if self._crumb_fetched:
            return self._crumb
        self._crumb_fetched = True
        try:
            data = self.get_json("crumbIssuer/api/json")
            self._crumb = (data["crumbRequestField"], data["crumb"])
        except (JenkinsError, KeyError, TypeError):
            # CSRF protection disabled, or API-token auth (which is exempt).
            self._crumb = None
        return self._crumb

    def post_form(self, path: str, form: dict[str, str]) -> bytes:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        crumb = self._get_crumb()
        if crumb:
            headers[crumb[0]] = crumb[1]
        data = urllib.parse.urlencode(form).encode()
        return self._request("POST", path, data=data, headers=headers)

    # ------------------------------------------------------- script console
    def run_script(self, groovy: str, computer_path: str | None = None) -> str:
        """Run Groovy in the script console.

        ``computer_path`` is the node's URL fragment (``computer/<name>/``);
        ``None`` runs on the controller. Requires Overall/Administer.
        """
        path = (computer_path.rstrip("/") + "/scriptText") if computer_path else "scriptText"
        return self.post_form(path, {"script": groovy}).decode("utf-8", errors="replace")
