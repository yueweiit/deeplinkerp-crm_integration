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

### 财务放行失败时的 CRM 响应日志

`push_sales_order_status_to_crm` 遇到 HTTP 4xx/5xx、连接失败或超时时，以既有 `ValidationError` 携带脱敏的失败证据；不提前提交业务事务。`confirm_deposit_and_push_to_mes_job` 完成业务回滚、恢复待确认状态后，重建具体失败事件（如 `Confirmed Deposit Push Production` / `In Production`）的 CRM 集成日志，并在 `Confirm Deposit External Sync` 中保存同一请求摘要、响应、HTTP 状态、外部状态和 traceId，随既有失败审计事务提交。此前被回滚的历史响应无法通过此次修改恢复。

日志不保存鉴权请求头、备注及附件；请求仅保留系统来源、ERP/CRM 单号、审批轮次、状态和 traceId。URL 移除用户信息、查询参数和片段；失败响应和异常说明对凭据字段、已知密钥及常见鉴权文本做脱敏，带空格、转义字符或未闭合引号的凭据值整体遮盖；输出限制深度、节点和文本长度。大响应优先保留 errorCode/code、traceId、message 等诊断摘要，先固定字段名再处理值，被截断的 data/errors 保留字段名并显示截断标记。无 HTTP 响应时状态码记 `0`，保留异常类型。成功请求、状态恢复、收款和财务权限规则不变。此修复复用现有日志字段，无新增数据库结构；定位测试见 `test_status_push_failure.py`，生产仍需发布新代码并重启相关进程。

### CRM 产品来源与 MES 路由

C1 `create_and_submit_sales_order` 与 R4 `resubmit_crm_sales_order` 可在 Sales Order 订单头和 Sales Order Item 明细接收 `custom_product_source`。取值为 `SELF_DEVELOPED`（自研款）、`EXTERNAL_PURCHASE`（贸易款）或空值；字段不必填、无默认值，不按物料编码或主数据补值。订单头填值时，所有明细必须有相同明确来源；明细出现贸易款而订单头为空也拒绝建单，防止误下发 MES。CRM 保证一单不混用自研与贸易。C1 幂等重试核对显式传入的明细来源，无法唯一匹配或来源冲突时拒绝返回成功。

R2 可在 `header.custom_product_source` 与 `items[].custom_product_source` 中修订来源；补行时应明确传入新行来源。C2 `complete_missing_items` 的完整 manifest 可用 `productSource` 传逐行来源，现有行传值时必须与 ERP 保存值一致。R2/C2 在写入前复核整单来源，贸易款头不能带空来源新行；来源变更仍受推送 MES 前的结构修订限制。旧来源为空的请求继续保持为空。

只有订单头明确为 `EXTERNAL_PURCHASE` 且明细均为贸易款时，整单走 `DIRECT` 并跳过 MES；自研款及空来源走 `MES`。旧 Company `custom_direct_fulfillment_product_prefixes` 保留历史数据但隐藏、不再决定未放行订单路径。因此来源为空的未放行旧 NSF 订单也会走 MES，发布前须核对这些订单。已放行订单优先读取已保存的 `custom_crm_fulfillment_route`、既有成功审计和 MES 记录，不因本次规则更新重新判定，也不批量填充历史来源。

直接履约仍须人工财务确认，继续按 E2/E3 回传 CRM 并保留审计及修订锁；跳过 MES 配置检查、B1 下发和后续 MES 回调，成功后进入 `Pending Final Payment`。MES 路径只有 MES 明确返回 `success=true` 才能进入 `Pending Production`。`production_routing.py` 为财务放行和 `mes_integration.settings.requires_sales_order_mes_sync` 的共享策略；worker 在外部同步前确定路径，完成后将实际 `DIRECT`/`MES` 写入订单路径快照。未知历史路径保持 `UNKNOWN` 并阻止 MES HTTP，需人工核对。生产发布须配套迁移新字段及重启相关进程；本地回归入口为 `test_api.py`、`test_production_routing.py` 和 `mes_integration/test_direct_fulfillment.py`。

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
