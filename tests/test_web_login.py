import asyncio
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from playwright.async_api import expect

from autoscout import web_login
from autoscout.web_login import WebLoginController
from autoscout.settings import Settings
from autoscout.web import create_app
from autoscout.playwright_runtime import playwright_session


def _wait_stage(client, platform, stage):
    until = time.monotonic() + 3
    while time.monotonic() < until:
        state = client.get(f"/api/auth/{platform}").json()
        if state["stage"] == stage:
            return state
        time.sleep(0.02)
    raise AssertionError(f"登录未进入 {stage}: {state}")


def test_web_login_accepts_phone_then_code_without_terminal_or_saved_secrets(
        tmp_path, monkeypatch):
    supplied = []

    async def fake_login(platform, settings, request_sms, input_reader):
        assert platform == "sonkwo" and request_sms is True
        path = settings.data_dir / "auth_sonkwo_status.json"
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        cancel = asyncio.Event()

        def stage(name, active=True):
            path.write_text(json.dumps({"platform": platform, "active": active,
                                        "stage": name, "message": name,
                                        "frame_at": None, "phone_entered": name != "awaiting_phone"}),
                            encoding="utf-8")

        stage("awaiting_phone")
        supplied.append(await input_reader("phone", 2, cancel))
        stage("awaiting_code")
        supplied.append(await input_reader("code", 2, cancel))
        stage("completed", False)

    monkeypatch.setattr(web_login, "login", fake_login)
    settings = Settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        token = client.get("/api/session").json()["token"]
        headers = {"X-Scout-Token": token}
        assert client.post("/api/auth/sonkwo/start", json={}).status_code == 403
        assert client.post("/api/auth/sonkwo/input",
                           json={"kind": "phone", "value": "123"}).status_code == 403
        assert client.post("/api/auth/sonkwo/start", headers=headers,
                           json={}).status_code == 202
        _wait_stage(client, "sonkwo", "awaiting_phone")
        assert client.post("/api/auth/steampy/start", headers=headers,
                           json={}).status_code == 409
        assert client.post("/api/auth/sonkwo/input", headers=headers,
                           json={"kind": "code", "value": "123456"}).status_code == 409
        assert client.post("/api/auth/sonkwo/input", headers=headers,
                           json={"kind": "phone", "value": "123"}).status_code == 422
        assert client.post("/api/auth/sonkwo/input", headers=headers,
                           json={"kind": "phone", "value": "13800000000"}).status_code == 202
        _wait_stage(client, "sonkwo", "awaiting_code")
        assert client.post("/api/auth/sonkwo/input", headers=headers,
                           json={"kind": "code", "value": "bad"}).status_code == 422
        assert client.post("/api/auth/sonkwo/input", headers=headers,
                           json={"kind": "code", "value": "123456"}).status_code == 202
        _wait_stage(client, "sonkwo", "completed")
        assert supplied == ["13800000000", "123456"]
        saved = (tmp_path / "auth_sonkwo_status.json").read_text(encoding="utf-8")
        assert "13800000000" not in saved and "123456" not in saved


@pytest.mark.asyncio
async def test_auth_page_completes_web_input_flow(tmp_path):
    static = Path(__file__).resolve().parents[1] / "autoscout" / "static"
    stage = "not_started"

    async def respond(route):
        nonlocal stage
        path = route.request.url.removeprefix("http://testserver")
        if path == "/auth":
            await route.fulfill(body=(static / "auth.html").read_bytes(),
                                content_type="text/html")
        elif path == "/auth.js":
            await route.fulfill(body=(static / "auth.js").read_bytes(),
                                content_type="text/javascript")
        elif path == "/style.css":
            await route.fulfill(body=(static / "style.css").read_bytes(),
                                content_type="text/css")
        elif path == "/api/session":
            await route.fulfill(json={"token": "test-token"})
        elif path == "/api/auth/sonkwo":
            await route.fulfill(json={"platform": "sonkwo", "stage": stage,
                                      "active": stage in {"awaiting_phone", "awaiting_code"},
                                      "message": stage, "frame_at": None,
                                      "phone_entered": stage != "awaiting_phone"})
        elif path == "/api/auth/steampy":
            await route.fulfill(json={"platform": "steampy", "stage": "not_started",
                                      "active": False, "message": "", "frame_at": None})
        elif path == "/api/auth/sonkwo/start":
            stage = "awaiting_phone"
            await route.fulfill(status=202, json={"started": True})
        elif path == "/api/auth/sonkwo/input":
            kind = json.loads(route.request.post_data)["kind"]
            stage = "awaiting_code" if kind == "phone" else "completed"
            await route.fulfill(status=202, json={"accepted": True})
        else:
            await route.fulfill(status=404)

    async with playwright_session(tmp_path) as playwright:
        try:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:
            pytest.skip(f"Chrome not available: {exc}")
        try:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("http://testserver/**", respond)
            await page.goto("http://testserver/auth")
            await page.locator("#sonkwo-start").click()
            await expect(page.locator("#sonkwo-phone-form")).to_be_visible()
            await page.locator("#sonkwo-phone-input").fill("13800000000")
            await page.locator("#sonkwo-phone-form button").click()
            await expect(page.locator("#sonkwo-code-form")).to_be_visible()
            await page.locator("#sonkwo-code-input").fill("123456")
            await page.locator("#sonkwo-code-form button").click()
            await expect(page.locator("#sonkwo-state")).to_have_text("最近一次登录成功")
            assert stage == "completed"
            assert not errors, errors
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_enabled", [False, True])
async def test_web_login_restores_previous_scan_schedule(tmp_path, monkeypatch,
                                                          previously_enabled):
    class FakeCruise:
        def __init__(self):
            self.enabled = previously_enabled
            self.pauses = 0
            self.resumes = 0

        def status(self):
            return {"enabled": self.enabled}

        async def pause(self):
            self.enabled = False
            self.pauses += 1

        def resume(self):
            self.enabled = True
            self.resumes += 1

    class FakeService:
        async def cancel(self):
            return False

    async def fake_login(*_args, **_kwargs):
        return None

    monkeypatch.setattr(web_login, "login", fake_login)
    cruise = FakeCruise()
    service = FakeService()
    controller = WebLoginController(Settings(tmp_path), service, service, service, cruise)
    await controller.start("sonkwo")
    await controller._task
    assert cruise.pauses == 1
    assert cruise.resumes == int(previously_enabled)
    assert cruise.enabled is previously_enabled
