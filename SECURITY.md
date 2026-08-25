# 安全策略

## 支持范围

当前只维护默认分支和最新发布版本。请先用最新代码复现问题，并删除日志中的账号、Cookie、Token、邮箱地址和验证码。

## 私下报告

请使用 GitHub 的 [Private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability)，或通过仓库维护者在 GitHub 个人主页公布的联系方式私下报告。

不要在公开 Issue、PR、截图或日志中提交：

- 密码、API key、私钥、Cookie、Session、access/refresh token；
- 邮箱池、数据库、浏览器 profile、HAR、网络抓包或一次性验证码；
- 未脱敏的服务器地址、代理凭据、内部域名或第三方服务响应。

## 运行安全基线

- 生产环境必须设置随机 `APP_PASSWORD`；空值只适合隔离的本机开发；
- `customer_portal_api/.env.example` 中的 JWT secret 和管理员密码只是占位符，不能直接用于生产；
- 将 Web UI、调试端口和数据库放在受控网络，不要直接暴露到公网；
- 使用最小权限的系统账户运行，限制 `data/` 目录权限并定期备份加密密钥；
- 日志和导出文件设置保留期限，过期后安全删除；
- 关闭不使用的后台任务和外部 provider。

## 凭据泄露处理

如果凭据曾经进入 Git 历史：

1. 立即在对应平台撤销或轮换凭据；
2. 保存必要的取证信息，但不要把原始秘密再次复制到 Issue/PR；
3. 使用 GitHub 官方建议的历史重写工具清理所有引用；
4. 检查 fork、缓存、Release 附件和 Actions 日志；
5. 重新扫描工作树和完整历史后再公开仓库。

只删除当前文件无法清除旧提交中的秘密。

## 公开仓库限制

公开仓库不包含 HAR 录制/分析材料、真实运行数据或生产凭据；代码中的可选 provider 和自动化模块仍需由使用者自行配置。任何接入都必须遵守目标平台的服务条款，不得把本项目用于绕过安全控制或批量滥用。
