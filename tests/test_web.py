from decimal import Decimal
from contextlib import asynccontextmanager
import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest
from httpx import ASGITransport, AsyncClient

from autoscout.domain import Assessment, MarketLookup, MarketQuote, Offer, Verdict
from autoscout.ports import FetchBatch, Sources
from autoscout.repository import Repository
from autoscout.scanner import ScanService
from autoscout.settings import Settings
from autoscout.titles import TitleCatalog
from autoscout.web import create_app
from autoscout import web_login


def test_local_api_requires_token_and_records_actual_cash(tmp_path):
    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    repo.begin_run("run-1", "")
    assessment_id = repo.save_assessment(
        "run-1",
        Assessment(Offer("Game", "https://www.sonkwo.cn/store/1", Decimal("10")),
                   Verdict.NEEDS_REVIEW, "人工核对"),
    )
    app = create_app(settings, repo)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/api/trades", json={"assessment_id": assessment_id, "actual_cost": "10"}).status_code == 403
        token = client.get("/api/session").json()["token"]
        headers = {"X-Scout-Token": token}
        bought = client.post("/api/trades", headers=headers,
                             json={"assessment_id": assessment_id, "actual_cost": "10"})
        assert bought.status_code == 201
        trade_id = bought.json()["id"]
        assert client.post(f"/api/trades/{trade_id}/settled", headers=headers,
                           json={"amount": "15"}).status_code == 409
        assert client.get("/api/cash").json()["realized_profit"] == "0.00"


def test_auth_preview_and_pointer_are_local_and_token_guarded(tmp_path):
    settings = Settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/auth").status_code == 200
        assert client.get("/auth.js").status_code == 200
        assert client.get("/api/auth/sonkwo").json()["stage"] == "not_started"
        assert client.get("/api/auth/sonkwo/frame").status_code == 404
        assert client.get("/api/auth/unknown").status_code == 404

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        state = {"platform": "sonkwo", "active": True, "interactive": True,
                 "stage": "awaiting_challenge", "session_id": "test-session",
                 "viewport": {"width": 1280, "height": 720}, "frame_at": "now"}
        (settings.data_dir / "auth_sonkwo_status.json").write_text(json.dumps(state), encoding="utf-8")
        control = settings.data_dir / "auth_sonkwo_control.sqlite3"
        with sqlite3.connect(control) as connection:
            connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                               "session TEXT, action TEXT, x INTEGER, y INTEGER)")
        public = client.get("/api/auth/sonkwo").json()
        assert public["stage"] == "awaiting_challenge"
        assert "session_id" not in public
        assert client.post("/api/auth/sonkwo/pointer", json={"action": "down", "x": 10, "y": 20}).status_code == 403
        token = client.get("/api/session").json()["token"]
        response = client.post("/api/auth/sonkwo/pointer", headers={"X-Scout-Token": token},
                               json={"action": "down", "x": 10, "y": 20})
        assert response.status_code == 200
        with sqlite3.connect(control) as connection:
            assert connection.execute("SELECT session, action, x, y FROM events").fetchone() == (
                "test-session", "down", 10, 20)
        cancelled = client.post("/api/auth/sonkwo/cancel", headers={"X-Scout-Token": token})
        assert cancelled.status_code == 200
        with sqlite3.connect(control) as connection:
            assert connection.execute("SELECT action FROM events ORDER BY id DESC LIMIT 1").fetchone() == ("cancel",)
        continued = client.post("/api/auth/sonkwo/continue", headers={"X-Scout-Token": token})
        assert continued.status_code == 200
        with sqlite3.connect(control) as connection:
            assert connection.execute("SELECT action FROM events ORDER BY id DESC LIMIT 1").fetchone() == ("continue",)


@pytest.mark.asyncio
async def test_remote_debug_exposes_only_password_protected_login_view(tmp_path, monkeypatch):
    async def fake_login(*_args, **_kwargs):
        return None

    monkeypatch.setattr(web_login, "login", fake_login)
    settings = Settings(tmp_path)
    app = create_app(settings, remote_password="12345678")
    remote = ASGITransport(app=app, client=("192.168.1.20", 5000))
    async with AsyncClient(transport=remote, base_url="http://192.168.1.10") as client:
        challenge = await client.get("/auth")
        assert challenge.status_code == 200
        assert "输入访问密码" in challenge.text
        assert (await client.get("/api/auth/sonkwo")).status_code == 401
        assert (await client.post("/api/auth/sonkwo/start", json={})).status_code == 401
        wrong = await client.post("/debug/login", data={"password": "bad"})
        assert wrong.status_code == 303 and "error=1" in wrong.headers["location"]
        non_ascii = await client.post("/debug/login", data={"password": "错误密码"})
        assert non_ascii.status_code == 303 and "error=1" in non_ascii.headers["location"]
        signed_in = await client.post("/debug/login", data={"password": "12345678"})
        assert signed_in.status_code == 303
        assert "httponly" in signed_in.headers["set-cookie"].lower()
        assert (await client.get("/auth")).status_code == 200
        assert "在网页完成账号登录" in (await client.get("/auth")).text
        assert (await client.get("/api/auth/sonkwo")).status_code == 200
        session = await client.get("/api/session")
        assert session.status_code == 200
        assert (await client.post("/api/auth/sonkwo/input",
                                  json={"kind": "code", "value": "123456"})).status_code == 403
        token = session.json()["token"]
        assert (await client.post("/api/auth/sonkwo/start", json={},
                                  headers={"X-Scout-Token": token})).status_code == 202
        await asyncio.sleep(0)
        assert (await client.get("/api/statistics")).status_code == 403
        assert (await client.get("/")).status_code == 403
    local = ASGITransport(app=app, client=("127.0.0.1", 5001))
    async with AsyncClient(transport=local, base_url="http://127.0.0.1") as client:
        assert (await client.get("/")).status_code == 200


@pytest.mark.asyncio
async def test_api_scan_runs_and_results_are_retrievable(tmp_path):
    class Offers:
        async def list_offers(self, keyword, page, status):
            return FetchBatch((Offer("Game", "https://www.sonkwo.cn/store/1", Decimal("50")),))

    class Quotes:
        async def lookup(self, offer):
            return MarketLookup(MarketQuote("Game", (Decimal("80"),)))

    @asynccontextmanager
    async def sources():
        yield Sources(Offers(), Quotes())

    settings = Settings(tmp_path)
    repo = Repository(settings.database_path)
    service = ScanService(settings, repo, TitleCatalog(), sources)
    app = create_app(settings, repo, service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        token = (await client.get("/api/session")).json()["token"]
        response = await client.post("/api/scans", headers={"X-Scout-Token": token},
                                     json={"keyword": "Game", "pages": 1})
        assert response.status_code == 202
        await service.wait()
        status = (await client.get("/api/status")).json()
        results = (await client.get("/api/assessments")).json()
        assert status["status"] == "completed"
        assert len(results) == 1
        assert results[0]["verdict"] == "price_only"
