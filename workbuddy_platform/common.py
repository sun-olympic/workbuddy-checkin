from dataclasses import dataclass
import json
import os
import time
import urllib.error
import urllib.request


class UnsupportedPlatformError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchedulerSettings:
    python_executable: str
    worker_path: str
    label: str
    plist_path: str
    daily_task: str
    logon_task: str


def run_codebuddy_auth_flow(base_url, process, signal_path, wait_for_login,
                            open_auth_url):
    """执行跨平台一致的 CodeBuddy 本地登录服务协议。"""
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
            headers={"x-codebuddy-request": "1"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        logout_data = payload.get("data", payload)
        if not logout_data.get("success"):
            raise RuntimeError("CodeBuddy 未能退出当前账号，请重试。")
        time.sleep(1.0)

    # 丢弃启动阶段旧登录态产生的通知；只接受本次浏览器授权信号。
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
        open_auth_url(auth_url)
        print("🌐 已打开 CodeBuddy 国内站登录页，请在浏览器完成授权。")
    else:
        print("🌐 CodeBuddy 已触发国内站登录，请在浏览器完成授权。")
    return wait_for_login(process, signal_path)
