// Sales Order production flow actions
frappe.ui.form.on("Sales Order", {
	onload: function(frm) {
		load_crm_integration_enabled(frm).then(function() {
			refresh_crm_integration_ui(frm);
		});
	},

	refresh: function(frm) {
		load_crm_integration_enabled(frm).then(function() {
			refresh_crm_integration_ui(frm);
		});
	},

	on_tab_change: function(frm) {
	},

	company: function(frm) {
		frm._crm_integration_company = null;
		frm._crm_integration_enabled = false;
		load_crm_integration_enabled(frm).then(function() {
			refresh_crm_integration_ui(frm);
		});
	},

	dashboard_update: function(frm) {
		refresh_payment_entry_link_count(frm);
	},

	after_submit: function(frm) {
		frm.reload_doc();
	}
});

$(document).ready(function() {
	$(".process-status-badge").remove();
});

$(document).on("page-change", function() {
	$(".process-status-badge").remove();
});


function load_crm_integration_enabled(frm) {
	if (!frm || !frm.doc || !frm.doc.company) {
		frm._crm_integration_enabled = false;
		return Promise.resolve(false);
	}

	if (frm._crm_integration_company === frm.doc.company) {
		return Promise.resolve(Boolean(frm._crm_integration_enabled));
	}

	frm._crm_integration_company = frm.doc.company;
	return frappe.db.get_value("Company", frm.doc.company, "custom_enable_crm_integration").then(function(r) {
		const value = r && r.message ? r.message.custom_enable_crm_integration : 0;
		frm._crm_integration_enabled = cint(value) === 1;
		return frm._crm_integration_enabled;
	});
}

function is_crm_integration_enabled(frm) {
	return Boolean(frm && frm._crm_integration_enabled);
}

function refresh_crm_integration_ui(frm) {
	cleanup_reject_cancel_button(frm);
	if (!is_crm_integration_enabled(frm)) {
		$(".process-status-badge").remove();
		$(".crm-payment-entry-link-wrapper").remove();
		return;
	}

	hide_submit_for_rejected_sales_order(frm);
	install_delivery_note_creation_guard(frm);
	install_sales_invoice_source_tracker(frm);
	add_production_flow_buttons(frm);
	display_process_status(frm);
	render_payment_entry_link_in_details(frm);
	restrict_fulfillment_creation(frm);
}

function hide_submit_for_rejected_sales_order(frm) {
	if (!is_crm_integration_enabled(frm)) {
		return;
	}

	if (!frm || !frm.doc || frm.doc.custom_process_status !== "Rejected") {
		return;
	}

	const clear_submit = function() {
		if (frm.page && frm.page.clear_primary_action) {
			frm.page.clear_primary_action();
		}
		$(".page-actions .primary-action[data-label='" + encodeURIComponent(__("Submit")) + "']").addClass("hide");
		$(".page-actions .primary-action[data-label='" + encodeURIComponent("提交") + "']").addClass("hide");
	};

	clear_submit();
	requestAnimationFrame(clear_submit);
	setTimeout(clear_submit, 300);
}

function install_sales_invoice_source_tracker(frm) {
	if (!frm || !frm.cscript || frm._crm_sales_invoice_source_tracker_installed) {
		return;
	}

	const original_make_sales_invoice = frm.cscript.make_sales_invoice;
	if (typeof original_make_sales_invoice !== "function") {
		return;
	}

	frm._crm_sales_invoice_source_tracker_installed = true;
	frm.cscript.make_sales_invoice = function() {
		remember_sales_invoice_source_sales_order(frm);
		return original_make_sales_invoice.apply(this, arguments);
	};
}

function remember_sales_invoice_source_sales_order(frm, sales_invoice_name) {
	if (!window.sessionStorage || !frm || !frm.doc || !frm.doc.name) {
		return;
	}

	sessionStorage.setItem(
		"crm_integration_sales_invoice_source",
		JSON.stringify({
			sales_invoice: sales_invoice_name || null,
			sales_order: frm.doc.name
		})
	);
}


function install_delivery_note_creation_guard(frm) {
	if (!frm || !frm.cscript || frm._crm_delivery_note_guard_installed) {
		return;
	}

	const original_make_delivery_note_based_on_delivery_date =
		frm.cscript.make_delivery_note_based_on_delivery_date;
	const original_make_delivery_note = frm.cscript.make_delivery_note;

	if (typeof original_make_delivery_note_based_on_delivery_date === "function") {
		frm.cscript.make_delivery_note_based_on_delivery_date = function() {
			if (is_crm_integration_enabled(frm) && !can_create_delivery_note(frm)) {
				show_delivery_note_block_message();
				return;
			}

			return original_make_delivery_note_based_on_delivery_date.apply(this, arguments);
		};
	}

	if (typeof original_make_delivery_note === "function") {
		frm.cscript.make_delivery_note = function() {
			if (is_crm_integration_enabled(frm) && !can_create_delivery_note(frm)) {
				show_delivery_note_block_message();
				return;
			}

			return original_make_delivery_note.apply(this, arguments);
		};
	}

	frm._crm_delivery_note_guard_installed = true;
}

function restrict_fulfillment_creation(frm) {
	if (!is_crm_integration_enabled(frm)) {
		return;
	}

	if (!frm || !frm.doc) {
		return;
	}

	const remove_buttons = function() {
		if (!can_create_delivery_note(frm)) {
			frm.remove_custom_button(__("Delivery Note"), __("Create"));
		}
	};

	remove_buttons();
	requestAnimationFrame(remove_buttons);
	setTimeout(remove_buttons, 300);
}


function can_create_delivery_note(frm) {
	return ["Deliverable", "Partially Delivered"].includes(frm.doc.custom_process_status);
}

function show_delivery_note_block_message() {
	frappe.msgprint({
		title: __("无法创建销售出库"),
		indicator: "orange",
		message: __("只有流程状态为 Deliverable 或 Partially Delivered 的销售订单才可以创建销售出库。")
	});
}

function add_production_flow_buttons(frm) {
	if (!is_crm_integration_enabled(frm)) {
		return;
	}

	cleanup_reject_cancel_button(frm);

	if (!frm || !frm.doc || frm.doc.__islocal) {
		return;
	}

	if (frm.doc.status === "Closed") {
		return;
	}

	const process_status = frm.doc.custom_process_status;

	if (can_replace_cancel_with_reject(frm)) {
		replace_cancel_button_with_reject(frm);
		frm.add_custom_button(__("允许生产（1单）"), function() {
			confirm_deposit_and_push_to_mes(frm);
		});
		apply_primary_action_style("允许生产（1单）");
	}

	if (frm.doc.docstatus === 1 && process_status === "Pending Final Payment") {
		frm.add_custom_button(__("核销尾款"), function() {
			confirm_reconcile_final_payment(frm);
		});
		apply_primary_action_style("核销尾款");
	}
}

function apply_primary_action_style(label) {
	requestAnimationFrame(function() {
		const labels = [...new Set([label, __(label)])];
		const selector = labels
			.map((button_label) => `.page-actions button[data-label="${encodeURIComponent(button_label)}"]`)
			.join(", ");
		const $button = $(selector);

		$button
			.removeClass("btn-default btn-secondary btn-xs")
			.addClass("btn-primary btn-sm primary-action");
	});
}

function native_cancel_buttons() {
	const labels = new Set(["Cancel", __("Cancel"), "取消", __("取消")].map(encodeURIComponent));
	return $(".page-actions button").not(".crm-reject-cancel-button").filter(function() {
		return labels.has($(this).attr("data-label"));
	});
}

function cleanup_reject_cancel_button(frm) {
	$(".page-actions .crm-processing-cancel-hidden")
		.removeClass("crm-processing-cancel-hidden")
		.removeClass("hidden hide");

	if (can_replace_cancel_with_reject(frm)) {
		return;
	}

	$(".page-actions .crm-reject-cancel-button").remove();
	$(".page-actions .crm-native-cancel-hidden")
		.removeClass("crm-native-cancel-hidden")
		.removeClass("hidden hide");

	if (frm && frm.doc && frm.doc.custom_process_status === "Deposit Confirmation Processing") {
		const hide_cancel = function() {
			native_cancel_buttons().addClass("crm-processing-cancel-hidden hidden");
		};

		hide_cancel();
		requestAnimationFrame(hide_cancel);
		setTimeout(hide_cancel, 300);
	}
}

function can_replace_cancel_with_reject(frm) {
	return Boolean(
		frm &&
		frm.doc &&
		frm.doc.docstatus === 1 &&
		frm.doc.custom_process_status === "Pending Deposit Confirmation"
	);
}

function replace_cancel_button_with_reject(frm) {
	const replace_button = function() {
		if (!can_replace_cancel_with_reject(frm)) {
			return;
		}

		const $cancel_button = native_cancel_buttons().first();

		if (!$cancel_button.length) {
			return;
		}

		if ($(".page-actions .crm-reject-cancel-button").length) {
			$cancel_button.addClass("crm-native-cancel-hidden hidden");
			return;
		}

		const $reject_button = $cancel_button.clone(false, false);
		$reject_button
			.removeClass("crm-native-cancel-hidden hidden hide btn-default btn-secondary")
			.addClass("crm-reject-cancel-button btn-primary primary-action")
			.attr("data-label", encodeURIComponent(__("财务驳回")))
			.text(__("财务驳回"))
			.off("click.crmReject")
			.on("click.crmReject", function(e) {
				e.preventDefault();
				e.stopImmediatePropagation();
				reject_sales_order(frm);
			});

		$cancel_button.addClass("crm-native-cancel-hidden hidden");
		$reject_button.insertAfter($cancel_button);
	};

	replace_button();
	requestAnimationFrame(replace_button);
	setTimeout(replace_button, 300);
}

function display_process_status(frm) {
	if (!is_crm_integration_enabled(frm)) {
		return;
	}

	const currentUrl = window.location.pathname;
	if (!currentUrl || currentUrl === "/desk/sales-order") {
		$(".process-status-badge").remove();
		return;
	}

	if (!frm || frm.doctype !== "Sales Order" || !frm.doc || !frm.doc.name) {
		return;
	}

	const $pageTitle = $(".page-title");
	if (!$pageTitle.length) {
		return;
	}

	$(".process-status-badge").remove();

	const process_status = frm.doc.custom_process_status || "Pending Confirmation";
	const color = get_process_status_color(process_status);
	const statusHtml = `<span class="process-status-badge indicator-pill no-indicator-dot whitespace-nowrap ${color}" style="margin-left: 12px;"><span>${__(process_status)}</span></span>`;

	const $pageHead = $(".page-head");
	if ($pageHead.length) {
		const $target = $pageHead.find(".page-title").length ? $pageHead.find(".page-title") : $pageHead;
		$target.append(statusHtml);
	}
}

function get_process_status_color(process_status) {
	const color_map = {
		"Pending Confirmation": "orange",
		"Rejected": "red",
		"Pending Deposit Confirmation": "yellow",
		"Deposit Confirmation Processing": "blue",
		"Pending Production": "blue",
		"Pending Final Payment": "yellow",
		"Deliverable": "blue",
		"Partially Delivered": "orange",
		"Completed": "green",
		"Closed": "green",
		"Cancelled": "red",
	};

	return color_map[process_status] || "gray";
}

function get_current_remark(frm) {
	const remark_field = frm.get_field && frm.get_field("custom_remark");
	if (remark_field && remark_field.get_value) {
		return remark_field.get_value() || "";
	}

	return frm.doc.custom_remark || "";
}

function reject_sales_order(frm) {
	frappe.confirm(
		__("确认驳回此销售订单？"),
		function() {
			frappe.call({
				method: "crm_integration.crm_integration.sales_order.reject_sales_order",
				args: {
					sales_order_name: frm.doc.name,
					remark: get_current_remark(frm)
				},
				freeze: true,
				freeze_message: __("正在驳回销售订单..."),
				callback: function(r) {
					if (r.message && r.message.status === "success") {
						frappe.show_alert({ message: r.message.message, indicator: "green" });
						frm.reload_doc();
					}
				}
			});
		}
	);
}

function confirm_reconcile_final_payment(frm) {
	const message = __("确认核销尾款？{0}/{1}", [
		format_sales_order_currency(frm, frm.doc.advance_paid),
		format_sales_order_currency(frm, frm.doc.grand_total)
	]);

	frappe.confirm(message, function() {
		reconcile_final_payment(frm);
	});
}

function reconcile_final_payment(frm) {
	frappe.call({
		method: "crm_integration.crm_integration.sales_order.reconcile_final_payment",
		args: {
			sales_order_name: frm.doc.name
		},
		freeze: true,
		freeze_message: __("正在核销尾款..."),
		callback: function(r) {
			if (!r.message || r.message.status !== "success") {
				return;
			}

			frappe.show_alert({ message: r.message.message, indicator: "green" });
			frm.reload_doc();
		}
	});
}

function confirm_deposit_and_push_to_mes(frm) {
	const open = () => window.CRMFinanceRelease.open([frm.doc.name], () => frm.reload_doc());
	if (window.CRMFinanceRelease) return open();
	return frappe.require("/assets/crm_integration/js/finance_release.js").then(open);
}

function format_sales_order_currency(frm, value) {
	return format_currency(flt(value), frm.doc.currency || frappe.defaults.get_default("currency"));
}


function render_payment_entry_link_in_details(frm) {
	if (!is_crm_integration_enabled(frm)) {
		return;
	}

	$(".crm-payment-entry-link-wrapper").remove();

	if (!frm || frm.doctype !== "Sales Order" || !frm.doc || frm.doc.__islocal) {
		return;
	}

	const $wrapper = $(`
		<div class="crm-payment-entry-link-wrapper section-body">
			<div class="form-column col-sm-12" data-fieldname="crm_payment_entry_link">
				<form>
					<div class="frappe-control input-max-width" data-fieldtype="HTML" data-fieldname="crm_payment_entry_link">
						<div class="form-group horizontal">
							<div class="clearfix">
								<label class="control-label" style="padding-right: 5px;">${__("Payment Entry")}</label>
								<span class="help"></span>
							</div>
							<div class="control-input-wrapper">
								<div class="control-input">
									<div class="form-links crm-payment-entry-form-links">
										<div class="document-link" data-doctype="Payment Entry">
											<div class="document-link-badge" data-doctype="Payment Entry">
												<a class="badge-link">${__("Payment Entry")}</a>
												<span class="count hidden" title="${__("Count of linked documents")}"></span>
											</div>
											<span class="open-notification hidden" title="${__("Open {0}", [__("Payment Entry")])}"></span>
											<button type="button" class="btn btn-new btn-secondary btn-xs icon-btn hidden" data-doctype="Payment Entry">
												<svg class="icon icon-sm"><use href="#icon-add"></use></svg>
											</button>
										</div>
									</div>
								</div>
								<div class="help-box small text-extra-muted hide"></div>
							</div>
						</div>
						<span class="tooltip-content">${__("Payment Entry")}</span>
					</div>
				</form>
			</div>
		</div>
	`);

	const $customer_section = frm.fields_dict.customer_section
		? $(frm.fields_dict.customer_section.wrapper)
		: $();

	if ($customer_section.length) {
		$customer_section.after($wrapper);
	} else {
		$(frm.wrapper).find(".form-layout").first().prepend($wrapper);
	}

	$wrapper.find(".badge-link, .open-notification").on("click", function() {
		open_payment_entry_list(frm, $(this).hasClass("open-notification"));
	});

	$wrapper.find(".btn-new").on("click", function(e) {
		e.preventDefault();
		e.stopPropagation();
		make_payment_entry_from_sales_order(frm);
	});

	schedule_payment_entry_new_button_visibility(frm, $wrapper);
	refresh_payment_entry_link_count(frm);
}

function schedule_payment_entry_new_button_visibility(frm, $wrapper) {
	update_payment_entry_new_button_visibility(frm, $wrapper);
	requestAnimationFrame(function() {
		update_payment_entry_new_button_visibility(frm, $wrapper);
	});
	setTimeout(function() {
		update_payment_entry_new_button_visibility(frm, $wrapper);
	}, 300);
}

function update_payment_entry_new_button_visibility(frm, $wrapper) {
	const can_create_payment_entry = frm.doc.docstatus === 1;
	$wrapper.find(".btn-new").toggleClass("hidden", !can_create_payment_entry);
}

function make_payment_entry_from_sales_order(frm) {
	let via_journal_entry = frm.doc.__onload && frm.doc.__onload.make_payment_via_journal_entry;
	if (has_discount_in_schedule(frm) && !via_journal_entry) {
		prompt_user_for_reference_date(frm);
	} else {
		make_mapped_payment_entry(frm);
	}
}

function make_mapped_payment_entry(frm, args) {
	args = args || { dt: frm.doc.doctype, dn: frm.doc.name };
	return frappe.call({
		method: get_method_for_payment(frm),
		args: args,
		callback: function(r) {
			var doclist = frappe.model.sync(r.message);
			remember_payment_entry_source_sales_order(frm, doclist[0].name);
			frappe.set_route("Form", doclist[0].doctype, doclist[0].name);
		}
	});
}

function remember_payment_entry_source_sales_order(frm, payment_entry_name) {
	if (!window.sessionStorage || !payment_entry_name) {
		return;
	}

	sessionStorage.setItem(
		"crm_integration_payment_entry_source",
		JSON.stringify({
			payment_entry: payment_entry_name,
			sales_order: frm.doc.name
		})
	);
}

function prompt_user_for_reference_date(frm) {
	frappe.prompt(
		{
			label: __("Cheque/Reference Date"),
			fieldname: "reference_date",
			fieldtype: "Date",
			reqd: 1
		},
		(values) => {
			let args = {
				dt: frm.doc.doctype,
				dn: frm.doc.name,
				reference_date: values.reference_date
			};
			make_mapped_payment_entry(frm, args);
		},
		__("Reference Date for Early Payment Discount"),
		__("Continue")
	);
}

function has_discount_in_schedule(frm) {
	let is_eligible = ["Sales Order", "Sales Invoice", "Purchase Order", "Purchase Invoice"].includes(
		frm.doctype
	);
	let has_payment_schedule = frm.doc.payment_schedule && frm.doc.payment_schedule.length;
	if (!is_eligible || !has_payment_schedule) return false;

	let has_discount = frm.doc.payment_schedule.some((row) => row.discount);
	return has_discount;
}

function get_method_for_payment(frm) {
	let method = "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry";
	if (frm.doc.__onload && frm.doc.__onload.make_payment_via_journal_entry) {
		method = "erpnext.accounts.doctype.journal_entry.journal_entry.get_payment_entry_against_order";
	}

	return method;
}

function refresh_payment_entry_link_count(frm) {
	const $link = $(".crm-payment-entry-link-wrapper .document-link[data-doctype='Payment Entry']");
	if (!frm || !frm.doc || frm.doc.__islocal || !$link.length) {
		return;
	}

	get_payment_entry_link_count(frm).then(function(count) {
		update_payment_entry_link_badges($link, count.open_count, count.count);
	});
}

function get_payment_entry_link_count(frm) {
	return new Promise(function(resolve) {
		if (!frm || !frm.doc || frm.doc.__islocal) {
			resolve({ open_count: 0, count: 0 });
			return;
		}

		const dashboard_count = get_payment_entry_count_from_dashboard(frm);
		if (dashboard_count) {
			resolve(dashboard_count);
			return;
		}

		const method = frm.dashboard && frm.dashboard.data && frm.dashboard.data.method
			? frm.dashboard.data.method
			: "frappe.desk.notifications.get_open_count";

		frappe.call({
			type: "GET",
			method: method,
			args: {
				doctype: frm.doctype,
				name: frm.docname,
				items: ["Payment Entry"]
			},
			callback: function(r) {
				resolve(get_payment_entry_count_from_response(r.message));
			},
			error: function() {
				resolve({ open_count: 0, count: 0 });
			}
		});
	});
}

function get_payment_entry_count_from_dashboard(frm) {
	if (!frm.dashboard_data || !frm.dashboard_data.count) {
		return null;
	}

	return get_payment_entry_count_from_response(frm.dashboard_data);
}

function get_payment_entry_count_from_response(data) {
	const empty_count = { open_count: 0, count: 0 };
	if (!data || !data.count) {
		return empty_count;
	}

	const linked_docs = []
		.concat(data.count.external_links_found || [])
		.concat(data.count.internal_links_found || []);

	return linked_docs.find((d) => d.doctype === "Payment Entry") || empty_count;
}

function update_payment_entry_link_badges($link, open_count, count) {
	const display_count = cint(count);
	const display_open_count = cint(open_count);

	$link
		.find(".count")
		.toggleClass("hidden", !display_count)
		.text(display_count > 99 ? "99+" : display_count);

	$link
		.find(".open-notification")
		.toggleClass("hidden", !display_open_count)
		.html(display_open_count > 99 ? "99+" : display_open_count);
}

function open_payment_entry_list(frm, show_open) {
	if (frm.dashboard && frm.dashboard.open_document_list) {
		frm.dashboard.open_document_list(
			$(".crm-payment-entry-link-wrapper .document-link[data-doctype='Payment Entry']"),
			show_open
		);
		return;
	}

	frappe.route_options = {
		reference_name: frm.doc.name
	};
	frappe.set_route("List", "Payment Entry", "List");
}
