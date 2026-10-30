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

### 数据库字段同步

更新代码后，目标站点需要完成数据库迁移；`after_install` 和 `after_migrate` 均调用
`crm_integration.setup.sync_custom_fields`，同步 CRM 自定义字段。仅更新源码或清缓存不会创建缺失的数据库列。
需要定向同步 CRM 字段时，在确认目标站点并备份后，从 bench 目录执行：

```bash
bench --site <目标站点> execute crm_integration.setup.sync_custom_fields
```

该方法会同步字段定义和物理列，并确保 CRM 临时补库单据类型存在；生产环境仍按现有授权发布流程执行。
`Delivery Note.custom_crm_repost_ids` 是 ERP 内部保存原生库存重估任务 ID 的只读、非必填字段，
由 ERP 在出库提交后写入，CRM 不需要传入。升级后应同时核对 Custom Field 元数据和数据库列。

CRM 集成日志的 `http_status_code=0` 表示没有可用的 HTTP 状态码；创建和更新日志时将显式的
`None` 转为 `0`，保留已有状态码及原始错误内容，避免内部异常再次触发日志非空约束错误。

### ERP 发货状态推送开关

`crm_shipment_status_push_enabled` 默认是 `1`，保持既有行为。需要暂停的站点通过
`bench --site <目标站点> set-config crm_shipment_status_push_enabled 0` 单独关闭 Delivery Note
提交／取消的 `SHIPMENT_CREATED`、`SHIPMENT_CANCELLED` 事件。新代码的 worker 也会跳过已排队但
尚未执行的这两类任务。销售订单状态更新、MES 回调及其他 CRM 状态事件不受此开关影响。
暂停期间不生成这两类出站日志，任务不会自动补发；恢复前应按 ERP Delivery Note 与 CRM 对账，
然后在目标站点将开关设为 `1` 并按发布流程重启相关进程。

### 按产品配置直接履约

管理员打开 **公司 → 对应公司 → 无需 MES 生产的产品编码前缀**，每行填写一个产品的三位编码，例如默认的 `NSF`（钢化膜）。以后增加免生产产品只需在名单中增加该产品编码，无需修改 Python、逐个型号建规则或重新构建镜像。该公司下同一产品的不同型号、颜色等明细全部随整张订单放行；识别取物料编码前三位，不取第一个型号决定其他产品。名单按公司隔离，清空表示该公司无免生产产品。支持换行、逗号或分号分隔并自动统一大写、去重，拒绝不足/超过三位及通配符；仅 System Manager / Administrator 可修改，Company 普通编辑权限不会获得该规则的修改权，服务端 Company validate hook 同时校验。

订单必须有明细、每行数量为正且全部属于同一个名单产品，才属于 `DIRECT`。空单、无效行及未配置产品不享受免生产例外。正常业务为一个产品一单，多型号无需拆单；若异常混入其他产品，财务放行前提示按产品拆单，不整单绕过 MES、不部分下发。

直接履约仍须人工财务确认，沿用公司 CRM 开关、订单权限、生产放行权限、收款关联核验、逐单审计和提交后队列。为兼容现有 CRM 消费流程，继续按 E2、E3 回传并保留 E3 幂等及持久修订锁，但不检查 MES 下发配置、不调用 MES 收单；成功后进入 `Pending Final Payment`，不自动核销尾款或改成 `Deliverable`。尾款/库存/发货校验及 C6 既有严格/旧格式契约继续适用。未完成的失败订单可以按公司当前名单对原单重新确认，不清除原 E3 事实，也不自动批量修复历史单。普通生产订单继续走 CRM/MES 编排，成功后进入 `Pending Production`。

`production_routing.py` 是共享策略来源。worker 在外部同步前只解析一次路径，并将该路径明确传给 MES 下发函数；下层不再读取公司名单，且只有 MES 返回 `success=true` 才能完成制造订单的放行。成功路径写入 Sales Order.`custom_crm_fulfillment_route`（隐藏、只读、允许提交后由内部流程更新、不复制到新修订单），与最终流程状态在同一事务内保存；日常表单/REST 写入由服务端拒绝。公司名单变化或清理日志不会改变这份业务快照。审计继续保存预核验及实际结果，既有审计、MES 成功记录仅作为旧单迁移的证据。

必须配套更新 `mes_integration` 的销售订单及出库回调：按相同策略跳过直接履约订单的 MES HTTP 请求和投递日志，返回内部 `status=skipped, reason=NO_MES_PRODUCTION`。多订单出库仍逐单回调其他需生产订单。Company 的 `custom_direct_fulfillment_product_prefixes` 和 Sales Order 的 `custom_crm_fulfillment_route` 由 `crm_integration.setup.sync_custom_fields` 安装/迁移同步；未同步订单字段时，财务确认在外部同步前拒绝继续。首次新增公司列时初始化空值为 NSF，后续迁移保留名单。历史订单按每批 200 单使用成功财务审计、MES 成功记录补存路径；旧 `Pending Production` 状态能确认 MES 路径，其他已完成/已取消且证据不足的旧单保存 `UNKNOWN`，不使用当前名单猜测。已有快照不会被重写，未放行订单仍按当前名单审批。核验接口保留原字段并可返回 `fulfillment_route=DIRECT/MES/MIXED/UNKNOWN`；UNKNOWN 阻止新放行或 MES HTTP 回调，由管理员核对原业务记录后定向修复。开发站点可定向执行 `crm_integration.patches.add_direct_fulfillment_products.execute`；生产按授权发布流程迁移、重启相关进程并刷新前端与翻译缓存。回归入口为 `test_production_routing.py`、`test_sales_order.py`、`test_finance_release.py` 和 `crm_integration/tests-finance-release.test.js`。

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
