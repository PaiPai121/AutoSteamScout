"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import json
import secrets
import socket
import sys

import uvicorn

from .auth import login
from .auth_monitor import request_login_cancel
from .browser import browser_sources
from .orders import OrderSyncService
from .payouts import PayoutSyncService, inspect_payout_navigation
from .ports import SourceError
from .repository import Repository
from .scanner import ScanService
from .settings import Settings
from .titles import TitleCatalog
from .web import create_app


def setup_logging(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        settings.data_dir / "autoscout.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), handler],
    )


async def scan_once(settings: Settings, keyword: str, pages: int) -> int:
    repo = Repository(settings.database_path)
    catalog = TitleCatalog.from_file(settings.aliases_path)
    service = ScanService(settings, repo, catalog, lambda: browser_sources(settings, catalog, repo.product_mapping))
    run_id = service.start(keyword, pages)
    print(f"扫描已开始：{run_id}", flush=True)
    waiting = asyncio.create_task(service.wait())
    while not waiting.done():
        await asyncio.wait({waiting}, timeout=10)
        if not waiting.done():
            progress = service.status()
            print(f"进行中：{progress['stage']}；已过 {progress['elapsed_seconds']} 秒；"
                  f"已核价 {progress['processed']} 项；最后活动 {progress['last_activity']}", flush=True)
    status = await waiting
    print(f"状态：{status['status']}；处理 {status['processed']} 项；机会 {status['opportunities']} 项", flush=True)
    if status["error"]:
        print(f"原因：{status['error']}", flush=True)
    print(f"结果数据库：{settings.database_path}", flush=True)
    return 0 if status["status"] in {"completed", "completed_with_warnings"} else 1


async def sync_orders_once(settings: Settings) -> int:
    repo = Repository(settings.database_path)
    service = OrderSyncService(settings, repo)
    service.start()
    print("正在读取杉果购买订单和 SteamPy 卖家订单…", flush=True)
    waiting = asyncio.create_task(service.wait())
    while not waiting.done():
        await asyncio.wait({waiting}, timeout=10)
        if not waiting.done():
            state = service.status()
            print(f"进行中：{state['stage']}；已过 {state['elapsed_seconds']} 秒；"
                  f"最后活动 {state['last_activity']}", flush=True)
    state = await waiting
    if state["status"] not in {"completed", "completed_with_warnings"}:
        print(f"订单同步未完成：{state['error']}；已保留上次完整统计。", file=sys.stderr, flush=True)
        return 1
    for warning in state.get("warnings", []):
        print(f"来源警告：{warning}", flush=True)
    summary = repo.account_order_summary()
    sonkwo, steampy = summary["sonkwo"], summary["steampy"]
    print(f"杉果：完成 {sonkwo['completed']} 单 / {sonkwo['units']} 件，合计实付 ¥{sonkwo['spent']}", flush=True)
    print(f"SteamPy：普通成功成交 {steampy['ordinary_sold']} 单，"
          f"求购成功成交 {steampy['request_sold']} 单；"
          f"普通成交原价 ¥{steampy['gross']}、已核实订单费用 ¥{steampy['fees']}，"
          f"全部成交扣费后收入 ¥{steampy['sale_net']}", flush=True)
    print(summary["profit_note"], flush=True)
    return 0


async def sync_payouts_once(settings: Settings) -> int:
    repo = Repository(settings.database_path)
    service = PayoutSyncService(settings, repo)
    service.start()
    print("正在读取 SteamPy 钱包完整流水与提现扣款…", flush=True)
    waiting = asyncio.create_task(service.wait())
    while not waiting.done():
        await asyncio.wait({waiting}, timeout=10)
        if not waiting.done():
            state = service.status()
            print(f"进行中：{state['stage']}；已过 {state['elapsed_seconds']} 秒；"
                  f"最后活动 {state['last_activity']}", flush=True)
    state = await waiting
    if state["status"] != "completed":
        print(f"钱包同步未完成：{state['error']}；已保留上次完整结果。", file=sys.stderr, flush=True)
        return 1
    summary = repo.wallet_payouts()
    print(f"钱包流水 {summary['wallet_bill_count']} 条；提现扣款 {summary['withdrawal_count']} 笔 / "
          f"¥{summary['wallet_debits']}；提现费用 ¥{summary['wallet_fees']}。", flush=True)
    print(f"尚待银行到账核对 {summary['awaiting_bank_check']} 笔；"
          "平台扣款流水不能单独证明银行卡到账。", flush=True)
    return 0


async def serve(settings: Settings, port: int, remote_debug: bool = False) -> None:
    password = (os.getenv("AUTOSCOUT_DEBUG_PASSWORD") or secrets.token_urlsafe(18)) if remote_debug else None
    if password is not None and len(password) < 8:
        raise ValueError("远程调试密码至少需要 8 个字符")
    app = create_app(settings, remote_password=password)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("0.0.0.0" if remote_debug else "127.0.0.1", port))
        listener.listen(128)
        selected_port = listener.getsockname()[1]
        print(f"本地控制台：http://127.0.0.1:{selected_port}", flush=True)
        if remote_debug:
            print(f"远程登录预览：http://<服务器局域网 IP>:{selected_port}/auth", flush=True)
            print(f"远程预览网页访问密码：{password}", flush=True)
            print("远程端口只允许登录画面；请只在可信网络使用，公网访问应通过 VPN 或 SSH 转发。", flush=True)
        config = uvicorn.Config(app, host="0.0.0.0" if remote_debug else "127.0.0.1", port=selected_port,
                               log_level="info", access_log=False)
        server = uvicorn.Server(config)
        await server.serve(sockets=[listener])


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AutoSteamScout 套现机会与资金台账")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="打开本地控制台")
    serve_parser.add_argument("--port", type=int, default=0, help="监听端口；0 表示自动选择")
    serve_parser.add_argument("--remote-debug", action="store_true",
                              help="允许其他设备用密码访问登录画面并手动操作验证")
    scan_parser = commands.add_parser("scan", help="运行一次只读扫描")
    scan_parser.add_argument("--keyword", default="")
    scan_parser.add_argument("--pages", type=int, default=1)
    commands.add_parser("sync-orders", help="只读同步已登录账号中的历史购买和销售订单")
    commands.add_parser("reconcile-orders", help="用最新商品名称规则重新配对已保存的订单，无需重新登录")
    commands.add_parser("sync-payouts", help="只读同步 SteamPy 钱包流水与提现扣款")
    commands.add_parser("inspect-payouts", help="只读检查 SteamPy 的钱包与提现记录入口")
    login_parser = commands.add_parser("login", help="在无头浏览器中通过终端短信验证保存账号会话")
    login_parser.add_argument("platform", choices=["sonkwo", "steampy"])
    login_parser.add_argument("--reuse-code", action="store_true", help="使用已收到的验证码，不重新请求短信")
    cancel_parser = commands.add_parser("cancel", help="从另一终端取消正在进行的无头登录")
    cancel_parser.add_argument("platform", choices=["sonkwo", "steampy"])
    args = parser.parse_args()
    settings = Settings.from_env()
    setup_logging(settings)
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            parser.error("端口必须在 0 到 65535 之间")
        asyncio.run(serve(settings, args.port, args.remote_debug))
        return 0
    if args.command == "scan":
        return asyncio.run(scan_once(settings, args.keyword, args.pages))
    if args.command == "sync-orders":
        return asyncio.run(sync_orders_once(settings))
    if args.command == "reconcile-orders":
        repo = Repository(settings.database_path)
        count = repo.reconcile_existing_account_orders(TitleCatalog.from_file(settings.aliases_path))
        matches = repo.account_reconciliations()
        print(f"已重新核对 {count} 笔销售订单；成功配对 {matches['matched_count']} 笔，"
              f"仍未配对 {matches['counts']['review'] + matches['counts']['unmatched']} 笔。", flush=True)
        return 0
    if args.command == "sync-payouts":
        return asyncio.run(sync_payouts_once(settings))
    if args.command == "inspect-payouts":
        try:
            print(json.dumps(asyncio.run(inspect_payout_navigation(settings)), ensure_ascii=False), flush=True)
            return 0
        except Exception as exc:
            print(f"提现入口检查未完成：{str(exc)[:200]}", file=sys.stderr, flush=True)
            return 1
    if args.command == "cancel":
        if request_login_cancel(settings.data_dir, args.platform):
            print(f"{args.platform}：取消请求已送达；登录进程会关闭无头浏览器。", flush=True)
            return 0
        print(f"{args.platform}：没有可取消的登录进程。", flush=True)
        return 1
    try:
        asyncio.run(login(args.platform, settings, request_sms=not args.reuse_code))
        return 0
    except SourceError as exc:
        print(f"登录未完成：{exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("登录已由 Ctrl+C 取消。", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
