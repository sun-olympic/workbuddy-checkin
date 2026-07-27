#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 签到 · 命令行运行器（CLI）
==================================
一个命令搞定全部配置与运行，无需手改 plist / 任务计划 / json：

  python3 checkin_cli.py wizard                 # 交互式配置向导（逐步引导，回车用默认值，可 clear 清空）
  python3 checkin_cli.py config                 # 同上：不带参数 = 进入向导；带参数 = 命令行改单项
  python3 checkin_cli.py config   --time 09:10 --wechat <pushplus_token> [--desktop on|off] [--retries 4] [--delay 30]
  python3 checkin_cli.py install                # 注册每日定时任务和登录补跑任务
  python3 checkin_cli.py uninstall              # 卸载定时任务
  python3 checkin_cli.py uninstall --purge      # 卸载并彻底删除所有签到运行配置
  python3 checkin_cli.py uninstall --purge --codebuddy  # 另删除 CodeBuddy CLI 和登录态
  python3 checkin_cli.py status                 # 查看当前配置 / 定时 / 注册状态
  python3 checkin_cli.py wx-bind                 # 微信一键绑定：弹出浏览器，除扫码外全自动
  python3 checkin_cli.py run [--dry-run|--force|--no-retry]   # 立即手动跑一次
  python3 checkin_cli.py test-notify            # 发送一条测试通知（验证系统通知/微信推送）

说明：
  - 定时时间、微信绑定等都通过 `config` 写入 checkin_config.json；macOS 使用
    LaunchAgents plist，Windows 使用任务计划程序。
  - 微信推送（任选其一或叠加，均需自备账号，脚本不内置任何平台）：
      ① 微信测试号（公众平台接口测试号，个人轻量、免企业认证，推荐）：运行 wx-bind，
         脚本自动获取凭证、创建模板、识别 OpenID、保存配置并发送测试消息；用户只需扫码。
      ② pushplus（第三方聚合）：填入 pushplus.plus 的 token，站点内扫码绑定即可。
      ③ 通用 webhook：填任意服务的 URL（Server酱 / WxPusher / 自建等），适配任意推送。
  - install / uninstall 在 macOS 使用 launchctl，在 Windows 使用任务计划程序。
  - 未安装 WorkBuddy 时，向导可安装独立 CodeBuddy CLI 并打开浏览器完成登录。
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
import tempfile
import plistlib
import shutil
import shlex
import urllib.request
import urllib.error
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(BASE_DIR, "workbuddy_checkin.py")
CONFIG_PATH = os.path.join(BASE_DIR, "checkin_config.json")
WX_SANDBOX_LOGIN = "https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login"
WX_TEMPLATE_TITLE = "签到通知"
WX_TEMPLATE_CONTENT = "结果：{{keyword1.DATA}}\n说明：{{keyword2.DATA}}\n时间：{{keyword3.DATA}}"
WX_BIND_VENV = os.path.join(BASE_DIR, ".venv-wx-bind")
PLIST_NAME = "com.user.workbuddy-checkin.plist"
PLIST_SRC = os.path.join(BASE_DIR, PLIST_NAME)
PLIST_DST = os.path.expanduser(os.path.join("~/Library/LaunchAgents", PLIST_NAME))
LABEL = "com.user.workbuddy-checkin"
WINDOWS_DAILY_TASK = "WorkBuddyCheckin"
WINDOWS_LOGON_TASK = "WorkBuddyCheckin-Logon"
WORKBUDDY_APP_CANDIDATES = (
    "/Applications/WorkBuddy.app",
    os.path.expanduser("~/Applications/WorkBuddy.app"),
)
CODEBUDDY_NPM_PACKAGE = "@tencent-ai/codebuddy-code"
CODEBUDDY_AUTH_FILENAMES = (
    "Tencent-Cloud.coding-copilot.info",
    "workbuddy-desktop.info",
)

# 用运行本 CLI 的 python 执行 worker（worker 仅用标准库，任意 python 皆可）
PY = sys.executable


# ------------------------- 配置读写 -------------------------
def read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def write_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    # 配置包含 appsecret / token，只允许当前用户读取。
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# ------------------------- plist 生成 -------------------------
def build_plist(hour, minute):
    """根据配置的时间生成 launchd plist 字典。"""
    log_out = os.path.join(BASE_DIR, "launchd.out.log")
    log_err = os.path.join(BASE_DIR, "launchd.err.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [PY, WORKER],
        "WorkingDirectory": BASE_DIR,
        "RunAtLoad": True,                 # 开机/登录补跑
        "StartCalendarInterval": {         # 每天定点跑
            "Hour": int(hour),
            "Minute": int(minute),
        },
        "StandardOutPath": log_out,
        "StandardErrorPath": log_err,
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
        },
    }


def write_plist(hour, minute):
    plist = build_plist(hour, minute)
    with open(PLIST_SRC, "wb") as f:
        plistlib.dump(plist, f)
    # 同步到 LaunchAgents（覆盖旧配置，含最新时间）
    os.makedirs(os.path.dirname(PLIST_DST), exist_ok=True)
    with open(PLIST_DST, "wb") as f:
        plistlib.dump(plist, f)
    return PLIST_DST


# ------------------------- launchctl 封装 -------------------------
def _run(cmd):
    """执行命令，返回 (returncode, stdout, stderr)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def launchctl_installed():
    rc, out, err = _run(["launchctl", "list"])
    if rc != 0:
        return False, err
    return (LABEL in out), ""


def _windows_task_exists(task_name):
    rc, _, err = _run(["schtasks", "/Query", "/TN", task_name])
    return rc == 0, err


def schedule_installed():
    """返回当前平台的定时任务是否完整注册。"""
    if sys.platform == "win32":
        daily, daily_error = _windows_task_exists(WINDOWS_DAILY_TASK)
        logon, logon_error = _windows_task_exists(WINDOWS_LOGON_TASK)
        return daily and logon, daily_error or logon_error
    return launchctl_installed()


def _windows_task_action():
    """生成 Windows 任务计划程序可接受的带引号命令行。"""
    return subprocess.list2cmdline([PY, WORKER])


def _install_windows():
    cfg = read_config()
    hour = int(cfg.get("_schedule_hour", 9))
    minute = int(cfg.get("_schedule_minute", 10))
    action = _windows_task_action()
    daily_command = [
        "schtasks", "/Create", "/F",
        "/TN", WINDOWS_DAILY_TASK,
        "/TR", action,
        "/SC", "DAILY", "/ST", f"{hour:02d}:{minute:02d}",
        "/RL", "LIMITED",
    ]
    logon_command = [
        "schtasks", "/Create", "/F",
        "/TN", WINDOWS_LOGON_TASK,
        "/TR", action,
        "/SC", "ONLOGON",
        "/RL", "LIMITED",
    ]
    for index, command in enumerate((daily_command, logon_command)):
        rc, out, err = _run(command)
        if rc != 0:
            if index == 1:
                _run(["schtasks", "/Delete", "/F", "/TN", WINDOWS_DAILY_TASK])
            return False, err or out or "Windows 任务计划程序注册失败"
    return True, "已注册 Windows 定时任务（每天 + 登录补跑）。"


def install():
    """先 bootout（若已存在）再 bootstrap，确保新配置生效。"""
    if sys.platform == "win32":
        return _install_windows()
    # 先尝试卸载旧的（忽略错误）
    _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    rc, out, err = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST_DST])
    if rc == 0:
        return True, "已注册定时任务（每天 + 开机/登录补跑）。"
    return False, (err or out or "未知错误") + "\n   请在本机终端手动执行：" \
        f"\n   launchctl bootstrap gui/{os.getuid()} {PLIST_DST}"


def uninstall():
    if sys.platform == "win32":
        failures = []
        removed = 0
        for task_name in (WINDOWS_DAILY_TASK, WINDOWS_LOGON_TASK):
            exists, _ = _windows_task_exists(task_name)
            if not exists:
                continue
            rc, out, err = _run(["schtasks", "/Delete", "/F", "/TN", task_name])
            if rc == 0:
                removed += 1
            else:
                failures.append(err or out or task_name)
        if failures:
            return False, " | ".join(failures)
        return True, f"已卸载 Windows 定时任务（删除 {removed} 项）。"

    rc, out, err = _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    # 删除 LaunchAgents 里的文件
    try:
        if os.path.exists(PLIST_DST):
            os.remove(PLIST_DST)
    except Exception as e:
        err += f" | 删除文件失败: {e}"
    not_loaded = any(text in (err or out).lower() for text in (
        "no such process", "could not find specified service", "service not found",
    ))
    if rc == 0 or not_loaded:
        return True, "已卸载定时任务并移除 plist。"
    return False, (err or out or "未知错误") + "\n   如提示未注册可忽略；本机也可手动：" \
        f"\n   launchctl bootout gui/{os.getuid()}/{LABEL}"


# ------------------------- 子命令实现 -------------------------
def _wxt_configured(cfg):
    """判断微信测试号四项是否齐全（appid/secret/touser/template_id）。"""
    return bool((cfg.get("wx_test_appid") or "").strip()
                and (cfg.get("wx_test_secret") or "").strip()
                and (cfg.get("wx_test_touser") or "").strip()
                and (cfg.get("wx_test_template_id") or "").strip())


def _find_workbuddy_app():
    """返回当前平台的 WorkBuddy 程序路径；未安装时返回空字符串。"""
    candidates = list(WORKBUDDY_APP_CANDIDATES)
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        candidates = [
            os.path.join(local_app_data, "Programs", "WorkBuddy", "WorkBuddy.exe"),
            os.path.join(local_app_data, "WorkBuddy", "WorkBuddy.exe"),
            os.path.join(program_files, "WorkBuddy", "WorkBuddy.exe"),
        ]
    return next((path for path in candidates if path and os.path.exists(path)), "")


def _launch_workbuddy_app(app_path):
    """启动当前平台的 WorkBuddy 客户端。"""
    if sys.platform == "win32":
        try:
            subprocess.Popen([app_path])
            return True
        except OSError:
            return False
    launched = subprocess.run(["open", app_path], check=False, capture_output=True)
    return launched.returncode == 0


def _prepare_schedule_definition(hour, minute):
    """macOS 预写 plist；Windows 在 install 时直接创建计划任务。"""
    if sys.platform == "darwin":
        return write_plist(hour, minute)
    return ""


def _find_codebuddy_cli():
    """返回独立 CodeBuddy CLI 路径；不使用 WorkBuddy.app 内嵌副本。"""
    return shutil.which("codebuddy") or shutil.which("cbc") or ""


def _ensure_codebuddy_cli():
    """确保无 WorkBuddy 模式所需的独立 CodeBuddy CLI 已安装。"""
    cli_path = _find_codebuddy_cli()
    if cli_path:
        print(f"✅ CodeBuddy CLI：{cli_path}")
        return cli_path

    print("⚠️  未找到独立 CodeBuddy CLI（无 WorkBuddy 模式需要它完成登录）。")
    npm = shutil.which("npm")
    if not npm:
        print("❌ 未找到 npm。请先安装 Node.js 18.20.8+，然后执行：")
        print(f"   npm install -g {CODEBUDDY_NPM_PACKAGE}")
        return ""
    try:
        answer = input("是否现在自动安装 CodeBuddy CLI？[Y/n]: ").strip().lower()
    except EOFError:
        answer = "n"
    if answer in ("n", "no"):
        print(f"❌ 已取消。可稍后手动执行：npm install -g {CODEBUDDY_NPM_PACKAGE}")
        return ""

    print("📦 正在安装独立 CodeBuddy CLI…")
    result = subprocess.run([npm, "install", "-g", CODEBUDDY_NPM_PACKAGE], check=False)
    if result.returncode != 0:
        print("❌ CodeBuddy CLI 安装失败，请根据上方 npm 提示处理后重试。")
        return ""
    cli_path = _find_codebuddy_cli()
    if not cli_path:
        print("❌ 安装完成但命令不在 PATH 中，请重新打开终端后重试。")
        return ""
    print(f"✅ CodeBuddy CLI：{cli_path}")
    return cli_path


def _workbuddy_token_ready():
    """检查 WorkBuddy 是否已产生可用于签到的未过期登录 token。"""
    try:
        import importlib
        worker = importlib.import_module("workbuddy_checkin")
        return bool(worker.extract_token())
    except Exception:
        return False


def _wait_for_workbuddy_token(timeout_seconds=180, poll_seconds=2):
    """等待 WorkBuddy 或 CodeBuddy CLI 产生有效 token。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _workbuddy_token_ready():
            return True
        time.sleep(poll_seconds)
    return _workbuddy_token_ready()


def _codebuddy_login_settings(signal_path):
    """跳过首次目录确认；hook 只记录认证事件，不作为登录成功依据。"""
    hook_path = signal_path.replace("\\", "/")
    return json.dumps({
        "trustAll": True,
        "hooks": {
            "Notification": [{
                "matcher": "auth_success",
                "hooks": [{
                    "type": "command",
                    "command": f"touch {shlex.quote(hook_path)}",
                }],
            }],
        },
    }, ensure_ascii=False)


def _wait_for_codebuddy_login(process, signal_path, timeout_seconds=180,
                              poll_seconds=0.5):
    """只以可读取的有效 token 判断成功；auth_success 不能替代凭证校验。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _workbuddy_token_ready():
            return True
        if process.poll() is not None:
            return _workbuddy_token_ready()
        time.sleep(poll_seconds)
    return _workbuddy_token_ready()


def _launch_codebuddy_login_and_wait(cli_path):
    """启动独立 CLI 的浏览器登录，并等待其官方登录状态落盘。"""
    print("🔐 正在启动无 WorkBuddy 登录，并自动唤起浏览器。")
    print("   除浏览器中的登录确认外，无需复制 Token；最多等待 180 秒。")
    with tempfile.TemporaryDirectory(prefix="workbuddy-login-") as temp_dir:
        signal_path = os.path.join(temp_dir, "auth-success")
        settings = _codebuddy_login_settings(signal_path)
        process = None
        try:
            expect = shutil.which("expect") if sys.platform == "darwin" else ""
            if expect:
                expect_script = (
                    'spawn -noecho $env(WORKBUDDY_CODEBUDDY_CLI) '
                    '--settings $env(WORKBUDDY_CODEBUDDY_SETTINGS)\n'
                    'set timeout 45\n'
                    'expect {\n'
                    '  -re {Tips for getting started|Recent activity} {}\n'
                    '  timeout { exit 124 }\n'
                    '  eof { exit 125 }\n'
                    '}\n'
                    'after 300\n'
                    'set send_slow {1 0.08}\n'
                    'send -s -- "/login"\n'
                    'set timeout 20\n'
                    'expect {\n'
                    '  -re {Switch Tencent Cloud CodeBuddy accounts} {}\n'
                    '  timeout { exit 126 }\n'
                    '  eof { exit 127 }\n'
                    '}\n'
                    'after 200\n'
                    'send -- "\\r"\n'
                    'set timeout 20\n'
                    'expect {\n'
                    '  -re {Select login method} {}\n'
                    '  timeout { exit 128 }\n'
                    '  eof { exit 129 }\n'
                    '}\n'
                    'after 300\n'
                    'send -- "\\r"\n'
                    'interact'
                )
                child_env = os.environ.copy()
                child_env["WORKBUDDY_CODEBUDDY_CLI"] = cli_path
                child_env["WORKBUDDY_CODEBUDDY_SETTINGS"] = settings
                process = subprocess.Popen([
                    expect, "-c", expect_script,
                ], cwd=BASE_DIR, env=child_env)
            else:
                process = subprocess.Popen([
                    cli_path, "--settings", settings,
                ], cwd=BASE_DIR, stdin=subprocess.PIPE, text=True)
                process.stdin.write("/login\n")
                process.stdin.flush()
        except OSError as e:
            if process is not None and process.poll() is None:
                process.terminate()
            print(f"❌ 无法启动 CodeBuddy CLI：{e}")
            return False

        try:
            ready = _wait_for_codebuddy_login(process, signal_path)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
    if not ready:
        print("❌ 等待登录超时。请确认浏览器授权成功后重试。")
        return False
    print("✅ 登录态：已从 CodeBuddy CLI 获取有效 token")
    print("✅ 环境预检通过。\n")
    return True


def _run_codebuddy_preflight():
    """完成无 WorkBuddy 模式的 CLI 安装与登录检查。"""
    cli_path = _ensure_codebuddy_cli()
    return bool(cli_path and _launch_codebuddy_login_and_wait(cli_path))


def _run_environment_preflight():
    """配置/安装前检查环境，可选择 WorkBuddy 或独立 CLI 登录。"""
    print("=== 环境预检 ===")
    if sys.platform not in ("darwin", "win32"):
        print("❌ 当前仅支持 macOS 和 Windows。")
        return False
    print(f"✅ 系统：{'Windows' if sys.platform == 'win32' else 'macOS'}")

    if sys.version_info < (3, 8):
        print("❌ Python 版本过低，请安装 Python 3.8 或更高版本。")
        return False
    print(f"✅ Python：{sys.version_info.major}.{sys.version_info.minor}")

    if _workbuddy_token_ready():
        print("✅ 登录态：已找到有效 token")
        print("✅ 环境预检通过。\n")
        return True

    app_path = _find_workbuddy_app()
    if not app_path:
        print("ℹ️  未安装 WorkBuddy，自动进入“无 WorkBuddy 模式”。")
        return _run_codebuddy_preflight()

    print(f"✅ WorkBuddy：{app_path}")

    print("⚠️  未找到有效登录 token。")
    print("请选择登录方式：")
    print("   1) 启动 WorkBuddy 登录")
    print("   2) 无 WorkBuddy 模式（独立 CodeBuddy CLI + 浏览器登录）")
    try:
        answer = input("请选择 [1]: ").strip()
    except EOFError:
        answer = "1"
    if answer == "2":
        return _run_codebuddy_preflight()
    if answer not in ("", "1"):
        print("❌ 无效选择，已取消环境预检。")
        return False

    if not _launch_workbuddy_app(app_path):
        print("❌ 无法自动启动 WorkBuddy，请手动打开后重试。")
        return False
    print("🔐 已启动 WorkBuddy，正在等待有效登录态（最多 180 秒）…")
    if not _wait_for_workbuddy_token():
        print("❌ 等待超时。请确认 WorkBuddy 已登录并产生请求日志后重试。")
        return False

    print("✅ 登录态：已自动获取有效 token")
    print("✅ 环境预检通过。\n")
    return True


def interactive_config(cfg):
    """交互式向导：逐步询问，回车用默认值；输入 clear 可清空某项。"""
    if not _run_environment_preflight():
        print("❌ 环境预检未通过，未写入配置。")
        return 1

    def ask(prompt, default=""):
        try:
            val = input(f"{prompt} [{default}]: ").strip()
        except EOFError:
            val = ""
        if val.lower() == "clear":
            return "__CLEAR__"
        # 注意：空输入返回空串，而非 default。default 仅用于显示，
        # 否则会把占位提示文字（如「留空=不设置」）当成真实值写入配置。
        return val

    def ask_yn(prompt, default_yes=True):
        d = "Y/n" if default_yes else "y/N"
        a = input(f"{prompt} [{d}]: ").strip().lower()
        if not a:
            return default_yes
        return a in ("y", "yes", "1", "true")

    def placeholder(set_val):
        return "（已设置）" if set_val else "（留空=不设置）"

    print("=== WorkBuddy 签到 · 交互式配置向导 ===")
    print("提示：直接回车 = 保留当前值 / 默认值；输入 clear = 清空该项。\n")

    # 1) 定时时间
    h = cfg.get("_schedule_hour", 9)
    m = cfg.get("_schedule_minute", 10)
    cur_time = f"{h:02d}:{m:02d}"
    while True:
        t = ask("⏰ 每日定时签到时间 (HH:MM)", cur_time)
        if t == "__CLEAR__":
            print("   ⚠️  定时时间不可清空，保留当前值")
            t = cur_time
        if not t:
            t = cur_time
        try:
            nh, nm = parse_time(t)
            break
        except ValueError as e:
            print(f"   ❌ {e}，请重新输入（如 09:10）")
    cfg["_schedule_hour"] = nh
    cfg["_schedule_minute"] = nm

    # 2) 系统通知（独立开关，不参与远程渠道单选）
    dft = "on" if cfg.get("desktop_notify", True) else "off"
    d = ask("🔔 系统通知 (on/off)", dft) or dft
    cfg["desktop_notify"] = d.lower() in ("on", "true", "1", "yes")

    # 3) 重试参数
    r = ask("🔁 失败最大重试次数", str(cfg.get("max_retries", 4))) or str(cfg.get("max_retries", 4))
    try:
        cfg["max_retries"] = max(0, int(r))
    except ValueError:
        pass
    dl = ask("⏳ 退避基数秒", str(cfg.get("retry_base_delay", 30))) or str(cfg.get("retry_base_delay", 30))
    try:
        cfg["retry_base_delay"] = max(1, int(dl))
    except ValueError:
        pass

    # 4) 远程通知方式（四选一）
    has_wxt = _wxt_configured(cfg)
    has_tok = bool(cfg.get("pushplus_token"))
    has_wh = bool(cfg.get("notify_webhook_url"))
    explicit_channel = (cfg.get("notify_channel") or "").strip()
    if explicit_channel in ("wx_test", "pushplus", "webhook", "none"):
        current_channel = explicit_channel
    elif has_wxt:
        current_channel = "wx_test"
    elif has_tok:
        current_channel = "pushplus"
    elif has_wh:
        current_channel = "webhook"
    else:
        current_channel = "none"

    channel_options = [
        ("wx_test", "微信测试号"),
        ("pushplus", "pushplus"),
        ("webhook", "自定义 webhook"),
        ("none", "不使用远程通知"),
    ]
    default_choice = next(
        str(index) for index, (key, _) in enumerate(channel_options, 1)
        if key == current_channel
    )
    print("\n📨 远程通知方式（单选，不包含系统通知）：")
    for index, (_, label) in enumerate(channel_options, 1):
        print(f"   {index}) {label}")
    while True:
        choice = ask("请选择通知方式编号", default_choice) or default_choice
        if choice.isdigit() and 1 <= int(choice) <= len(channel_options):
            notify_channel, notify_label = channel_options[int(choice) - 1]
            break
        print("   ❌ 请输入 1-4")
    cfg["notify_channel"] = notify_channel
    bind_wechat = False
    bind_mode = ""

    if notify_channel == "wx_test":
        print(f"🧪 微信测试号：{'当前已绑定' if has_wxt else '当前未绑定'}")
        bind_wechat = (not has_wxt) or ask_yn("是否重新绑定微信？", default_yes=False)
        if bind_wechat:
            print("\n🔗 微信绑定方式：")
            print("   1) 自动：打开浏览器，除扫码外全部自动完成（推荐）")
            print("   2) 手动：按向导输入 AppID、AppSecret 和模板 ID")
            while True:
                mode_choice = ask("请选择绑定方式编号", "1") or "1"
                if mode_choice in ("1", "2"):
                    bind_mode = "auto" if mode_choice == "1" else "manual"
                    break
                print("   ❌ 请输入 1-2")
            if bind_mode == "auto":
                print("   ✅ 保存基础配置后将打开浏览器；自动失败可降级为手动输入。")
            else:
                print("   ✅ 保存基础配置后将进入手动输入向导。")
        else:
            print("   ℹ️  使用已有微信绑定。")
    elif notify_channel == "pushplus":
        print("📱 pushplus：去 https://www.pushplus.plus 获取 token")
        tok = ask("   pushplus token", placeholder(has_tok))
        if tok == "__CLEAR__":
            cfg["pushplus_token"] = ""
        elif tok and tok != "（已设置）":
            cfg["pushplus_token"] = tok
    elif notify_channel == "webhook":
        wh = ask("   webhook URL", placeholder(has_wh))
        if wh == "__CLEAR__":
            cfg["notify_webhook_url"] = ""
            cfg["notify_webhook_method"] = ""
            cfg["notify_webhook_template"] = ""
        elif wh and wh != "（已设置）":
            cfg["notify_webhook_url"] = wh
            cur_m = cfg.get("notify_webhook_method", "POST")
            mth = ask("   webhook 请求方法 (POST/GET)", cur_m or "POST") or (cur_m or "POST")
            if mth.upper() in ("POST", "GET"):
                cfg["notify_webhook_method"] = mth.upper()
            tpl = ask("   webhook 模板 JSON（可选，{title}/{content}）", "（留空=默认）")
            if tpl == "__CLEAR__":
                cfg["notify_webhook_template"] = ""
            elif tpl and tpl != "（留空=默认）":
                cfg["notify_webhook_template"] = tpl

    # 5) 预览 + 确认
    print("\n--- 配置预览 ---")
    print(f"  定时时间      : {cfg['_schedule_hour']:02d}:{cfg['_schedule_minute']:02d}")
    print(f"  最大重试/退避 : {cfg.get('max_retries')} / {cfg.get('retry_base_delay')}s")
    print(f"  系统通知      : {'开启' if cfg.get('desktop_notify', True) else '关闭'}")
    print(f"  远程通知      : {notify_label}")
    if notify_channel == "wx_test":
        binding_preview = (
            f"将使用{'自动' if bind_mode == 'auto' else '手动'}方式绑定"
            if bind_wechat else "使用已有绑定"
        )
        print(f"  微信测试号    : {binding_preview}")

    if not ask_yn("确认保存以上配置？", default_yes=True):
        print("❌ 已取消，未做任何修改。")
        return 0

    write_config(cfg)
    dst = _prepare_schedule_definition(cfg["_schedule_hour"], cfg["_schedule_minute"])
    print(f"\n✅ 配置已保存：{CONFIG_PATH}")
    if dst:
        print(f"✅ 已生成 plist：{dst}（每天 {cfg['_schedule_hour']:02d}:{cfg['_schedule_minute']:02d} + 登录补跑）")
    else:
        print("✅ Windows 定时配置已就绪；执行 install 后创建“每天 + 登录补跑”任务。")
    if bind_wechat:
        print("\n🌐 正在进入微信绑定流程…")
        bind_args = argparse.Namespace(
            mode=bind_mode,
            no_bootstrap=False,
            install_browser=False,
        )
        bind_result = cmd_wx_bind(bind_args)
        if bind_result != 0:
            print("⚠️  基础配置已保存，但微信绑定未完成；可稍后重新运行 wx-bind。")
            return bind_result
    print("\n下一步：")
    print(f"   python3 {os.path.basename(__file__)} install       # 注册定时任务")
    print(f"   python3 {os.path.basename(__file__)} test-notify   # 验证推送链路")
    return 0


def cmd_wizard(args):
    """进入交互式配置向导（等价于 config 不带任何参数）。"""
    return interactive_config(read_config())


def cmd_config(args):
    cfg = read_config()

    # 没有任何参数 → 进入交互式向导（不敲参数也能配）
    no_args = not (args.time or args.wechat is not None or args.desktop
                   or args.retries is not None or args.delay is not None
                   or args.webhook is not None or args.webhook_template is not None
                   or args.webhook_method is not None
                   or args.wx_appid is not None or args.wx_secret is not None
                   or args.wx_touser is not None or args.wx_template is not None
                   or args.wx_data is not None
                   or getattr(args, "wizard", False))
    if no_args:
        return interactive_config(cfg)

    changed = []

    # 时间
    if args.time:
        try:
            h, m = parse_time(args.time)
        except ValueError as e:
            print(f"❌ 时间格式错误：{e}")
            return 1
        cfg["_schedule_hour"] = h
        cfg["_schedule_minute"] = m
        changed.append(f"定时时间 = {h:02d}:{m:02d}")

    # 微信（pushplus token）
    if args.wechat is not None:
        cfg["pushplus_token"] = args.wechat.strip()
        if args.wechat.strip():
            cfg["notify_channel"] = "pushplus"
            changed.append("微信绑定 = 已设置（pushplus token）")
        else:
            if cfg.get("notify_channel") == "pushplus":
                cfg["notify_channel"] = "none"
            changed.append("微信绑定 = 已清除")

    # 桌面通知开关
    if args.desktop is not None:
        cfg["desktop_notify"] = (args.desktop.lower() in ("on", "true", "1", "yes"))
        changed.append(f"系统通知 = {cfg['desktop_notify']}")

    # 重试参数
    if args.retries is not None:
        cfg["max_retries"] = max(0, int(args.retries))
        changed.append(f"最大重试 = {cfg['max_retries']}")
    if args.delay is not None:
        cfg["retry_base_delay"] = max(1, int(args.delay))
        changed.append(f"退避基数 = {cfg['retry_base_delay']}s")

    # 通用自定义 webhook
    if args.webhook is not None:
        cfg["notify_webhook_url"] = args.webhook.strip()
        if args.webhook.strip():
            cfg["notify_channel"] = "webhook"
            changed.append("自定义 webhook = 已设置")
        else:
            if cfg.get("notify_channel") == "webhook":
                cfg["notify_channel"] = "none"
            changed.append("自定义 webhook = 已清除")
    if args.webhook_template is not None:
        cfg["notify_webhook_template"] = args.webhook_template
        changed.append("自定义 webhook 模板 = 已设置")
    if args.webhook_method is not None:
        cfg["notify_webhook_method"] = args.webhook_method.upper()
        changed.append(f"自定义 webhook 方法 = {cfg['notify_webhook_method']}")

    # 微信公众平台「接口测试号」（原生支持，推到个人微信）
    for arg, key in ((args.wx_appid, "wx_test_appid"),
                     (args.wx_secret, "wx_test_secret"),
                     (args.wx_touser, "wx_test_touser"),
                     (args.wx_template, "wx_test_template_id"),
                     (args.wx_data, "wx_test_data")):
        if arg is not None:
            cfg[key] = arg.strip()
            changed.append(f"微信测试号[{key}] = 已设置")
    if any(arg is not None and arg.strip() for arg in (
            args.wx_appid, args.wx_secret, args.wx_touser, args.wx_template)):
        cfg["notify_channel"] = "wx_test"
    elif cfg.get("notify_channel") == "wx_test" and not _wxt_configured(cfg):
        cfg["notify_channel"] = "none"

    write_config(cfg)

    # macOS 重写 plist；Windows 由 install 直接更新任务计划。
    h = cfg.get("_schedule_hour")
    m = cfg.get("_schedule_minute")
    if h is not None and m is not None:
        dst = _prepare_schedule_definition(h, m)
        if dst:
            print(f"✅ 已重写 plist：{dst}（每天 {h:02d}:{m:02d} + 登录补跑）")
        else:
            print(f"✅ 已保存 Windows 定时时间：{h:02d}:{m:02d}（重新执行 install 生效）。")
    else:
        print("ℹ️  尚未设置定时时间（用 --time HH:MM 设置）。")

    if changed:
        print("📝 配置变更：")
        for c in changed:
            print(f"   - {c}")
    else:
        print("ℹ️  未指定任何变更项。")

    # 微信绑定提示
    wxt_ok = bool((cfg.get("wx_test_appid") or "").strip()
                  and (cfg.get("wx_test_secret") or "").strip()
                  and (cfg.get("wx_test_touser") or "").strip()
                  and (cfg.get("wx_test_template_id") or "").strip())
    if cfg.get("pushplus_token") or wxt_ok:
        print("✅ 微信已绑定，签到结果将推送到你的微信。")
    else:
        print("💡 未绑定微信：系统通知仍可用。推荐方式：")
        print(f"   1) 微信测试号（一键配置，推荐！）：")
        print(f"      python3 {os.path.basename(__file__)} wx-bind")
        print(f"      （浏览器弹窗扫码；凭证、模板、OpenID、保存和测试均自动完成）")
        print(f"   2) pushplus（一个 token 搞定）：")
        print(f"      python3 {os.path.basename(__file__)} config --wechat <你的token>")
        print(f"   3) 通用 webhook：")
        print(f"      python3 {os.path.basename(__file__)} config --webhook <URL> [--webhook-method GET]")
    return 0


EXTRACT_CREDS_JS = """() => {
    const res = {appid:'', appsecret:''};
    const inputs = Array.from(document.querySelectorAll('input'));
    for (const i of inputs) {
        const v = (i.value||'').trim();
        const key = ((i.id||'')+(i.name||'')+(i.placeholder||'')).toLowerCase();
        if (/appid|app_id|开发者id|开发者/i.test(key) && /^wx[0-9a-f]{8,}$/i.test(v)) res.appid = v;
        if (/appsecret|app_secret|开发者密码|密码/i.test(key) && /^[0-9a-f]{16,}$/i.test(v)) res.appsecret = v;
    }
    if (!res.appid) { const m = document.body.innerText.match(/wx[0-9a-f]{8,}/i); if (m) res.appid = m[0]; }
    if (!res.appsecret) { const m = document.body.innerText.match(/[0-9a-f]{32}/i); if (m) res.appsecret = m[0]; }
    return res;
}"""


def _install_playwright():
    """自动安装 playwright 包与 Chromium 浏览器（用于 wx-login）。返回是否成功。"""
    print("🔧 未检测到 Playwright，正在尝试自动安装（需联网，浏览器约 150MB）…")
    rc1 = subprocess.run([sys.executable, "-m", "pip", "install", "playwright"],
                         capture_output=True, text=True)
    if rc1.returncode != 0:
        print("❌ 安装 playwright 包失败：")
        print((rc1.stderr or "").strip()[-600:])
        return False
    rc2 = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                         capture_output=True, text=True)
    if rc2.returncode != 0:
        print("⚠️ 安装 Chromium 浏览器失败，将尝试使用系统已装的 Chrome；")
        print("   你也可以手动执行：playwright install chromium")
        print((rc2.stderr or "").strip()[-600:])
    return True


def _find_qr(page):
    """在页面（含子 frame）中定位二维码元素，覆盖 mp + open.weixin.qq.com 两种页面。"""
    selectors = [
        "img[src*='loginqrcode']", "img[src*='qrcode']", "canvas",
        "[class*='qrcode']", "[class*='qrCode']",
        "#wx_login_qrcode_img",         # open.weixin.qq.com 扫码页
        "img.qrcode_img",               # 备用
        "[class*='qrconnect']",         # 容器
    ]
    for s in selectors:
        el = page.query_selector(s)
        if el:
            return el
    for fr in page.frames:
        for s in selectors:
            el = fr.query_selector(s)
            if el:
                return el
    return None


def _auto_create_template(page, appid, secret):
    """尝试通过浏览器自动化建模板；成功返回 template_id，失败返回空串。
    策略：滚动到模板区域 → 点新增按钮 → 填表 → 提交 → 解析返回的 template_id。
    若 DOM 操作失败（页面结构变化），再引导用户手动创建后用 API 自动检测。"""
    TPL_TITLE = "签到通知"
    TPL_CONTENT = WX_TEMPLATE_CONTENT

    try:
        # Step A: 记下现有模板列表（用于后续比对）
        old_templates = set()
        try:
            token = _wx_api_token(appid, secret)
            if token:
                url = (f"https://api.weixin.qq.com/cgi-bin/template/get_all_private_template"
                       f"?access_token={token}")
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())
                for t in data.get("template_list", []):
                    old_templates.add(t["template_id"])
        except Exception:
            pass

        # Step B: 尝试 DOM 自动化
        page.wait_for_timeout(2000)
        # 向下滚动找到模板区域
        page.evaluate("window.scrollBy(0, 1200)")
        page.wait_for_timeout(1000)

        # 尝试找到「新增测试模板」按钮并点击
        for sel in [
            "input[type='button'][value*='新增']",
            "input[type='submit'][value*='新增']",
            "button:has-text('新增测试模板')",
            "text='新增测试模板'",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    print("  ↪ 已点击「新增测试模板」")
                    break
            except Exception:
                continue
        else:
            # DOM 匹配失败 → 让用户手动在浏览器里创建，用 API 检测
            raise RuntimeError("找不到按钮")

        # 填表单
        page.evaluate("""(args) => {
            const { title, content } = args;
            // 尝试填充标题
            let dest = document.querySelector('input[type="text"]') ||
                       document.querySelector('input:not([type])') ||
                       document.querySelector('input');
            if (dest) { dest.focus(); dest.value = ''; dest.value = title; dest.dispatchEvent(new Event('input',{bubbles:true})); }

            // 尝试填充内容
            let ta = document.querySelector('textarea');
            if (ta) { ta.focus(); ta.value = ''; ta.value = content; ta.dispatchEvent(new Event('input',{bubbles:true})); }
        }""", {"title": TPL_TITLE, "content": TPL_CONTENT})
        page.wait_for_timeout(500)

        # 提交
        for sel in [
            "input[value='确定']",
            "button:has-text('确定')",
            "button[type='submit']",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=1000):
                    btn.click(timeout=3000)
                    page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

    except Exception as e:
        print(f"  DOM 自动化失败：{e}")
        # 不需要异常处理，等下面降级

    # Step C: 无论 DOM 成没成，用 API 检测新模板
    try:
        token = _wx_api_token(appid, secret)
        if not token:
            return ""
        url = (f"https://api.weixin.qq.com/cgi-bin/template/get_all_private_template"
               f"?access_token={token}")
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for t in data.get("template_list", []):
            if t["template_id"] not in old_templates:
                # 验证是双花括号
                if "{{" in t.get("content", ""):
                    return t["template_id"]
        return ""
    except Exception:
        return ""


def _try_capture_openid(page, follow_png, appid, secret):
    """抓关注者 OpenID：优先用 API（已登录态下用户列表立即可见），
    若 API 失败再降级为页面截图+扫码。返回 OpenID 或空串。"""
    # 1) 先用 API 拉（最稳，与 wx-setup 一致）
    try:
        sys.path.insert(0, BASE_DIR)
        import importlib
        mod = importlib.import_module("__not_loaded__")  # 不可用
    except Exception:
        pass

    # 直接用 urllib 调 API（避免循环导入 worker）
    try:
        import urllib.parse
        token_url = (f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
                     f"&appid={urllib.parse.quote(appid)}&secret={urllib.parse.quote(secret)}")
        req = urllib.request.Request(token_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            tk = json.loads(r.read())
        access_token = tk.get("access_token", "")
        if not access_token:
            print(f"   （API 拿 token 失败：{tk.get('errmsg', 'unknown')}）")
        else:
            users_url = (f"https://api.weixin.qq.com/cgi-bin/user/get"
                         f"?access_token={access_token}&next_openid=")
            req = urllib.request.Request(users_url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                ud = json.loads(r.read())
            openids = (ud.get("data") or {}).get("openid", [])
            if openids:
                return openids[0]
    except Exception as e:
        print(f"   （API 拉关注者失败：{e}，降级为页面扫码）")

    # 2) 降级：页面截图引导扫码（老逻辑，作为兜底）
    try:
        page.screenshot(path=follow_png)
        _open_local_path(follow_png)
        print("📲 已打开「关注者二维码」截图，请用微信扫码关注测试号（关注后 OpenID 才会出现）。")
        print("   等待关注中（最多 60 秒，若已关注可忽略）…")
        for _ in range(30):
            try:
                oid = page.evaluate("""() => {
                    const t = document.body ? document.body.innerText : '';
                    const ms = t.match(/[0-9a-zA-Z_]{28,}/g) || [];
                    for (const s of ms) {
                        // 排除 wx 开头的（appid）和全 32 位十六进制（appsecret）
                        if (/^wx/i.test(s)) continue;
                        if (/^[0-9a-f]{32}$/i.test(s)) continue;
                        return s;
                    }
                    return '';
                }""")
                if oid:
                    return oid
            except Exception:
                pass
            page.wait_for_timeout(2000)
    except Exception:
        pass
    return ""


def _matching_template_id(templates):
    """返回可直接用于签到通知的现有模板，避免重复创建。"""
    # 微信客户端可能折叠“整行只有变量”的普通 keyword，因此必须
    # 同时校验静态标签；旧的无标签模板不能继续复用。
    required_segments = tuple(
        re.sub(r"\s+", "", line)
        for line in WX_TEMPLATE_CONTENT.splitlines()
        if line.strip()
    )
    for template in templates or []:
        title = re.sub(r"\s+", "", str(template.get("title") or ""))
        content = re.sub(r"\s+", "", str(template.get("content") or ""))
        if WX_TEMPLATE_TITLE in title and all(
                segment in content for segment in required_segments):
            return str(template.get("template_id") or "")
    return ""


def _choose_openid(before, current, configured=""):
    """从关注者列表选择扫码用户；新出现的关注者优先，绝不猜多个旧用户。"""
    before_set = set(before or [])
    current_list = list(dict.fromkeys(current or []))
    new_followers = [openid for openid in current_list if openid not in before_set]
    if len(new_followers) == 1:
        return new_followers[0]
    if configured and configured in current_list:
        return configured
    if len(current_list) == 1:
        return current_list[0]
    return ""


def _with_wx_binding(cfg, appid, secret, openid, template_id):
    """生成完整微信绑定配置，同时保留签到时间等无关设置。"""
    result = dict(cfg)
    result.update({
        "wx_test_appid": appid,
        "wx_test_secret": secret,
        "wx_test_touser": openid,
        "wx_test_template_id": template_id,
        "notify_channel": "wx_test",
    })
    return result


def _wx_api_templates(appid, secret):
    """获取测试号模板列表；网络或接口失败时返回空列表。"""
    token = _wx_api_token(appid, secret)
    if not token:
        return []
    try:
        url = ("https://api.weixin.qq.com/cgi-bin/template/get_all_private_template"
               f"?access_token={token}")
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if data.get("errcode") not in (None, 0):
            print(f"  ⚠️  获取模板列表失败：{data.get('errmsg', data.get('errcode'))}")
            return []
        return data.get("template_list") or []
    except Exception as e:
        print(f"  ⚠️  网络错误（获取模板列表）：{e}")
        return []


def _bootstrap_wx_bind_runtime():
    """把 Playwright 装入项目专用 venv，返回重启命令；不污染当前 Python。"""
    venv_python = os.path.join(WX_BIND_VENV, "bin", "python3")
    if not os.path.exists(venv_python):
        print("🔧 首次运行：正在创建微信绑定专用环境…")
        proc = subprocess.run([sys.executable, "-m", "venv", WX_BIND_VENV])
        if proc.returncode != 0:
            return []

    print("📦 正在安装浏览器自动化组件 Playwright（只需一次）…")
    proc = subprocess.run([
        venv_python, "-m", "pip", "install", "--disable-pip-version-check", "playwright",
    ])
    if proc.returncode != 0:
        return []
    return [
        venv_python,
        os.path.abspath(__file__),
        "wx-bind",
        "--no-bootstrap",
        "--no-fallback",
        "--mode",
        "auto",
    ]


def _extract_credentials_from_context(context):
    """在所有标签页中查找沙箱控制台与 appid/appsecret。"""
    for candidate in reversed(context.pages):
        try:
            creds = candidate.evaluate(EXTRACT_CREDS_JS)
        except Exception:
            continue
        appid = (creds.get("appid") or "").strip()
        secret = (creds.get("appsecret") or "").strip()
        if appid and secret:
            return candidate, appid, secret
    return None, "", ""


def _open_login_and_wait_for_scan(context, timeout_seconds=180):
    """打开沙箱页，必要时点击登录，等待用户扫码后返回控制台页与凭证。"""
    page = context.new_page()
    page.goto(WX_SANDBOX_LOGIN, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1500)

    dashboard, appid, secret = _extract_credentials_from_context(context)
    if dashboard:
        print("✅ 检测到已有微信登录状态，无需重复扫码。")
        return dashboard, appid, secret

    for candidate in reversed(context.pages):
        try:
            login = candidate.get_by_text("登录", exact=True).first
            if login.count() and login.is_visible(timeout=1000):
                login.click(timeout=5000)
                break
        except Exception:
            continue

    print("📱 请用手机微信扫描浏览器中的登录二维码，并在手机上确认登录。")
    print(f"   除扫码外无需再复制或填写任何内容；等待时间最长 {timeout_seconds} 秒。")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        dashboard, appid, secret = _extract_credentials_from_context(context)
        if dashboard:
            dashboard.bring_to_front()
            return dashboard, appid, secret
        for candidate in reversed(context.pages):
            try:
                candidate.wait_for_timeout(1000)
                break
            except Exception:
                continue
    return None, "", ""


def _click_new_template(page):
    """定位并点击新增测试模板入口。"""
    page.bring_to_front()
    for frame in page.frames:
        for locator in (
            frame.get_by_text("新增测试模板", exact=False).first,
            frame.locator("button:has-text('新增测试模板')").first,
            frame.locator("input[value*='新增测试模板']").first,
        ):
            try:
                if locator.count() and locator.is_visible(timeout=1000):
                    locator.scroll_into_view_if_needed()
                    locator.click(timeout=5000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
    return False


FILL_TEMPLATE_JS = """(args) => {
    const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const ownText = (el) => Array.from(el.childNodes)
        .filter(node => node.nodeType === Node.TEXT_NODE)
        .map(node => node.textContent || '').join('').trim();
    const validDialog = (el) => {
        if (!visible(el)) return false;
        const text = el.innerText || '';
        if (!/新增测试模板/.test(text) || !/模板标题/.test(text) || !/模板内容/.test(text)) return false;
        const hasInput = Array.from(el.querySelectorAll('input')).some(visible);
        const hasContent = Array.from(el.querySelectorAll('textarea, [contenteditable="true"]')).some(visible);
        return hasInput && hasContent;
    };

    // 从弹窗标题向上找包含完整表单的最近祖先。背景表单即使可见，也不会进入候选。
    const roots = [];
    const markers = Array.from(document.querySelectorAll('body *')).filter(el =>
        visible(el) && /新增测试模板/.test(ownText(el)));
    for (const marker of markers) {
        let node = marker;
        while (node && node !== document.body) {
            if (validDialog(node)) {
                roots.push(node);
                break;
            }
            node = node.parentElement;
        }
    }
    roots.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
    const root = roots[0];
    if (!root) return {title:false, content:false, submit:false, dialog:false};

    const describe = (el) => {
        const parent = el.closest('label, .weui-cell, .form-item, .control-group, tr, li, div');
        return [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                parent ? parent.innerText.slice(0, 120) : ''].filter(Boolean).join(' ').toLowerCase();
    };
    const controls = Array.from(root.querySelectorAll('input, textarea, [contenteditable="true"]')).filter(visible);
    const title = controls.find(el => el.tagName === 'INPUT' && /模板?标题|title/.test(describe(el))) ||
                  controls.find(el => el.tagName === 'INPUT' && ['text', ''].includes(el.type || ''));
    const content = controls.find(el => el.tagName === 'TEXTAREA' && /模板?内容|content/.test(describe(el))) ||
                    controls.find(el => el.tagName === 'TEXTAREA') ||
                    controls.find(el => el.isContentEditable && /模板?内容|content/.test(describe(el)));
    const set = (el, value) => {
        if (!el) return false;
        el.focus();
        if (el.isContentEditable) el.textContent = value;
        else {
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
        }
        for (const type of ['input', 'change', 'blur']) el.dispatchEvent(new Event(type, {bubbles:true}));
        return true;
    };
    const buttons = Array.from(root.querySelectorAll('button, input[type="submit"], input[type="button"], a')).filter(visible);
    const submit = buttons.find(el => /^(提交|确定|保存|添加)$/.test((el.innerText || el.value || '').trim()));
    if (submit) submit.setAttribute('data-workbuddy-template-submit', 'true');
    return {
        title: set(title, args.title),
        content: set(content, args.content),
        submit: !!submit,
        dialog: true,
    };
}"""


def _fill_and_submit_template(page):
    """填写测试模板表单并提交，兼容普通页、弹窗和 iframe。"""
    for frame in reversed(page.frames):
        try:
            result = frame.evaluate(FILL_TEMPLATE_JS, {
                "title": WX_TEMPLATE_TITLE,
                "content": WX_TEMPLATE_CONTENT,
            })
            if not (result.get("dialog") and result.get("title")
                    and result.get("content") and result.get("submit")):
                continue
            submit = frame.locator('[data-workbuddy-template-submit="true"]').first
            submit.click(timeout=5000)
            page.wait_for_timeout(1000)
            return True
        except Exception:
            continue
    return False


def _ensure_wx_template(page, appid, secret):
    """复用或自动创建签到模板，全程不要求用户粘贴 template_id。"""
    template_id = _matching_template_id(_wx_api_templates(appid, secret))
    if template_id:
        print("✅ 已找到兼容的签到模板，自动复用。")
        return template_id

    print("📝 正在自动创建微信消息模板…")
    if not _click_new_template(page) or not _fill_and_submit_template(page):
        return ""

    for _ in range(20):
        template_id = _matching_template_id(_wx_api_templates(appid, secret))
        if template_id:
            print("✅ 消息模板已自动创建。")
            return template_id
        page.wait_for_timeout(1000)
    return ""


def _show_follower_qr(page):
    """滚动并高亮测试号关注二维码，返回是否定位成功。"""
    page.bring_to_front()
    script = """() => {
        const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
        const imgs = Array.from(document.querySelectorAll('img, canvas')).filter(visible);
        let target = imgs.find(el => /qrcode|二维码/i.test([el.id, el.className, el.getAttribute('src'), el.alt].join(' ')) &&
            /测试号|关注|用户/.test((el.closest('section, tr, li, div') || el.parentElement || el).innerText || ''));
        if (!target) {
            const labels = Array.from(document.querySelectorAll('body *')).filter(el =>
                visible(el) && /测试号二维码|关注者二维码/.test(el.innerText || '') && el.children.length < 8);
            for (const label of labels) {
                const box = label.closest('section, tr, li, div') || label.parentElement;
                target = box && box.querySelector('img, canvas');
                if (target && visible(target)) break;
            }
        }
        if (!target) return false;
        target.scrollIntoView({behavior:'smooth', block:'center'});
        target.style.outline = '6px solid #07c160';
        target.style.outlineOffset = '8px';
        return true;
    }"""
    for frame in page.frames:
        try:
            if frame.evaluate(script):
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


def _ensure_wx_recipient(page, appid, secret, configured="", timeout_seconds=150):
    """复用唯一接收者，或展示二维码并等待新关注者出现。"""
    token = _wx_api_token(appid, secret)
    if not token:
        return ""
    before, _, _ = _wx_api_get_followers(token)
    selected = _choose_openid(before, before, configured)
    if selected:
        print("✅ 已识别唯一微信接收者，无需重复关注。")
        return selected

    located = _show_follower_qr(page)
    print("📲 请用手机微信扫描浏览器中绿色框出的测试号二维码并关注。")
    if not located:
        print("   页面结构有变化，已保留浏览器窗口；请在页面中找到“测试号二维码”扫码。")
    print("   关注后脚本会自动识别，无需复制 OpenID 或按回车。")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current, _, _ = _wx_api_get_followers(token)
        selected = _choose_openid(before, current, configured)
        if selected:
            print("✅ 已自动识别扫码关注的微信用户。")
            return selected
        page.wait_for_timeout(2500)
    return ""


def _launch_wx_browser(playwright):
    """优先复用本机 Chrome；没有 Chrome 时回退 Playwright Chromium。"""
    errors = []
    attempts = [
        {"channel": "chrome", "headless": False, "args": ["--start-maximized"]},
        {"headless": False, "args": ["--start-maximized"]},
    ]
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as e:
            errors.append(str(e))
    raise RuntimeError("；".join(errors)[-800:])


def _send_wx_binding_test(cfg):
    """绑定完成后立即发送测试消息。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("wbck_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    title, message = mod.build_test_message()
    return mod._wechat_test_send(title, message, cfg)


def _cmd_wx_bind_auto(args):
    """可见浏览器一键绑定微信；用户只负责扫码，其余步骤自动完成。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if getattr(args, "no_bootstrap", False):
            print("❌ Playwright 安装后仍无法导入，请检查网络后重试。")
            return 1
        command = _bootstrap_wx_bind_runtime()
        if not command:
            print("❌ 自动安装浏览器组件失败，请检查网络后重新运行 wx-bind。")
            return 1
        return subprocess.call(command)

    print("=" * 46)
    print("  WorkBuddy · 微信一键绑定")
    print("  你只需扫码，AppID / Secret / OpenID / 模板均自动处理")
    print("=" * 46)
    browser = None
    page = None
    try:
        with sync_playwright() as playwright:
            browser = _launch_wx_browser(playwright)
            context = browser.new_context(no_viewport=True)
            page, appid, secret = _open_login_and_wait_for_scan(context)
            if not page:
                print("❌ 等待微信扫码登录超时，未修改任何配置。")
                return 1

            print(f"✅ 已自动获取测试号凭证：appid={appid[:8]}…")
            template_id = _ensure_wx_template(page, appid, secret)
            if not template_id:
                raise RuntimeError("未能自动创建或识别消息模板")

            current_cfg = read_config()
            openid = _ensure_wx_recipient(
                page, appid, secret,
                configured=(current_cfg.get("wx_test_touser") or "").strip(),
            )
            if not openid:
                raise RuntimeError("等待关注二维码扫码超时，未识别到接收者")

            cfg = _with_wx_binding(current_cfg, appid, secret, openid, template_id)
            print("🔔 正在发送绑定测试消息…")
            if not _send_wx_binding_test(cfg):
                raise RuntimeError("微信测试消息发送失败；请检查微信接口返回")
            write_config(cfg)
            print("💾 微信凭证已自动保存。")
            print("🎉 微信绑定完成！请在手机微信中确认收到测试消息。")
            return 0
    except Exception as e:
        if page:
            try:
                screenshot = os.path.join(BASE_DIR, "wx_bind_error.png")
                page.screenshot(path=screenshot, full_page=True)
                print(f"🖼️  已保存失败现场截图：{screenshot}")
            except Exception:
                pass
        print(f"❌ 微信绑定未完成：{e}")
        print("   配置仅在全部步骤成功后写入，可直接重新运行 wx-bind。")
        return 1
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def _run_wx_bind_flow(args):
    """调度自动/手动微信绑定；自动失败时可降级到手动向导。"""
    mode = (getattr(args, "mode", "") or "").strip().lower()
    if mode not in ("auto", "manual"):
        print("🔗 请选择微信绑定方式：")
        print("   1) 自动绑定（推荐，除扫码外全自动）")
        print("   2) 手动输入（AppID / AppSecret / 模板 ID）")
        while True:
            try:
                choice = input("请选择 [1]: ").strip() or "1"
            except EOFError:
                choice = "1"
            if choice in ("1", "2"):
                mode = "auto" if choice == "1" else "manual"
                break
            print("❌ 请输入 1-2")

    if mode == "manual":
        return cmd_wx_setup(args)

    result = _cmd_wx_bind_auto(args)
    if result == 0:
        return 0
    if getattr(args, "no_fallback", False):
        return result

    print("\n⚠️  自动绑定未完成，可切换到手动输入向导。")
    try:
        fallback = input("是否立即切换到手动绑定？[Y/n]: ").strip().lower()
    except EOFError:
        fallback = "n"
    if fallback in ("n", "no"):
        return result
    return cmd_wx_setup(args)


def cmd_wx_bind(args):
    """微信绑定统一入口：支持自动、手动以及自动失败降级。"""
    return _run_wx_bind_flow(args)


def cmd_wx_login(args):
    """引导式获取微信测试号（强化版）：用浏览器打开沙箱登录页，优先直连绕开代理
    以加载二维码；二维码显示在浏览器窗口中可直接扫描手机；扫码登录后**全自动**：
    抓 appid/secret → 浏览器自动建模板 → API 自动拉 OpenID → 写入配置。
    你只需扫两次码（登录 + 关注）。缺依赖时加 --install-browser 自动安装。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if getattr(args, "install_browser", False):
            if not _install_playwright():
                return 1
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                print("❌ 自动安装后仍无法导入 Playwright，请手动：")
                print("   pip install playwright && playwright install chromium")
                return 1
        else:
            print("❌ 当前 python 环境未安装 Playwright。")
            # 探测是否存在我们之前创建的 playwright 独立 venv
            venv_py = os.path.expanduser(
                "~/.workbuddy/binaries/python/envs/playwright/bin/python3"
            )
            if os.path.exists(venv_py):
                print(f"   💡 检测到独立 playwright venv，建议用它的 python 跑：")
                print(f"      {venv_py} {os.path.abspath(__file__)} wx-login")
                print(f"   （或加 --install-browser 让脚本尝试自动装到当前环境，但会污染全局）")
            else:
                print("   二选一：")
                print("   1) 自动装到当前环境：python3 checkin_cli.py wx-login --install-browser")
                print("   2) 创建独立 venv：")
                print("      ~/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m venv \\")
                print("        ~/.workbuddy/binaries/python/envs/playwright && \\")
                print("        ~/.workbuddy/binaries/python/envs/playwright/bin/pip install \\")
                print("        -i https://pypi.tuna.tsinghua.edu.cn/simple playwright && \\")
                print("        PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ \\")
                print("        ~/.workbuddy/binaries/python/envs/playwright/bin/playwright install chromium")
            return 1

    WX_SANDBOX_LOGIN = "https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login"
    qr_png = os.path.join(tempfile.gettempdir(), "workbuddy_wx_qr.png")
    follow_png = os.path.join(tempfile.gettempdir(), "workbuddy_wx_follow_qr.png")

    print("🔵 启动无头浏览器打开微信测试号登录页（优先直连，绕开代理以加载二维码）…")
    browser = None
    last_err = ""
    # 启动配置：先尝试「直连」绕开代理（二维码 CDN 常被代理拦截），再回退系统代理
    attempts = [
        ("chrome", {"server": "direct://"}),
        ("chrome", None),
        (None, {"server": "direct://"}),
        (None, None),
    ]
    try:
        with sync_playwright() as p:
            for channel, proxy in attempts:
                try:
                    kwargs = {"headless": False}
                    if channel:
                        kwargs["channel"] = channel
                    if proxy is not None:
                        kwargs["proxy"] = proxy
                    b = p.chromium.launch(**kwargs)
                except Exception as e:
                    last_err = str(e)
                    continue
                try:
                    page = b.new_page()
                    page.goto(WX_SANDBOX_LOGIN, wait_until="domcontentloaded", timeout=30000)
                    if "errcode" in (page.content() or ""):
                        raise RuntimeError("登录页未正常加载（返回空 JSON）")

                    # 检测是否落在入口页 —— 若页面有「登录」按钮且未显示 appid，点击进入扫码页
                    try:
                        page.wait_for_timeout(2000)
                        body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
                        has_login = "登录" in body_text or "login" in body_text.lower()
                        is_dashboard = re.search(r"appid|appsecret|AppID|AppSecret", body_text, re.I)
                        if has_login and not is_dashboard:
                            # 精确匹配「登录」文本，排除「微信号扫一扫登录」等子串
                            btn = page.get_by_text("登录", exact=True).first
                            if btn.is_visible(timeout=2000):
                                print("  ↪ 点击「登录」进入微信扫码授权页…")
                                btn.click(timeout=3000)
                                page.wait_for_timeout(3000)
                    except Exception:
                        pass

                    browser = b
                    break
                except Exception as e:
                    last_err = str(e)
                    try:
                        b.close()
                    except Exception:
                        pass
                    continue
            if browser is None:
                print("❌ 无法打开微信沙箱登录页。最后错误：")
                print("   " + last_err[:300])
                print("   可能原因：本机网络无法访问 mp/res.wx.qq.com（代理/VPN/非大陆网络）。")
                print("   建议：换浏览器、关代理或换设备打开 " + WX_SANDBOX_LOGIN)
                return 1

            print("✅ 页面已打开，等待二维码加载…")

            # 轮询二维码：最长 40s，每 2s 重确认
            qr_found = False
            for _ in range(20):
                qr_el = _find_qr(page)
                if qr_el:
                    try:
                        box = qr_el.bounding_box()
                        if box and box["width"] > 20 and box["height"] > 20:
                            qr_found = True
                            break
                    except Exception:
                        pass
                page.wait_for_timeout(2000)

            if not qr_found:
                # 兜底截一张整页存本地
                page.screenshot(path=qr_png)
                print(f"⚠️ 未定位到二维码元素，截图已保存：{qr_png}")

            print("📱 浏览器窗口已打开，请用手机微信扫描窗口中的二维码，点「确认登录」。")
            print("   等待登录中（最多 150 秒）…")

            try:
                page.wait_for_function(
                    "() => { const t = document.body ? document.body.innerText : ''; "
                    "return /AppSecret|appsecret|AppID|开发者/i.test(t); }",
                    timeout=150000,
                )
            except Exception:
                print("⏰ 等待超时，未检测到登录。请确认已扫码并点「确认登录」后重试。")
                browser.close()
                return 1

            # 抓取 appid / appsecret（等字段填充，重试若干次）
            creds = {"appid": "", "appsecret": ""}
            for _ in range(12):
                creds = page.evaluate(EXTRACT_CREDS_JS)
                if creds.get("appid") and creds.get("appsecret"):
                    break
                page.wait_for_timeout(1000)

            appid = (creds.get("appid") or "").strip()
            appsecret = (creds.get("appsecret") or "").strip()
            if not (appid and appsecret):
                print("⚠️ 未能自动提取 appid/appsecret（页面结构可能变化）。")
                print("   请手动复制沙箱页的 AppID / AppSecret，再执行：")
                print(f"   python3 {os.path.basename(__file__)} config --wx-appid <id> --wx-secret <sec> "
                      f"--wx-touser <openid> --wx-template <tpl>")
                browser.close()
                return 1

            cfg = read_config()
            cfg["wx_test_appid"] = appid
            cfg["wx_test_secret"] = appsecret
            write_config(cfg)
            print(f"✅ 已写入 appid={appid[:8]}…  appsecret={appsecret[:6]}…\n")

            # 自动建模板（用浏览器自动化）
            print("📝 正在自动创建模板…")
            template_id = _auto_create_template(page, appid, appsecret)
            if template_id:
                cfg["wx_test_template_id"] = template_id
                write_config(cfg)
                print(f"✅ 模板已自动创建并写入：{template_id[:16]}…\n")
            else:
                # 失败：保留浏览器，让用户手动操作
                print("  ⚠️ 自动建模板失败（沙箱页面结构可能变化）。")
                print("  🌐 浏览器保持打开中，请手动操作：")
                print("     1) 在浏览器里找到「模板消息」→「新增测试模板」")
                print("     2) 标题：签到通知")
                print("     3) 内容（务必双花括号）：")
                print("        结果：{{keyword1.DATA}}")
                print("        说明：{{keyword2.DATA}}")
                print("        时间：{{keyword3.DATA}}")
                print("     4) 提交后把模板 ID 粘贴到下面，回车继续…\n")
                manual_tpl = input("  模板ID（直接回车跳过）: ").strip()
                if manual_tpl:
                    template_id = manual_tpl
                    cfg["wx_test_template_id"] = template_id
                    write_config(cfg)
                    print(f"  ✅ 已写入模板 ID：{template_id[:16]}…\n")
                else:
                    print("  ⚠️ 跳过模板写入。\n")

            # 优先用 API 拉关注者 OpenID（已登录态下立即可见）
            openid = _try_capture_openid(page, follow_png, appid, appsecret)
            if openid:
                cfg["wx_test_touser"] = openid
                print(f"✅ 已自动写入 OpenID={openid[:8]}…（已脱敏）")

            write_config(cfg)
            print(f"✅ 已写入 appid={appid}  appsecret={appsecret[:6]}…（已脱敏）")

            # 状态总览
            all_ok = openid and cfg.get("wx_test_template_id")
            if all_ok:
                print("\n🎉 全 4 项凭证已全部自动写入，绑定完成！")
                print("   验证：python3 checkin_cli.py test-notify")
            else:
                if not openid:
                    print("   ⚠️ OpenID 未拿到：扫描测试号关注者二维码关注后，再跑一遍 wx-login 或用 wx-setup")
                if not cfg.get("wx_test_template_id"):
                    print("   ⚠️ 模板未自动创建：沙箱页「模板消息」→「新增测试模板」，复制模板 ID 后：")
                    print(f"      python3 checkin_cli.py config --wx-template <模板ID>")
            browser.close()
            return 0
    except Exception as e:
        print(f"❌ 浏览器执行出错：{e}")
        print("   若提示缺少浏览器，请运行：python3 checkin_cli.py wx-login --install-browser")
        return 1


def _browser_open(url, label="沙箱页"):
    """尝试用当前平台的默认浏览器打开 URL。"""
    try:
        webbrowser.open(url)
        print(f"  🌐 已自动打开{label}。若未弹出，手动打开：{url}")
    except Exception:
        print(f"  🌐 请打开{label}：{url}")


def _open_local_path(path):
    """跨平台打开本地截图等文件。"""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            webbrowser.open("file://" + os.path.abspath(path))
    except Exception:
        print(f"  📄 请手动打开：{path}")


def cmd_wx_setup(args):
    """微信测试号一键配置（半自动，无需装浏览器）：
    1) 自动打开沙箱页 → 微信扫码登录 → 复制 appid/appsecret 贴入；
    2) 调 API 自动拉关注者 OpenID，你只需用手机扫一下关注者二维码（不用拷贝！）；
    3) 自动打开沙箱页 → 创建模板 → 复制模板 ID 贴入。
    OpenID 全部自动获取，只需手动粘贴 appid/secret 和 template_id。"""
    B = "=" * 42
    SANDBOX_URL = "https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login"

    print(B)
    print("  微信测试号 · 一键配置")
    print(B)
    print()

    cfg = read_config()

    # ========== Step 1: appid / appsecret ==========
    print("【Step 1/3】复制 AppID 和 AppSecret")
    print("  脚本会自动打开沙箱页 → 你用微信扫码登录 → 复制下面两项。\n")

    existing_appid = (cfg.get("wx_test_appid") or "").strip()
    existing_secret = (cfg.get("wx_test_secret") or "").strip()
    appid = ""
    secret = ""

    if existing_appid and existing_secret:
        print(f"  📎 检测到已有凭证 appid={existing_appid[:8]}…，要复用吗？")
        ans = input("  复用现有凭证？[Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            print(f"  ✅ 复用 appid={existing_appid[:8]}…  secret={existing_secret[:6]}…\n")
            cfg["wx_test_appid"] = existing_appid
            cfg["wx_test_secret"] = existing_secret
            write_config(cfg)
            appid, secret = existing_appid, existing_secret
            # 已有凭证，直接跳到下一步
            pass

    if not appid:
        _browser_open(SANDBOX_URL, "微信沙箱登录页")
        print("  👆 用微信扫码登录，然后将页面上显示的 appID / appsecret 复制粘贴到下面。")
        print("  （若页面为空白 JSON 或二维码不显示：换网络 / 关代理 / 换设备打开。）\n")
        while True:
            appid = input("  AppID: ").strip()
            if appid and appid.startswith("wx"):
                break
            print("  ⚠️  AppID 应以 wx 开头，请核对后重新输入。")
        while True:
            secret = input("  AppSecret: ").strip()
            if len(secret) >= 16:
                break
            print("  ⚠️  AppSecret 至少 16 个字符，请核对后重新输入。")
        cfg["wx_test_appid"] = appid
        cfg["wx_test_secret"] = secret
        write_config(cfg)
        print(f"  ✅ 已保存 appid={appid[:8]}…  secret={secret[:6]}…\n")

    # ========== Step 2: OpenID（API 自动拉，不用拷贝！）==========
    print("【Step 2/3】获取 OpenID（自动，不用拷贝）")
    access_token = _wx_api_token(appid, secret)
    if not access_token:
        print("  ❌ 无法获取 access_token，请检查 appid/appsecret 是否正确。")
        return 1

    openids, total, _ = _wx_api_get_followers(access_token)

    if openids:
        openid = openids[0]
        print(f"  ✅ 已有 {len(openids)} 个关注者，自动获取 OpenID：{openid[:8]}…\n")
    else:
        print("  ⚠️  测试号还没有关注者，需要你用手机扫一下码。")
        _browser_open(SANDBOX_URL, "沙箱页（找关注者二维码）")
        print("  在沙箱页中部找到「测试号二维码」区域的「关注者二维码」，用手机微信扫一下点关注。")
        print("  （注意：必须用登录沙箱的同一个微信账号扫！）")
        print("  扫码后脚本会每 3 秒自动检测，无需按任何键。等待中…\n")

        openid = _wx_api_poll_follower(access_token)

    if not openid:
        print("  ❌ 超时未检测到关注者。请确认已扫码，然后手动复制 OpenID：")
        print(f"     python3 {os.path.basename(__file__)} config --wx-touser <OpenID>")
        return 1

    cfg["wx_test_touser"] = openid
    write_config(cfg)
    print(f"  ✅ 已保存你的 OpenID（自动获取，无需拷贝）\n")

    # ========== Step 3: template_id ==========
    print("【Step 3/3】创建模板消息 + 复制模板 ID")
    _browser_open(SANDBOX_URL, "沙箱页（创建模板）")
    print("  在沙箱页找到「模板消息接口」→ 点「新增测试模板」，按下面内容填写：\n")
    print("  ┌─ 模板标题 ─────────────────────┐")
    print("  │  签到通知                        │")
    print("  ├─ 模板内容（注意是双花括号！）──┤")
    print("  │  结果：{{keyword1.DATA}}        │")
    print("  │  说明：{{keyword2.DATA}}        │")
    print("  │  时间：{{keyword3.DATA}}         │")
    print("  └─────────────────────────────────┘\n")

    existing_tpl = (cfg.get("wx_test_template_id") or "").strip()
    template_id = ""
    if existing_tpl:
        print(f"  📎 检测到已有 template_id={existing_tpl[:12]}…，要复用吗？")
        ans = input("  复用现有模板？[Y/n]: ").strip().lower()
        if ans not in ("n", "no"):
            template_id = existing_tpl
            print("  ✅ 复用。")
        else:
            template_id = ""
    if not template_id:
        print("  提交模板后在页面上会显示「模板ID」，复制粘贴到下面：")
        while True:
            template_id = input("  模板ID: ").strip()
            if not template_id:
                print("  ⚠️  不能为空。")
                continue
            # 自动验证格式，坏模板自动删除引导重建
            template_id = _wx_verify_and_fix_template(cfg, appid, secret, template_id)
            break

    cfg["wx_test_template_id"] = template_id
    write_config(cfg)
    print(f"  ✅ 已保存 template_id\n")

    # ========== 确认 & 保存 & 测试 ==========
    _wx_setup_confirm_and_send(cfg, appid, secret, openid, template_id)
    return 0


def _wx_setup_confirm_and_send(cfg, appid, secret, openid, template_id):
    """确认保存并发送测试通知。"""
    B = "=" * 42
    print(B)
    print("  配置预览")
    print(B)
    print(f"  AppID      : {appid}")
    print(f"  AppSecret  : {secret[:6]}…（已脱敏）")
    print(f"  OpenID     : {openid[:8]}…（已脱敏）")
    print(f"  模板ID     : {template_id}")

    print("\n  确认保存以上配置？[Y/n]: ", end="")
    try:
        ans = input().strip().lower()
    except EOFError:
        ans = "y"
    if ans in ("n", "no"):
        print("  ❌ 已取消，配置已保留。")
        return

    h = cfg.get("_schedule_hour", 9)
    m = cfg.get("_schedule_minute", 10)
    dst = _prepare_schedule_definition(h, m)
    print(f"\n✅ 配置已保存：{CONFIG_PATH}")
    if dst:
        print(f"✅ 已生成 plist：{dst}")
    else:
        print("✅ Windows 定时配置已保存；执行 install 后生效。")

    print("\n🔔 正在发送测试通知到你的微信…")
    import importlib.util
    spec = importlib.util.spec_from_file_location("wbck_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg_fresh = mod.load_config()
    mod.notify("🔔 WorkBuddy 签到测试", "恭喜，微信测试号绑定成功！签到结果将推送到这里。", cfg_fresh)
    print("✅ 测试通知已发送，请查看你的微信！")
    print("\n下一步：")
    print(f"   python3 {os.path.basename(__file__)} install       # 注册定时任务")
    print(f"   python3 {os.path.basename(__file__)} test-notify   # 随时复测推送")


# ---- 微信 API 辅助函数 ----
def _wx_api_token(appid, secret):
    """调微信 cgi-bin/token 获取 access_token；失败返回空串。"""
    url = (f"https://api.weixin.qq.com/cgi-bin/token"
           f"?grant_type=client_credential&appid={appid}&secret={secret}")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
            token = data.get("access_token", "")
            if not token:
                errcode = data.get("errcode", "")
                errmsg = data.get("errmsg", "")
                print(f"  ⚠️  获取 token 失败：errcode={errcode} {errmsg}")
            return token
    except Exception as e:
        print(f"  ⚠️  网络错误（获取 token）：{e}")
        return ""


def _wx_api_get_followers(access_token, next_openid=""):
    """调微信 cgi-bin/user/get 获取关注者 OpenID 列表。返回 (openids, total, next_openid)。"""
    url = (f"https://api.weixin.qq.com/cgi-bin/user/get"
           f"?access_token={access_token}&next_openid={next_openid}")
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
            if "errcode" in data and data["errcode"] != 0:
                errmsg = data.get("errmsg", "unknown")
                print(f"  ⚠️  获取关注者列表失败：{errmsg}")
                return [], 0, ""
            total = data.get("total", 0)
            openids = (data.get("data") or {}).get("openid", [])
            nxt = data.get("next_openid", "")
            return openids, total, nxt
    except Exception as e:
        print(f"  ⚠️  网络错误（获取关注者）：{e}")
        return [], 0, ""


def _wx_api_poll_follower(access_token):
    """轮询关注者列表，直到有人关注或超时。返回第一个关注者的 openid，失败返回空串。"""
    import select

    for i in range(40):
        # 非阻塞检查是否有回车输入（手动触发检测）
        rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
        if rlist:
            try:
                sys.stdin.readline()
            except Exception:
                pass

        openids, total, _ = _wx_api_get_followers(access_token)
        if openids:
            if i > 0:
                print()
            return openids[0]

        print(f"\r  🔄 等待关注… 第 {i+1}/40 次（当前 {total} 个关注者）", end="", flush=True)
        time.sleep(3)

    print()
    return ""


def _wx_api_get_template(appid, secret, template_id):
    """查单个模板的 content 字段；返回 (ok, content_dict)。"""
    token = _wx_api_token(appid, secret)
    if not token:
        return False, {}
    try:
        url = (f"https://api.weixin.qq.com/cgi-bin/template/get_all_private_template"
               f"?access_token={token}")
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
            data = json.loads(r.read())
        for t in data.get("template_list", []):
            if t.get("template_id") == template_id:
                return True, t
        return False, {"error": "模板未找到"}
    except Exception as e:
        return False, {"error": str(e)}


def _wx_api_delete_template(appid, secret, template_id):
    """调 API 删除模板；返回 (ok, errmsg)。"""
    token = _wx_api_token(appid, secret)
    if not token:
        return False, "无法获取 access_token"
    try:
        body = json.dumps({"template_id": template_id}).encode("utf-8")
        url = (f"https://api.weixin.qq.com/cgi-bin/template/del_private_template"
               f"?access_token={token}")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
            resp = json.loads(r.read())
        if resp.get("errcode") == 0:
            return True, "ok"
        return False, resp.get("errmsg", "unknown")
    except Exception as e:
        return False, str(e)


def _wx_verify_and_fix_template(cfg, appid, secret, template_id):
    """验证模板是否使用双花括号；若不是，自动删除并引导重建。返回最终可用的 template_id。"""
    SANDBOX_URL = "https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login"

    ok, tmpl = _wx_api_get_template(appid, secret, template_id)
    if not ok:
        print(f"  ⚠️  无法查询模板信息：{tmpl.get('error', '未知错误')}")
        return template_id  # 查询失败不阻塞

    content = tmpl.get("content", "")
    # 检查是否有双花括号（正确）vs 单花括号（错误）
    if "{{" in content:
        print("  ✅ 模板格式正确（双花括号），可以使用。")
        return template_id

    # 单花括号 → 自动删除并引导重建
    print(f"  ⚠️  检测到单花括号格式，微信无法渲染！正在自动删除…")

    del_ok, err = _wx_api_delete_template(appid, secret, template_id)
    if del_ok:
        print("  🧹 已自动删除错误的模板。")
    else:
        print(f"  ⚠️  自动删除失败：{err}，请在沙箱页手动删除。")

    print(f"\n  👇 请在沙箱页重新创建（务必用双花括号）：")
    _browser_open(SANDBOX_URL, "沙箱页（创建模板）")
    print()
    print("  ┌─ 模板标题 ─────────────────────┐")
    print("  │  签到通知                        │")
    print("  ├─ 模板内容（注意双花括号！）──┤")
    print("  │  结果：{{keyword1.DATA}}        │")
    print("  │  说明：{{keyword2.DATA}}        │")
    print("  │  时间：{{keyword3.DATA}}         │")
    print("  └─────────────────────────────────┘")
    print()

    while True:
        new_id = input("  新的模板ID: ").strip()
        if not new_id:
            print("  ⚠️  不能为空。")
            continue

        ok2, tmpl2 = _wx_api_get_template(appid, secret, new_id)
        if not ok2:
            print("  ⚠️  无法验证，请确认正确后重输。")
            continue
        content2 = tmpl2.get("content", "")
        if "{{" not in content2:
            print("  ⚠️  仍是单花括号！请删除后确保输入的是双花括号 {{ }}，再重输。")
            # 自动删掉错的
            _wx_api_delete_template(appid, secret, new_id)
            continue
        print("  ✅ 格式验证通过！")
        return new_id


def cmd_install(args):
    if not _run_environment_preflight():
        print("❌ 环境预检未通过，未注册定时任务。")
        return 1
    if sys.platform == "darwin" and not os.path.exists(PLIST_DST):
        # 没有 plist 时尝试从 config 生成
        cfg = read_config()
        h = cfg.get("_schedule_hour", 9)
        m = cfg.get("_schedule_minute", 10)
        write_plist(h, m)
        print(f"ℹ️  未找到 plist，已按配置生成（{h:02d}:{m:02d}）。")
    ok, msg = install()
    print(("✅ " if ok else "⚠️ ") + msg)
    return 0 if ok else 1


def cmd_uninstall(args):
    purge_codebuddy = getattr(args, "purge_codebuddy", False)
    if purge_codebuddy and not getattr(args, "purge", False):
        print("❌ --codebuddy 必须与 --purge 一起使用。")
        return 2

    if purge_codebuddy and not getattr(args, "yes", False):
        print("⚠️  将删除 CodeBuddy CLI、全部 CodeBuddy 配置及账号登录态。")
        print("   这也会清除本机共享的 WorkBuddy/CodeBuddy 登录凭证，且不可恢复。")
        try:
            answer = input("确认继续？请输入 y: ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("ℹ️  已取消，未删除任何内容。")
            return 1

    ok, msg = uninstall()
    print(("✅ " if ok else "⚠️ ") + msg)
    if not getattr(args, "purge", False):
        return 0 if ok else 1

    removed, failures = _purge_paths(_purge_targets())
    if purge_codebuddy:
        codebuddy_removed, codebuddy_failures = _purge_codebuddy()
        removed.extend(codebuddy_removed)
        failures.extend(codebuddy_failures)
    for path in removed:
        print(f"🧹 已删除：{path}")
    if failures:
        for failure in failures:
            print(f"❌ 删除失败：{failure}")
        return 1
    if purge_codebuddy:
        print("✅ 已彻底删除签到数据、CodeBuddy CLI、配置和账号登录态；项目源码保留。")
    else:
        print("✅ 已彻底删除签到配置、凭证缓存、日志、截图和浏览器运行环境；项目源码保留。")
    print("⚠️  上述运行数据不可恢复。")
    return 0 if ok else 1


def _purge_targets():
    """彻底卸载时删除的运行期文件；明确排除脚本和文档源码。"""
    return [
        CONFIG_PATH,
        PLIST_SRC,
        PLIST_DST,
        WX_BIND_VENV,
        os.path.join(BASE_DIR, ".wx_test_token.json"),
        os.path.join(BASE_DIR, ".wecom_token.json"),
        os.path.join(BASE_DIR, "checkin.log"),
        os.path.join(BASE_DIR, "launchd.out.log"),
        os.path.join(BASE_DIR, "launchd.err.log"),
        os.path.join(BASE_DIR, "wx_bind_error.png"),
        os.path.join(BASE_DIR, "wx_qr_screenshot.png"),
        os.path.join(BASE_DIR, "__pycache__"),
        os.path.join(BASE_DIR, "tests", "__pycache__"),
        os.path.join(BASE_DIR, ".pytest_cache"),
        os.path.join(BASE_DIR, ".mypy_cache"),
        os.path.join(BASE_DIR, ".ruff_cache"),
        os.path.join(BASE_DIR, ".coverage"),
        os.path.join(BASE_DIR, "htmlcov"),
        os.path.join(tempfile.gettempdir(), "workbuddy_wx_qr.png"),
        os.path.join(tempfile.gettempdir(), "workbuddy_wx_follow_qr.png"),
    ]


def _purge_paths(paths):
    """删除给定运行期路径，返回 (已删除路径, 失败说明)。"""
    removed = []
    failures = []
    for raw_path in paths:
        path = os.fspath(raw_path)
        if not os.path.lexists(path):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            removed.append(path)
        except Exception as e:
            failures.append(f"{path}: {e}")
    return removed, failures


def _codebuddy_purge_targets():
    """CodeBuddy 完整卸载目标，仅包含官方 CLI 路径、配置和共享登录文件。"""
    home = os.path.expanduser("~")
    targets = [
        os.path.join(home, ".codebuddy"),
        os.path.join(home, ".local", "share", "codebuddy"),
        os.path.join(home, ".cache", "codebuddy"),
    ]
    custom_config = os.environ.get("CODEBUDDY_CONFIG_DIR", "").strip()
    if custom_config:
        candidate = os.path.abspath(os.path.expanduser(custom_config))
        resolved = os.path.realpath(candidate)
        protected = {
            os.path.abspath(os.path.sep),
            os.path.abspath(home),
            os.path.abspath(BASE_DIR),
        }
        if (resolved not in protected
                and "codebuddy" in os.path.basename(candidate).lower()):
            targets.append(candidate)

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local"
        )
        app_data = os.environ.get("APPDATA") or os.path.join(
            home, "AppData", "Roaming"
        )
        targets.extend([
            os.path.join(local_app_data, "codebuddy"),
            os.path.join(app_data, "CodeBuddy Code"),
        ])
        targets.extend(
            os.path.join(
                local_app_data, "CodeBuddyExtension", "Data", "Public",
                "auth", filename,
            )
            for filename in CODEBUDDY_AUTH_FILENAMES
        )
    else:
        targets.extend([
            os.path.join(home, ".local", "bin", name)
            for name in ("codebuddy", "cbc", "codebuddy-code", "cbc-prewarm")
        ])
        if sys.platform == "darwin":
            targets.extend(
                os.path.join(
                    home, "Library", "Application Support", "CodeBuddyExtension",
                    "Data", "Public", "auth", filename,
                )
                for filename in CODEBUDDY_AUTH_FILENAMES
            )
    return list(dict.fromkeys(targets))


def _logout_codebuddy(cli_path):
    """在删除 CLI 前调用官方 /logout，让系统钥匙串/凭据管理器清除登录态。"""
    try:
        process = subprocess.Popen([cli_path, "/logout"], cwd=BASE_DIR)
    except OSError:
        return False
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    return True


def _purge_codebuddy():
    """退出账号、移除 npm 包，并清理 CodeBuddy CLI 的所有本地数据。"""
    failures = []
    cli_path = _find_codebuddy_cli()
    if cli_path:
        _run([cli_path, "daemon", "stop"])
        _run([cli_path, "daemon", "uninstall"])
        if not _logout_codebuddy(cli_path):
            failures.append("CodeBuddy /logout 启动失败，系统凭据可能需要手动清理")

    npm = shutil.which("npm")
    if npm:
        rc, out, err = _run([
            npm, "uninstall", "-g", CODEBUDDY_NPM_PACKAGE,
        ])
        if rc != 0:
            failures.append(err or out or "CodeBuddy npm 全局包卸载失败")

    removed, path_failures = _purge_paths(_codebuddy_purge_targets())
    failures.extend(path_failures)
    return removed, failures


def cmd_status(args):
    cfg = read_config()
    config_exists = os.path.isfile(CONFIG_PATH)
    h = cfg.get("_schedule_hour")
    m = cfg.get("_schedule_minute")
    token = cfg.get("pushplus_token", "")
    channel_labels = {
        "desktop": "不使用（旧配置）",
        "wx_test": "微信测试号",
        "pushplus": "pushplus",
        "webhook": "自定义 webhook",
        "none": "不通知",
    }
    selected_channel = (cfg.get("notify_channel") or "").strip()
    print("=== WorkBuddy 签到 · 当前状态 ===")
    print(f"  配置状态      : {'已配置' if config_exists else '不存在（已清理）'}")
    print(f"  定时时间      : {f'{h:02d}:{m:02d}' if config_exists and h is not None else '未配置'}")
    remote_status = channel_labels.get(
        selected_channel, "兼容模式（按已有配置发送）"
    ) if config_exists else "未配置"
    print(f"  远程通知      : {remote_status}")
    print(f"  pushplus      : {'已配置' if token else '未配置'}"
          + (f"  token={token[:6]}…{token[-4:]}" if token else ""))
    desktop_status = cfg.get("desktop_notify", True) if config_exists else "未配置"
    print(f"  系统通知      : {desktop_status}")
    wh = cfg.get("notify_webhook_url", "")
    print(f"  自定义webhook : {'已设置' if wh else '未设置'}"
          + (f"  method={cfg.get('notify_webhook_method','POST')}" if wh else ""))
    wxt_appid = (cfg.get("wx_test_appid") or "").strip()
    wxt_ok = bool(wxt_appid and (cfg.get("wx_test_secret") or "").strip()
                  and (cfg.get("wx_test_touser") or "").strip()
                  and (cfg.get("wx_test_template_id") or "").strip())
    wx_status = "已绑定 ✓" if wxt_ok else ("未绑定" if config_exists else "未配置")
    print(f"  微信测试号    : {wx_status}"
          + (f"  appid={wxt_appid[:6]}…" if wxt_appid else ""))
    max_retries = cfg.get("max_retries", 4) if config_exists else "未配置"
    retry_delay = f"{cfg.get('retry_base_delay', 30)}s" if config_exists else "未配置"
    print(f"  最大重试      : {max_retries}")
    print(f"  退避基数      : {retry_delay}")
    print(f"  worker 脚本   : {WORKER}")
    if sys.platform == "darwin":
        plist_status = PLIST_DST if os.path.isfile(PLIST_DST) else f"{PLIST_DST}（文件不存在）"
        print(f"  plist 位置    : {plist_status}")
    elif sys.platform == "win32":
        print(f"  计划任务      : {WINDOWS_DAILY_TASK} / {WINDOWS_LOGON_TASK}")
    installed, _ = schedule_installed()
    print(f"  定时注册状态  : {'已注册 ✓' if installed else '未注册（执行 install）'}")
    return 0


def cmd_run(args):
    cmd = [PY, WORKER]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")
    if args.no_retry:
        cmd.append("--no-retry")
    rc = subprocess.call(cmd)
    return rc


def cmd_test_notify(args):
    # 动态加载 worker 的 notify 函数，发一条测试通知
    import importlib.util
    spec = importlib.util.spec_from_file_location("wbck_worker", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cfg = mod.load_config()
    title, message = mod.build_test_message()
    mod.notify(title, message, cfg)
    print("✅ 已发送测试通知（系统通知应已弹出；若绑定了微信，稍后微信也会收到）。")
    return 0


# ------------------------- 工具 -------------------------
def parse_time(s):
    s = s.strip().replace("：", ":")
    if ":" in s:
        h, m = s.split(":", 1)
    elif " " in s.strip():
        h, m = s.strip().split(None, 1)
    else:
        raise ValueError("应为 HH:MM 格式")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("小时 0-23，分钟 0-59")
    return h, m


def build_parser():
    p = argparse.ArgumentParser(
        prog="checkin_cli.py",
        description="WorkBuddy 签到 CLI 运行器（配置/安装/运行一站式）",
    )
    sub = p.add_subparsers(dest="cmd")

    pc = sub.add_parser("config", help="配置定时时间 / 微信绑定 / 重试等")
    pc.add_argument("--time", help="定时时间，格式 HH:MM，例如 09:10")
    pc.add_argument("--wechat", help="pushplus token（绑定微信推送）；传空字符串可清除",
                    default=None)
    pc.add_argument("--desktop", help="系统通知开关：on / off", default=None)
    pc.add_argument("--retries", help="失败最大重试次数（默认 4）", type=int, default=None)
    pc.add_argument("--delay", help="退避基数秒（默认 30）", type=int, default=None)
    pc.add_argument("--webhook", help="通用自定义 webhook URL（适配 Server酱/WxPusher/企微等）；空串清除",
                    default=None)
    pc.add_argument("--webhook-template", help="webhook 请求体 JSON 模板，支持 {title}/{content} 占位",
                    default=None)
    pc.add_argument("--webhook-method", help="webhook 请求方法 POST/GET（默认 POST）", default=None)
    pc.add_argument("--wizard", action="store_true", help="进入交互式向导（也可直接不带参数运行 config）")
    # 微信公众平台「接口测试号」绑定（脚本原生支持，推到个人微信）
    pc.add_argument("--wx-appid", help="微信测试号 appid", default=None)
    pc.add_argument("--wx-secret", help="微信测试号 appsecret", default=None)
    pc.add_argument("--wx-touser", help="微信测试号接收者 OpenID（touser）", default=None)
    pc.add_argument("--wx-template", help="微信测试号模板 ID（template_id）", default=None)
    pc.add_argument("--wx-data", help="微信测试号模板 data 的 JSON 模板，支持 {title}/{content}/{time} 占位（可选）", default=None)
    pc.set_defaults(func=cmd_config)

    pw = sub.add_parser("wizard", help="进入交互式配置向导（逐步引导，也可直接运行 config 不带参数进入）")
    pw.set_defaults(func=cmd_wizard)

    pwb = sub.add_parser(
        "wx-bind",
        help="绑定微信：可选自动浏览器流程或手动输入，自动失败可降级",
    )
    pwb.add_argument("--mode", choices=("auto", "manual"), help="绑定方式；不传则交互选择")
    pwb.add_argument("--no-bootstrap", action="store_true", help=argparse.SUPPRESS)
    pwb.add_argument("--no-fallback", action="store_true", help=argparse.SUPPRESS)
    pwb.set_defaults(func=cmd_wx_bind)

    # 兼容旧命令名；行为与 wx-bind 完全一致。
    plx = sub.add_parser("wx-login", help="wx-bind 的兼容别名")
    plx.add_argument("--mode", choices=("auto", "manual"), help=argparse.SUPPRESS)
    plx.add_argument("--no-bootstrap", action="store_true", help=argparse.SUPPRESS)
    plx.add_argument("--no-fallback", action="store_true", help=argparse.SUPPRESS)
    plx.add_argument("--install-browser", action="store_true", help=argparse.SUPPRESS)
    plx.set_defaults(func=cmd_wx_bind)

    pws = sub.add_parser("wx-setup", help="微信测试号一键配置（纯 API + 少量手动粘贴，不依赖浏览器）：贴 appid/secret → 自动拉 OpenID → 贴模板 ID → 保存并测试推送")
    pws.set_defaults(func=cmd_wx_setup)

    pi = sub.add_parser("install", help="注册每日定时任务和登录补跑任务")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="卸载定时任务")
    pu.add_argument(
        "--purge", action="store_true",
        help="彻底删除签到配置、凭证缓存、日志、截图及 Playwright 专用环境（不可恢复）",
    )
    pu.add_argument(
        "--codebuddy", "--purge-codebuddy", dest="purge_codebuddy",
        action="store_true",
        help="与 --purge 一起使用：另删除 CodeBuddy CLI、配置及账号登录态",
    )
    pu.add_argument(
        "--yes", action="store_true",
        help="跳过 --codebuddy 的不可恢复删除确认",
    )
    pu.set_defaults(func=cmd_uninstall)

    ps = sub.add_parser("status", help="查看当前配置 / 定时 / 注册状态")
    ps.set_defaults(func=cmd_status)

    pr = sub.add_parser("run", help="立即手动跑一次签到")
    pr.add_argument("--dry-run", action="store_true", help="只查询不签到")
    pr.add_argument("--force", action="store_true", help="强制重签")
    pr.add_argument("--no-retry", action="store_true", help="关闭失败重试")
    pr.set_defaults(func=cmd_run)

    pn = sub.add_parser("test-notify", help="发送一条测试通知")
    pn.set_defaults(func=cmd_test_notify)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n⚠️ 已取消当前操作。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
