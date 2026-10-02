import json

import pytest

from autoscout.session_restore import ensure_steampy_session


@pytest.mark.asyncio
async def test_steampy_profile_recovers_from_saved_login_snapshot(tmp_path):
    """Regression: seller and wallet pages redirected to /login after a saved login."""
    snapshot = tmp_path / "storage_state.json"
    snapshot.write_text(json.dumps({
        "cookies": [{"name": "userInfo", "value": "private-cookie",
                     "domain": "steampy.com", "path": "/"}],
        "origins": [{"origin": "https://steampy.com",
                     "localStorage": [{"name": "auth", "value": "private-state"}]}],
    }), encoding="utf-8")

    class Locator:
        async def wait_for(self, **_kwargs):
            return None

        @property
        def first(self):
            return self

    class Page:
        url = "https://steampy.com/login"
        visits = 0
        stored = None

        async def goto(self, url, **_kwargs):
            self.visits += 1
            self.url = "https://steampy.com/login" if self.visits == 1 else url

        async def evaluate(self, _script, pairs):
            self.stored = pairs

        def locator(self, selector):
            assert "卖家中心" in selector
            return Locator()

    class Context:
        cookies = None
        saved = None

        async def add_cookies(self, cookies):
            self.cookies = cookies

        async def storage_state(self, path):
            self.saved = path

    context = Context()
    page = Page()
    assert await ensure_steampy_session(context, page, tmp_path, 30000)
    assert page.visits == 2
    assert context.cookies[0]["name"] == "userInfo"
    assert page.stored == [("auth", "private-state")]
    assert context.saved == str(snapshot)


@pytest.mark.asyncio
async def test_steampy_expired_profile_without_snapshot_requires_web_login(tmp_path):
    class Page:
        url = "https://steampy.com/login"

        async def goto(self, *_args, **_kwargs):
            pass

    assert not await ensure_steampy_session(object(), Page(), tmp_path, 30000)


@pytest.mark.asyncio
async def test_valid_steampy_profile_does_not_replace_fresher_login(tmp_path):
    class Locator:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            pass

    class Page:
        url = "https://steampy.com/home"

        async def goto(self, *_args, **_kwargs):
            pass

        def locator(self, _selector):
            return Locator()

    class Context:
        async def add_cookies(self, _cookies):
            raise AssertionError("有效登录不应被旧快照覆盖")

    assert await ensure_steampy_session(Context(), Page(), tmp_path, 30000)
