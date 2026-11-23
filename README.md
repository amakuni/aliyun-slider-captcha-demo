# 阿里云滑块验证码 Python 组件

基于真实 Chrome、Playwright、OpenCV 和 Windows SendInput 实现的阿里云验证码 2.0 滑块组件。当前登录流程适配九州通 `flow.jztit.com`，提供缺口识别、屏幕坐标换算、滑轨比例修正、拖动校正、失败重试以及 Selenium 登录态同步能力。

> 仅用于有权测试或自动化的系统。验证结果由目标网站的服务端风控决定，本组件不承诺所有网络环境均可通过。

## 项目结构

```text
aliyun-slider-captcha-demo/
├─ aliyun_slider/
│  ├─ __init__.py              # 包对外接口
│  ├─ solver.py                # 浏览器启动、缺口识别、滑块拖动和登录流程
│  └─ selenium_bridge.py       # Playwright Cookie 同步到 Selenium
├─ requirements.txt            # 固定版本依赖
├─ .gitignore                  # 临时文件和运行数据忽略规则
└─ README.md                   # 项目说明
```

## 核心功能

- 使用本机真实 Chrome，而非 Playwright 自带 Chromium。
- 使用独立、持久化的 Chrome 用户配置目录。
- 等待验证码背景图和拼图块加载完成后再处理图片。
- 通过 OpenCV 分析背景图和拼图轮廓，定位缺口位置。
- 换算原图、CSS 像素和 Windows 物理屏幕像素之间的距离。
- 根据滑轨与拼图的实际位移比例修正拖动距离。
- 使用 PyDirectInput 调用系统级鼠标事件，并分段移动和校正。
- 验证失败后刷新验证码并重试。
- 以业务页面跳转判断登录结果，避免仅凭滑块消失误判成功。
- 登录后动态读取 Cookie，并可同步至同域名的 Selenium WebDriver。

## 对外接口

| 接口 | 作用 |
|---|---|
| `launch_browser()` | 启动真实 Chrome，返回 Playwright 上下文和页面对象 |
| `login_flow_jzt()` | 执行九州通账号登录、滑块验证和失败重试，返回布尔结果 |
| `sync_cookies_to_selenium()` | 将 Playwright 当前站点 Cookie 写入 Selenium WebDriver |

以上接口统一从 `aliyun_slider` 包导出。

## 处理流程

```text
启动真实 Chrome
  → 打开登录页并填写凭据
  → 点击登录以唤起阿里云验证码
  → 等待背景图和拼图块加载
  → 解码图片并识别缺口
  → 计算页面距离与物理屏幕距离
  → 测量滑轨和拼图的位移比例
  → SendInput 按住、分段拖动并校正
  → 等待业务页面跳转
  → 读取动态 Cookie 或同步 Selenium
```

## 运行环境

- Windows 10、Windows 11 或带交互式桌面的 Windows Server。
- Python 3.8 及以上，建议使用 Python 3.9 64 位。
- 本机已安装 Google Chrome。
- Windows 桌面必须保持解锁，Chrome 窗口需要可见并处于前台。
- 拖动过程中不应操作鼠标或切换远程桌面窗口。

本组件依赖真实窗口和系统级鼠标事件，不能用于无头模式、Windows 服务、计划任务的 Session 0 或已经断开的 RDP 会话。

## 安装依赖

在项目根目录创建虚拟环境并安装依赖：

```powershell
py -3.9 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.org/simple
```

官方源访问不稳定时，可使用阿里云镜像：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

组件通过 `channel='chrome'` 使用本机 Chrome，不需要执行 `playwright install chromium`。

## 依赖说明

| 依赖包 | 固定版本 | 用途 |
|---|---:|---|
| playwright | 1.48.0 | 浏览器控制、页面操作、验证码图片及 Cookie 读取 |
| pyee | 12.0.0 | Playwright 事件依赖，提供 `pyee.asyncio` |
| greenlet | 3.1.1 | Playwright 同步 API 依赖 |
| PyDirectInput | 1.0.4 | 通过 SendInput 发送系统级鼠标事件 |
| opencv-python | 4.10.0.84 | 图片处理、轮廓分析和缺口定位 |
| numpy | 1.26.4 | 图像矩阵计算 |
| selenium | 4.26.1 | 接管验证后的业务页面自动化 |

安装和运行必须使用同一个 Python 解释器，建议统一采用 `.venv\Scripts\python.exe -m pip`，避免依赖被安装到系统 Python 或其他虚拟环境。

## 配置项

账号密码优先使用 `login_flow_jzt()` 的 `account` 和 `pwd` 参数，其次读取环境变量，最后读取 `aliyun_slider/.env`。

| 配置 | 是否必需 | 说明 |
|---|---|---|
| `JZT_ACCOUNT` | 未通过参数提供账号时必需 | 九州通登录账号 |
| `JZT_PWD` | 未通过参数提供密码时必需 | 九州通登录密码 |
| `JZT_CHROME_USER_DATA` | 否 | 自定义独立 Chrome 用户配置目录 |

默认 Chrome 配置目录为：

```text
%LOCALAPPDATA%\jzt_chrome_profile
```

该目录用于保存浏览器登录状态，不能使用 Chrome 日常默认的 `User Data` 目录。Chrome 136 及以上版本会拒绝对默认目录开启远程调试。

验证码截图、识别图片和失败诊断文件保存在：

```text
aliyun_slider/jzt_pw_tmp/
```

## Cookie 与 Selenium

组件没有固定 Cookie。登录成功后，通过 Playwright 的 `context.cookies()` 获取当前会话 Cookie。

同步到 Selenium 时需要满足：

1. Playwright 已完成登录。
2. Selenium 已打开相同站点域名。
3. 写入 Cookie 后重新访问业务页面。
4. 根据业务 URL 或登录后的页面元素确认登录状态。

`HttpOnly` Cookie 由浏览器上下文直接读取，不需要通过页面 JavaScript 获取。Selenium 需要与本机 Chrome 匹配的驱动。

## 二次开发

当前 `login_flow_jzt()` 是九州通站点的登录编排。接入其他使用阿里云验证码的站点时，需要按目标页面调整 `solver.py` 中：

- `HOME_URL` 和 `LOGIN_URL`。
- 账号、密码输入框及登录按钮选择器。
- 背景图、拼图块、滑块和刷新按钮选择器。
- 图片显示尺寸与原始尺寸的比例。
- 登录成功后的 URL 或页面元素判断条件。

如果目标网站的验证码 iframe、DOM 结构或服务端验证流程不同，仅替换登录地址不足以完成适配。

## 常见问题

### 缺少 `pyee.asyncio`

通常是 `pyee` 版本过旧或安装到了其他 Python 环境。使用运行组件的同一解释器重新安装固定版本：

```powershell
.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall pyee==12.0.0 greenlet==3.1.1 playwright==1.48.0 -i https://pypi.org/simple
```

### 找不到 Playwright 安装包

确认 Python 为 3.8 或更高版本，并使用官方 PyPI 或完整镜像源。出现 `pyppeteer requires pyee<9` 时，不要为旧版 pyppeteer 降级本项目所需的 `pyee`。

### Chrome 启动超时

检查 Chrome 是否已安装、配置目录是否被残留进程占用，以及是否误用了 Chrome 默认 `User Data` 目录。关闭使用同一自动化配置目录的 Chrome 后再运行。

### 滑块不移动或拖动中断

确认 PyDirectInput 安装成功、桌面未锁定、Chrome 位于前台，并避免在拖动期间操作鼠标。断开的远程桌面和 Session 0 无法提供所需交互环境。

### 滑块吻合但登录失败

不能仅根据拼图位置或滑块消失判断成功。应检查目标网站是否完成服务端验证并跳转到登录后的业务页面，同时保留终端日志和诊断图片用于定位。
