import base64
import ntpath
import os
import shutil
import stat
import subprocess
import socket
import sys
import time
import webbrowser

from .common import run_codebuddy_auth_flow


class WindowsPlatform:
    display_name = "Windows"
    python_command = "python"
    npm_command = "npm.cmd"

    def __init__(self, scheduler_settings=None):
        self.scheduler = scheduler_settings

    def _settings(self):
        if self.scheduler is None:
            raise RuntimeError("scheduler settings are required")
        return self.scheduler

    def configure_console(self, stdout=None, stderr=None, kernel32=None):
        """统一 Windows 控制台与 Python 流的 UTF-8 编码。"""
        stdout = sys.stdout if stdout is None else stdout
        stderr = sys.stderr if stderr is None else stderr
        if kernel32 is None:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
            except (AttributeError, OSError):
                kernel32 = None
        if kernel32 is not None:
            try:
                kernel32.SetConsoleOutputCP(65001)
                kernel32.SetConsoleCP(65001)
            except (AttributeError, OSError):
                pass
        for stream in (stdout, stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8", errors="replace")
                except (AttributeError, OSError, ValueError):
                    pass

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
        python_executable = self._background_python_executable(
            settings.python_executable,
        )
        action = subprocess.list2cmdline([
            python_executable, settings.worker_path,
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

    @staticmethod
    def _background_python_executable(python_executable):
        """优先使用无控制台的 pythonw，避免计划任务触发时闪窗。"""
        if ntpath.basename(python_executable).lower() == "pythonw.exe":
            return python_executable
        candidate = ntpath.join(
            ntpath.dirname(python_executable), "pythonw.exe",
        )
        if os.path.isfile(candidate):
            return candidate
        return python_executable

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
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        paths = [os.environ.get("WORKBUDDY_PATH", "")]
        discovered = shutil.which("WorkBuddy.exe")
        if discovered:
            paths.append(discovered)
        paths.extend(self._registry_workbuddy_paths())
        for root, parts in (
            (local_app_data, ("Programs", "WorkBuddy", "WorkBuddy.exe")),
            (local_app_data, ("WorkBuddy", "WorkBuddy.exe")),
            (program_files, ("WorkBuddy", "WorkBuddy.exe")),
            (program_files_x86, ("WorkBuddy", "WorkBuddy.exe")),
        ):
            if root:
                paths.append(ntpath.join(root, *parts))
        for path in paths:
            candidate = self._workbuddy_executable(path)
            if candidate and os.path.exists(candidate):
                return candidate
        return ""

    @staticmethod
    def _workbuddy_executable(value):
        """从注册表路径、安装目录或 DisplayIcon 中提取 exe。"""
        value = os.path.expandvars(str(value or "").strip())
        if not value:
            return ""
        if value.startswith('"') and '"' in value[1:]:
            value = value[1:value.find('"', 1)]
        elif ".exe" in value.lower():
            value = value[:value.lower().find(".exe") + 4]
        value = value.strip().strip('"')
        if not value.lower().endswith(".exe"):
            value = ntpath.join(value, "WorkBuddy.exe")
        return value

    @classmethod
    def _registry_workbuddy_paths(cls):
        """从 App Paths 和卸载信息发现自定义目录的 WorkBuddy。"""
        try:
            import winreg
        except ImportError:
            return []

        paths = []
        hives = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        app_path_key = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion"
            r"\App Paths\WorkBuddy.exe"
        )
        for hive in hives:
            try:
                with winreg.OpenKey(hive, app_path_key) as key:
                    paths.append(winreg.QueryValueEx(key, "")[0])
            except OSError:
                pass

        uninstall_keys = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows"
            r"\CurrentVersion\Uninstall",
        )
        for hive in hives:
            for uninstall_key in uninstall_keys:
                paths.extend(
                    cls._workbuddy_paths_from_uninstall_key(
                        winreg, hive, uninstall_key,
                    )
                )
        return paths

    @staticmethod
    def _workbuddy_paths_from_uninstall_key(winreg, hive, key_path):
        paths = []
        try:
            with winreg.OpenKey(hive, key_path) as parent:
                index = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(parent, subkey_name) as subkey:
                            display_name = winreg.QueryValueEx(
                                subkey, "DisplayName",
                            )[0]
                            if "workbuddy" not in str(display_name).lower():
                                continue
                            for value_name in (
                                "DisplayIcon", "InstallLocation",
                            ):
                                try:
                                    paths.append(winreg.QueryValueEx(
                                        subkey, value_name,
                                    )[0])
                                except OSError:
                                    pass
                    except OSError:
                        continue
        except OSError:
            pass
        return paths

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
            creationflags=getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000,
            ),
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

    def stop_shared_login_processes(self, run):
        """彻底清理登录态前停止仍可能写回 Token 的 WorkBuddy。"""
        run(["taskkill", "/F", "/T", "/IM", "WorkBuddy.exe"])

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

            def open_auth_url(auth_url):
                try:
                    os.startfile(auth_url)
                except OSError:
                    webbrowser.open(auth_url)

            return run_codebuddy_auth_flow(
                base_url, process, signal_path, wait_for_login,
                open_auth_url,
            )
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
