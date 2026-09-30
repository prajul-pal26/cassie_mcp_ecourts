"""Proxy-pool admin — inspect and add Webshare proxies at runtime.

All 6 eCourts case-details searches route their upstream calls through this
pool (round-robin across the proxy lanes), so adding proxies here raises the
throughput of every search immediately.

Gated by header `X-Admin-Token` (must equal env WARM_TOKEN when set).

  GET  /api/admin/proxies   -> current pool: count + full CLI URLs + rate cap
  POST /api/admin/proxies   -> add a proxy (curl cmd / url / user:pass@host:port),
                               validates it live, persists it, rebuilds lanes
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from app.routers.common import json_response
from app.upstream import proxy_pool

log = logging.getLogger("routers.proxies")

router = APIRouter(tags=["eCourts · Proxies (admin)"])

# Declared so Swagger /docs shows an "Authorize" button (lock icon) where you
# paste the token once, and it's sent as the X-Admin-Token header.
_admin_key = APIKeyHeader(name="X-Admin-Token", auto_error=False)


def _authed(token) -> bool:
    expected = os.getenv("WARM_TOKEN")
    if not expected:
        return True  # no token configured → dev-open
    return token == expected


class AddProxyReq(BaseModel):
    proxy: str = Field(
        'curl --proxy "http://user-1:pass@p.webshare.io:80/" https://ipv4.webshare.io/',
        description="A full curl command, a proxy URL, or user:pass@host:port")


@router.get("/api/admin/proxies",
            summary="List all proxies in the pool (count + CLI URLs + rate cap)")
async def list_proxies(token: str = Depends(_admin_key)):
    if not _authed(token):
        return json_response({"error": "unauthorized"}, 401)
    return json_response(proxy_pool.summary(reveal=True))


@router.post("/api/admin/proxies",
             summary="Add a proxy (curl/url/user:pass@host) — validated live, then used")
async def add_proxy(body: AddProxyReq, token: str = Depends(_admin_key)):
    if not _authed(token):
        return json_response({"error": "unauthorized"}, 401)

    # 1) parse the input into a normalized proxy URL
    try:
        url = proxy_pool.parse_proxy_input(body.proxy)
    except Exception as e:
        return json_response({"success": False, "error": f"could not parse proxy: {e}"}, 400)

    # 2) validate it works live (fetch our exit IP through it)
    def _check():
        import warnings
        warnings.filterwarnings("ignore")
        try:
            from curl_cffi import requests as rq
            sess = rq.Session(impersonate="chrome")
        except Exception:
            import requests as rq
            sess = rq.Session()
        sess.verify = False
        r = sess.get("https://ipv4.webshare.io/",
                     proxies={"http": url, "https": url}, timeout=20)
        return r.status_code, (r.text or "").strip()

    try:
        code, ip = await run_in_threadpool(_check)
    except Exception as e:
        return json_response({
            "success": False,
            "error": f"proxy did not work: {str(e)[:120]}",
            "proxy": url,
        }, 400)
    if code != 200 or not ip:
        return json_response({
            "success": False,
            "error": f"proxy check failed (HTTP {code})",
            "proxy": url,
        }, 400)

    # 3) persist + rebuild lanes so all searches pick it up
    proxy_pool.add_proxy(url)
    from app.upstream.bridge import reset_lanes
    reset_lanes()
    log.info("proxy added, exit_ip=%s, pool=%d", ip, proxy_pool.summary()["total_proxies"])

    return json_response({
        "success": True,
        "added": url,
        "exit_ip": ip,
        "total_proxies": proxy_pool.summary()["total_proxies"],
        "effective_rps_cap": proxy_pool.effective_rps_cap(0.0),
        "note": "Runtime-added proxies live in this instance's data dir; for "
                "permanence across redeploys add the account to WEBSHARE_PROXY_AUTH_N.",
    })
