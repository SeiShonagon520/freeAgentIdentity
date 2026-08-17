"""共享浏览器进程池 —— 无头批量注册的内存优化。

动机：每开一个独立 ``Camoufox()`` 都要付一份 Firefox 主进程 + 共享库的
基础成本（实测 PSS ~444MB 未开页 / ~710MB 加载 chatgpt.com）。共享池让
N 个并发注册复用少数几个浏览器进程，每个注册只开一个独立指纹的 context
（增量 ~295MB PSS），把每并发成本从 ~710MB 降到 ~434MB，约省 40%。

线程模型
--------
Playwright 的 sync Browser 对象绑定启动线程、不可跨线程复用（实测报
``Cannot switch to a different thread``），因此池用 **async API**：

* 一个专用线程跑一个 asyncio 事件循环，循环内预启动 ``pool_size`` 个
  ``AsyncCamoufox`` 浏览器。
* worker（任务线程）通过 ``asyncio.run_coroutine_threadsafe`` 把注册协程
  提交到该事件循环，多个协程并发执行、共享浏览器进程。
* OTP 轮询等同步阻塞调用在 async 流程里用 ``asyncio.to_thread`` 隔离，
  不阻塞事件循环。

并发控制
--------
* 全局 ``Semaphore(pool_size * max_contexts_per_browser)`` 限制总 context 数。
* 每个浏览器一个 ``Semaphore(max_contexts_per_browser)`` 限制单浏览器 context，
  既避免单进程内存爆，也把崩溃影响面限制在少数注册内。

生命周期
--------
模块级惰性单例（按 headless 区分），任务结束时由任务框架调用
``shutdown_shared_pool()`` 释放。
"""
from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable

from camoufox.async_api import AsyncCamoufox

from .browser_register_async import BrowserProxyBlockedError, register_in_context


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(str(os.environ.get(name, default) or default).strip())
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name, "1" if default else "0") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


_DEFAULT_POOL_SIZE = _env_int("BROWSER_POOL_SIZE", 4, minimum=1)
_DEFAULT_MAX_CONTEXTS = _env_int("BROWSER_POOL_MAX_CONTEXTS", 8, minimum=1)
_DEFAULT_CONTEXT_START_INTERVAL_MS = _env_int(
    "BROWSER_CONTEXT_START_INTERVAL_MS",
    175,
)
_DEFAULT_STARTUP_CONCURRENCY = _env_int(
    "BROWSER_POOL_STARTUP_CONCURRENCY",
    16,
    minimum=1,
)
_DEFAULT_BLOCK_IMAGES = _env_bool("CHATGPT_BROWSER_BLOCK_IMAGES", True)
_DEFAULT_REGISTER_TIMEOUT_SECONDS = _env_int(
    "BROWSER_REGISTER_TIMEOUT_SECONDS",
    600,
    minimum=60,
)
_DEFAULT_CONTEXT_CLOSE_TIMEOUT_SECONDS = _env_int(
    "BROWSER_CONTEXT_CLOSE_TIMEOUT_SECONDS",
    15,
    minimum=1,
)
_DEFAULT_BROWSER_RECYCLE_TIMEOUT_SECONDS = _env_int(
    "BROWSER_RECYCLE_TIMEOUT_SECONDS",
    45,
    minimum=5,
)

_locks: dict[str, threading.Lock] = {}
_pools: dict[str, "BrowserProcessPool"] = {}


def _pool_key(headless: bool) -> str:
    return "headless" if headless else "headed"


class BrowserRegistrationTimeoutError(RuntimeError):
    """The shared browser stopped responding before one registration settled."""


@dataclass(slots=True)
class _BrowserSlot:
    manager: Any
    browser: Any
    semaphore: asyncio.Semaphore
    recycle_lock: asyncio.Lock
    generation: int = 0


class BrowserProcessPool:
    """一个 headless 共享池：专用事件循环 + N 个 AsyncCamoufox 浏览器。"""

    def __init__(
        self,
        *,
        headless: bool,
        pool_size: int,
        max_contexts_per_browser: int,
        context_start_interval_ms: int = _DEFAULT_CONTEXT_START_INTERVAL_MS,
        startup_concurrency: int = _DEFAULT_STARTUP_CONCURRENCY,
        block_images: bool = _DEFAULT_BLOCK_IMAGES,
        registration_timeout_seconds: float = _DEFAULT_REGISTER_TIMEOUT_SECONDS,
        context_close_timeout_seconds: float = _DEFAULT_CONTEXT_CLOSE_TIMEOUT_SECONDS,
        browser_recycle_timeout_seconds: float = _DEFAULT_BROWSER_RECYCLE_TIMEOUT_SECONDS,
    ):
        self.headless = headless
        self.pool_size = max(int(pool_size or 1), 1)
        self.max_contexts = max(int(max_contexts_per_browser or 1), 1)
        self.capacity = self.pool_size * self.max_contexts
        self.context_start_interval = max(int(context_start_interval_ms or 0), 0) / 1000
        self.startup_concurrency = min(
            max(int(startup_concurrency or 1), 1),
            self.capacity,
        )
        self.block_images = bool(block_images and self.headless)
        self.registration_timeout = max(float(registration_timeout_seconds or 0), 0.1)
        self.context_close_timeout = max(float(context_close_timeout_seconds or 0), 0.1)
        self.browser_recycle_timeout = max(float(browser_recycle_timeout_seconds or 0), 0.1)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        # Keep the AsyncCamoufox manager alive alongside the Browser.  The
        # manager owns the Playwright driver process; retaining only Browser
        # makes browser.close() insufficient and leaks the driver/zombies.
        self._browsers: list[_BrowserSlot] = []
        self._global_sem: asyncio.Semaphore | None = None
        self._startup_sem: asyncio.Semaphore | None = None
        self._context_start_lock: asyncio.Lock | None = None
        self._next_context_start = 0.0
        self._init_error: BaseException | None = None
        self._closed = False
        # OTP polling and proxy rotation are blocking I/O.  asyncio's default
        # executor only has cpu_count + 4 workers (12 on the production host),
        # which leaves browser contexts idle once concurrency exceeds that.
        self._io_executor = ThreadPoolExecutor(
            max_workers=self.capacity,
            thread_name_prefix="browser-pool-io",
        )

        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"browser-pool-{_pool_key(headless)}",
            daemon=True,
        )
        self._thread.start()
        # 等待浏览器全部预启动完成（或失败）
        if not self._ready.wait(timeout=60 + self.pool_size * 30):
            raise RuntimeError("浏览器进程池启动超时")

    # ------------------------------------------------------------------ loop

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.set_default_executor(self._io_executor)
        try:
            self._loop.run_until_complete(self._async_init())
        except BaseException as exc:  # noqa: BLE001 - 初始化失败要回传给主线程
            self._init_error = exc
            self._ready.set()
            try:
                self._loop.run_until_complete(self._async_shutdown())
            finally:
                self._cancel_pending_tasks()
                self._loop.close()
                self._io_executor.shutdown(wait=False, cancel_futures=True)
            self._loop = None
            return
        self._ready.set()
        # 事件循环常驻，直到 shutdown
        self._loop.run_forever()
        try:
            self._loop.run_until_complete(self._async_shutdown())
        finally:
            self._cancel_pending_tasks()
            self._loop.close()
            self._io_executor.shutdown(wait=False, cancel_futures=True)
            self._loop = None

    def _cancel_pending_tasks(self) -> None:
        if self._loop is None:
            return
        pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )

    async def _async_init(self) -> None:
        self._global_sem = asyncio.Semaphore(self.capacity)
        # Initial navigation and login-form hydration are the CPU-heavy part
        # of registration.  Limit only that phase so 30 workers do not all
        # drive Firefox to 800% at once; OTP/network waits remain fully
        # concurrent after the entry form is submitted.
        self._startup_sem = asyncio.Semaphore(self.startup_concurrency)
        self._context_start_lock = asyncio.Lock()
        for _ in range(self.pool_size):
            manager, browser = await self._launch_browser()
            self._browsers.append(
                _BrowserSlot(
                    manager=manager,
                    browser=browser,
                    semaphore=asyncio.Semaphore(self.max_contexts),
                    recycle_lock=asyncio.Lock(),
                )
            )

    async def _launch_browser(self):
        manager = AsyncCamoufox(
            headless=self.headless,
            # Registration pages do not need visual assets.  Camoufox's
            # native blocker avoids downloading/decoding them without a
            # Python route callback on every request.
            block_images=self.block_images,
            enable_cache=False,
        )
        browser = await manager.__aenter__()
        return manager, browser

    async def _close_browser(self, manager, browser) -> None:
        if manager is None and browser is None:
            return
        try:
            if manager is not None:
                exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
                done, _ = await asyncio.wait(
                    {exit_task},
                    timeout=self.browser_recycle_timeout,
                )
                if done:
                    exit_task.result()
                    return
                exit_task.cancel()
        except Exception:
            pass
        try:
            if browser is not None:
                close_task = asyncio.create_task(browser.close())
                done, _ = await asyncio.wait(
                    {close_task},
                    timeout=self.context_close_timeout,
                )
                if done:
                    close_task.result()
                else:
                    close_task.cancel()
        except Exception:
            pass

    async def _recycle_browser_slot(
        self,
        slot: _BrowserSlot,
        *,
        expected_generation: int,
        log_fn: Callable[..., None],
    ) -> None:
        async with slot.recycle_lock:
            if slot.generation != expected_generation:
                return
            old_manager, old_browser = slot.manager, slot.browser
            slot.manager = None
            slot.browser = None
            slot.generation += 1
            await self._close_browser(old_manager, old_browser)
            if self._closed:
                return
            try:
                manager, browser = await asyncio.wait_for(
                    self._launch_browser(),
                    timeout=self.browser_recycle_timeout,
                )
            except Exception as exc:
                log_fn(f"共享浏览器进程重建失败: {exc}", level="error")
                return
            slot.manager = manager
            slot.browser = browser
            log_fn("卡死的共享浏览器进程已重建", level="warning")

    async def _async_shutdown(self) -> None:
        browsers, self._browsers = self._browsers, []
        for slot in reversed(browsers):
            async with slot.recycle_lock:
                manager, browser = slot.manager, slot.browser
                slot.manager = None
                slot.browser = None
                slot.generation += 1
                await self._close_browser(manager, browser)
        self._browsers.clear()

    # ------------------------------------------------------------- register

    async def _wait_for_context_start_slot(self) -> None:
        """Spread expensive page starts over a few seconds instead of one CPU spike."""
        if self.context_start_interval <= 0 or self._context_start_lock is None:
            return
        async with self._context_start_lock:
            loop = asyncio.get_running_loop()
            delay = self._next_context_start - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_context_start = loop.time() + self.context_start_interval

    def register(
        self,
        *,
        email: str,
        password: str,
        proxy: str | None,
        proxy_rotate_callback: Callable[[], str | None] | None = None,
        max_proxy_attempts: int = 6,
        otp_callback: Callable[[], str],
        bind_totp_2fa: bool = False,
        log_fn: Callable[..., None],
    ) -> dict:
        if self._closed:
            raise RuntimeError("浏览器进程池已关闭")
        if not self._ready.is_set() or self._loop is None:
            raise RuntimeError(f"浏览器进程池启动失败: {self._init_error}")
        if self._init_error is not None:
            raise RuntimeError(f"浏览器进程池启动失败: {self._init_error}")

        future = asyncio.run_coroutine_threadsafe(
            self._register_coro(
                email=email,
                password=password,
                proxy=proxy,
                proxy_rotate_callback=proxy_rotate_callback,
                max_proxy_attempts=max_proxy_attempts,
                otp_callback=otp_callback,
                bind_totp_2fa=bind_totp_2fa,
                log_fn=log_fn,
            ),
            self._loop,
        )
        sync_timeout = (
            self.registration_timeout
            + self.context_close_timeout
            + self.browser_recycle_timeout
            + 30
        )
        try:
            return future.result(timeout=sync_timeout)
        except FutureTimeoutError as exc:
            # ``concurrent.futures.TimeoutError`` is an alias of the built-in
            # TimeoutError.  A completed registration may legitimately raise
            # the latter for OTP polling; only treat it as an event-loop stall
            # when the future itself is still pending.
            if future.done():
                raise
            future.cancel()
            raise BrowserRegistrationTimeoutError(
                f"共享浏览器事件循环超过 {int(sync_timeout)} 秒未响应"
            ) from exc

    async def _register_coro(
        self,
        *,
        email,
        password,
        proxy,
        proxy_rotate_callback,
        max_proxy_attempts,
        otp_callback,
        bind_totp_2fa,
        log_fn,
    ) -> dict:
        state: dict[str, Any] = {}
        task = asyncio.create_task(
            self._register_with_retries(
                email=email,
                password=password,
                proxy=proxy,
                proxy_rotate_callback=proxy_rotate_callback,
                max_proxy_attempts=max_proxy_attempts,
                otp_callback=otp_callback,
                bind_totp_2fa=bind_totp_2fa,
                log_fn=log_fn,
                state=state,
            )
        )
        done, _ = await asyncio.wait({task}, timeout=self.registration_timeout)
        if done:
            return task.result()

        task.cancel()
        await asyncio.wait(
            {task},
            timeout=self.context_close_timeout + 5,
        )

        slot = state.get("slot")
        generation = state.get("generation")
        log_fn(
            f"浏览器注册超过 {int(self.registration_timeout)} 秒，"
            "正在取消 worker 并重建卡死浏览器",
            level="error",
        )
        if isinstance(slot, _BrowserSlot) and isinstance(generation, int):
            await self._recycle_browser_slot(
                slot,
                expected_generation=generation,
                log_fn=log_fn,
            )
        raise BrowserRegistrationTimeoutError(
            f"浏览器注册超过 {int(self.registration_timeout)} 秒，worker 已终止"
        )

    async def _register_with_retries(
        self,
        *,
        email,
        password,
        proxy,
        proxy_rotate_callback,
        max_proxy_attempts,
        otp_callback,
        bind_totp_2fa,
        log_fn,
        state: dict[str, Any],
    ) -> dict:
        async with self._global_sem:
            # 轮询找一个有剩余 context 配额的浏览器
            while True:
                if self._browsers and not any(
                    slot.browser is not None for slot in self._browsers
                ):
                    raise RuntimeError("共享浏览器池无可用进程，需要重启任务后重建")
                for slot in self._browsers:
                    if slot.browser is not None and not slot.semaphore.locked():
                        async with slot.semaphore:
                            browser = slot.browser
                            if browser is None:
                                continue
                            state["slot"] = slot
                            state["generation"] = slot.generation
                            attempts = max(int(max_proxy_attempts or 1), 1)
                            current_proxy = proxy
                            for attempt in range(1, attempts + 1):
                                try:
                                    await self._wait_for_context_start_slot()
                                    return await register_in_context(
                                        browser,
                                        email=email,
                                        password=password,
                                        proxy=current_proxy,
                                        otp_callback=otp_callback,
                                        bind_totp_2fa=bind_totp_2fa,
                                        log=log_fn,
                                        startup_gate=self._startup_sem,
                                        close_timeout_seconds=self.context_close_timeout,
                                    )
                                except BrowserProxyBlockedError as exc:
                                    if not callable(proxy_rotate_callback):
                                        raise
                                    if attempt >= attempts:
                                        # Feed the final bad route back to the
                                        # task-wide allocator too, so another
                                        # worker cannot immediately reuse it.
                                        try:
                                            await asyncio.to_thread(
                                                proxy_rotate_callback
                                            )
                                        except Exception:
                                            pass
                                        raise
                                    log_fn(
                                        f"代理线路被 ChatGPT 拒绝或不可用 "
                                        f"({attempt}/{attempts}): {exc}；正在换节点重试",
                                        level="warning",
                                    )
                                    try:
                                        rotated = await asyncio.to_thread(
                                            proxy_rotate_callback
                                        )
                                    except Exception as rotate_exc:
                                        raise BrowserProxyBlockedError(
                                            f"{exc}；代理轮换失败: {rotate_exc}"
                                        ) from rotate_exc
                                    current_proxy = str(rotated or current_proxy or "").strip() or None
                                    await asyncio.sleep(1.0)
                            raise RuntimeError("浏览器代理重试状态异常")
                await asyncio.sleep(0.2)

    # ------------------------------------------------------------ lifecycle

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._loop is not None and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._async_shutdown(), self._loop
                )
                future.result(timeout=90)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=90)


def get_shared_pool(
    headless: bool,
    *,
    pool_size: int = _DEFAULT_POOL_SIZE,
    max_contexts_per_browser: int = _DEFAULT_MAX_CONTEXTS,
) -> BrowserProcessPool:
    """返回（或创建）给定 headless 形态的共享池。"""
    key = _pool_key(headless)
    with _locks.setdefault(key, threading.Lock()):
        pool = _pools.get(key)
        if pool is None or pool._closed:
            pool = BrowserProcessPool(
                headless=headless,
                pool_size=pool_size,
                max_contexts_per_browser=max_contexts_per_browser,
            )
            _pools[key] = pool
        return pool


def shutdown_shared_pool(headless: bool | None = None) -> None:
    """释放一个或全部共享池（任务结束时调用）。"""
    keys = [_pool_key(headless)] if headless is not None else list(_pools.keys())
    for key in keys:
        with _locks.setdefault(key, threading.Lock()):
            pool = _pools.pop(key, None)
            if pool is not None:
                pool.shutdown()


__all__ = [
    "BrowserProcessPool",
    "BrowserRegistrationTimeoutError",
    "get_shared_pool",
    "shutdown_shared_pool",
]
