(function (root, factory) {
	const api = factory(root);
	if (typeof module === "object" && module.exports) module.exports = api;
	root.CRMFinanceRelease = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
	"use strict";
	const API = "crm_integration.crm_integration.finance_release.";
	const REJECT_API = "crm_integration.crm_integration.sales_order.reject_sales_order";
	const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
	const t = (value, args = []) => String((root.__ || (text => text))(value)).replace(/\{(\d+)\}/g, (_, i) => args[i] ?? "");
	const buttonLabel = count => count === 1 ? t("允许生产（1单）") : t("允许生产（{0}单）", [count]);
	const link = (type, name) => `<a href="/desk/${type}/${encodeURIComponent(name)}">${esc(name)}</a>`;
	const money = (value, currency) => `${Number(value || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${esc(currency || "")}`;
	let active = null;
	function eligibleNames(rows) { return rows.filter(row => row.can_release).map(row => row.name); }
	function retryNames(rows) { return rows.filter(row => row.state === "failed").map(row => row.name); }
	function failedReview(row) { return row.state === "failed" || row.sync_status === "Failed" || row.last_confirmation?.status === "Failed"; }
	function reconcileResults(results, review) {
		for (const result of results) {
			const row = review.find(item => item.name === result.name);
			if (result.state === "processing" && row?.process_status === "Pending Deposit Confirmation" && failedReview(row)) result.state = "failed";
			if (row && result.state === "failed") row.state = "failed";
		}
	}
	function stateLabel(row) {
		if (row.process_status === "Rejected") return t("Rejected");
		if (row.fulfillment_route === "UNKNOWN") return t("Historical order fulfillment route is unknown. Verify its original MES handling before retrying.");
		if (["Pending Production", "Pending Final Payment", "Deliverable", "Partially Delivered", "Completed"].includes(row.process_status)) return row.fulfillment_route === "DIRECT" ? t("已完成财务放行（无需 MES 生产）") : t("已完成生产放行同步");
		if (row.process_status === "Deposit Confirmation Processing" || row.state === "processing") return t("CRM/MES 同步处理中");
		if (failedReview(row)) return row.sync_error?.message || t("同步失败，可重新核验后重试");
		return row.can_release ? t("待人工确认允许生产") : row.reason || t("不可处理");
	}
	function rowsHTML(rows) {
		return rows.map(row => `<tr><td>${link("sales-order", row.name)}${row.custom_crm_order_no ? `<small>${esc(row.custom_crm_order_no)}</small>` : ""}<small>${esc(row.customer_name || "")}</small></td><td>${row.grand_total === undefined ? "—" : money(row.grand_total, row.currency)}</td><td>${(row.receipts || []).map(payment => `${link("payment-entry", payment.name)} · ${esc(payment.posting_date)}<br>${esc(t(payment.docstatus === 1 ? "已提交" : payment.docstatus === 2 ? "已取消" : "草稿"))} · ${esc(t(payment.payment_type))} · ${money(payment.allocated_amount, payment.currency)}`).join("<hr>") || esc(t("未找到可见的关联收款记录"))}${row.receipts_complete === false ? `<p class="text-muted">${esc(t("收款记录可能不完整"))}</p>` : ""}</td><td>${esc(stateLabel(row))}${row.fulfillment_route === "DIRECT" ? `<small>${esc(t("该产品无需 MES 生产，仍需核销尾款后发货。"))}</small>` : ""}${row.last_confirmation ? `<small>${esc(row.last_confirmation.user)} · ${esc(row.last_confirmation.creation)}</small>` : ""}${row.can_reject ? `<button type="button" class="btn btn-default btn-xs crm-release-reject" data-name="${esc(row.name)}">${esc(t("驳回"))}</button>` : ""}</td></tr>`).join("");
	}
	async function open(names, onUpdate) {
		if (active) return;
		const unique = [...new Set(names)];
		if (!unique.length || unique.length > 100) { root.frappe.msgprint(t("请选择 1 至 100 张订单。")); return; }
		const call = async (method, args) => (await root.frappe.call({ method: API + method, args })).message;
		const opener = root.document.activeElement;
		const panel = root.$(`<div class="crm-release-overlay"><section class="crm-release-drawer" role="dialog" aria-modal="true" aria-label="${esc(t("允许生产"))}"><header><h3>${esc(t("财务生产放行"))}</h3><button class="btn btn-default crm-release-close" aria-label="${esc(t("关闭"))}">×</button></header><div class="crm-release-body"><p>${esc(t("定金按线下约定人工判断；系统不计算应付定金或是否收足。"))}</p><p class="text-muted">${esc(t("下方只显示系统中可见的收款凭证分配记录；未录入或不可见不代表未收款。"))}</p><p class="crm-release-count" role="status">${esc(t("正在核验订单…"))}</p><div class="crm-release-table"></div><p class="crm-release-error text-danger" role="alert"></p></div><footer><button class="btn btn-default crm-release-retry" hidden>${esc(t("仅重新核验失败订单"))}</button><button class="btn btn-default crm-release-close">${esc(t("取消"))}</button><button class="btn btn-primary crm-release-confirm" disabled>${esc(t("允许生产"))}</button></footer></section></div>`).appendTo(root.document.body);
		let review = [], results = [], busy = false, timer = null, generation = 0, scope = unique;
		const close = (force = false) => { if (busy && !force) return; generation++; if (timer) clearTimeout(timer); panel.remove(); root.document.removeEventListener("keydown", keys); if (active === instance) active = null; opener?.focus(); };
		const keys = event => {
			if (event.key === "Escape") close();
			if (event.key !== "Tab") return;
			const elements = panel.find("button,a[href]").filter(":visible:not(:disabled)").toArray();
			const first = elements[0], last = elements[elements.length - 1];
			if (event.shiftKey && root.document.activeElement === first) { event.preventDefault(); last?.focus(); }
			else if (!event.shiftKey && root.document.activeElement === last) { event.preventDefault(); first?.focus(); }
		};
		const instance = { close }; active = instance; root.document.addEventListener("keydown", keys); panel.find(".crm-release-close").on("click", () => close());
		function paint() {
			panel.find(".crm-release-table").html(`<table class="table"><thead><tr>${["订单 / 客户", "ERP 订单金额", "关联收款（分配额 / 账户币种）", "核验与同步结果"].map(label => `<th>${esc(t(label))}</th>`).join("")}</tr></thead><tbody>${rowsHTML(review)}</tbody></table>`);
			const eligible = eligibleNames(review);
			panel.find(".crm-release-count").text(t("{0}: {1} · {2}: {3}", [t("可确认"), eligible.length, t("暂不可处理"), review.length - eligible.length]));
			panel.find(".crm-release-confirm").text(buttonLabel(eligible.length)).prop("disabled", busy || !eligible.length || results.length > 0);
			panel.find(".crm-release-retry").prop("hidden", !retryNames(results).length);
			panel.find(".crm-release-reject").prop("disabled", busy || results.length > 0);
		}
		async function load(selected) {
			const current = ++generation;
			try { const response = await call("get_finance_release_review", { sales_orders: selected }); if (current !== generation || active !== instance) return false; review = response.orders; paint(); return true; }
			catch (error) { if (current === generation && active === instance) panel.find(".crm-release-error").text(t("无法读取核验结果，请刷新后重试。")); }
		}
		async function poll(count = 0) {
			if (!await load(scope)) return;
			reconcileResults(results, review);
			paint();
			if (active === instance && review.some(row => row.process_status === "Deposit Confirmation Processing") && count < 12) timer = setTimeout(() => poll(count + 1), 5000);
		}
		panel.find(".crm-release-confirm").on("click", async () => {
			if (busy || results.length) return;
			const selected = eligibleNames(review); if (!selected.length) return;
			busy = true; panel.find("button").prop("disabled", true); panel.find(".crm-release-error").text("");
			try { const response = await call("confirm_production_release_batch", { sales_orders: selected }); results = response.orders; if (active !== instance) { onUpdate?.(); return; } for (const row of review) { const result = results.find(item => item.name === row.name); if (result) { row.state = result.state; row.process_status = result.process_status || row.process_status; row.can_release = false; } } onUpdate?.(); }
			catch (error) { panel.find(".crm-release-error").text(t("响应未确认，请关闭后重新核验；处理中订单不会重复提交。")); results = [{ state: "unknown" }]; }
			finally { busy = false; panel.find("button").prop("disabled", false); paint(); if (active === instance && results.some(row => row.state === "processing")) timer = setTimeout(() => poll(), 3000); }
		});
		panel.find(".crm-release-table").on("click", ".crm-release-reject", event => {
			const name = root.$(event.currentTarget).attr("data-name");
			if (busy || results.length || !review.some(row => row.name === name && row.can_reject)) return;
			root.frappe.confirm(`${esc(t("确认驳回此销售订单？"))} ${esc(name)}`, async () => {
				if (busy || results.length || !review.some(row => row.name === name && row.can_reject)) return;
				busy = true;
				panel.find("button").prop("disabled", true);
				panel.find(".crm-release-error").text("");
				let succeeded = false;
				try {
					const response = await root.frappe.call({ method: REJECT_API, args: { sales_order_name: name }, freeze: true, freeze_message: t("正在驳回销售订单...") });
					succeeded = response.message?.status === "success";
					if (!succeeded) throw new Error("Reject was not confirmed");
					const row = review.find(item => item.name === name);
					if (row) { row.process_status = "Rejected"; row.can_release = false; row.can_reject = false; }
				} catch (_) {
					panel.find(".crm-release-error").text(t("驳回未完成，请核对 ERP 订单和 CRM 状态后重试。"));
				} finally {
					await load(scope);
					busy = false;
					paint();
					if (succeeded) onUpdate?.();
				}
			});
		});
		panel.find(".crm-release-retry").on("click", async () => { const failed = retryNames(results); if (!failed.length || busy) return; if (timer) clearTimeout(timer); scope = failed; results = []; await load(scope); });
		panel.find(".crm-release-close").first().trigger("focus"); await load(unique);
	}
	if (root.frappe?.router) root.frappe.router.on("change", () => active?.close(true));
	return { open, eligibleNames, retryNames, failedReview, reconcileResults, stateLabel, rowsHTML, buttonLabel };
});
