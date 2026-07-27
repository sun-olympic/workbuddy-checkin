import base64
import os
import shutil
import stat
import subprocess
import json
import socket
import time
import urllib.error
import urllib.request
import webbrowser


class WindowsPlatform:
    display_name = "Windows"
    npm_command = "npm.cmd"

    def __init__(self, scheduler_settings=None):
        self.scheduler = scheduler_settings

    def _settings(self):
        if self.scheduler is None:
            raise RuntimeError("scheduler settings are required")
        return self.scheduler

    def prepare_schedule(self, hour, minute, write_plist):
        del hour, minute, write_plist
        return ""

    def _task_exists(self, task_name, run):
        rc, _, err = run(["schtasks", "/Query", "/TN", task_name])
        return rc == 0, err

    def schedule_installed(self, run):
        settings = self._settings()
        daily, daily_error = self._task_exists(settings.daily_task, run)
        logon, logon_error = self._task_exists(settings.logon_task, run)
        return daily and logon, daily_error or logon_error

    def install_schedule(self, config, run):
        settings = self._settings()
        hour = int(config.get("_schedule_hour", 9))
        minute = int(config.get("_schedule_minute", 10))
        action = subprocess.list2cmdline([
            settings.python_executable, settings.worker_path,
        ])
        commands = ([
            "schtasks", "/Create", "/F", "/TN", settings.daily_task,
            "/TR", action, "/SC", "DAILY", "/ST",
            "{:02d}:{:02d}".format(hour, minute), "/RL", "LIMITED",
        ], [
            "schtasks", "/Create", "/F", "/TN", settings.logon_task,
            "/TR", action, "/SC", "ONLOGON", "/RL", "LIMITED",
        ])
        for index, command in enumerate(commands):
            rc, out, err = run(command)
            if rc != 0:
                if index == 1:
                    run([
                        "schtasks", "/Delete", "/F", "/TN",
                        settings.daily_task,
                    ])
                return False, err or out or "Windows 任务计划程序注册失败"
        return True, "已注册 Windows 定时任务（每天 + 登录补跑）。"

    def uninstall_schedule(self, run):
        settings = self._settings()
        failures = []
        removed = 0
        for task_name in (settings.daily_task, settings.logon_task):
            exists, _ = self._task_exists(task_name, run)
            if not exists:
                continue
            rc, out, err = run([
                "schtasks", "/Delete", "/F", "/TN", task_name,
            ])
            if rc == 0:
                removed += 1
            else:
                failures.append(err or out or task_name)
        if failures:
            return False, " | ".join(failures)
        return True, "已卸载 Windows 定时任务（删除 {} 项）。".format(removed)

    def status_rows(self):
        settings = self._settings()
        return [("计划任务", "{} / {}".format(
            settings.daily_task, settings.logon_task,
        ))]

    def schedule_definition_ready(self):
        return True

    def find_workbuddy_app(self, candidates):
        del candidates
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        paths = [
            os.path.join(
                local_app_data, "Programs", "WorkBuddy", "WorkBuddy.exe",
            ),
            os.path.join(local_app_data, "WorkBuddy", "WorkBuddy.exe"),
            os.path.join(program_files, "WorkBuddy", "WorkBuddy.exe"),
        ]
        return next((path for path in paths if path and os.path.exists(path)), "")

    def launch_workbuddy_app(self, app_path):
        try:
            subprocess.Popen([app_path])
            return True
        except OSError:
            return False

    def find_npm(self, which=shutil.which):
        for name in ("npm.cmd", "npm.exe", "npm"):
            path = which(name)
            if path:
                return path
        return ""

    def wx_bind_python(self, venv_path):
        return os.path.join(venv_path, "Scripts", "python.exe")

    def codebuddy_auth_paths(self, filenames):
        home = os.path.expanduser("~")
        local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local"
        )
        base = os.path.join(
            local_app_data, "CodeBuddyExtension", "Data", "Public", "auth",
        )
        return tuple(os.path.join(base, name) for name in filenames)

    def open_local_path(self, path):
        os.startfile(path)

    def send_desktop_notification(self, title, message, run=subprocess.run):
        def xml_escape(value):
            return (str(value).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;")
                    .replace("'", "&apos;"))

        toast_xml = (
            '<toast><visual><binding template="ToastGeneric">'
            "<text>{}</text><text>{}</text>"
            "</binding></visual></toast>"
        ).format(xml_escape(title), xml_escape(message))
        script = (
            "[void][Windows.UI.Notifications.ToastNotificationManager,"
            "Windows.UI.Notifications,ContentType=WindowsRuntime];"
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
            "$xml.LoadXml('{}');"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
            "[Windows.UI.Notifications.ToastNotificationManager]"
            "::CreateToastNotifier('WorkBuddy Check-in').Show($toast);"
        ).format(toast_xml)
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded],
            check=False, capture_output=True, timeout=10,
        )

    def codebuddy_purge_paths(self, filenames):
        home = os.path.expanduser("~")
        local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local"
        )
        app_data = os.environ.get("APPDATA") or os.path.join(
            home, "AppData", "Roaming"
        )
        paths = [
            os.path.join(local_app_data, "codebuddy"),
            os.path.join(app_data, "CodeBuddy Code"),
        ]
        paths.extend(self.codebuddy_auth_paths(filenames))
        return paths

    def retry_readonly_removal(self, function, path, error):
        if (isinstance(error, PermissionError)
                or getattr(error, "winerror", None) == 5):
            os.chmod(path, stat.S_IWRITE)
            function(path)
            return True
        return False

    def login_codebuddy(self, cli_path, base_dir, settings, signal_path,
                        wait_for_login, run_command):
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

            login_methods = (status or {}).get("loginMethods") or []
            method_ids = {item.get("id") for item in login_methods}
            if "internal" not in method_ids:
                raise RuntimeError(
                    "CodeBuddy 登录服务未返回国内站登录入口，请重试。"
                )

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
                try:
                    os.startfile(auth_url)
                except OSError:
                    webbrowser.open(auth_url)
                print("🌐 已打开 CodeBuddy 国内站登录页，请在浏览器完成授权。")
            else:
                print("🌐 CodeBuddy 已触发国内站登录，请在浏览器完成授权。")
            return wait_for_login(process, signal_path)
        finally:
            if process is not None:
                if process.poll() is None:
                    run_command([
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                    ])
                    if process.poll() is None:
                        process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
