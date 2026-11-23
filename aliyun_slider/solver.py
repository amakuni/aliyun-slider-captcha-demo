# -*- coding: utf-8 -*-
# @Desc : 九州通(flow.jztit.com) Playwright 登录 + 阿里云验证码2.0滑块 通用模块
# 背景：九州通换阿里云验证码2.0后，Selenium/CDP合成鼠标事件被风控识别（真人手动拖也失败），
#       只有「真实Chrome + 真实用户配置 + 操作系统层鼠标事件」能通过。
# 方案要点（多轮实跑验证，详见 .workbuddy/memory/2026-11-20.md）：
#   ① launch_persistent_context(channel='chrome') 接管真实Chrome用户配置，风控分最低
#   ② 滑块拖动优先用 PyDirectInput，缺失时用 PyAutoGUI 的系统鼠标事件；CDP合成事件会被拒
#   ③ 视口原点用 Win32 Chrome_RenderWidgetHostHWND 精确定位（JS估算会偏几像素）
#   ④ 拖动前强制最大化窗口（复用用户配置时 --start-maximized 会被忽略）
#   ⑤ 缺口定位用拼图alpha外轮廓在背景Canny边缘上模板匹配(calc_gap)
#   ⑥ 类人轨迹(_human_track)：起步慢-加速-减速-过冲回拉；PAUSE=0保证连贯(否则一顿一顿被判机器)
#   ⑦ 拖动中按住状态实测比率R并半步阻尼校正，收敛到±1.5px
#   ⑧ 登录成功判据 = URL跳转 index.html（"滑块消失"不可靠：服务端二次校验失败也不跳转）
#
# 对外主入口：
#   login_flow_jzt(page, account, pwd, ...) -> bool   # 登录+过滑块+重试，返回是否成功
#   launch_browser(pw, ...) -> (context, page)        # 启动接管真实Chrome
# 也可独立自测：先关闭所有Chrome窗口，运行
#   D:/Python310/python.exe util/jzt_pw_login.py [manual]
#
# 账号密码：优先用函数入参；其次环境变量 JZT_ACCOUNT/JZT_PWD；再次 util/.env

import os
import sys
import time
import base64
import struct

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # .../util
sys.path.append(BASE_DIR)

from playwright.sync_api import sync_playwright

try:
    import pydirectinput
except ImportError:
    # 项目虚拟环境已提供 PyAutoGUI，避免仅因可选依赖缺失而中断登录。
    import pyautogui as pydirectinput

HOME_URL = 'http://flow.jztit.com/'
LOGIN_URL = 'http://flow.jztit.com/user/login.html'
# 临时诊断文件（缺口图/截图/失败HTML）放 log 下，避免污染代码目录
FILE_DIR = os.path.join(BASE_DIR, 'jzt_pw_tmp')
os.makedirs(FILE_DIR, exist_ok=True)

# Chrome 用户配置目录。
# ⚠️ 不能用 Chrome 默认配置目录(%LOCALAPPDATA%\Google\Chrome\User Data)：
#    Chrome 136+ 禁止对默认目录开启远程调试（"DevTools remote debugging requires
#    a non-default data directory"），launch_persistent_context 会直接超时。
#    这里用独立的自动化专用配置：持久化目录，跨次运行保留Cookie/历史/指纹，
#    同样能起到"真实老用户"的风控效果；可用环境变量 JZT_CHROME_USER_DATA 覆盖。
USER_DATA_DIR = os.environ.get('JZT_CHROME_USER_DATA') or \
    os.path.expandvars(r'%LOCALAPPDATA%\jzt_chrome_profile')
# Chrome 默认配置目录（仅用于 preflight 检测误传）
_DEFAULT_CHROME_UDD = os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\User Data')


def get_credentials(account=None, pwd=None):
    """
    凭据优先级：函数入参 > 环境变量 JZT_ACCOUNT/JZT_PWD > util/.env。
    子类通常直接传入账号密码，故入参优先。
    """
    account = account or os.environ.get('JZT_ACCOUNT')
    pwd = pwd or os.environ.get('JZT_PWD')
    if account and pwd:
        return account, pwd
    # 尝试读 util/.env（若其中已配置九州通账号）
    env_file = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_file):
        kv = {}
        with open(env_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    kv[k.strip()] = v.strip().strip('"').strip("'")
        account = account or kv.get('JZT_ACCOUNT')
        pwd = pwd or kv.get('JZT_PWD')
    if not account or not pwd:
        raise RuntimeError('未提供九州通账号：请传入 account/pwd，或设置 JZT_ACCOUNT/JZT_PWD')
    return account, pwd


# ---------------------------------------------------------------- 浏览器启动

def _preflight_check(user_data_dir):
    """
    启动前环境自检。Windows Server/虚拟机上 launch_persistent_context 卡死的
    常见根因在这里提前暴露，避免无限等待：
      1. Chrome 未安装（channel='chrome' 找不到可执行文件）
      2. user_data_dir 误传 Chrome 默认配置目录（Chrome 136+ 拒绝远程调试 => 必超时）
      3. 本模块专用配置目录被残留 chrome.exe 占用（新实例转交后退出，playwright 死等）
      4. 非交互式会话（服务/计划任务 Session 0、RDP 已断开）——GUI Chrome 起不来，
         且 SendInput 拖滑块、窗口最大化全都依赖活动桌面，无解
    :return: 诊断信息列表（空=全部通过）
    """
    problems = []

    # 1) Chrome 是否安装
    chrome_paths = [
        os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe'),
    ]
    if not any(os.path.exists(p) for p in chrome_paths):
        problems.append('未找到 Chrome 安装（channel="chrome" 需要本机装有 Google Chrome）：'
                        + ' | '.join(chrome_paths))

    # 2) 误传默认配置目录：Chrome 136+ 报
    #    "DevTools remote debugging requires a non-default data directory" 后拒绝挂调试管道
    udd_norm = os.path.normcase(os.path.normpath(user_data_dir))
    if udd_norm == os.path.normcase(os.path.normpath(_DEFAULT_CHROME_UDD)):
        problems.append('user_data_dir 指向 Chrome 默认配置目录(%s)，Chrome 136+ 禁止对其'
                        '远程调试，必然超时。请改用独立目录（默认 %s，'
                        '或设环境变量 JZT_CHROME_USER_DATA）'
                        % (_DEFAULT_CHROME_UDD, USER_DATA_DIR))

    # 3) 是否交互式桌面（Session 0 = 服务/计划任务，无桌面；RDP断开也会变非交互）
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetDesktopWindow()
        fg = user32.GetForegroundWindow()
        if not hwnd:
            problems.append('无桌面窗口(GetDesktopWindow为空)，疑似运行在服务/Session 0，'
                            'GUI Chrome 无法启动')
        # 交互桌面存在但没有前台窗口，通常意味着会话被锁定/RDP已断开
        elif not fg:
            problems.append('检测不到前台窗口，会话可能已锁定或 RDP 已断开'
                            '（滑块拖动需要活动且未锁定的桌面）')
    except Exception as e:
        problems.append('桌面环境检测失败: %s' % e)

    # 4) 【本模块专用配置目录】是否被残留 chrome 进程占用。
    #    用独立目录后，用户自己日常开的Chrome不再冲突，只查命令行里带我们目录的进程。
    try:
        import subprocess
        ps = ('Get-CimInstance Win32_Process -Filter "Name=\'chrome.exe\'" '
              '| Select-Object -ExpandProperty CommandLine')
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for line in (out.stdout or '').splitlines():
            if user_data_dir.lower() in line.lower():
                problems.append('配置目录 %s 被残留 chrome.exe 占用（命令行匹配），'
                                '请先结束该进程: %s...' % (user_data_dir, line[:120]))
                break
    except Exception:
        pass  # 查询手段不可用不阻断启动

    lock = os.path.join(user_data_dir, 'SingletonLock')
    if os.path.exists(lock):
        problems.append('用户配置存在 SingletonLock 残留: %s（上次Chrome未正常退出）' % lock)

    return problems


def launch_browser(pw, user_data_dir=None, downloads_path=None, headless=False,
                   launch_timeout=60000, strict_preflight=True):
    """
    以持久化上下文启动真实Chrome（接管真实用户配置，风控分最低）。
    channel='chrome' 用系统安装的真Chrome而非playwright自带chromium，
    配合真实 User Data，navigator.webdriver 等特征由真实用户配置兜底。
    ignore_default_args 去掉 playwright 默认注入的自动化开关。

    ⚠️ 服务器/虚拟机部署必读：本方案依赖【活动的交互式桌面】——
       GUI Chrome(headless=False) + SendInput 真实鼠标 + 窗口最大化，三者缺一不可。
       所以【不能】作为 Windows 服务/计划任务(Session 0)运行，也【不能】在 RDP
       断开后运行（断开会锁会话，SendInput/前台窗口全部失效）。
       正确姿势：服务器保持一个自动登录的控制台会话，或用 tscon 把 RDP 会话
       切到 console，让桌面始终活动。
    :param user_data_dir: Chrome用户配置目录，默认本机真实配置
    :param downloads_path: 下载文件落盘目录，None则用playwright默认临时目录
    :param headless: 必须False，阿里云滑块无法在无头环境通过（需真实渲染+SendInput）
    :param launch_timeout: 启动超时(ms)，超时抛错而非无限卡死
    :param strict_preflight: True=自检发现问题直接抛错(服务器部署建议保持True)
    :return: (context, page)
    """
    udir = user_data_dir or USER_DATA_DIR
    problems = _preflight_check(udir)
    if problems:
        msg = '浏览器启动前自检未通过：\n  - ' + '\n  - '.join(problems)
        if strict_preflight:
            raise RuntimeError(msg)
        print('[警告] ' + msg)

    kwargs = dict(
        user_data_dir=udir,
        channel='chrome',
        headless=headless,
        viewport=None,                       # 用真实窗口尺寸
        accept_downloads=True,               # 显式开启下载（进销存导出必需）
        timeout=launch_timeout,              # 关键：超时报错，不再无限卡死
        ignore_default_args=[
            '--enable-automation',           # 关键：去掉自动化横幅/特征
            '--enable-blink-features=IdleDetection',
        ],
        args=[
            '--ignore-certificate-errors',
            '--start-maximized',
            '--disable-blink-features=AutomationControlled',
            # 服务器/虚拟机防弹框卡死。注意：
            # ① --no-first-run/--no-default-browser-check/--password-store=basic/
            #    --use-mock-keychain/--disable-infobars 是 playwright 默认就带的，不再重复；
            # ② 【禁止】再传自己的 --disable-features：Chrome 只认最后一个该参数，
            #    会整体覆盖 playwright 的长列表，把 DestroyProfileOnBrowserClose 等
            #    重新启用 => 每次退出销毁配置目录，Cookie持久化失效。
            '--disable-session-crashed-bubble',   # 禁"恢复页面?"气泡（异常退出后出现）
            '--hide-crash-restore-bubble',
            '--disable-gpu',                 # 虚拟机常无GPU加速，避免渲染等待
            '--mute-audio',
        ],
    )
    if downloads_path:
        # 下载临时目录指到业务目录；最终仍由 download.save_as 规整命名落盘
        kwargs['downloads_path'] = downloads_path

    print('启动Chrome接管: user_data_dir=%s, timeout=%dms' % (udir, launch_timeout))
    try:
        context = pw.chromium.launch_persistent_context(**kwargs)
    except Exception as e:
        raise RuntimeError(
            'launch_persistent_context 失败: %s\n'
            '常见原因：①Chrome未安装或版本过旧 ②用户配置被占用(已有chrome.exe) '
            '③运行在无桌面的服务/Session 0会话 ④虚拟机无交互桌面。'
            '若为卡死后超时，请检查服务器上是否有残留 chrome.exe 进程。' % e)

    page = context.pages[0] if context.pages else context.new_page()
    # 抹掉 webdriver 标记（launch_persistent_context + channel=chrome 下双保险）
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context, page


# ---------------------------------------------------------------- 缺口定位（与DP版同方案）

def calc_gap(bg_path, pu_path, save_path=None):
    """
    用拼图块【透明通道外轮廓】在背景图Canny边缘上做模板匹配，定位缺口左缘。
    内部纹理会干扰匹配，所以只保留外轮廓带（mask - 腐蚀mask）。
    :return: (gap_x, x0, score)
    """
    import cv2
    import numpy as np
    bg = cv2.imdecode(np.fromfile(bg_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    pu = cv2.imdecode(np.fromfile(pu_path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if pu.ndim != 3 or pu.shape[2] < 4:
        raise ValueError('拼图图片没有alpha通道')
    alpha = pu[:, :, 3]
    mask = (alpha > 128).astype(np.uint8) * 255
    ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        raise ValueError('拼图alpha通道全透明')
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    er = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=2)
    band = (mask - er)[y0:y1 + 1, x0:x1 + 1]
    bg_edge = cv2.Canny(bg, 100, 200)
    res = cv2.matchTemplate(bg_edge, band, cv2.TM_CCOEFF_NORMED)
    _, score, _, max_loc = cv2.minMaxLoc(res)
    gap_x = max_loc[0]
    if save_path:
        cv2.rectangle(bg, (gap_x, max_loc[1]),
                      (gap_x + (x1 - x0), max_loc[1] + (y1 - y0)), (0, 0, 255), 2)
        cv2.imencode('.png', bg)[1].tofile(os.path.join(save_path, 'out.png'))
    return gap_x, x0, score


def _png_size(data):
    """从PNG二进制头读取宽高"""
    if len(data) > 24 and data[:8] == b'\x89PNG\r\n\x1a\n':
        w, h = struct.unpack('>II', data[16:24])
        return w, h
    return 0, 0


def _base64_to_bytes(src):
    """base64 data URI -> 字节；非data URI返回空"""
    if not src:
        return b''
    if ',' in src:
        head, src = src.split(',', 1)
        if 'base64' not in head:
            return b''
    elif src.startswith('http') or src.startswith('/'):
        return b''
    src = ''.join(src.split())
    src += '=' * (-len(src) % 4)
    try:
        return base64.b64decode(src)
    except Exception:
        return b''


# ---------------------------------------------------------------- 真实鼠标拖动

def _ensure_dpi_aware():
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _viewport_screen_offset(page):
    """
    【备用方案】JS估算视口原点偏移（CSS像素）。
    最大化窗口时 screenY/outerHeight 有边框负边距等怪癖，实测 y 会偏高几个像素，
    优先用 _render_widget_origin() 的 Win32 精确值，这里只做兜底。
    """
    return page.evaluate('''() => ({
        sx: window.screenX,
        sy: window.screenY + (window.outerHeight - window.innerHeight),
        winX: window.screenX, winY: window.screenY,
        outerW: window.outerWidth, outerH: window.outerHeight,
        innerW: window.innerWidth, innerH: window.innerHeight,
    })''')


def _find_chrome_hwnd_by_title(page):
    """
    【关键】按页面标题精确定位我们自己的Chrome主窗口HWND。
    不能再用 GetForegroundWindow：服务器上若开着别的Chrome窗口（日常的/残留的），
    前台可能是那个窗口 => 最大化错窗口、量错视口原点、鼠标按到别人窗口上（实跑事故：
    JS screenPos=(0,0) 但 Win32 报原点(977,85)，两个"Chrome"串台，滑块没抓住）。
    Chrome 窗口标题 = "页面title - Google Chrome"，用 page.title() 精确匹配。
    :return: hwnd 或 None
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.restype = ctypes.c_int

    try:
        page_title = page.title() or ''
    except Exception:
        page_title = ''
    if not page_title:
        return None

    candidates = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        # 只看顶层可见窗口
        if not user32.IsWindowVisible(hwnd):
            return True
        cbuf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cbuf, 256)
        if not cbuf.value.startswith('Chrome_WidgetWin'):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        tbuf = ctypes.create_unicode_buffer(n + 2)
        user32.GetWindowTextW(hwnd, tbuf, n + 1)
        title = tbuf.value
        # Chrome 标题格式: "<页面title> - Google Chrome"；
        # 验证码弹出时页面title可能带前后缀，做包含匹配
        if page_title in title or title.startswith(page_title):
            candidates.append((hwnd, title))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not candidates:
        return None
    if len(candidates) > 1:
        # 多个同标题窗口（同页面开了两份）：优先取当前前台那个，否则取第一个并告警
        fg = user32.GetForegroundWindow()
        for h, t in candidates:
            if h == fg:
                return h
        print(f'警告: 找到{len(candidates)}个同标题Chrome窗口，取第一个: {candidates[0][1]!r}')
    return candidates[0][0]


def _activate_window(hwnd):
    """
    把指定HWND带到前台。SetForegroundWindow 有系统焦点限制（前台锁定），
    标准绕法：先 AttachThreadInput 挂到前台线程再设，最后恢复。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    SW_RESTORE = 9

    if user32.IsIconic(hwnd):           # 最小化了先还原
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    my_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached_fg = attached_my = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached_fg = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        if my_tid and my_tid != cur_tid:
            attached_my = bool(user32.AttachThreadInput(cur_tid, my_tid, True))
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, 5)      # SW_SHOW
        ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached_fg:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        if attached_my:
            user32.AttachThreadInput(cur_tid, my_tid, False)
    return ok


def _render_widget_origin(hwnd=None):
    """
    【精确方案】Win32 定位 Chrome 网页内容子窗口 Chrome_RenderWidgetHostHWND，
    其 GetWindowRect().left/top 即视口原点的【物理屏幕像素】坐标。
    :param hwnd: 目标Chrome主窗口句柄（必须由 _find_chrome_hwnd_by_title 得到，
                 不要用 GetForegroundWindow——会串到别的Chrome窗口上）。None时退回前台窗口(兼容)。
    :return: (x_phys, y_phys) 或 None（找不到子窗口时）
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    if hwnd is None:
        hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    if not buf.value.startswith('Chrome_WidgetWin'):
        print(f'警告: 窗口类名为 {buf.value!r}，不是Chrome窗口，Win32视口定位跳过')
        return None
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(child, _lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(child, buf, 256)
        if buf.value == 'Chrome_RenderWidgetHostHWND':
            rect = wintypes.RECT()
            user32.GetWindowRect(child, ctypes.byref(rect))
            found.append((rect.left, rect.top))
            return False  # 找到即停
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def _maximize_chrome_window(page):
    """
    激活并强制最大化【我们自己的】Chrome窗口（按页面标题定位HWND，不信前台窗口）。
    launch_persistent_context 时 Chrome 可能恢复上次窗口状态，--start-maximized 常被忽略；
    小窗口/副屏会导致验证码元素在视口外，bounding_box坐标落到窗口外。
    :return: 目标窗口hwnd（供后续 _render_widget_origin 复用，避免二次枚举）或 None
    """
    _ensure_dpi_aware()
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    SW_MAXIMIZE = 3

    page.bring_to_front()
    time.sleep(0.5)

    hwnd = _find_chrome_hwnd_by_title(page)
    if not hwnd:
        print('警告: 按页面标题未找到我们的Chrome窗口（标题匹配失败），退回前台窗口方案')
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if not buf.value.startswith('Chrome_WidgetWin'):
            print(f'警告: 前台窗口类名 {buf.value!r} 也不是Chrome，跳过最大化')
            return None

    _activate_window(hwnd)
    time.sleep(0.3)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    time.sleep(0.8)

    # 校验：JS 报告的窗口位置应与 Win32 窗口矩形一致（防再次串台）
    info = page.evaluate('''() => ({
        innerW: window.innerWidth, innerH: window.innerHeight,
        screenW: screen.width, screenH: screen.height,
        sx: window.screenX, sy: window.screenY,
    })''')
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print(f'窗口已最大化: hwnd={hwnd}, Win32窗口rect=({rect.left},{rect.top},{rect.right},{rect.bottom}), '
          f'JS screenPos=({info["sx"]},{info["sy"]}), inner={info["innerW"]}x{info["innerH"]}, '
          f'screen={info["screenW"]}x{info["screenH"]}')
    # 一致性检查：最大化后窗口左上应贴近(0,0)且JS screenX应≈rect.left
    if abs(info['sx'] - rect.left) > 4 or abs(rect.left) > 8:
        print(f'⚠️ 警告: JS窗口位置({info["sx"]})与Win32窗口位置({rect.left})不一致，'
              f'疑似仍锁定到了别的Chrome窗口！请关闭服务器上的其他Chrome后重试')
    if info['innerW'] < info['screenW'] * 0.8:
        print('警告: 最大化后视口仍明显小于屏幕，可能有缩放/副屏，注意核对坐标')
    return hwnd


def _slider_geometry(page, slider, bg_el, pu_el):
    """
    量出滑块验证码的几何映射关系，算出「滑块位移 -> 拼图位移」的比率 k。
    根源：页面源码 initAliyunCaptcha({slideStyle: {width: 360}}) 把滑轨设为360px，
    而背景图受容器限制实际显示约300px，两者不等 => 位移不是1:1。
        k = 滑块可动范围 / 拼图可动范围
          = (滑轨宽 - 滑块宽) / (背景显示宽 - 拼图显示宽)
    要让拼图走 distance，鼠标必须拖 distance * k。这是确定的几何量，不需要试探。
    :return: dict(track_w, slider_w, bg_w, pu_w, ratio) 量取失败时 ratio=1.0
    """
    fallback = {'track_w': 0, 'slider_w': 0, 'bg_w': 0, 'pu_w': 0, 'ratio': 1.0}
    try:
        geo = page.evaluate('''(els) => {
            const [slider, bg, pu] = els;
            // 滑轨 = 滑块的父容器（阿里云SDK结构：滑轨 > 滑块）
            const track = slider.parentElement || slider;
            const w = el => {
                const b = el.getBoundingClientRect();
                return b.width;
            };
            return {
                track_w: w(track), slider_w: w(slider),
                bg_w: w(bg), pu_w: w(pu),
                trackClass: track.className || '',
            };
        }''', [slider, bg_el, pu_el])
    except Exception as e:
        print(f'几何量取失败: {e}')
        return fallback

    track_w = geo['track_w'] or 0
    slider_w = geo['slider_w'] or 0
    bg_w = geo['bg_w'] or 0
    pu_w = geo['pu_w'] or 0
    slider_range = track_w - slider_w
    puzzle_range = bg_w - pu_w
    if slider_range > 1 and puzzle_range > 1:
        ratio = slider_range / puzzle_range
    else:
        ratio = 1.0
    print(f'几何量取: 滑轨宽={track_w:.1f}(class={geo.get("trackClass", "")!r}), '
          f'滑块宽={slider_w:.1f}, 背景显示宽={bg_w:.1f}, 拼图显示宽={pu_w:.1f}')
    print(f'  滑块可动={slider_range:.1f}px, 拼图可动={puzzle_range:.1f}px '
          f'=> 映射比率 k={ratio:.4f}')
    return {'track_w': track_w, 'slider_w': slider_w, 'bg_w': bg_w,
            'pu_w': pu_w, 'ratio': ratio}


def _read_positions(page):
    """
    原子读取滑块+拼图块的视口CSS left。
    必须一次evaluate同时取两个值：分两次取会夹着SDK的rAF动画，读到不一致的中间态。
    """
    try:
        return page.evaluate('''() => {
            const s = document.querySelector('#aliyunCaptcha-sliding-slider');
            const p = document.querySelector('#aliyunCaptcha-puzzle');
            return {
                slider: s ? s.getBoundingClientRect().left : null,
                puzzle: p ? p.getBoundingClientRect().left : null,
            };
        }''')
    except Exception:
        return {'slider': None, 'puzzle': None}


def _cursor_pos():
    """读当前鼠标物理屏幕坐标（用于拖动中做边界钳制，防止飞出屏幕）。"""
    import ctypes
    from ctypes import wintypes

    class POINT(ctypes.Structure):
        _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]

    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _read_stable_positions(page, timeout=0.6, eps=0.4, interval=0.03):
    """
    轮询读取直到拼图/滑块位置稳定（连续两次读数差<eps）再返回。
    比固定 sleep 更可靠：SDK 更新拼图位置有 rAF 动画延迟，
    固定 sleep 太短读到滞后值→过冲→震荡，太长又拖慢伺服循环。
    这里读到「真正停稳」即刻返回，通常几十毫秒。
    """
    prev = _read_positions(page)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(interval)
        cur = _read_positions(page)
        if cur['puzzle'] is None:
            return cur
        if prev['puzzle'] is not None and abs(cur['puzzle'] - prev['puzzle']) < eps:
            return cur
        prev = cur
    return prev


def _move_smooth(phys, max_x=None, min_x=None):
    """
    分段随机步长移动鼠标（物理像素）。一次性moveRel是瞬移直线，容易被行为风控判为机器；
    分小步+轻微y抖动+随机步间隔更接近真人，也给SDK留出更新拼图位置的时间。
    max_x/min_x：物理屏幕X边界，超出则钳制（防止R爆炸时把鼠标甩出屏幕）。
    """
    import random
    phys = int(round(phys))
    if phys == 0:
        return
    # 边界钳制：基于当前真实光标位置，保证移动后不越界
    if max_x is not None or min_x is not None:
        cx0, _ = _cursor_pos()
        target = cx0 + phys
        if max_x is not None and target > max_x:
            phys = max_x - cx0
        if min_x is not None and cx0 + phys < min_x:
            phys = min_x - cx0
        if phys == 0:
            return
    sign = 1 if phys > 0 else -1
    total = abs(phys)
    n = max(4, min(28, total // 8))
    done = 0
    for s in range(n):
        remain = total - done
        if s == n - 1 or remain <= 1:
            step = remain
        else:
            # 前段快后段慢：真人习惯大致定位后减速对准
            step = int(round(remain / (n - s) * random.uniform(0.8, 1.2)))
            step = max(1, min(step, remain))
        if step <= 0:
            break
        pydirectinput.moveRel(sign * step, random.choice([0, 0, 0, 1, -1]))
        done += step
        time.sleep(random.uniform(0.008, 0.022))


def _human_track(phys_distance):
    """
    生成类人拖动轨迹（物理像素位移序列 + 每步间隔秒）。
    移植自 util/fetch_processor_login.py::_human_like_drag 的成熟算法，特征：
      起步慢 -> 中段加速(大步长) -> 临近目标减速(小步长) -> 小幅过冲 -> 回拉
    阿里云 VerifyIntelligentCaptcha 除了校验拼图位置，还分析整个拖动过程的行为特征：
    匀速/顿挫/耗时过长/无过冲回拉 都会被判机器。这套轨迹是对该检测的针对性应对。
    :return: (tracks, overshoot) tracks=[(dx, dy, sleep_sec), ...]
    """
    import random
    phys_distance = int(round(phys_distance))
    if phys_distance <= 0:
        return [], 0

    # 过冲量：越过目标几像素再回拉，模拟人手校准（真人几乎必有此动作）
    overshoot = random.randint(3, 7)
    target = phys_distance + overshoot
    mid = target * 0.7

    tracks = []
    current = 0
    y_drift = 0
    while current < target:
        remaining = target - current
        if current < mid:
            step = random.uniform(5, 12)                                  # 加速段：大步长
        else:
            step = max(1, remaining * random.uniform(0.18, 0.32))          # 减速段：小步长
        step = min(int(round(step)), remaining)
        if step <= 0:
            step = 1
        # 纵向轻微抖动（真人手不可能走绝对直线），累计偏移最后归位
        dy = random.choice([-1, 0, 0, 0, 1])
        y_drift += dy
        # 步间隔：加速段短、减速段长，模拟人手速度变化
        pause = random.uniform(0.006, 0.020) if current < mid else random.uniform(0.020, 0.048)
        tracks.append((step, dy, pause))
        current += step

    # 回拉过冲部分（按实际累计算，避免取整误差）
    back = current - phys_distance
    if back > 0:
        tracks.append((-back, -y_drift, random.uniform(0.03, 0.07)))
    elif y_drift:
        tracks.append((0, -y_drift, random.uniform(0.02, 0.05)))
    return tracks, overshoot


def _micro_track(phys):
    """
    校正用的无过冲微轨迹（物理像素，可正可负）。
    为什么不用 _human_track 做校正：它带3~7px过冲+回拉，对8px级别的小校正占比过大，
    过冲再回拉的过程会让拼图来回拉锯、读数忽大忽小，配合半步阻尼反而难收敛。
    校正阶段只需「平稳小幅移动到位」，不需要拟人过冲（人手微调也是直接挪，不再甩过头）。
    分成2~4小步+轻微y抖动，净位移精确等于目标（末步吸收取整误差）。
    :return: tracks=[(dx, dy, sleep), ...]
    """
    import random
    phys = int(round(phys))
    if phys == 0:
        return []
    sign = 1 if phys > 0 else -1
    total = abs(phys)
    n = max(2, min(4, total // 4 + 1))   # 小距离分2~4步
    tracks = []
    done = 0
    y_drift = 0
    for s in range(n):
        remain = total - done
        if s == n - 1:
            step = remain                 # 末步补满，保证净位移精确
        else:
            step = max(1, int(round(remain / (n - s))))
            step = min(step, remain)
        dy = 0 if s == n - 1 else random.choice([-1, 0, 0, 1])
        if s == n - 1:
            dy = -y_drift                 # 末步y归零
        y_drift += dy
        tracks.append((sign * step, dy, random.uniform(0.012, 0.03)))
        done += step
    return tracks


def _exec_track(tracks, max_x=None, min_x=None):
    """
    执行类人轨迹，返回实际累计x物理位移。带屏幕边界钳制。
    关键：鼠标库的 PAUSE 必须为0！它默认0.1s，每次moveRel后强制睡0.1秒，
    35步轨迹会额外拖3.5秒 => 变成肉眼可见的一顿一顿慢挪（不是连贯拖动），
    且总时长超过阿里云拖动窗口，SDK中途判超时重置验证，拼图不再跟手。
    """
    pydirectinput.PAUSE = 0
    done = 0.0
    for dx, dy, pause in tracks:
        step = int(round(dx))
        if step == 0 and dy == 0:
            continue
        if max_x is not None or min_x is not None:
            cx0, _ = _cursor_pos()
            tgt = cx0 + step
            if max_x is not None and tgt > max_x:
                step = max_x - cx0
            if min_x is not None and cx0 + step < min_x:
                step = min_x - cx0
        if step != 0 or dy != 0:
            pydirectinput.moveRel(step, dy)
        done += step
        time.sleep(pause)
    return done


def real_mouse_drag(page, slider_handle, distance_css, ratio=1.2, grab='mid'):
    """
    操作系统级真实鼠标拖动 + 【类人轨迹一次到位 + 极少次校正】。
    Playwright 的 page.mouse 是 CDP 合成事件，会被阿里云行为风控识别，不能用。

    教训演进：
      v1 一次冲到位 -> 比率不准，冲过头顶到滑轨尽头留空隙
      v2 小步伺服逼近 -> 位置吻合了但验证仍失败：顿挫小步+每步JS轮询+耗时10s+，
         轨迹本身就是机器特征（阿里云 VerifyIntelligentCaptcha 分析行为序列）
      v3(本版) 拟人轨迹：起步慢-加速-减速-小幅过冲-回拉，全程0.5~1.5s贴近真人，
         JS读取从80次压到4~5次；R由整段轨迹实测标定，偏差>1px时最多2次轻校正
    :param distance_css: 拼图块需要移动的页面CSS距离（calc_gap算出的缺口距离）
    :param ratio: R的初值猜测（拖动后用实测值修正并返回，供下轮使用）
    :return: dict(grabbed, ratio, err, puzzle_moved)
    """
    _ensure_dpi_aware()
    import random
    pydirectinput.FAILSAFE = False

    page.bring_to_front()
    time.sleep(0.5)
    # 每次拖动前重新确认窗口仍是最大化状态（可能被中途还原/拖动过）；
    # 返回按页面标题锁定的hwnd，供视口原点定位复用（防止串到别的Chrome窗口）
    target_hwnd = _maximize_chrome_window(page)

    pr = page.evaluate('window.devicePixelRatio') or 1.0
    box = slider_handle.bounding_box()
    if not box:
        raise RuntimeError('滑块 bounding_box 为空（元素不可见？）')
    # 元素身份校验：阿里云滑块是个约40x40的小方块，
    # 如果量出来宽>100说明选择器错配到了背景图/滑轨，硬拖必然把鼠标甩飞
    if box['width'] > 100 or box['height'] > 100:
        raise RuntimeError(f'滑块元素尺寸异常 {box["width"]:.0f}x{box["height"]:.0f}，'
                           f'疑似错配到背景图/滑轨而非滑块本体，终止拖动')
    if grab == 'left':
        vx = box['x'] + 6
    else:
        vx = box['x'] + box['width'] / 2
    vy = box['y'] + box['height'] / 2

    origin = _render_widget_origin(target_hwnd)   # 物理像素（锁定我们自己的窗口）
    off = _viewport_screen_offset(page)  # CSS像素（对照打印+兜底）
    if origin:
        ox_phys, oy_phys = origin
        src = 'Win32(RenderWidget)'
    else:
        ox_phys, oy_phys = off['sx'] * pr, off['sy'] * pr
        src = 'JS估算(兜底)'
    cx = ox_phys + vx * pr
    cy = oy_phys + vy * pr
    print(f'视口原点[{src}]: 物理({ox_phys:.0f}, {oy_phys:.0f}), dpr={pr}, '
          f'抓取点({grab})屏幕物理坐标: ({cx:.0f}, {cy:.0f}), 滑块box={box}')

    # 屏幕尺寸保护：偏移异常时坐标会飞出屏幕，直接报错而不是乱按
    import ctypes
    vw = ctypes.windll.user32.GetSystemMetrics(0)
    vh = ctypes.windll.user32.GetSystemMetrics(1)
    if not (0 <= cx <= vw and 0 <= cy <= vh):
        raise RuntimeError(f'计算出的屏幕坐标({cx:.0f},{cy:.0f})超出屏幕范围({vw}x{vh})，'
                           f'视口偏移换算异常，终止拖动')

    result = {'grabbed': False, 'ratio': ratio, 'err': None, 'puzzle_moved': 0.0}

    # 鼠标可活动的物理X边界：从抓取点到屏幕右缘留10px，防止R爆炸时甩出屏幕
    import ctypes as _ct
    screen_w = _ct.windll.user32.GetSystemMetrics(0)
    max_x = screen_w - 10
    min_x = 5

    pydirectinput.moveTo(int(cx), int(cy))
    time.sleep(random.uniform(0.12, 0.22))
    pydirectinput.mouseDown()
    time.sleep(random.uniform(0.08, 0.14))

    p0 = _read_stable_positions(page)
    if p0['puzzle'] is None:
        print('读不到拼图块位置，放弃本次拖动')
        pydirectinput.mouseUp()
        return result
    print(f'按下时基准: 拼图left={p0["puzzle"]:.1f}, 滑块left={p0["slider"]}')

    # ============ 类人轨迹拖动 + 极少次校正 ============
    # 位置吻合却验证失败 => 是轨迹被行为风控判为机器（顿挫小步/耗时过长/无过冲回拉/
    # 几十次JS读取本身也是指纹）。改用 _human_track 生成的拟人轨迹一次性拖到位
    # （起步慢-加速-减速-过冲-回拉，总耗时约0.5~1.5秒贴近真人），
    # 之后只做最多2次轻量校正，把JS读取次数压到最低。
    R = min(max(ratio, 0.5), 2.0)   # 鼠标物理px / 拼图CSS px，用实测值修正
    R_MAX = 2.0

    # 首次拖动距离：按初值R估算
    phys_target = distance_css * R
    tracks, overshoot = _human_track(phys_target)
    print(f'类人轨迹: 目标拼图{distance_css}px × R={R:.3f} = 鼠标{phys_target:.0f}物理px, '
          f'{len(tracks)}步, 过冲{overshoot}px')
    t_start = time.time()
    actual_phys = _exec_track(tracks, max_x=max_x, min_x=min_x)
    drag_dur = time.time() - t_start
    # 松手前停顿：真人释放前的短暂静止
    time.sleep(random.uniform(0.10, 0.30))

    # 读落点：SDK弹性/跟随动画在鼠标停后还会持续一小段，
    # 立即读会被动画慢速段骗过(read_stable误判已停)读到滞后值 => 校正方向失真。
    # 先固定等动画彻底结束，再用read_stable确认静止。
    time.sleep(0.35)
    p1 = _read_stable_positions(page, timeout=0.8, eps=0.3)
    moved = (p1['puzzle'] - p0['puzzle']) if p1['puzzle'] is not None else 0.0
    d_slider = (p1['slider'] - p0['slider']) if (p1['slider'] is not None and p0['slider'] is not None) else None
    err = distance_css - moved
    print(f'轨迹完成({drag_dur:.2f}s): 鼠标实走{actual_phys:.0f}物理px -> '
          f'拼图{moved:.1f}/{distance_css}px 偏差{err:+.1f}px '
          f'滑块{"?" if d_slider is None else f"{d_slider:.1f}"}px')

    # 没抓住判定：鼠标走了但拼图几乎没动
    if moved < 3:
        if d_slider is not None and d_slider >= 3:
            print('  ⚠️ 滑块在动但拼图没跟 => 元素映射异常，本次放弃')
        else:
            print('  拼图没跟着动 => 没抓住滑块，本次放弃')
        pydirectinput.mouseUp()
        result['puzzle_moved'] = moved
        return result

    result['grabbed'] = True
    # 实测R：用整段轨迹的累计位移标定（比探测段更抗噪），供本轮校正+下轮初值
    R = min(max(actual_phys / max(moved, 0.1), 0.5), R_MAX)
    result['ratio'] = R

    # ---- 校正循环：半步阻尼逐次收敛 ----
    # 实跑账目：校正阶段「补8物理px拼图跳14.4px」，局部灵敏度与主轨迹实测R对不上
    # （动画残余+读数滞后），所以：
    #   ① 步长只补偏差的一半(×0.5)——即使真实灵敏度是R的0.5~2倍，半步也单调收敛不震荡
    #   ② 校正走无过冲微轨迹（_human_track的3~7px过冲对8px小校正占比失真，会来回拉锯）
    #   ③ 次数放宽到5次，配合半步阻尼足够收敛到±1.5px
    #   ④ 每次校正后等动画停稳再读（0.25s + read_stable）
    for corr in range(5):
        if abs(err) <= 1.5:
            break
        fix_phys = err * R * 0.5          # 半步阻尼
        if abs(fix_phys) < 1:
            fix_phys = 1.0 if fix_phys >= 0 else -1.0
        fix_phys = min(max(fix_phys, -60), 60)
        print(f'  校正#{corr + 1}: 偏差{err:+.1f}px -> 半步补{fix_phys:+.1f}物理px (R={R:.3f})')
        _exec_track(_micro_track(fix_phys), max_x=max_x, min_x=min_x)
        actual_phys += fix_phys
        time.sleep(0.25)                   # 等SDK跟随动画停稳
        pc = _read_stable_positions(page, timeout=0.8, eps=0.3)
        if pc['puzzle'] is not None:
            moved = pc['puzzle'] - p0['puzzle']
            err = distance_css - moved
            R = min(max(actual_phys / max(moved, 0.1), 0.5), R_MAX)
            result['ratio'] = R
            print(f'  校正后: 拼图{moved:.1f}px 偏差{err:+.1f}px R={R:.3f}')

    time.sleep(random.uniform(0.08, 0.20))
    pydirectinput.mouseUp()

    result['err'] = err
    result['puzzle_moved'] = moved
    print(f'松手: 累计鼠标位移{actual_phys:.0f}物理px, 最终比率R={result["ratio"]:.4f}, '
          f'末次偏差={err:+.1f}px, 全程{time.time() - t_start:.2f}s')
    return result


# ---------------------------------------------------------------- 滑块验证码

def auto_drag_slider(page, max_try=15):
    """
    自动拖阿里云滑块：Playwright测量 + PyDirectInput真实拖动。
    拼图目标位移三要素：图片角色按宽度运行时判断 / 原图像素按显示宽缩放 / 减去拼图块初始偏移。
    映射比率R（鼠标物理位移/拼图CSS位移）：无法从DOM可靠推出（实测DOM几何1.05 vs 真实1.24），
    由 real_mouse_drag 在【按住鼠标的同一次拖动内】探测标定并微调收敛，
    本函数只负责把上轮实测R作为下轮初值传递。
    :return: True 验证通过 / False 失败
    """
    ratio = None          # 映射比率R初值，None表示首轮由DOM几何粗估
    grab = 'mid'

    for i in range(max_try):
        print(f'--- 第{i + 1}次自动滑块尝试 '
              f'(比率初值={"待标定" if ratio is None else f"{ratio:.4f}"}, 抓取={grab}) ---')

        # 每轮重新定位：验证失败后阿里云会销毁重建整个验证码组件
        try:
            slider = page.wait_for_selector('#aliyunCaptcha-sliding-slider', timeout=10000)
            img1 = page.wait_for_selector('#aliyunCaptcha-img', timeout=5000)
            img2 = page.wait_for_selector('#aliyunCaptcha-puzzle', timeout=5000)
            page.wait_for_function('''() => {
                const images = [
                    document.querySelector('#aliyunCaptcha-img'),
                    document.querySelector('#aliyunCaptcha-puzzle')
                ];
                return images.every(img => img && img.complete && img.naturalWidth > 0
                    && img.src.startsWith('data:image'));
            }''', timeout=10000)
        except Exception:
            slider = img1 = img2 = None
        if not slider or not img1 or not img2:
            print('验证码元素缺失，尝试重新点击登录按钮唤起验证码...')
            if not _reopen_captcha(page):
                return False
            continue

        # ---- 测量：宽者为背景大图，窄者为拼图块 ----
        d1 = _base64_to_bytes(img1.get_attribute('src'))
        d2 = _base64_to_bytes(img2.get_attribute('src'))
        w1, _ = _png_size(d1)
        w2, _ = _png_size(d2)
        print(f'#aliyunCaptcha-img 原图宽: {w1}, #aliyunCaptcha-puzzle 原图宽: {w2}')
        if not w1 or not w2:
            print('图片base64解析失败，重试')
            _reopen_captcha(page)
            continue
        if w1 >= w2:
            bg_el, pu_el, bg_bytes, pu_bytes, bg_w = img1, img2, d1, d2, w1
        else:
            bg_el, pu_el, bg_bytes, pu_bytes, bg_w = img2, img1, d2, d1, w2
        bg_path = os.path.join(FILE_DIR, 'bg.png')
        pu_path = os.path.join(FILE_DIR, 'puzzle.png')
        with open(bg_path, 'wb') as f:
            f.write(bg_bytes)
        with open(pu_path, 'wb') as f:
            f.write(pu_bytes)

        bg_box = bg_el.bounding_box()
        pu_box = pu_el.bounding_box()
        disp_w = bg_box['width']
        scale = disp_w / float(bg_w)

        gap_x, x0, score = calc_gap(bg_path, pu_path, save_path=FILE_DIR)
        pu_offset = pu_box['x'] - bg_box['x']
        # distance = 拼图块需要移动的页面CSS距离（不含比率，比率在拖动时乘）
        distance = int(round((gap_x - x0) * scale - pu_offset))
        print(f'缺口左缘原图x={gap_x}, 拼图内容左缘x0={x0}, 置信度={score:.3f}, '
              f'显示宽={disp_w}px 缩放比={scale:.4f}, 拼图块初始偏移={pu_offset:.1f}px, '
              f'拼图需移动(CSS): {distance}px')

        if distance <= 5 or distance > disp_w or score < 0.1:
            print('距离计算异常或置信度过低，重试')
            _reopen_captcha(page)
            continue

        # ---- 映射比率：真实R由 real_mouse_drag 按住鼠标时实测标定，
        #      这里只给一个初值猜测（用于探测段长度）；DOM量取值仅作参考打印 ----
        geo = _slider_geometry(page, slider, bg_el, pu_el)
        if ratio is None:
            ratio = geo['ratio'] if geo['ratio'] > 1.02 else 1.2
            print(f'比率初值(仅用于探测段): {ratio:.4f} (DOM几何量取={geo["ratio"]:.4f})')
        else:
            print(f'比率初值(沿用上次实测): {ratio:.4f} (DOM几何量取={geo["ratio"]:.4f})')

        try:
            drag_result = real_mouse_drag(page, slider, distance, ratio=ratio, grab=grab)
        except RuntimeError as e:
            # 元素尺寸异常/坐标越界等保护性中断：换抓取点重试，不让整个程序崩
            print(f'拖动被保护性中断: {e}')
            grab = 'left' if grab == 'mid' else 'mid'
            _reopen_captcha(page)
            continue

        # 下一轮用本轮按住状态下的实测R做初值（同一验证码组件生命周期内R稳定）
        if drag_result['grabbed'] and drag_result['ratio'] > 0.5:
            ratio = drag_result['ratio']

        time.sleep(0.25)
        try:
            page.screenshot(path=os.path.join(FILE_DIR, f'after_try{i + 1}.png'))
        except Exception:
            pass

        if not drag_result['grabbed']:
            # 探测段拼图没跟着动 => mousedown没抓住滑块，换抓取点重试
            grab = 'left' if grab == 'mid' else 'mid'
            print(f'未抓住滑块，切换抓取点为: {grab}')
        elif drag_result['err'] is not None:
            print(f'拖后状态: 拼图位移={drag_result["puzzle_moved"]:.1f}px, '
                  f'目标={distance}px, 末次偏差={drag_result["err"]:+.1f}px, '
                  f'实测R={drag_result["ratio"]:.4f}')

        # 服务端二次校验和业务跳转可能晚于滑块本地结果，等待期间不要刷新验证码。
        if is_logged_in(page, timeout=4) or _captcha_passed(page):
            print(f'第{i + 1}次滑块验证通过')
            return True

        print(f'第{i + 1}次滑块验证失败')
        time.sleep(1)
        if i < max_try - 1:
            _reopen_captcha(page)
    return False


def _reopen_captcha(page):
    """验证失败后重新唤起：优先点刷新图标，否则重点登录按钮"""
    time.sleep(1)
    for sel in ('#aliyunCaptcha-btn-refresh', '.aliyunCaptcha-refresh', '#aliyunCaptcha-refresh',
                'span[class*="refresh"]', 'div[class*="refresh"]'):
        try:
            el = page.query_selector(sel)
            if el:
                print(f'点击刷新按钮: {sel}')
                el.click()
                time.sleep(2)
                slider = page.query_selector('#aliyunCaptcha-sliding-slider')
                if slider and slider.is_visible():
                    return True
        except Exception:
            pass

    # 关掉可能的错误遮罩，再重新点登录
    for sel in ('.aliyunCaptcha-mask', 'div[class*="mask"]'):
        try:
            m = page.query_selector(sel)
            if m:
                m.click()
                time.sleep(0.5)
        except Exception:
            pass
    try:
        btn = page.wait_for_selector('input[name="commit"]', timeout=3000)
        print('重新点击登录按钮唤起验证码')
        btn.click()
        time.sleep(2)
        return bool(page.query_selector('#aliyunCaptcha-sliding-slider'))
    except Exception:
        return False


def _captcha_passed(page):
    """验证是否通过：无错误提示 且 滑块消失/隐藏"""
    try:
        err = page.query_selector('div[class*="verify-error"]')
        if err and err.is_visible():
            print(f'页面错误提示: {err.inner_text()}')
            return False
        slider = page.query_selector('#aliyunCaptcha-sliding-slider')
        if slider and slider.is_visible():
            return False
        return True
    except Exception:
        return False


def _dump_diagnose(page, tag):
    """保存截图+验证码区域HTML，便于事后分析"""
    try:
        page.screenshot(path=os.path.join(FILE_DIR, f'fail_{tag}.png'))
    except Exception as e:
        print('截图失败:', e)
    try:
        box = page.query_selector('#aliyunCaptcha-sliding') or \
              page.query_selector('#aliyunCaptcha-window-popup')
        html = box.inner_html() if box else page.content()
        with open(os.path.join(FILE_DIR, f'fail_{tag}.html'), 'w', encoding='utf-8') as f:
            f.write(html)
    except Exception as e:
        print('保存HTML失败:', e)


# ---------------------------------------------------------------- 登录

def login(page, account=None, pwd=None):
    """打开登录页 -> 填账号密码 -> 点登录唤起验证码。返回是否已在登录页就绪。"""
    account, pwd = get_credentials(account, pwd)
    # 先进根地址：已登录会直接进主页；未登录一般重定向到登录页
    page.goto(HOME_URL, wait_until='domcontentloaded')
    time.sleep(2)
    # 已经登录过（复用真实配置带了有效Cookie）会直接进主页
    if 'index.html' in page.url:
        print('检测到已登录，跳过验证码:', page.url)
        return True
    if not page.query_selector('#loginName'):
        # 根地址没重定向到登录页时，直接访问登录页
        page.goto(LOGIN_URL, wait_until='domcontentloaded')
        time.sleep(2)
    if not page.query_selector('#loginName'):
        raise RuntimeError(f'未找到登录表单(#loginName)，当前URL: {page.url}')
    print('登录页就绪:', page.url)

    page.fill('#loginName', '')
    page.fill('#loginName', account)
    page.fill('#loginPwd', '')
    page.fill('#loginPwd', pwd)
    print('账号密码已填入')

    page.click('input[name="commit"]')
    print('已点击登录按钮，等待验证码弹出...')
    time.sleep(2)
    return False


def is_logged_in(page, timeout=6.0):
    """
    登录成功判定：以 URL 含 index.html 为准（源码 onBizResultCallback 里
    bizResult=true 才 window.location.href='/index.html'）。
    关键：不能用「滑块消失」判成功——阿里云服务端 VerifyIntelligentCaptcha
    二次校验失败时滑块也会消失，但业务没通过、URL 不会跳转（表现为"吻合了也被判失败"）。
    :param timeout: 等待跳转的最长秒数
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if 'index.html' in page.url:
            return True
        time.sleep(0.3)
    # 兜底：离开了登录页也认为成功（个别环境跳别的地址）
    return 'login' not in page.url and 'flow.jztit.com' in page.url


def login_flow_jzt(page, account=None, pwd=None, max_retry=5, drag_max_try=15,
                   on_fail_dump=True):
    """
    九州通完整登录流程（对外主入口），带整轮重试。
    单轮 = 填账号密码点登录 -> 唤起验证码 -> 自动拖滑块 -> 校验URL跳index.html。
    失败重试策略（针对"有时吻合了也被判失败"）：
      - 服务端二次校验可能随机失败(风控评分/网络)，即使拼图位置对也过不了；
      - 所以以"最终URL是否跳index.html"为唯一成功判据，没跳就整轮重来（刷新登录页）；
      - 每轮之间递增等待，给风控降降火。
    :return: True 登录成功(URL已到index.html) / False 重试耗尽仍失败
    """
    for attempt in range(1, max_retry + 1):
        print(f'\n========== 登录总尝试 {attempt}/{max_retry} ==========')
        try:
            # 每轮从干净状态开始：回到登录页（复用配置可能已在主页）
            already = login(page, account, pwd)
            if already or is_logged_in(page, timeout=3):
                print('已登录（无需滑块）')
                return True

            if not page.query_selector('#aliyunCaptcha-sliding-slider'):
                # 点了登录但验证码没弹（可能账号密码错/网络慢），刷新重试
                print('验证码未弹出，检查账号密码或网络，刷新重试')
                _dump_diagnose(page, f'no_captcha_try{attempt}') if on_fail_dump else None
                _reload_login(page)
                time.sleep(min(2 + attempt, 8))
                continue

            ok = auto_drag_slider(page, max_try=drag_max_try)
            print(f'滑块拖动结果: {ok}，等待登录跳转确认...')
            # 不管滑块本地判定如何，以URL跳index.html为最终判据
            if is_logged_in(page, timeout=6):
                print(f'\n>>> 登录成功（第{attempt}轮），当前URL: {page.url} <<<')
                return True

            print(f'第{attempt}轮：滑块已过但URL未跳转index.html（服务端二次校验未通过），整轮重试')
            if on_fail_dump:
                _dump_diagnose(page, f'not_logged_try{attempt}')
            _reload_login(page)
            # 递增退避：风控可能需要更长冷却
            time.sleep(min(2 + attempt * 2, 12))
        except Exception as e:
            print(f'第{attempt}轮登录异常: {e}')
            if on_fail_dump:
                try:
                    _dump_diagnose(page, f'exception_try{attempt}')
                except Exception:
                    pass
            try:
                _reload_login(page)
            except Exception:
                pass
            time.sleep(min(2 + attempt * 2, 12))
    print(f'\n>>> 登录失败：已重试{max_retry}轮仍未成功 <<<')
    return False


def _reload_login(page):
    """刷新回登录页，清掉可能的错误遮罩/残留验证码，为下一轮重试做准备"""
    try:
        page.goto(LOGIN_URL, wait_until='domcontentloaded')
        time.sleep(2)
    except Exception as e:
        print(f'刷新登录页失败: {e}')


# ---------------------------------------------------------------- 独立自测入口

def main():
    """
    模块独立自测：验证登录+滑块能否跑通。
    运行前先关闭所有Chrome窗口。
      python util/jzt_pw_login.py           # 全自动登录
      python util/jzt_pw_login.py manual    # 只填账密唤起验证码，人工拖（验证环境）
    账号密码从 环境变量/util/.env 读取（见 get_credentials）。
    """
    manual_only = len(sys.argv) > 1 and sys.argv[1].lower() == 'manual'

    with sync_playwright() as pw:
        context, page = launch_browser(pw)
        try:
            if manual_only:
                already = login(page)
                print('\n>>> manual 模式：浏览器保持打开，请人工拖动滑块 <<<')
                if not already:
                    input('手动操作完成后按回车退出...')
                print('当前URL:', page.url, '已登录:', is_logged_in(page, timeout=2))
                return

            ok = login_flow_jzt(page)
            print(f'\n>>> login_flow_jzt 返回: {ok} <<<')
            if ok:
                print('登录成功，当前URL:', page.url)
            else:
                print(f'登录失败，诊断文件见: {FILE_DIR}')
            time.sleep(3)
        finally:
            # 自测阶段保留浏览器便于观察；生产由调用方管理 context 生命周期
            pass


if __name__ == '__main__':
    main()
