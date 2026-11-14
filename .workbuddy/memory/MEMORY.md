# PT-signin 项目长期记忆

## 工作流约定

- **不要自动推送 GitHub**（origin = https://github.com/zljiscx/auto-signin.git，分支 main）。只在用户明确下达推送指令时推送；若判断当次改动值得推送，先问一句「要不要推送」，同意后再推。
- 提交说明要简明扼要，只讲重点：一句话主题 + 3~4 条要点，不写背景铺垫。

## 环境

- 生产运行环境：飞牛 NAS Docker；另有 Windows 本地与 InstaBox 的 zljiscx 机器。
- 本地 Windows 无 GitHub 凭据助手（portable git 的 git-credential-manager 路径失效），推送报该警告但实际成功，可忽略。

## 签到模式

- 站点四种模式：`browser`（本地无头浏览器）/ `browser_api` / `api` / `buddy`，新增 `desktop`（桌面浏览器经 CDP 复用真实指纹，专解 Cloudflare 托管挑战）。
- 躺平站（tangpt.top）纯 API 不可行：不带 Turnstile token 直接报 "Captcha verification token is missing."，只能走浏览器类模式。
- 躺平站的 Turnstile 在无头 Chrome 里永远拿不到 token，必须 desktop 模式；桌面浏览器地址：容器内 `172.20.0.1:9222`，外网/组网 `100.100.1.10:9222`（Chrome/154，Linux X11，同一实例）。

## 踩坑：DrissionPage 远端 CDP

- Turnstile 的「Verify you are human」在跨域 iframe 内，父文档与 CDP DOM 树都读不到文字，只能按 `.cf-turnstile` 组件坐标点击（x+30、垂直居中）。
- DrissionPage 4.0.4 有两级类级缓存 `ChromiumPage._PAGES` 与 `Browser.BROWSERS`；断开/关标签后重连必拿到已断开的旧对象，表现为「与页面的连接已断开」，甚至永久阻塞。
- 带调试连接调 `GET /json/close/<id>` 会挂住；`put /json/new` 才能新建标签。
- 结论：远端浏览器整进程复用一条连接，结束只把标签退回 `about:blank`，不断开、不关标签。

