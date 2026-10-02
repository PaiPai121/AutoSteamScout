"""Expired access credentials must refresh without sending another SMS."""

import json
import time

import pytest

from autoscout.ports import SourceError
from autoscout.session_restore import ensure_sonkwo_session


def credentials(token="expired-private-token", expiry=None, account="account-1"):
    return [{"name": name, "value": str(value), "domain": ".sonkwo.cn", "path": "/",
             "expires": time.time() + 31536000, "secure": True, "httpOnly": False, "sameSite": "Lax"}
            for name, value in {
                "access_token": token, "id": account, "refresh_token": "private-refresh",
                "access_token_expires_in": expiry if expiry is not None else int((time.time()-3600)*1000),
            }.items()]


class Response:
    def __init__(self, status, payload):
        self.status = status
        self.ok = status == 200
        self.payload = payload

    async def json(self):
        return self.payload


class Context:
    def __init__(self, cookies, *, refresh_rejected=False, account_status=200):
        self.items = cookies
        self.request = self
        self.refresh_calls = 0
        self.probe_calls = 0
        self.refresh_rejected = refresh_rejected
        self.account_status = account_status

    async def cookies(self, _origin):
        return self.items

    async def add_cookies(self, cookies):
        items = {item["name"]: item for item in self.items}
        items.update({item["name"]: item for item in cookies})
        self.items = list(items.values())

    async def get(self, url, **kwargs):
        assert url == "https://api.sonkwo.cn/auth/account/detail"
        self.probe_calls += 1
        if self.account_status != 200:
            return Response(self.account_status, {})
        if kwargs["headers"]["Authorization"] != "Bearer fresh-private-token":
            return Response(401, {"success": False})
        return Response(200, {"success": True, "data": {"id": "account-1"}})

    async def post(self, url, **kwargs):
        assert url == "https://api.sonkwo.cn/auth/session/login/refresh"
        assert kwargs["data"] == {"refreshToken": "private-refresh"}
        self.refresh_calls += 1
        if self.refresh_rejected:
            return Response(200, {"success": False, "errorCode": 4001})
        return Response(200, {"success": True, "data": {
            "token": "fresh-private-token", "refreshToken": "new-private-refresh",
            "createdAt": int(time.time()*1000), "expiresIn": 3600, "id": "account-1", "tokenType": "Bearer",
        }})

    async def storage_state(self, path):
        from pathlib import Path
        Path(path).write_text(json.dumps({"cookies": self.items, "origins": []}), encoding="utf-8")


@pytest.mark.asyncio
async def test_one_hour_access_expiry_refreshes_and_saves_verified_state(tmp_path, caplog):
    context = Context(credentials())
    stages = []
    assert await ensure_sonkwo_session(context, tmp_path, 30000, stages.append)
    assert context.refresh_calls == context.probe_calls == 1
    stored = json.loads((tmp_path / "storage_state.json").read_text(encoding="utf-8"))
    values = {c["name"]: c["value"] for c in stored["cookies"]}
    assert values["access_token"] == "fresh-private-token"
    assert values["refresh_token"] == "new-private-refresh"
    assert int(values["access_token_expires_in"]) > time.time()*1000
    assert "自动续期杉果会话" in stages
    assert "private-token" not in caplog.text and "private-refresh" not in caplog.text
    assert not (tmp_path / "storage_state.pending.json").exists()


@pytest.mark.asyncio
async def test_unexpected_401_also_refreshes_once(tmp_path):
    context = Context(credentials(expiry=int((time.time()+3600)*1000)))
    assert await ensure_sonkwo_session(context, tmp_path, 30000)
    assert context.refresh_calls == 1 and context.probe_calls == 2


@pytest.mark.asyncio
async def test_valid_current_session_is_not_replaced_by_an_older_snapshot(tmp_path):
    (tmp_path / "storage_state.json").write_text(json.dumps({"cookies": credentials()}), encoding="utf-8")
    context = Context(credentials("fresh-private-token", int((time.time()+3600)*1000)))
    assert await ensure_sonkwo_session(context, tmp_path, 30000)
    assert context.refresh_calls == 0


@pytest.mark.asyncio
async def test_missing_browser_cookies_can_restore_saved_snapshot(tmp_path):
    (tmp_path / "storage_state.json").write_text(json.dumps({"cookies": credentials()}), encoding="utf-8")
    context = Context([])
    assert await ensure_sonkwo_session(context, tmp_path, 30000)
    assert context.refresh_calls == 1


@pytest.mark.asyncio
async def test_rejected_refresh_requires_login_and_preserves_another_account_snapshot(tmp_path):
    snapshot = tmp_path / "storage_state.json"
    original = json.dumps({"cookies": credentials(account="different-account")})
    snapshot.write_text(original, encoding="utf-8")
    context = Context(credentials(), refresh_rejected=True)
    assert not await ensure_sonkwo_session(context, tmp_path, 30000)
    assert context.refresh_calls == 1
    assert snapshot.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_server_failure_is_not_reported_as_expired_login(tmp_path):
    snapshot = tmp_path / "storage_state.json"
    snapshot.write_text("original snapshot", encoding="utf-8")
    context = Context(credentials("fresh-private-token", int((time.time()+3600)*1000)), account_status=503)
    with pytest.raises(SourceError, match="暂不可用.*保留原会话"):
        await ensure_sonkwo_session(context, tmp_path, 30000)
    assert context.refresh_calls == 0
    assert snapshot.read_text(encoding="utf-8") == "original snapshot"


@pytest.mark.asyncio
async def test_rejected_refresh_does_not_discard_a_still_valid_access_token(tmp_path):
    context = Context(credentials("fresh-private-token", int((time.time()+30)*1000)), refresh_rejected=True)
    assert await ensure_sonkwo_session(context, tmp_path, 30000)
    assert context.refresh_calls == 1 and context.probe_calls == 1


@pytest.mark.asyncio
async def test_network_error_cannot_leak_request_headers_into_traceback(tmp_path):
    import traceback

    class BrokenContext(Context):
        async def get(self, *_args, **_kwargs):
            raise RuntimeError("Authorization: Bearer fresh-private-token")

    context = BrokenContext(credentials("fresh-private-token", int((time.time()+3600)*1000)))
    with pytest.raises(SourceError) as caught:
        await ensure_sonkwo_session(context, tmp_path, 30000)
    trace = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "fresh-private-token" not in trace
