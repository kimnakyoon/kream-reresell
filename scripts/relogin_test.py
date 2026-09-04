"""세션을 끊고(kream 쿠키 삭제) 자동 재로그인이 되는지 확인한다."""
import logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from playwright.sync_api import sync_playwright
from kream_reresell import auth
from kream_reresell.browser import real_chrome_context
from kream_reresell.config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
settings = Settings()
with sync_playwright() as pw, real_chrome_context(pw, show_chrome=settings.show_chrome) as ctx:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://kream.co.kr/", wait_until="domcontentloaded"); page.wait_for_timeout(1500)
    print("before:", auth.is_logged_in(page))
    ctx.clear_cookies()
    page.goto("https://kream.co.kr/", wait_until="domcontentloaded"); page.wait_for_timeout(1500)
    print("after clear:", auth.is_logged_in(page))
    auth.ensure_logged_in(page, settings)
    print("relogin:", auth.is_logged_in(page))
