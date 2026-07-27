import os
import signal
import shutil
import subprocess


class MacOSPlatform:
    display_name = "macOS"
    npm_command = "npm"

    def __init__(self, scheduler_settings=None):
        self.scheduler = scheduler_settings

    def _settings(self):
        if self.scheduler is None:
            raise RuntimeError("scheduler settings are required")
        return self.scheduler

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

    def retry_readonly_removal(self, function, path, error):
        del function, path, error
        return False

    def login_codebuddy(self, cli_path, base_dir, settings, signal_path,
                        wait_for_login, run_command):
        del run_command
        process = None
        uses_process_group = False
        expect = shutil.which("expect")
        try:
            if expect:
                expect_script = (
                    'spawn -noecho $env(WORKBUDDY_CODEBUDDY_CLI) '
                    '--settings $env(WORKBUDDY_CODEBUDDY_SETTINGS)\n'
                    'set timeout 45\n'
                    'expect {\n'
                    '  -re {Select login method} {}\n'
                    '  timeout { exit 124 }\n'
                    '  eof { exit 125 }\n'
                    '}\n'
                    'set timeout 20\n'
                    'expect {\n'
                    '  -re {Enter to login} {}\n'
                    '  timeout { exit 126 }\n'
                    '  eof { exit 127 }\n'
                    '}\n'
                    'set timeout 2\n'
                    'expect {\n'
                    '  -re {Enter to login} { exp_continue }\n'
                    '  timeout {}\n'
                    '  eof { exit 128 }\n'
                    '}\n'
                    'send -- "\\r"\n'
                    'interact'
                )
                child_env = os.environ.copy()
                child_env["WORKBUDDY_CODEBUDDY_CLI"] = cli_path
                child_env["WORKBUDDY_CODEBUDDY_SETTINGS"] = settings
                process = subprocess.Popen(
                    [expect, "-c", expect_script], cwd=base_dir,
                    env=child_env, start_new_session=True,
                )
                uses_process_group = True
            else:
                process = subprocess.Popen(
                    [cli_path, "--settings", settings], cwd=base_dir,
                    stdin=subprocess.PIPE, text=True,
                )
                process.stdin.write("/login\n")
                process.stdin.flush()

            return wait_for_login(process, signal_path)
        finally:
            if process is not None:
                if uses_process_group:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        if process.poll() is None:
                            process.terminate()
                elif process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if uses_process_group:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
