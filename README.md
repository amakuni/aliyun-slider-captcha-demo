# 阿里云滑块验证码 Python 方案

基于真实 Chrome、Playwright、OpenCV 和 Windows SendInput 的阿里云验证码 2.0 滑块案例。项目保留已在九州通真实登录页验证的实现，包含缺口识别、屏幕坐标换算、滑轨比例修正、拟人拖动、失败重试和 Selenium Cookie 同步。

> 仅用于你有权测试或自动化的系统。验证码是否通过仍由目标网站的服务端风控决定，本方案不保证所有网络环境都能通过。

## 1. 项目结构

```text
aliyun-slider-captcha-demo/
├─ aliyun_slider/
│  ├─ __init__.py              # 对外接口
│  ├─ solver.py                # 滑块识别、系统级拖动、登录与重试
│  └─ selenium_bridge.py       # Playwright Cookie 同步到 Selenium
├─ examples/
│  ├─ login_flow.py            # 案例一：完整登录
│  └─ selenium_cookie_sync.py  # 案例二：登录后接管 Selenium
├─ requirements.txt            # 固定版本依赖
├─ .gitignore
└─ README.md
```

## 2. 运行条件

- Windows 10/11 或带交互式桌面的 Windows Server。
- Python 3.8 及以上，建议 Python 3.9 64 位。
- 已安装 Google Chrome。
- 必须保持桌面解锁，Chrome 窗口可见且处于活动状态。
- 拖动过程中不要操作鼠标，也不要切换远程桌面窗口。

不能在 Windows 服务、计划任务的 Session 0、已断开的 RDP 会话或无头模式中运行。原因是方案依赖真实窗口和系统级鼠标事件。

## 3. 安装

在项目目录执行：

```powershell
py -3.9 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.org/simple
```

如果官方源访问不稳定，可使用阿里云镜像：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

本项目使用 `channel='chrome'` 启动本机 Chrome，不需要执行 `playwright install chromium`。

### 依赖说明

| 包 | 固定版本 | 用途 |
|---|---:|---|
| playwright | 1.48.0 | 启动真实 Chrome、操作页面、读取验证码和 Cookie |
| pyee | 12.0.0 | Playwright 事件依赖，提供 `pyee.asyncio` |
| greenlet | 3.1.1 | Playwright 同步 API 依赖 |
| PyDirectInput | 1.0.4 | 通过 SendInput 发送系统级鼠标事件 |
| opencv-python | 4.10.0.84 | 背景图和拼图块处理、缺口定位 |
| numpy | 1.26.4 | 图像矩阵计算 |
| selenium | 4.26.1 | 第二个案例中的后续业务页面自动化 |

## 4. 案例一：完整登录

账号密码通过环境变量传入，代码中没有固定账号或 Cookie：

```powershell
$env:JZT_USERNAME='你的账号'
$env:JZT_PASSWORD='你的密码'
.venv\Scripts\python.exe examples\login_flow.py
```

成功后终端会输出最终页面 URL。浏览器配置保存在 `.runtime/chrome-profile`，下次运行可复用仍然有效的浏览器登录态。

## 5. 案例二：同步到 Selenium

```powershell
$env:JZT_USERNAME='你的账号'
$env:JZT_PASSWORD='你的密码'
.venv\Scripts\python.exe examples\selenium_cookie_sync.py
```

执行过程：

1. Playwright 打开真实 Chrome 并完成滑块验证。
2. 从当前上下文动态读取 Cookie。
3. 写入同域名的 Selenium Chrome。
4. Selenium 打开业务首页，后续可继续执行查询和下载。

Selenium 需要与本机 Chrome 匹配的驱动；Selenium 4 可尝试通过 Selenium Manager 自动管理驱动。

## 6. 技术流程

```text
打开登录页
  → 填写账号密码并点击登录
  → 等待背景图和拼图块加载完成
  → Base64 图片解码
  → OpenCV 定位缺口
  → 原图像素换算为页面 CSS 像素
  → CSS 像素换算为 Windows 物理屏幕像素
  → 实测滑轨位移与拼图位移比例
  → SendInput 分段拖动并校正
  → 等待业务 URL 跳转确认成功
  → 动态读取 Cookie（需要时同步 Selenium）
```

关键实现位于 `aliyun_slider/solver.py`：

- 等待图片 `naturalWidth > 0`，避免图片尚未加载时开始识别。
- 同时结合图像识别结果和实际页面尺寸计算目标位移。
- 使用物理屏幕坐标拖动，处理 Windows 缩放带来的坐标差异。
- 失败后优先点击 `#aliyunCaptcha-btn-refresh` 刷新验证。
- 以业务页面跳转作为成功条件，不能仅凭滑块消失判断成功。

## 7. Cookie 说明

项目没有写死 Cookie。Cookie 在登录成功后通过 `context.cookies()` 动态获取。

案例使用独立 Chrome 配置目录保存浏览器状态。如果需要强制重新登录，可停止程序后删除：

```text
.runtime/chrome-profile
.runtime/chrome-selenium-profile
```

删除配置目录会清除该案例保存的浏览器登录状态。

## 8. 常见问题

### `No module named 'pyee.asyncio'`

通常是 `pyee` 版本过旧，或依赖安装到了其他 Python 环境。使用运行脚本时的同一个解释器执行：

```powershell
.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall pyee==12.0.0 greenlet==3.1.1 playwright==1.48.0 -i https://pypi.org/simple
```

### `No matching distribution found for playwright`

检查 Python 是否低于 3.8，并改用官方 PyPI 或阿里云镜像。务必使用 `python.exe -m pip`，避免安装到系统 Python 而非虚拟环境。

### `pyppeteer requires pyee<9`

这是环境中其他包的依赖冲突。本项目使用 Playwright，需要保留 `pyee==12.0.0`，不要为了旧版 pyppeteer 降级。

### Chrome 启动失败或配置被占用

关闭使用同一配置目录的 Chrome 进程后重试。两个案例已经使用不同的 `.runtime` 配置目录，不会接管日常 Chrome 配置。

### 拖动没有反应

确认已安装 PyDirectInput、桌面未锁定、Chrome 位于前台，并且程序不是运行在 Windows 服务或断开的远程会话中。

## 9. 二次开发

`login_flow_jzt()` 是九州通站点的完整登录编排。接入其他站点时，主要调整 `solver.py` 中：

- `HOME_URL`、`LOGIN_URL`：站点地址。
- `login()`：账号、密码和登录按钮选择器。
- 滑块选择器：背景图、拼图块、滑块按钮和刷新按钮。
- 登录成功条件：目标 URL 或登录后的页面元素。

Cookie 同步可独立复用：

```python
from aliyun_slider import sync_cookies_to_selenium

count = sync_cookies_to_selenium(context, selenium_driver, login_url)
```

同步前 Selenium 和 Playwright 必须访问相同站点域名；`HttpOnly` Cookie 不需要也不能通过页面 JavaScript 读取。
