"""Verify and recover marketplace sessions without unnecessary SMS logins."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeout

from .ports import SourceError


log = logging.getLogger(__name__)
STEAMPY_HOME = "https://steampy.com/home"
SELLER_MENU = "li.ivu-menu-submenu:has-text('卖家中心')"
SONKWO_API_ORIGIN = "https://api.sonkwo.cn"
SONKWO_HEADERS = {"Origin": "https://www.sonkwo.cn", "Referer": "https://www.sonkwo.cn/"}


async def _sonkwo_credentials(context) -> dict[str, str]:
    return {item["name"]: item["value"]
            for item in await context.cookies(SONKWO_API_ORIGIN)}


async def sonkwo_session_valid(context, timeout_ms: int) -> bool:
    """Verify the protected account API; a rendered header is not proof of login."""
    values = await _sonkwo_credentials(context)
    if not values.get("access_token") or not values.get("id"):
        return False
    try:
        response = await context.request.get(
            SONKWO_API_ORIGIN + "/auth/account/detail", params={"accountId": values["id"]},
            headers={**SONKWO_HEADERS, "Authorization": "Bearer " + values["access_token"]},
            timeout=timeout_ms)
        if response.status == 401:
            return False
        if not response.ok:
            raise SourceError(f"杉果账号检查暂不可用（HTTP {response.status}），保留原会话")
        payload = await response.json()
    except SourceError:
        raise
    except Exception:
        # Playwright request traces can include Authorization headers.
        raise SourceError("杉果账号检查连接失败，保留原会话；可稍后重试") from None
    if not isinstance(payload, dict):
        raise SourceError("杉果账号检查返回格式已改变，保留原会话")
    if payload.get("success") is not True:
        if str(payload.get("errorCode")) in {"401", "4001", "1011"}:
            return False
        raise SourceError("杉果账号检查未返回成功结果，保留原会话")
    account = payload.get("data")
    if not isinstance(account, dict) or str(account.get("id")) != values["id"]:
        raise SourceError("杉果账号检查结果与本地会话不一致，停止使用")
    return True


async def save_sonkwo_session(context, profile: Path) -> None:
    """Keep the latest verified credentials; replace the snapshot atomically."""
    profile.mkdir(parents=True, exist_ok=True)
    pending = profile / "storage_state.pending.json"
    await context.storage_state(path=str(pending))
    pending.replace(profile / "storage_state.json")


async def _restore_sonkwo_snapshot(context, profile: Path, current: dict[str, str]) -> bool:
    try:
        state = json.loads((profile / "storage_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        return False
    cookies = [item for item in state["cookies"] if isinstance(item, dict)
               and str(item.get("domain", "")).lstrip(".") in {"sonkwo.cn", "www.sonkwo.cn"}]
    values = {item.get("name"): item.get("value") for item in cookies}
    if not values.get("access_token") or not values.get("id"):
        return False
    if current.get("id") and values["id"] != current["id"]:
        return False
    if current.get("id") == values["id"] and (values.get("access_token"), values.get("refresh_token")) == (
            current.get("access_token"), current.get("refresh_token")):
        return False
    await context.add_cookies(cookies)
    log.info("杉果正在核验本机保存的会话快照")
    return True


async def _refresh_sonkwo_token(context, timeout_ms: int) -> bool:
    """Use the same official refresh endpoint and cookie contract as the site SDK."""
    cookies = await context.cookies(SONKWO_API_ORIGIN)
    values = {item["name"]: item["value"] for item in cookies}
    if not values.get("refresh_token") or not values.get("id"):
        return False
    try:
        response = await context.request.post(
            SONKWO_API_ORIGIN + "/auth/session/login/refresh",
            data={"refreshToken": values["refresh_token"]}, headers=SONKWO_HEADERS, timeout=timeout_ms)
        if response.status == 401:
            return False
        if not response.ok:
            raise SourceError(f"杉果会话续期暂不可用（HTTP {response.status}），保留原会话")
        payload = await response.json()
    except SourceError:
        raise
    except Exception:
        raise SourceError("杉果会话续期连接失败，保留原会话；可稍后重试") from None
    if not isinstance(payload, dict):
        raise SourceError("杉果会话续期返回格式已改变")
    if payload.get("success") is not True:
        if str(payload.get("errorCode")) in {"401", "4001", "1011"}:
            return False
        raise SourceError("杉果会话续期未返回成功结果，保留原会话")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
        raise SourceError("杉果会话续期字段已改变")
    try:
        expires_at = float(data["createdAt"]) + float(data["expiresIn"]) * 1000
        if not math.isfinite(expires_at) or expires_at <= time.time() * 1000:
            raise ValueError("invalid expiry")
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError("杉果续期有效时间字段无效") from exc
    replacements = {"access_token": data["token"], "access_token_expires_in": str(int(expires_at)),
                    "refresh_token": str(data.get("refreshToken") or values["refresh_token"]),
                    "token_type": str(data.get("tokenType") or "Bearer"),
                    "id": str(data.get("id") or values["id"])}
    existing = {item["name"]: item for item in cookies
                if str(item.get("domain", "")).lstrip(".") == "sonkwo.cn"}
    updated = []
    for name, value in replacements.items():
        item = dict(existing.get(name) or {"name": name, "domain": ".sonkwo.cn", "path": "/",
                    "expires": time.time() + 31536000, "httpOnly": False, "secure": True, "sameSite": "Lax"})
        item["value"] = value
        updated.append(item)
    await context.add_cookies(updated)
    log.info("杉果访问令牌已自动续期，正在验证账号")
    return True


async def ensure_sonkwo_session(context, profile: Path, timeout_ms: int,
                                progress: Callable[[str], None] | None = None) -> bool:
    """Recover a lost profile and refresh a short-lived access token before use.

    Only rejected credentials lead to web login. Network and server errors keep
    the saved session and produce a retryable source error. Secrets are never logged.
    """
    report = progress or (lambda _message: None)
    report("检查杉果账号会话")
    values = await _sonkwo_credentials(context)
    if not values.get("access_token") or not values.get("id"):
        report("核验杉果本机会话快照")
        await _restore_sonkwo_snapshot(context, profile, values)
        values = await _sonkwo_credentials(context)
    try:
        near_expiry = float(values.get("access_token_expires_in", "0")) <= time.time() * 1000 + 60000
    except (TypeError, ValueError):
        near_expiry = True
    if not near_expiry and await sonkwo_session_valid(context, timeout_ms):
        await save_sonkwo_session(context, profile)
        return True
    report("自动续期杉果会话")
    await _refresh_sonkwo_token(context, timeout_ms)
    if await sonkwo_session_valid(context, timeout_ms):
        await save_sonkwo_session(context, profile)
        return True
    # A verified snapshot may be newer than a damaged browser profile. Never
    # restore another account or replace a still-valid current session.
    if await _restore_sonkwo_snapshot(context, profile, await _sonkwo_credentials(context)):
        if await sonkwo_session_valid(context, timeout_ms) or (
                await _refresh_sonkwo_token(context, timeout_ms) and await sonkwo_session_valid(context, timeout_ms)):
            await save_sonkwo_session(context, profile)
            return True
    return False


async def ensure_steampy_session(context, page, profile: Path, timeout_ms: int) -> bool:
    """Open the seller account and restore its saved state if the profile was logged out.

    Credential values are used only in the browser; never include them in logs or
    status messages. A rejected snapshot remains intact for a fresh web login.
    """
    await page.goto(STEAMPY_HOME, wait_until="domcontentloaded", timeout=timeout_ms)
    if urlsplit(page.url).path != "/login":
        try:
            await page.locator(SELLER_MENU).first.wait_for(
                state="visible", timeout=min(timeout_ms, 10000))
            if urlsplit(page.url).path != "/login":
                return True
        except PlaywrightTimeout:
            if urlsplit(page.url).path != "/login":
                return False

    snapshot = profile / "storage_state.json"
    try:
        state = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(state, dict):
        return False
    cookies = [cookie for cookie in state.get("cookies", [])
               if isinstance(cookie, dict)
               and str(cookie.get("domain", "")).endswith("steampy.com")]
    origins = [origin for origin in state.get("origins", [])
               if isinstance(origin, dict)
               and origin.get("origin") == "https://steampy.com"]
    if not cookies and not origins:
        return False
    try:
        if cookies:
            await context.add_cookies(cookies)
        if origins and urlsplit(page.url).netloc == "steampy.com":
            pairs = [(item["name"], item["value"])
                     for item in origins[0].get("localStorage", [])
                     if isinstance(item, dict) and "name" in item and "value" in item]
            await page.evaluate(
                "pairs => { for (const [key, value] of pairs) localStorage.setItem(key, value) }",
                pairs,
            )
        await page.goto(STEAMPY_HOME, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.locator(SELLER_MENU).first.wait_for(
            state="visible", timeout=min(timeout_ms, 10000))
    except Exception as exc:
        log.warning("SteamPy 本机会话快照恢复未完成：%s", type(exc).__name__)
        return False
    if urlsplit(page.url).path == "/login":
        return False
    await context.storage_state(path=str(snapshot))
    log.info("SteamPy 浏览器配置已从本机登录快照恢复")
    return True
