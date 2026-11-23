"""将 Playwright 登录态同步到 Selenium。"""

from playwright.sync_api import BrowserContext
from selenium.webdriver.remote.webdriver import WebDriver


def sync_cookies_to_selenium(
    context: BrowserContext,
    driver: WebDriver,
    login_url: str,
) -> int:
    """把当前站点 Cookie 写入已打开相同站点的 Selenium 浏览器。

    Cookie 由本次浏览器会话动态读取，不使用固定 Cookie。调用方需在同步后
    自行访问业务页面，并根据实际页面判断是否已登录。

    Args:
        context: 已完成滑块验证的 Playwright 浏览器上下文。
        driver: 与登录站点使用相同域名的 Selenium WebDriver。
        login_url: 登录页完整 URL，用于筛选 Cookie 和建立当前域。

    Returns:
        写入 Selenium 的 Cookie 数量。
    """
    driver.get(login_url)
    driver.delete_all_cookies()
    cookies = context.cookies([login_url])
    for source in cookies:
        cookie = {
            key: source[key]
            for key in ('name', 'value', 'domain', 'path', 'secure', 'httpOnly')
            if key in source
        }
        if source.get('expires', -1) > 0:
            cookie['expiry'] = int(source['expires'])
        if source.get('sameSite') in ('Strict', 'Lax', 'None'):
            cookie['sameSite'] = source['sameSite']
        driver.add_cookie(cookie)
    return len(cookies)
