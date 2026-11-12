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
