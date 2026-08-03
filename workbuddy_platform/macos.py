import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser


class MacOSPlatform:
    display_name = "macOS"
    python_command = "python3"
    npm_command = "npm"

    def __init__(self, scheduler_settings=None):
        self.scheduler = scheduler_settings

    def _settings(self):
        if self.scheduler is None:
            raise RuntimeError("scheduler settings are required")
        return self.scheduler

    def configure_console(self):
        """macOS 终端默认使用 UTF-8，无需调整。"""

    def prepare_schedule(self, hour, minute, write_plist):
        return write_plist(hour, minute)

    def schedule_installed(self, run):
        settings = self._settings()
        rc, out, err = run(["launchctl", "list"])
        if rc != 0:
            return False, err
        return settings.label in out, ""

    def install_schedule(self, config, run):
        del config
        settings = self._settings()
        uid = os.getuid()
        run(["launchctl", "bootout", "gui/{}/{}".format(uid, settings.label)])
        rc, out, err = run([
            "launchctl", "bootstrap", "gui/{}".format(uid),
            settings.plist_path,
        ])
        if rc == 0:
            return True, "已注册定时任务（每天 + 开机/登录补跑）。"
        detail = err or out or "未知错误"
        return False, (
            detail + "\n   请在本机终端手动执行：\n"
            "   launchctl bootstrap gui/{} {}".format(
                uid, settings.plist_path,
            )
        )

    def uninstall_schedule(self, run):
        settings = self._settings()
        uid = os.getuid()
        rc, out, err = run([
            "launchctl", "bootout", "gui/{}/{}".format(uid, settings.label),
        ])
        try:
            if os.path.exists(settings.plist_path):
                os.remove(settings.plist_path)
        except Exception as exc:
            err = (err or "") + " | 删除文件失败: {}".format(exc)
        not_loaded = any(text in (err or out).lower() for text in (
            "no such process", "could not find specified service",
            "service not found",
        ))
        if rc == 0 or not_loaded:
            return True, "已卸载定时任务并移除 plist。"
        detail = err or out or "未知错误"
        return False, (
            detail + "\n   如提示未注册可忽略；本机也可手动：\n"
            "   launchctl bootout gui/{}/{}".format(uid, settings.label)
        )

    def status_rows(self):
        settings = self._settings()
        value = settings.plist_path
        if not os.path.isfile(value):
            value += "（文件不存在）"
        return [("plist 位置", value)]

    def schedule_definition_ready(self):
        return os.path.exists(self._settings().plist_path)

    def find_workbuddy_app(self, candidates):
        return next(
            (path for path in candidates if path and os.path.exists(path)), "",
        )

    def launch_workbuddy_app(self, app_path):
        result = subprocess.run(
            ["open", app_path], check=False, capture_output=True,
        )
        return result.returncode == 0

    def find_npm(self, which=shutil.which):
        return which("npm") or ""

    def wx_bind_python(self, venv_path):
        return os.path.join(venv_path, "bin", "python3")

    def codebuddy_auth_paths(self, filenames):
        base = os.path.expanduser(
            "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth"
        )
        return tuple(os.path.join(base, name) for name in filenames)

    def open_local_path(self, path):
        subprocess.run(["open", path], check=False)

    def send_desktop_notification(self, title, message, run=subprocess.run):
        escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            'display notification "{}" with title "{}" sound name "Glass"'
            .format(escaped_message, escaped_title)
        )
        run(
            ["osascript", "-e", script],
            check=False, capture_output=True, timeout=10,
        )

    def codebuddy_purge_paths(self, filenames):
        home = os.path.expanduser("~")
        paths = [
            os.path.join(home, ".local", "bin", name)
            for name in ("codebuddy", "cbc", "codebuddy-code", "cbc-prewarm")
        ]
        paths.extend(self.codebuddy_auth_paths(filenames))
        return paths

    def stop_shared_login_processes(self, run):
        """彻底清理登录态前停止仍可能写回 Token 的 WorkBuddy。"""
        main_pattern = "WorkBuddy.app/Contents/MacOS/Electron"
        running, _, _ = run(["pgrep", "-f", main_pattern])
        if running != 0:
            return

        run([
            "osascript", "-e",
            'tell application id "com.workbuddy.workbuddy" to quit',
        ])
        for _ in range(20):
            running, _, _ = run(["pgrep", "-f", main_pattern])
            if running != 0:
                return
            time.sleep(0.25)

        run(["pkill", "-KILL", "-f", "WorkBuddy.app/Contents/"])

    def retry_readonly_removal(self, function, path, error):
        del function, path, error
        return False

    def login_codebuddy(self, cli_path, base_dir, settings, signal_path,
                        wait_for_login, run_command):
        del run_command
        process = None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base_url = "http://127.0.0.1:{}".format(port)
        try:
            process = subprocess.Popen([
                cli_path, "--serve", "--host", "127.0.0.1",
                "--port", str(port), "--settings", settings,
            ], cwd=base_dir, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            status = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    request = urllib.request.Request(
                        base_url + "/api/v1/auth/account/status",
                        headers={"x-codebuddy-request": "1"}, method="GET",
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    status = payload.get("data", payload)
                    break
                except (OSError, ValueError, urllib.error.URLError):
                    if process.poll() not in (None, 0):
                        break
                    time.sleep(0.25)

            if status is None:
                raise RuntimeError("CodeBuddy 登录服务启动失败，请重试。")
            login_methods = status.get("loginMethods") or []
            method_ids = {item.get("id") for item in login_methods}
            if login_methods and "internal" not in method_ids:
                raise RuntimeError(
                    "CodeBuddy 登录服务未返回国内站登录入口，请重试。"
                )

            if status.get("authenticated"):
                request = urllib.request.Request(
                    base_url + "/api/v1/auth/account/logout",
                    data=b"",
                    headers={"x-codebuddy-request": "1"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                logout_data = payload.get("data", payload)
                if not logout_data.get("success"):
                    raise RuntimeError("CodeBuddy 未能退出当前账号，请重试。")
                # logout 返回时，启动阶段的旧账号刷新任务可能仍在收尾；
                # 等它结束后再 login，避免旧任务取消新的浏览器会话。
                time.sleep(1.0)

            # 丢弃 CLI 启动阶段由旧登录态产生的通知信号；最终仍以
            # 新凭证实际落盘且 UID 改变作为切换成功条件。
            try:
                os.remove(signal_path)
            except FileNotFoundError:
                pass

            request = urllib.request.Request(
                base_url + "/api/v1/auth/account/login",
                data=json.dumps({"method": "internal"}).encode("utf-8"),
                headers={
                    "x-codebuddy-request": "1",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            login_data = payload.get("data", payload)
            if not login_data.get("success"):
                raise RuntimeError("CodeBuddy 未能启动浏览器登录，请重试。")

            auth_url = login_data.get("authUrl", "")
            if auth_url:
                webbrowser.open(auth_url)
                print("🌐 已打开 CodeBuddy 国内站登录页，请在浏览器完成授权。")
            else:
                print("🌐 CodeBuddy 已触发国内站登录，请在浏览器完成授权。")

            return wait_for_login(process, signal_path)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
