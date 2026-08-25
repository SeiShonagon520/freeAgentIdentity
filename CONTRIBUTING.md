# 贡献指南

感谢参与。公开仓库只接受可审阅、可测试且不依赖真实账号或第三方秘密的改动。

## 开发环境

```bash
python -m venv .venv
# Windows: .\\.venv\\Scripts\\Activate.ps1
source .venv/bin/activate
pip install -r requirements.txt
```

前端依赖：

```bash
cd frontend
npm ci
```

## 提交前检查

```bash
pytest
git diff --check
cd frontend
npm run build
```

如果改动涉及配置、发布流程或安全边界，请在 PR 描述中说明影响范围和回滚方式。

## 数据与安全要求

- 只使用合成邮箱、合成 Token 和本地 mock 响应；
- 不提交 `.env`、数据库、浏览器 profile、HAR、Cookie、OTP、截图或日志；
- 不新增验证码求解、反自动化、代理轮换、支付流程或第三方账号批量操作代码；
- 不把凭据写入测试、README、Issue 模板或 GitHub Actions 输出；
- 新增外部依赖时，注明许可证、用途和是否会发送数据到第三方。

## 代码风格

- Python 遵循 PEP 8，尽量补充类型注解；
- React/TypeScript 保持现有 ESLint 和组件风格；
- 日志中只记录脱敏后的标识，不打印密码、Cookie、Token、邮箱验证码或完整 URL 查询参数；
- 错误信息应帮助本地调试，但不能泄露服务端密钥或用户数据。

## Issue 与 Pull Request

安全问题请按照 [SECURITY.md](SECURITY.md) 私下报告，不要在公开 Issue 中粘贴利用细节或敏感数据。普通 Bug/功能建议请提供最小可复现步骤、环境信息和脱敏日志。
