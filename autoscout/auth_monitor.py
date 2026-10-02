"""Private, redacted live frames from a headless account login."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import time


log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AuthMonitor:
    def __init__(self, data_dir: Path, platform: str, show_input: bool = False):
        if platform not in {"sonkwo", "steampy"}:
            raise ValueError("未知平台")
        self.platform = platform
        self.show_input = show_input
        self.data_dir = data_dir
        self.status_path = data_dir / f"auth_{platform}_status.json"
        self.frame_path = data_dir / f"auth_{platform}_live.png"
        self.control_path = data_dir / f"auth_{platform}_control.sqlite3"
        self._page = None
        self._tasks: list[asyncio.Task] = []
        self._session = secrets.token_urlsafe(18)
        self._last_capture_warning = 0.0
        self.cancel_requested = asyncio.Event()
        self.continue_requested = asyncio.Event()
        self._state = {
            "platform": platform, "active": False, "stage": "not_started",
            "message": "", "started_at": None, "frame_at": None,
            "last_activity": None, "interactive": False,
            "stage_at": None,
            "session_id": self._session, "viewport": None,
            "phone_entered": False,
            "show_login_input": show_input,
        }

    def _write(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, self.status_path)

    def stage(self, stage: str, message: str = "") -> None:
        self._state.update(stage=stage, message=message, stage_at=_now(), last_activity=_now())
        self._write()

    def phone_filled(self) -> None:
        self._state.update(phone_entered=True, last_activity=_now())
        self._write()

    async def start(self, page) -> None:
        self._page = page
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.frame_path.unlink(missing_ok=True)
        with sqlite3.connect(self.control_path, timeout=2) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS events ("
                               "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                               "session TEXT NOT NULL, action TEXT NOT NULL, "
                               "x INTEGER NOT NULL, y INTEGER NOT NULL)")
            connection.execute("DELETE FROM events")
        self._state.update(active=True, stage="checking_session", message="检查现有会话",
                           started_at=_now(), frame_at=None, stage_at=_now(), last_activity=_now(),
                           interactive=True, viewport=page.viewport_size)
        self._write()
        self._tasks = [asyncio.create_task(self._loop()),
                       asyncio.create_task(self._control_loop())]

    async def _capture(self, path: Path, mask_inputs: bool = True) -> None:
        options = {"path": str(path), "timeout": 15000, "animations": "disabled"}
        if mask_inputs:
            options.update(mask=[self._page.locator("input:not([type='checkbox'])")],
                           mask_color="#10151c")
        await self._page.screenshot(**options)

    async def capture_diagnostic(self) -> Path | None:
        path = self.data_dir / f"auth_{self.platform}_diagnostic.png"
        try:
            await self._capture(path)
            return path
        except Exception:
            log.exception("无法保存 %s 登录诊断画面", self.platform)
            return None

    async def _loop(self) -> None:
        temporary = self.frame_path.with_suffix(".png.tmp")
        while True:
            try:
                if self._state["stage"] in {"checking_session", "opening_login"}:
                    await asyncio.sleep(0.2)
                    continue
                await self._capture(temporary, mask_inputs=not self.show_input)
                os.replace(temporary, self.frame_path)
                self._state.update(frame_at=_now(), last_activity=_now())
                self._write()
            except asyncio.CancelledError:
                raise
            except Exception:
                if time.monotonic() - self._last_capture_warning >= 30:
                    log.warning("%s 登录画面暂时不可用", self.platform, exc_info=True)
                    self._last_capture_warning = time.monotonic()
            await asyncio.sleep(0.5)

    async def _control_loop(self) -> None:
        while True:
            try:
                with sqlite3.connect(self.control_path, timeout=2) as connection:
                    events = connection.execute(
                        "SELECT id, action, x, y FROM events WHERE session = ? "
                        "ORDER BY id LIMIT 100", (self._session,)
                    ).fetchall()
                    if events:
                        connection.execute("DELETE FROM events WHERE id <= ?", (events[-1][0],))
                for _, action, x, y in events:
                    if action == "move":
                        await self._page.mouse.move(x, y)
                    elif action == "down":
                        await self._page.mouse.move(x, y)
                        await self._page.mouse.down()
                    elif action == "up":
                        await self._page.mouse.move(x, y)
                        await self._page.mouse.up()
                    elif action == "cancel":
                        self.cancel_requested.set()
                        self.stage("cancelling", "正在取消登录并关闭无头浏览器")
                    elif action == "continue":
                        self.continue_requested.set()
                        self.stage("challenge_confirmed", "已收到拼图完成确认，正在准备输入短信验证码")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("%s 登录画面操作暂时不可用", self.platform, exc_info=True)
            await asyncio.sleep(0.05)

    async def stop(self, success: bool, message: str) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        if self.show_input:
            # A debug frame can contain typed values. Replace it with a masked
            # final frame, or remove it if the page can no longer be captured.
            temporary = self.frame_path.with_suffix(".png.tmp")
            try:
                await self._capture(temporary)
                os.replace(temporary, self.frame_path)
                self._state["frame_at"] = _now()
            except Exception:
                temporary.unlink(missing_ok=True)
                self.frame_path.unlink(missing_ok=True)
                self._state["frame_at"] = None
        self._state.update(active=False, stage="completed" if success else "failed",
                           message=message, stage_at=_now(), last_activity=_now(), interactive=False)
        self._write()


def request_login_cancel(data_dir: Path, platform: str) -> bool:
    """Signal a running login from a second terminal without touching its browser profile."""
    if platform not in {"sonkwo", "steampy"}:
        raise ValueError("未知平台")
    status_path = data_dir / f"auth_{platform}_status.json"
    try:
        state = json.loads(status_path.read_text(encoding="utf-8"))
        if not state.get("active"):
            return False
        control_path = data_dir / f"auth_{platform}_control.sqlite3"
        with sqlite3.connect(control_path, timeout=2) as connection:
            connection.execute("INSERT INTO events (session, action, x, y) "
                               "VALUES (?, 'cancel', 0, 0)", (state["session_id"],))
        return True
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return False
