"""MCP-сервер: простейшие браузерные операции.

Если установлен Playwright (`pip install playwright && playwright install chromium`)
— используются реальные операции. Иначе — stub с mock-данными.
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import _base


class BrowserServer(_base.BaseToolServer):
    name = "browser"

    def _register_tools(self) -> None:
        from common.logging import get_logger
        log = get_logger("tools.browser")

        try:
            from playwright.async_api import async_playwright
            has_pw = True
        except ImportError:
            has_pw = False

        @self.mcp.tool()
        async def browser_navigate(url: str) -> str:
            """Открыть URL и вернуть текстовое содержимое страницы."""
            if not has_pw:
                return _base._ok(
                    mock=True,
                    message="Playwright не установлен. Возвращаем mock.",
                    url=url, title="(mock)", text="(empty)",
                )
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    page = await browser.new_page()
                    await page.goto(url, timeout=30000)
                    title = await page.title()
                    text = await page.inner_text("body")
                    await browser.close()
                    return _base._ok(url=url, title=title, text=text[:8192])
            except Exception as e:
                return _base._err(str(e))

        @self.mcp.tool()
        async def browser_screenshot(url: str,
                                     save_path: str | None = None) -> str:
            """Сделать скриншот страницы."""
            if not has_pw:
                return _base._err("Playwright не установлен. Установите: "
                                  "pip install playwright && playwright install chromium")
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(headless=True)
                    page = await browser.new_page()
                    await page.goto(url, timeout=30000)
                    path = save_path or "/tmp/screenshot.png"
                    await page.screenshot(path=path, full_page=True)
                    await browser.close()
                    return _base._ok(saved_to=path)
            except Exception as e:
                return _base._err(str(e))


def main():
    _base.run_stdio_server(BrowserServer())


if __name__ == "__main__":
    main()
