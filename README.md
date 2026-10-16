### Crm Integration

connect to crm

### 协作入口

CRM 订单、销售出库及 ERP 侧 CRM 接口的独立仓库为 [yueweiit/deeplinkerp-crm_integration](https://github.com/yueweiit/deeplinkerp-crm_integration)。从 `main` 分支更新后开发，PR 目标也选择 `main`，Frappe App 名保持 `crm_integration`。
2026-10-16 的应用基线与线上 `3711df05962d0a09f5f5a7363c431a323c33e934` 完全一致，生产源码已保存为标签 `production/2026-10-16/crm_integration`。
整理前尚未部署的订单修订等改动保存在合并历史；上述生产标签及 `archive/2026-10-16/crm_integration-before-production-alignment` 标签继续保留于原仓库 `yueweiit/DeepLinkERP`，需要时单独评审恢复。
ERP 页面、财务和 OA 分别使用各自独立仓库，跨应用修改拆分 PR 并说明依赖。详见[独立仓库说明](https://github.com/yueweiit/deeplinkerp-deeplinkerp_branding/blob/main/docs/contributing/erp-branches.md)。
本次分支整理没有发布服务器，也没有修改接口或业务数据；合并代码后仍需按现有流程发布。

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app --branch main crm_integration https://github.com/yueweiit/deeplinkerp-crm_integration.git
bench install-app crm_integration
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/crm_integration
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI：在创建 PR 和推送到 `main` 时安装应用并运行测试。
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### License

mit
