# GitHub 公开发布检查清单

- [ ] 工作树中没有 `.env`、数据库、日志、导出文件或浏览器 profile；
- [ ] 当前版本和 Git 历史均未包含密码、私钥、API key、Cookie、Token 或验证码；
- [ ] HAR、网络抓包、支付流程和内部诊断材料未进入提交或 Release 附件；
- [ ] README、CONTRIBUTING 和 SECURITY 与实际功能边界一致；
- [ ] `.gitignore` 和 `.dockerignore` 已覆盖本地凭据与运行时目录；
- [ ] `pytest`、`git diff --check` 和前端构建通过；
- [ ] GitHub Actions 没有把 Secret 打印到日志；
- [ ] 生产环境的 `APP_PASSWORD`、JWT secret 和管理员密码已替换为随机值；
- [ ] 发布前已检查 fork、旧 tag 和历史提交中的敏感内容。
