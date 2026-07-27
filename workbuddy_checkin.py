#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到脚本（HTTP 直连版，macOS / Windows）
======================================================
原理：
    1. 从 WorkBuddy 本机日志或独立 CodeBuddy CLI 的官方登录状态中读取 Bearer token。
    2. 直接调用云端签到接口完成签到，无需 GUI、无需辅助功能授权、无需坐标校准。
    3. 因为全程是后台 HTTP 请求，所以「锁屏 / 未开窗口」也能执行；只要
       WorkBuddy 或 CodeBuddy CLI 中仍有有效登录态即可。

接口（逆向自 WorkBuddy.app 的 app.asar）：
    POST https://copilot.tencent.com/billing/meter/checkin-status   # 查状态
    POST https://copilot.tencent.com/billing/meter/daily-checkin    # 签到
鉴权头：
    Authorization: Bearer <token>      (token 即日志里的 JWT)
    X-User-Id: <uid>                   (取自 JWT payload 的 sub)

依赖：仅标准库（urllib / ssl / json / re / subprocess），无需 pip 安装任何东西。

用法：
    python3 workbuddy_checkin.py            # 执行签到（已签则跳过），失败自动重试
    python3 workbuddy_checkin.py --dry-run  # 只查询签到状态，不签到
    python3 workbuddy_checkin.py --force    # 忽略「今日已签」强制再签一次
    python3 workbuddy_checkin.py --no-retry # 关闭失败自动重试

通知：
    - 默认弹 macOS / Windows 系统通知。
    - 想推到微信（任选其一或叠加）：
      ① 微信测试号（公众平台接口测试号，个人轻量、免企业认证）：填
         wx_test_appid / wx_test_secret / wx_test_touser / wx_test_template_id。
      ② pushplus（第三方聚合，需自备 token）：填 "pushplus_token"（pushplus.plus）。
      ③ 通用 webhook（任意服务：Server酱 / WxPusher / 自建等）：填 notify_webhook_url。
    - 不填则不推送微信，仅系统通知。
"""

import os
import sys
import json
import re
import time
import base64
import datetime
import logging
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import ssl

from workbuddy_platform import UnsupportedPlatformError, get_platform

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.expanduser("~/.workbuddy/logs")
CODEBUDDY_AUTH_FILENAMES = (
    "Tencent-Cloud.coding-copilot.info",
    "workbuddy-desktop.info",
)
LOG_PATH = os.path.join(BASE_DIR, "checkin.log")
CONFIG_PATH = os.path.join(BASE_DIR, "checkin_config.json")

CHECKIN_STATUS_URL = "https://copilot.tencent.com/billing/meter/checkin-status"
CHECKIN_CLAIM_URL = "https://copilot.tencent.com/billing/meter/daily-checkin"

# 失败自动重试参数
MAX_RETRIES = 4          # 最多额外重试次数（加上首次共 5 次）
RETRY_BASE_DELAY = 30    # 退避基数（秒）：第 n 次重试等 base * 2^(n-1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("workbuddy-checkin")


def _platform_adapter():
    return get_platform(sys.platform)


# ------------------------- 配置 -------------------------
def load_config():
    """读取 checkin_config.json（不存在则用默认）。可配置项：
    - pushplus_token: 微信推送 token（pushplus.plus），空则不推
    - desktop_notify: 是否弹系统通知（默认 True）
    - max_retries: 失败重试次数（默认 4）
    - retry_base_delay: 退避基数秒（默认 30）
    """
    cfg = {
        "pushplus_token": "",
        "desktop_notify": True,
        "max_retries": MAX_RETRIES,
        "retry_base_delay": RETRY_BASE_DELAY,
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        # 合并用户配置：保留默认值 + 允许任意新增字段（wx_test_* / webhook / notify_* 等）
        cfg.update(user)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("读取配置失败，使用默认: %s", e)
    return cfg


# ------------------------- token 提取 -------------------------
def _b64decode(seg: str) -> dict:
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def _codebuddy_auth_paths():
    """返回当前平台可能存在的 CodeBuddy 官方登录状态路径。"""
    try:
        platform = _platform_adapter()
    except UnsupportedPlatformError:
        return ()
    return platform.codebuddy_auth_paths(CODEBUDDY_AUTH_FILENAMES)


def extract_token():
    """
    从 CodeBuddy CLI 登录状态或 WorkBuddy 主线程日志提取最新有效 token。
    返回 (token, uid)，找不到返回 None。
    """
    jwt_re = re.compile(r'Bearer\s+(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)')
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    best = None  # (exp, token, uid)

    # 独立 CodeBuddy CLI 与 WorkBuddy 使用同一个 authentication id
    #（workbuddy-desktop）。优先读取它维护的官方登录状态，不复制凭证到项目配置。
    for auth_path in _codebuddy_auth_paths():
        try:
            with open(auth_path, "r", encoding="utf-8") as auth_file:
                state = json.load(auth_file)
            token = str((state.get("auth") or {}).get("accessToken") or "").strip()
            if not token:
                continue
            payload = _b64decode(token.split(".")[1])
            exp = float(payload.get("exp") or 0)
            uid = payload.get("sub") or (state.get("account") or {}).get("uid")
            if exp > now and uid and (best is None or exp > best[0]):
                best = (exp, token, uid)
        except (OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            continue

    if not os.path.isdir(LOGS_DIR):
        return (best[1], best[2]) if best else None
    for root, _, files in os.walk(LOGS_DIR):
        for fn in files:
            if not fn.startswith("workbuddyMainThread"):
                continue
            try:
                data = open(os.path.join(root, fn), "rb").read().decode("utf-8", "replace")
            except Exception:
                continue
            for m in jwt_re.finditer(data):
                tok = m.group(1)
                try:
                    payload = _b64decode(tok.split(".")[1])
                    exp = payload.get("exp") or 0
                    uid = payload.get("sub")
                    if exp > now and (best is None or exp > best[0]):
                        best = (exp, tok, uid)
                except Exception:
                    pass
    return (best[1], best[2]) if best else None


# ------------------------- HTTP 请求 -------------------------
def _post(url, token, uid, body=None):
    data = json.dumps(body or {}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "X-User-Id": uid or "",
    }
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


# ------------------------- 通知 -------------------------
def notify(title, message, cfg):
    """发送结果通知。notify_channel 存在时严格单选；旧配置保持兼容。"""
    selected = (cfg.get("notify_channel") or "").strip().lower()
    known_channels = {"desktop", "wx_test", "pushplus", "webhook", "none"}
    exclusive = selected in known_channels

    def enabled(channel):
        return not exclusive or selected == channel

    # 1) 桌面通知是独立开关，不参与远程渠道单选。
    if cfg.get("desktop_notify", True):
        try:
            _platform_adapter().send_desktop_notification(
                title, message, subprocess.run,
            )
        except UnsupportedPlatformError:
            pass
        except Exception as e:
            logger.warning("桌面通知失败: %s", e)

    # 2) 微信推送（pushplus）
    token = (cfg.get("pushplus_token") or "").strip()
    if enabled("pushplus") and token:
        try:
            url = f"https://www.pushplus.plus/send?token={token}"
            body = json.dumps({
                "title": title,
                "content": message,
                "template": "txt",
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
                logger.info("pushplus 微信推送结果: HTTP %s", r.status)
        except Exception as e:
            logger.warning("pushplus 微信推送失败: %s", e)

    # 3) 微信公众平台「接口测试号」模板消息（原生支持，需配 appid/secret/touser/template_id）
    if enabled("wx_test") and _wechat_test_configured(cfg):
        try:
            _wechat_test_send(title, message, cfg)
        except Exception as e:
            logger.warning("微信测试号推送异常: %s", e)

    # 4) 通用自定义 webhook（适配任意推送服务：Server酱 / WxPusher / 自建等）
    if not enabled("webhook"):
        return
    url = (cfg.get("notify_webhook_url") or "").strip()
    if not url:
        return
    try:
        method = (cfg.get("notify_webhook_method") or "POST").upper()
        template = (cfg.get("notify_webhook_template") or "").strip()
        if template:
            # 仅做 {title}/{content} 占位替换，避免 str.format 与 JSON 花括号冲突
            body_str = template.replace("{title}", title).replace("{content}", message)
            try:
                payload = json.loads(body_str)
            except Exception:
                logger.warning("webhook 模板非合法 JSON，改用默认 {title,content} 格式")
                payload = {"title": title, "content": message}
        else:
            payload = {"title": title, "content": message}

        if method == "GET":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode({'title': title, 'content': message})}"
            req = urllib.request.Request(url, method="GET")
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"}, method="POST",
            )
        with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
            logger.info("webhook 推送结果: HTTP %s", r.status)
    except Exception as e:
        logger.warning("webhook 推送失败: %s", e)


# ------------------------- 微信测试号（公众平台接口测试号） -------------------------
WX_TEST_TOKEN_CACHE = os.path.join(BASE_DIR, ".wx_test_token.json")


def _wechat_test_configured(cfg):
    return bool((cfg.get("wx_test_appid") or "").strip()
                and (cfg.get("wx_test_secret") or "").strip()
                and (cfg.get("wx_test_touser") or "").strip()
                and (cfg.get("wx_test_template_id") or "").strip())


def _get_wx_test_token(appid, secret, force_refresh=False):
    """获取 access_token，带文件缓存（有效期 7200s，提前 60s 失效换新）。
    设 force_refresh=True 时跳过缓存直接重取（用于 40001 异常恢复）。"""
    if not force_refresh:
        try:
            if os.path.exists(WX_TEST_TOKEN_CACHE):
                c = json.load(open(WX_TEST_TOKEN_CACHE, encoding="utf-8"))
                if c.get("appid") == appid and time.time() < c.get("expire_at", 0) - 60:
                    return c["access_token"]
        except Exception:
            pass
    url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
           f"&appid={urllib.parse.quote(appid)}&secret={urllib.parse.quote(secret)}")
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
        tk = json.loads(r.read().decode("utf-8", "replace"))
    if "access_token" not in tk:
        raise RuntimeError(f"获取 access_token 失败: {tk}")
    token = tk["access_token"]
    expires = int(tk.get("expires_in", 7200))
    try:
        json.dump({"appid": appid, "access_token": token,
                   "expire_at": time.time() + expires},
                  open(WX_TEST_TOKEN_CACHE, "w", encoding="utf-8"))
    except Exception:
        pass
    return token


def _default_wx_test_data(title, message, now):
    """默认模板字段：keyword1=签到结果, keyword2=说明, keyword3=发送时间。
    测试号后台建模板时按此约定写（务必使用双花括号）：
        结果：{{keyword1.DATA}}
        说明：{{keyword2.DATA}}
        时间：{{keyword3.DATA}}
    单花括号会被微信存为纯文本，无法替换；普通 keyword 行保留
    静态标签，避免微信客户端折叠只含变量的行。"""
    return {
        "keyword1": {"value": title},
        "keyword2": {"value": message},
        "keyword3": {"value": now},
    }


def _wechat_test_send(title, message, cfg):
    """发送微信测试号模板消息；成功返回 True，任何失败返回 False。"""
    appid = (cfg.get("wx_test_appid") or "").strip()
    secret = (cfg.get("wx_test_secret") or "").strip()
    touser = (cfg.get("wx_test_touser") or "").strip()
    tpl = (cfg.get("wx_test_template_id") or "").strip()
    if not (appid and secret and touser and tpl):
        logger.warning("微信测试号配置不完整（需 appid/secret/touser/template_id），跳过")
        return False

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_tpl = (cfg.get("wx_test_data") or "").strip()
    if data_tpl:
        data_str = (data_tpl.replace("{title}", title)
                    .replace("{content}", message)
                    .replace("{time}", now))
        try:
            data = json.loads(data_str)
        except Exception:
            logger.warning("wx_test_data 非合法 JSON，改用默认字段")
            data = _default_wx_test_data(title, message, now)
    else:
        data = _default_wx_test_data(title, message, now)

    body = json.dumps({
        "touser": touser,
        "template_id": tpl,
        "data": data,
    }, ensure_ascii=False).encode("utf-8")

    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            access_token = _get_wx_test_token(appid, secret,
                                              force_refresh=(attempt > 0))
        except Exception as e:
            logger.warning("微信测试号获取 access_token 失败: %s", e)
            return False
        url = ("https://api.weixin.qq.com/cgi-bin/message/template/send"
               f"?access_token={urllib.parse.quote(access_token)}")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=ssl.create_default_context()) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            logger.warning("微信测试号推送网络异常: %s", e)
            return False

        if resp.get("errcode") == 0:
            logger.info("微信测试号推送成功")
            return True

        errcode = resp.get("errcode", -1)
        errmsg = resp.get("errmsg", "")
        # 40001: access_token 过期或被别处刷新 → 清缓存重试一次
        if errcode == 40001 and attempt == 0:
            logger.info("微信测试号 access_token 过期，清除缓存重试…")
            try:
                os.remove(WX_TEST_TOKEN_CACHE)
            except Exception:
                pass
            continue

        logger.warning("微信测试号推送返回非 0: %s", resp)
        return False
    return False


# ------------------------- 主流程 -------------------------
def do_checkin(dry_run=False, force=False):
    """
    执行一次签到尝试。
    返回 (ok: bool, transient: bool, outcome: str)：
      - ok=True        ：签到成功 / 今日已签 / dry-run 成功
      - ok=False, transient=True  ：临时错误（网络/5xx），可重试
      - ok=False, transient=False ：确定性失败（无 token / token 过期 / 未知错误），不可重试
    """
    tok_uid = extract_token()
    if not tok_uid:
        logger.error("未找到有效 Bearer token（WorkBuddy/CodeBuddy CLI 均无有效登录态）")
        print("❌ 未找到有效登录 token。")
        print("   请运行 checkin_cli.py wizard，选择 WorkBuddy 或无 WorkBuddy 登录模式。")
        return False, False, "failed"
    token, uid = tok_uid
    logger.info("提取 token 成功: uid=%s token_len=%d", uid, len(token))

    # 1) 查询今日签到状态（仅用于展示；失败不阻断后续签到尝试）
    data = {}
    try:
        status, sj = _post(CHECKIN_STATUS_URL, token, uid)
        data = (sj.get("data") or {}) if isinstance(sj, dict) else {}
        logger.info("签到状态: %s", data)
    except Exception as e:
        logger.warning("查询签到状态失败（不影响签到尝试）: %s", e)

    today_checked = data.get("today_checked_in")
    active = data.get("active")
    print(f"📋 状态接口: 今日已签={today_checked} active={active} | "
          f"连续天数={data.get('streak_days')} | 总积分={data.get('total_credits')}")

    if dry_run:
        print("（dry-run，未执行签到）")
        return True, False, "dry_run"

    # 仅当活动激活且状态接口明确显示已签时才跳过（避免 active=False 误判）
    if active and today_checked and not force:
        print("✅ 今日已签到，跳过。")
        return True, False, "already_checked_in"

    # 2) 执行签到（以 daily-checkin 实际返回为准，状态接口仅供参考）
    try:
        cstatus, cj = _post(CHECKIN_CLAIM_URL, token, uid)
    except urllib.error.HTTPError as e:
        # 服务端在「已签到」时返回 HTTP 400 + body {"code":10001}，需解析 body 判断
        body = e.read(2000).decode("utf-8", "replace")
        try:
            cj = json.loads(body)
        except Exception:
            cj = {"msg": body}
        code = cj.get("code") if isinstance(cj, dict) else "?"
        msg = cj.get("msg") if isinstance(cj, dict) else body
        if code == 10001 or "已签到" in msg:
            logger.info("接口确认今日已签到: %s", cj)
            print("✅ 今日已签到（接口确认），无需重复。")
            return True, False, "already_checked_in"
        # 4xx 中除了「已签」都视为确定性失败（含 401 token 过期）
        logger.error("签到 HTTP %d: %s", e.code, body)
        print(f"❌ 签到请求失败 (HTTP {e.code}): {msg}")
        return False, False, "failed"
    except urllib.error.URLError as e:
        # 网络层错误（DNS / 超时 / 连接被拒）→ 临时错误，可重试
        logger.error("签到网络错误（可重试）: %s", e)
        print(f"❌ 签到网络错误（将重试）: {e}")
        return False, True, "failed"
    except Exception as e:
        logger.error("签到异常（可重试）: %s", e)
        print(f"❌ 签到异常（将重试）: {e}")
        return False, True, "failed"

    if isinstance(cj, dict) and cj.get("code") == 0:
        d = cj.get("data") or {}
        logger.info("签到成功: %s", d)
        print(f"🎉 签到成功！今日获得积分: {d.get('today_credit')} | "
              f"连续: {d.get('streak_days')} 天 | 总积分: {d.get('total_credits')}")
        return True, False, "checked_in"
    # code=10001 / “今天已签到” 视为今日已签完成（幂等）
    msg = cj.get("msg") if isinstance(cj, dict) else str(cj)
    code = cj.get("code") if isinstance(cj, dict) else "?"
    if code == 10001 or "已签到" in msg:
        logger.info("接口确认今日已签到: %s", cj)
        print("✅ 今日已签到（接口确认），无需重复。")
        return True, False, "already_checked_in"
    # 其它非 0 返回（多数 5xx 之类或业务错误）→ 临时可重试
    logger.error("签到返回非 0（可重试）: %s", cj)
    print(f"⚠️ 签到接口返回: code={code} msg={msg}")
    return False, True, "failed"


def classify_message(ok, transient, outcome="checked_in"):
    """生成通知文案。"""
    if ok and outcome == "already_checked_in":
        return (
            "✅ WorkBuddy 今日已签到",
            "检测到用户此前已经签到，本次未执行自动签到",
        )
    if ok:
        return "🎉 WorkBuddy 自动签到成功", "本次自动签到已完成"
    if transient:
        return "⚠️ WorkBuddy 签到失败", "网络/服务端临时错误，已超出重试次数"
    return "❌ WorkBuddy 签到失败", "确定性错误（token 过期/无 token），请重新登录 WorkBuddy"


def classify_retry_message(attempt, delay):
    """生成首次临时失败的即时通知，避免在整个退避期间静默。"""
    return (
        "⚠️ WorkBuddy 签到失败，正在重试",
        f"第 {attempt} 次自动签到失败，将在 {delay} 秒后自动重试",
    )


def build_test_message():
    """沿用真实签到成功文案，仅附加测试标记。"""
    title, message = classify_message(True, False)
    return f"{title}【测试】", f"【测试消息】{message}"


def main():
    args = sys.argv[1:]
    dry = "--dry-run" in args
    force = "--force" in args
    no_retry = "--no-retry" in args
    cfg = load_config()

    max_retries = 0 if no_retry else int(cfg.get("max_retries", MAX_RETRIES))
    base_delay = int(cfg.get("retry_base_delay", RETRY_BASE_DELAY))

    attempt = 0
    ok = False
    transient = False
    outcome = "failed"
    while True:
        attempt += 1
        ok, transient, outcome = do_checkin(dry_run=dry, force=force)
        if ok or not transient or attempt > max_retries:
            break
        delay = base_delay * (2 ** (attempt - 1))
        logger.warning("第 %d 次失败（临时错误），%d 秒后重试…", attempt, delay)
        print(f"⏳ {delay} 秒后第 {attempt + 1} 次重试…")
        # 首次失败就立即告知用户；后续重试不重复推送，避免刷屏。
        # 重试结束后下方仍会发送最终成功/失败结果。
        if attempt == 1 and not dry:
            retry_title, retry_message = classify_retry_message(attempt, delay)
            notify(retry_title, retry_message, cfg)
        time.sleep(delay)

    if attempt > 1:
        logger.info("共尝试 %d 次，结果 ok=%s", attempt, ok)

    title, message = classify_message(ok, transient, outcome=outcome)
    if dry:
        # dry-run 仅查询，不弹通知（避免每次手动查询都打扰）
        logger.info("[dry-run] 跳过通知发送（title=%s）", title)
    else:
        notify(title, message, cfg)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
