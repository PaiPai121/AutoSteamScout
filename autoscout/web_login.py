"""Coordinate a headless account login started from the protected web page."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re

from .auth import login
from .ports import SourceError
from .settings import Settings


log = logging.getLogger(__name__)


class LoginInputInbox:
    """Keep phone and SMS values in memory only until the browser consumes them."""

    def __init__(self) -> None:
        self._queues = {"phone": asyncio.Queue(maxsize=1), "code": asyncio.Queue(maxsize=1)}

    def submit(self, kind: str, value: str) -> None:
        if kind == "phone" and not re.fullmatch(r"1\d{10}", value):
            raise ValueError("请输入 11 位中国大陆手机号")
        if kind == "code" and not re.fullmatch(r"\d{4,8}", value):
            raise ValueError("请输入 4 至 8 位短信验证码")
        if kind not in self._queues:
            raise ValueError("未知登录输入")
        try:
            self._queues[kind].put_nowait(value)
        except asyncio.QueueFull as exc:
            raise SourceError("输入已送达，正在等待浏览器处理") from exc

    async def read(self, kind: str, timeout: float, cancel: asyncio.Event) -> str:
        get_input = asyncio.create_task(self._queues[kind].get())
        cancelled = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait({get_input, cancelled}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancel.is_set():
                raise SourceError("登录已取消")
            if get_input in done:
                return get_input.result()
            raise SourceError("网页输入等待超时；请在登录页重新开始")
        finally:
            for task in (get_input, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(get_input, cancelled, return_exceptions=True)


class WebLoginController:
    def __init__(self, settings: Settings, scanner, order_sync, payout_sync, cruise):
        self.settings = settings
        self.scanner = scanner
        self.order_sync = order_sync
        self.payout_sync = payout_sync
        self.cruise = cruise
        self._task: asyncio.Task | None = None
        self._platform: str | None = None
        self._inbox: LoginInputInbox | None = None
        self._resume_cruise = False
        self._closing = False
        self._lock = asyncio.Lock()

    def _outside_login_active(self, platform: str) -> bool:
        path = self.settings.data_dir / f"auth_{platform}_status.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not state.get("active"):
                return False
            recent = state.get("frame_at") or state.get("last_activity")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(recent)).total_seconds()
            return age < 30
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    async def start(self, platform: str, reuse_code: bool = False) -> None:
        if platform not in {"sonkwo", "steampy"}:
            raise ValueError("未知平台")
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise SourceError("另一项网页登录正在进行，请先完成或取消")
            if any(self._outside_login_active(name) for name in ("sonkwo", "steampy")):
                raise SourceError("已有登录画面正在运行，请先完成或取消")
            self._resume_cruise = self.cruise.status()["enabled"]
            await self.cruise.pause()
            await self.scanner.cancel()
            await self.order_sync.cancel()
            await self.payout_sync.cancel()
            self._platform = platform
            self._inbox = LoginInputInbox()
            self._task = asyncio.create_task(self._run(platform, self._inbox, reuse_code))

    async def _run(self, platform: str, inbox: LoginInputInbox, reuse_code: bool) -> None:
        try:
            await login(platform, self.settings, request_sms=not reuse_code,
                        input_reader=inbox.read)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("%s 网页登录未完成：%s", platform, exc)
        finally:
            self._inbox = None
            self._platform = None
            if self._resume_cruise and not self._closing:
                self.cruise.resume()

    def submit(self, platform: str, kind: str, value: str) -> None:
        if self._task is None or self._task.done() or self._platform != platform or self._inbox is None:
            raise SourceError("当前没有正在等待输入的网页登录")
        path = self.settings.data_dir / f"auth_{platform}_status.json"
        try:
            stage = json.loads(path.read_text(encoding="utf-8"))["stage"]
        except (OSError, ValueError, KeyError) as exc:
            raise SourceError("登录画面尚未准备好，请稍后重试") from exc
        if stage != f"awaiting_{kind}":
            raise SourceError("当前登录步骤不接收这项输入，请刷新状态")
        self._inbox.submit(kind, value)

    async def shutdown(self) -> None:
        self._closing = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
