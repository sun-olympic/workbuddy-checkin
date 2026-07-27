# WorkBuddy 自动签到（macOS）

通过 WorkBuddy HTTP 接口每日自动签到，无需保持 WorkBuddy 窗口运行或授予辅助功能权限，也支持完全不安装 WorkBuddy。支持 macOS 通知、微信测试号、pushplus 和 webhook。

## 环境预检

`wizard` 和 `install` 会先检查 macOS、Python 3.8+ 和有效登录态。没有登录态时可选择：

- **WorkBuddy 模式**：自动打开已安装的 WorkBuddy，等待登录完成。
- **无 WorkBuddy 模式**：使用独立 CodeBuddy CLI 打开浏览器登录；未安装 CLI 时，向导可通过 npm 自动安装。

两种模式都只需首次完成一次扫码/授权。脚本直接读取官方登录状态，不会把登录 Token 复制到项目配置。完全免登录不可行，因为签到接口必须携带有效凭证。

自动绑定微信时建议已安装 Chrome；Playwright 会自动安装到项目专用环境。

## 快速开始

```bash
cd /path/to/workbuddy-checkin

# 1. 配置时间、系统通知和远程通知
python3 checkin_cli.py wizard

# 2. 注册每日定时任务
python3 checkin_cli.py install

# 3. 验证通知与定时状态
python3 checkin_cli.py test-notify
python3 checkin_cli.py status
```

`config` 不带参数时与 `wizard` 等价。状态中必须显示“定时注册状态：已注册”，否则到点不会执行。

## 无 WorkBuddy 模式

```bash
python3 checkin_cli.py wizard
```

未检测到 WorkBuddy 时会自动进入无 WorkBuddy 模式；已安装 WorkBuddy 时，在登录方式中选择 `2`。向导会：

1. 检查独立 CodeBuddy CLI。
2. 如未安装，询问后自动执行 `npm install -g @tencent-ai/codebuddy-code`。
3. 打开浏览器，等待用户完成一次扫码/授权。
4. 获取有效登录态后继续配置。

此模式需要 Node.js 18.20.8+ 和 npm，但不需要安装或运行 WorkBuddy。

## 绑定微信

```bash
# 交互选择自动或手动
python3 checkin_cli.py wx-bind

# 直接指定
python3 checkin_cli.py wx-bind --mode auto
python3 checkin_cli.py wx-bind --mode manual
```

- **自动模式**：打开可见浏览器，自动获取 AppID/AppSecret、创建模板、识别 OpenID、保存配置并发送测试消息。用户只需扫码。
- **手动模式**：不安装 Playwright，按向导输入 AppID、AppSecret 和模板 ID；OpenID 仍会自动获取。
- 自动模式失败后，直接回车即可降级到手动模式。
- `wx-login` 是 `wx-bind` 的兼容别名；`wx-setup` 是旧的手动入口。

微信模板使用以下结构，静态标签用于避免客户端折叠变量行：

```text
结果：{{keyword1.DATA}}
说明：{{keyword2.DATA}}
时间：{{keyword3.DATA}}
```

旧模板显示不完整时，重新执行 `wx-bind --mode auto`。

## 通知与重试

- macOS 系统通知是独立开关，不参与远程通知单选。
- 远程通知只能选择一种：微信测试号、pushplus、webhook 或不使用。
- 首次临时失败会立即通知“正在重试”，重试结束后再通知最终结果。
- 如果用户当天已签到，仍会通知“此前已签到，本次未执行自动签到”。

## 常用命令

| 操作 | 命令 |
| --- | --- |
| 配置向导 | `python3 checkin_cli.py wizard` |
| 注册定时任务 | `python3 checkin_cli.py install` |
| 查看状态 | `python3 checkin_cli.py status` |
| 立即签到 | `python3 checkin_cli.py run` |
| 只查询状态 | `python3 checkin_cli.py run --dry-run` |
| 关闭重试运行 | `python3 checkin_cli.py run --no-retry` |
| 发送测试通知 | `python3 checkin_cli.py test-notify` |
| 卸载定时任务，保留配置 | `python3 checkin_cli.py uninstall` |
| 彻底清理运行数据 | `python3 checkin_cli.py uninstall --purge` |

彻底清理会删除配置、微信凭证/token 缓存、plist、日志、截图、Python/测试缓存和 Playwright 专用环境；项目源码保留。

## 删除定时任务

```bash
# 仅删除定时任务，保留配置
python3 checkin_cli.py uninstall

# 删除定时任务并彻底清理所有签到相关配置
python3 checkin_cli.py uninstall --purge
```

彻底清理不会删除独立 CodeBuddy CLI 及其账号登录状态，以免影响 CodeBuddy 的正常使用。

## 命令行配置示例

```bash
# 修改时间与重试参数
python3 checkin_cli.py config --time 09:10 --retries 4 --delay 30

# pushplus
python3 checkin_cli.py config --wechat <token>

# webhook（默认 POST JSON）
python3 checkin_cli.py config --webhook "https://example.com/hook"

# 清除微信测试号绑定
python3 checkin_cli.py config \
  --wx-appid "" --wx-secret "" --wx-touser "" --wx-template ""
```

## 排查

1. 运行 `python3 checkin_cli.py status`，确认配置、微信绑定和定时注册均正常。
2. 运行 `python3 checkin_cli.py test-notify` 验证通知链路。
3. 查看 `checkin.log`、`launchd.out.log` 和 `launchd.err.log`。
4. 扫码页空白时，关闭代理/VPN 或切换网络，并使用手机微信扫码后确认登录。

本项目不使用微信个人号协议，避免封号风险。
