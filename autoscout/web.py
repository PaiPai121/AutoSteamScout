"""Local dashboard and API. No trading action is automated by these routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal
import ipaddress
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from .browser import browser_sources
from .cruise import CruiseService
from .orders import OrderSyncService
from .payouts import PayoutSyncService
from .ports import SourceError
from .repository import InvalidTradeTransition, Repository
from .scanner import ScanBusy, ScanService
from .settings import Settings
from .titles import TitleCatalog
from .web_login import WebLoginController


STATIC_DIR = Path(__file__).parent / "static"


class ScanRequest(BaseModel):
    keyword: str = Field(default="", max_length=80)
    pages: int = Field(default=1, ge=1, le=20)


class PurchaseRequest(BaseModel):
    assessment_id: int = Field(gt=0)
    actual_cost: Decimal = Field(gt=0)
    reference: str = Field(default="", max_length=120)


class ProductMappingRequest(BaseModel):
    assessment_id: int = Field(gt=0)
    steampy_id: str = Field(pattern=r"^[1-9][0-9]*$", max_length=40)
    note: str = Field(default="", max_length=300)


class TradeActionRequest(BaseModel):
    amount: Decimal = Field(ge=0)
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    reference: str = Field(default="", max_length=120)


class BankReceiptRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    received_at: str = Field(min_length=10, max_length=10)
    note: str = Field(default="", max_length=120)


class PointerRequest(BaseModel):
    action: str
    x: int = Field(ge=0, le=4096)
    y: int = Field(ge=0, le=4096)


class LoginStartRequest(BaseModel):
    reuse_code: bool = False


class LoginInputRequest(BaseModel):
    kind: str
    value: str = Field(min_length=4, max_length=11)


def create_auth_router(settings: Settings, token: str,
                       web_login: WebLoginController) -> APIRouter:
    def require_token(x_scout_token: str | None = Header(default=None)) -> None:
        if not x_scout_token or not secrets.compare_digest(x_scout_token, token):
            raise HTTPException(403, "无效操作令牌，请刷新页面")

    router = APIRouter()

    @router.get("/style.css")
    def style():
        return FileResponse(STATIC_DIR / "style.css", media_type="text/css")

    @router.get("/auth")
    def auth_page():
        return FileResponse(STATIC_DIR / "auth.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @router.get("/auth.js")
    def auth_script():
        return FileResponse(STATIC_DIR / "auth.js", media_type="text/javascript")

    @router.get("/api/session")
    def session():
        return {"token": token}

    @router.get("/api/auth/{platform}")
    def auth_status(platform: str):
        if platform not in {"sonkwo", "steampy"}:
            raise HTTPException(404, "未知平台")
        path = settings.data_dir / f"auth_{platform}_status.json"
        if not path.exists():
            return {"platform": platform, "active": False, "stage": "not_started",
                    "message": "点击开始登录以检查会话", "started_at": None,
                    "frame_at": None, "last_activity": None}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            state.pop("session_id", None)
            return state
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(503, "登录状态暂不可读，请稍后刷新") from exc

    @router.get("/api/auth/{platform}/frame")
    def auth_frame(platform: str):
        if platform not in {"sonkwo", "steampy"}:
            raise HTTPException(404, "未知平台")
        path = settings.data_dir / f"auth_{platform}_live.png"
        if not path.is_file():
            raise HTTPException(404, "尚无登录画面")
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "no-store"})

    @router.post("/api/auth/{platform}/start", status_code=202,
                 dependencies=[Depends(require_token)])
    async def auth_start(platform: str, request: LoginStartRequest):
        if platform not in {"sonkwo", "steampy"}:
            raise HTTPException(404, "未知平台")
        try:
            await web_login.start(platform, request.reuse_code)
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"started": True, "platform": platform}

    @router.post("/api/auth/{platform}/input", status_code=202,
                 dependencies=[Depends(require_token)])
    async def auth_input(platform: str, request: LoginInputRequest):
        if platform not in {"sonkwo", "steampy"}:
            raise HTTPException(404, "未知平台")
        try:
            web_login.submit(platform, request.kind, request.value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": True}

    @router.post("/api/auth/{platform}/pointer", dependencies=[Depends(require_token)])
    def auth_pointer(platform: str, request: PointerRequest):
        if platform not in {"sonkwo", "steampy"}:
            raise HTTPException(404, "未知平台")
        if request.action not in {"move", "down", "up", "cancel", "continue"}:
            raise HTTPException(400, "未知鼠标操作")
        status_path = settings.data_dir / f"auth_{platform}_status.json"
        try:
            state = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(409, "登录尚未运行") from exc
        viewport = state.get("viewport") or {}
        if not state.get("active") or not state.get("interactive"):
            raise HTTPException(409, "登录画面不在运行")
        if request.action == "continue" and state.get("stage") != "awaiting_challenge":
            raise HTTPException(409, "当前无需确认拼图")
        if request.x >= viewport.get("width", 0) or request.y >= viewport.get("height", 0):
            raise HTTPException(400, "坐标超出登录画面")
        control_path = settings.data_dir / f"auth_{platform}_control.sqlite3"
        try:
            with sqlite3.connect(control_path, timeout=2) as connection:
                pending = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                if pending >= 300:
                    raise HTTPException(429, "鼠标操作积压，请稍后重试")
                connection.execute(
                    "INSERT INTO events (session, action, x, y) VALUES (?, ?, ?, ?)",
                    (state["session_id"], request.action, request.x, request.y),
                )
        except (OSError, sqlite3.Error, KeyError) as exc:
            raise HTTPException(503, "登录画面操作暂不可用") from exc
        return {"queued": True}

    @router.post("/api/auth/{platform}/cancel", dependencies=[Depends(require_token)])
    def auth_cancel(platform: str):
        return auth_pointer(platform, PointerRequest(action="cancel", x=0, y=0))

    @router.post("/api/auth/{platform}/continue", dependencies=[Depends(require_token)])
    def auth_continue(platform: str):
        return auth_pointer(platform, PointerRequest(action="continue", x=0, y=0))

    return router


def create_app(settings: Settings | None = None, repository: Repository | None = None,
               service: ScanService | None = None,
               remote_password: str | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    repository = repository or Repository(settings.database_path)
    repository.reprice_legacy_assessments(settings.policy)
    catalog = TitleCatalog.from_file(settings.aliases_path)
    repository.reconcile_existing_account_orders(catalog)
    service = service or ScanService(settings, repository, catalog,
                                    lambda: browser_sources(settings, catalog, repository.product_mapping))
    order_sync = OrderSyncService(settings, repository, service)
    payout_sync = PayoutSyncService(settings, repository, service, order_sync)
    cruise = CruiseService(settings, service, order_sync, payout_sync)
    web_login = WebLoginController(settings, service, order_sync, payout_sync, cruise)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.auto_scan:
            cruise.resume()
        try:
            yield
        finally:
            await web_login.shutdown()
            await cruise.pause()
            await order_sync.cancel()
            await payout_sync.cancel()

    token = secrets.token_urlsafe(32)
    debug_session = secrets.token_urlsafe(32) if remote_password else None
    app = FastAPI(title="AutoSteamScout", version="0.4.10", docs_url=None,
                  redoc_url=None, lifespan=lifespan)
    allowed_hosts = ["*"] if remote_password else ["127.0.0.1", "localhost", "testserver"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    if remote_password:
        @app.middleware("http")
        async def remote_debug_gate(request: Request, call_next):
            client_host = request.client.host if request.client else ""
            try:
                local = ipaddress.ip_address(client_host).is_loopback
            except ValueError:
                local = False
            if not local:
                path = request.url.path
                if path not in {"/auth", "/auth.js", "/style.css", "/api/session", "/debug/login"} \
                        and not path.startswith("/api/auth/"):
                    return Response(status_code=403)
                if path == "/style.css" or path == "/debug/login":
                    return await call_next(request)
                cookie = request.cookies.get("autoscout_debug", "")
                if not secrets.compare_digest(cookie.encode("utf-8"), debug_session.encode("utf-8")):
                    if path == "/auth" and request.method == "GET":
                        return FileResponse(STATIC_DIR / "debug_login.html", media_type="text/html",
                                            headers={"Cache-Control": "no-store"})
                    return Response(status_code=401)
            return await call_next(request)

        @app.post("/debug/login")
        async def debug_login(request: Request):
            body = await request.body()
            if len(body) > 4096:
                return Response(status_code=413)
            fields = parse_qs(body.decode("utf-8", errors="replace"))
            submitted = fields.get("password", [""])[0]
            if not secrets.compare_digest(submitted.encode("utf-8"), remote_password.encode("utf-8")):
                return RedirectResponse("/auth?error=1", status_code=303)
            response = RedirectResponse("/auth", status_code=303)
            response.set_cookie("autoscout_debug", debug_session, max_age=8 * 3600,
                                httponly=True, samesite="strict", path="/",
                                secure=request.url.scheme == "https")
            return response

    app.include_router(create_auth_router(settings, token, web_login))

    def require_token(x_scout_token: str | None = Header(default=None)) -> None:
        if not x_scout_token or not secrets.compare_digest(x_scout_token, token):
            raise HTTPException(403, "无效操作令牌，请刷新页面")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/app.js")
    def script():
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    @app.get("/api/config")
    def config():
        return {"sell_fee_rate": str(settings.fee_rate),
                "payout_fee_rate": str(settings.payout_fee_rate),
                "min_profit": str(settings.min_profit), "min_roi": str(settings.min_roi),
                "undercut": str(settings.undercut), "max_pages": settings.max_pages}

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/cruise")
    def cruise_status():
        return cruise.status()

    @app.post("/api/cruise/pause", dependencies=[Depends(require_token)])
    async def pause_cruise():
        await cruise.pause()
        return cruise.status()

    @app.post("/api/cruise/resume", dependencies=[Depends(require_token)])
    async def resume_cruise():
        cruise.resume()
        return cruise.status()

    @app.get("/api/statistics")
    def statistics():
        return repository.scan_statistics()

    @app.get("/api/account-orders/summary")
    def account_order_summary():
        return repository.account_order_summary()

    @app.get("/api/finance/overview")
    def financial_overview():
        return repository.financial_overview()

    @app.get("/api/account-orders/refunds")
    def refunded_purchases():
        return repository.refunded_purchases()

    @app.get("/api/account-orders/status")
    def account_order_status():
        return order_sync.status()

    @app.get("/api/account-orders/ledger")
    def account_order_ledger(platform: str = "all", state: str = "all", query: str = "",
                             page: int = 1, page_size: int = 50):
        try:
            return repository.account_order_ledger(
                platform=platform, state=state, query=query, page=page,
                page_size=page_size, catalog=catalog)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/account-orders/reconciliation")
    def account_order_reconciliation():
        return repository.account_reconciliations()

    @app.get("/api/account-orders/inventory")
    def purchase_inventory(state: str = "all", query: str = "",
                           page: int = 1, page_size: int = 30):
        try:
            return repository.purchase_inventory(
                state=state, query=query, page=page, page_size=page_size)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/payouts")
    def payouts():
        return repository.wallet_payouts()

    @app.get("/api/account-orders/turnover")
    def account_turnover():
        return repository.turnover_report(catalog)

    @app.get("/api/payouts/status")
    def payout_status():
        return payout_sync.status()

    @app.post("/api/payouts/sync", status_code=202,
              dependencies=[Depends(require_token)])
    async def start_payout_sync():
        try:
            payout_sync.start()
            return payout_sync.status()
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/payouts/{bill_id}/receipt", dependencies=[Depends(require_token)])
    def record_bank_receipt(bill_id: str, request: BankReceiptRequest):
        try:
            repository.record_bank_receipt(bill_id, request.amount,
                                           request.received_at, request.note)
            return repository.wallet_payouts()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/payouts/{bill_id}/receipt", dependencies=[Depends(require_token)])
    def clear_bank_receipt(bill_id: str):
        repository.clear_bank_receipt(bill_id)
        return repository.wallet_payouts()

    @app.get("/api/account-orders/{platform}")
    def account_orders(platform: str):
        try:
            return repository.account_orders(platform)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/account-orders/sync", status_code=202,
              dependencies=[Depends(require_token)])
    async def start_order_sync():
        try:
            order_sync.start()
            return order_sync.status()
        except SourceError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs")
    def runs():
        return repository.runs()

    @app.get("/api/assessments")
    def assessments(run_id: str | None = None):
        from .turnover import candidate_history
        results = repository.assessments(run_id)
        if results:
            history = repository.turnover_report(catalog, include_units=True)
            for result in results:
                matching = result.get("matching") or {}
                result["own_history"] = candidate_history(
                    matching.get("offer") if matching.get("accepted") else None,
                    matching.get("market"), history, catalog,
                    Decimal(result["buy_price"]), settings.policy)
        return results

    @app.get("/api/product-mappings")
    def product_mappings():
        return repository.product_mappings()

    @app.post("/api/product-mappings", status_code=201, dependencies=[Depends(require_token)])
    def confirm_product_mapping(request: ProductMappingRequest):
        try:
            return repository.confirm_product_mapping(request.assessment_id, request.steampy_id, request.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/product-mappings/{sonkwo_id}", dependencies=[Depends(require_token)])
    def clear_product_mapping(sonkwo_id: str):
        repository.clear_product_mapping(sonkwo_id)
        return {"cleared": True}

    @app.post("/api/scans", status_code=202, dependencies=[Depends(require_token)])
    async def start_scan(request: ScanRequest):
        if order_sync.status()["status"] == "running":
            raise HTTPException(409, "历史订单同步正在运行，请稍后再扫描")
        try:
            return {"run_id": service.start(request.keyword, request.pages)}
        except ScanBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/scans/cancel", dependencies=[Depends(require_token)])
    async def cancel_scan():
        return {"cancelled": await service.cancel()}

    @app.get("/api/cash")
    def cash():
        return repository.cash_report()

    @app.get("/api/trades")
    def trades():
        return repository.trades()

    @app.get("/api/trades/{trade_id}/events")
    def trade_events(trade_id: str):
        return repository.trade_events(trade_id)

    @app.post("/api/trades", status_code=201, dependencies=[Depends(require_token)])
    def purchase(request: PurchaseRequest):
        try:
            return repository.purchase(request.assessment_id, request.actual_cost, request.reference)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/trades/{trade_id}/{event}", dependencies=[Depends(require_token)])
    def transition(trade_id: str, event: str, request: TradeActionRequest):
        try:
            return repository.transition(trade_id, event, request.amount, request.fee, request.reference)
        except InvalidTradeTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
