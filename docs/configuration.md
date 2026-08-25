# 配置说明

## 配置文件

```bash
cp .env.example .env
```

`.env` 只用于本机或受控部署，始终不要提交。共享/生产环境至少设置：

```env
APP_PASSWORD=<随机生成的长密码>
ACCOUNT_MANAGER_DATABASE_URL=sqlite:///./data/account_manager.db
```

## 凭据存储

邮箱密码、刷新令牌、Cookie、TOTP 密钥和第三方 API key 都属于敏感数据。建议：

- 使用操作系统 Secret 管理器或容器 Secret 注入；
- 不在命令行参数、Issue、PR 和日志中传递完整凭据；
- 备份数据库时同时按受控流程备份加密密钥；
- 删除账号或导出文件后，按组织的数据保留策略清理备份副本。

## 外部服务

项目包含可选的邮箱、代理、验证码和平台适配器。启用前请：

1. 只使用自己控制或明确获授权的服务；
2. 阅读目标平台和 provider 的服务条款；
3. 先在本地 mock 或测试账号上验证；
4. 将 API key 放在未跟踪的 Secret 文件中；
5. 限制网络访问范围，并关闭不使用的 provider。

## HAR 与调试材料

公开版不包含 HAR 录制/分析入口。HAR、浏览器 trace、Cookie、OTP、截图和完整请求日志可能包含可复用凭据，不能上传到 GitHub。调试时请先脱敏，再只保留能复现问题的最小片段。
