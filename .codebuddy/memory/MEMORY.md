# MEMORY.md — 项目长期记忆

## 项目：PT-signin（Python 自动签到，Flask + DrissionPage + requests）
- 四种执行模式：`browser`（DrissionPage 模拟真人，处理按钮/验证码/Cloudflare）、`api`（requests 直连接口，支持「登录接口」换取 Cookie 后签到）、`browser_api`（浏览器登录+API签到）、`buddy`（Buddy 令牌式签到，见下）。
- 浏览器模式通用登录适配（2026-09-16 加入）：
  - **易踩坑**：给 React/Vue 受控输入框赋值必须用原生 value setter（`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set`）再派发 input/change，否则框架状态不更新、提交拿到空值。`utils.py` 的 `JS_FILL_TEMPLATE`/`JS_FILL_CAPTCHA_TEMPLATE` 已实现。
  - 字段识别走 `executors/browser.py` 的 `detect_login_fields`（启发式兜底），用户手动 selector 优先（`_pick_selector` 自愈：配置能定位才用，否则探测/DEFAULT），最终回退 `DEFAULT_*_SELECTORS`。
  - **DrissionPage v4 致命坑（`run_js` 必须 `return`）**：`page.run_js(script)` 仅在脚本**以 `return` 开头**时才返回值，否则只执行副作用并返回 `None`。所有需要返回值的脚本（探测/判定/取值）的 IIFE 必须以 `return` 开头；纯副作用脚本（填充/点击）不需要。`return (function(){...})()` 在 v4 与各旧版都安全。
  - **browser_api 模式（浏览器登录+API签到）**：`BrowserExecutor.login_and_refresh_cookies()` 是独立一次性方法，登录成功后必须 `driver.close()`（否则浏览器泄漏）。登录后通过 `_capture_browser_headers` 把浏览器真实 `User-Agent/Accept/Accept-Language/Referer` 写入 `ctx.browser_headers`（UA 同时写 `ctx.vars['ua']`）；`ApiExecutor._do_request` 会把 `ctx.browser_headers` 作为默认值合并进 API 请求头（步骤显式配置优先），让接口请求与浏览器保持一致的 UA/Referer，规避 WAF 拦截。
  - **`ctx.browser_headers` 已持久化**：sites 表新增 `headers` 列（plain JSON，非加密）。`SignContext.__init__` 会加载 `site['headers']` 进 `ctx.browser_headers`；`sign_service.sign_site` 在每次运行后（与签到成功与否无关）通过 `update_site_browser_headers` 写回。这样**下次纯 API 模式（已有 Cookie、跳过浏览器）也会加载并复用统一请求头**，不再因为没有浏览器而丢失 UA 等头。编辑站点页在 Cookies 下方以只读文本框展示已提取的请求头。
  - **JS 字符串拼接用 `json.dumps` 而非手动转义**：把 Python 值（尤其含单引号/XPath/中文）拼进 JS 时，用 `json.dumps(value)` 生成合法 JSON 字符串，并让模板占位符 `%s` 不带自带引号。切忌 `sel.replace("'","\\'")`（会截断含 `'` 的 XPath 如 `contains(.,'登录')`）。`_selector_exists` 按逗号拆多 CSS 选择器时，XPath（以 `//` 开头）须整体不拆，否则其内置逗号被拆断。
  - 依赖装在项目 `.venv`（`DrissionPage==4.0.4`），跑脚本用 `.\.venv\Scripts\python.exe`。
- **`buddy` 模式（Buddy 令牌式签到，2026-09-17 新增，整合自 `D:\.Buddy\Buddy-signin`）**：
  - 动机：Buddy加油站/WorkBuddy 用 `Bearer <accessToken>` 鉴权 + `X-User-Id`(JWT sub) + `X-Domain`，有 token 续期（轮换 refresh_token），原生三种模式无法支持「自动续期+永久登录」（`api` 模式的 login 步骤不回写 token，续期链会断）。
  - 实现：`executors/buddy.py` 的 `BuddyExecutor(mode='buddy')`，完全隔离不动 browser/api/browser_api。复用 buddy_checkin.py 三接口：`/v2/billing/meter/daily-checkin`、`/v2/billing/meter/checkin-activity-status`、`/v2/plugin/auth/token/refresh`；成功判定 `code==0` 或 `code==10001/已签到`。
  - 令牌来源：① 站点 `token_store` 列（加密 JSON，NAS/Docker 端由用户在「令牌」框粘贴一次）；② 本机（Windows）`WorkBuddy` 桌面登录态文件 `~/AppData/Local/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info`（留空自动读）。
  - **续期持久化**：`sites` 表新增 `token_store` 列（加密 TEXT）；`SignContext._load_token_store` 解密加载；`executors/buddy.py` 的 `_persist_token_store` 续期后回写（含轮换后的 refresh_token + expires_at + last_refresh_date）→ 实现一次读取永久登录。复用本项目 `sign_service` 重试/调度/日志，**不自带 times**（按本项目 sign_times 时间表、按站点顺序执行）。
  - 用户确认：用「新增 buddy 模式 + 表单粘贴 token_store」方式整合，且 Buddy 签到不自带 times。
- **推送消息统一格式约定（2026-09-17 确立）**：企业微信推送（sign_service.run_all_scheduled_sign 汇总）每站只发结果，格式 `✅ {站名} - 签到成功` / `❌ {站名} - 签到失败` / `🔄 {站名} - 今日已签到`（emoji 由 sign_service 状态前缀给出，msg 本身为 `签到成功`/`签到失败`/`今日已签到`）。所有 `SignResult.message` 必须是「只含结果、无日期/无签到方式标注/无响应片段」的短句；判定细节、`（检测到标识）`/`（点击按钮后）` 等签到方式、响应片段、积分汇总等诊断信息一律写 `ctx.log`（执行日志），不进推送。`executors/buddy.py`、`browser.py`、`api.py` 均已按此收敛。
- 远程仓库：`origin` = `https://github.com/zljiscx/auto-signin.git`，默认分支 `main`。推送用 HTTPS 需 Personal Access Token（GitHub 已禁用密码）。`.codebuddy/` 为智能体私有记忆，建议加进 `.gitignore` 勿推到公开仓库。
- 部署环境：用户有 Windows 本机与飞牛 NAS（Docker）。Docker 下中文响应可能因 charset 探测库版本不同乱码——`executors/api.py` 已改为无 charset 时优先 UTF-8 解码。
