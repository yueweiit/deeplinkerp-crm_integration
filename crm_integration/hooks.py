app_name = "crm_integration"
app_title = "Crm Integration"
app_publisher = "yuewei"
app_description = "connect to crm"
app_email = "308642281@qq.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "crm_integration",
# 		"logo": "/assets/crm_integration/logo.png",
# 		"title": "Crm Integration",
# 		"route": "/crm_integration",
# 		"has_permission": "crm_integration.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
app_include_css = ["/assets/crm_integration/css/finance_release.css?v=0.0.2"]
app_include_js = ["/assets/crm_integration/js/finance_release.js?v=0.0.4"]

# include js in doctype views
doctype_js = {
	"Sales Order": "public/js/sales_order.js",
	"Payment Entry": "public/js/payment_entry.js",
	"Sales Invoice": "public/js/sales_invoice.js",
	"Work Order": "public/js/work_order.js",
	"Production Plan": "public/js/production_plan.js",
	"Delivery Note": "public/js/delivery_note.js",
}
doctype_list_js = {
	"Sales Order": "public/js/sales_order_list.js",
	"Payment Entry": "public/js/payment_entry_list.js",
}
doctype_css = {"Sales Order": "public/css/sales_order.css"}

# include js, css files in header of web template
# web_include_css = "/assets/crm_integration/css/crm_integration.css"
# web_include_js = "/assets/crm_integration/js/crm_integration.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "crm_integration/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "crm_integration/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "crm_integration.utils.jinja_methods",
# 	"filters": "crm_integration.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "crm_integration.install.before_install"
# after_install = "crm_integration.install.after_install"
after_install = "crm_integration.setup.after_install"
after_migrate = "crm_integration.setup.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "crm_integration.uninstall.before_uninstall"
# after_uninstall = "crm_integration.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "crm_integration.utils.before_app_install"
# after_app_install = "crm_integration.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "crm_integration.utils.before_app_uninstall"
# after_app_uninstall = "crm_integration.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "crm_integration.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "crm_integration.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Work Order": {"validate": "crm_integration.crm_integration.finance_release.validate_production_sources"},
	"Production Plan": {"validate": "crm_integration.crm_integration.finance_release.validate_production_sources"},
	"Sales Order": {
		"before_insert": "crm_integration.crm_integration.sales_order.prevent_duplicate_crm_order_no",
		"validate": "crm_integration.crm_integration.finance_release.protect_process_status",
		"before_update_after_submit": [
			"crm_integration.crm_integration.finance_release.protect_process_status",
			"crm_integration.crm_integration.sales_order.protect_crm_order_identity",
		],
		"after_insert": "crm_integration.crm_integration.integration_log.log_inbound_sales_order",
		"before_submit": "crm_integration.crm_integration.sales_order.prevent_rejected_sales_order_submit",
		"before_cancel": "crm_integration.crm_integration.sales_order.prevent_deposit_confirmation_processing_cancel",
		"on_submit": "crm_integration.crm_integration.sales_order.set_pending_deposit_confirmation_on_submit",
		"on_cancel": "crm_integration.crm_integration.sales_order.set_cancelled_process_status_on_cancel",
	},
	"Delivery Note": {
		"before_insert": "crm_integration.crm_integration.delivery_note.set_pending_final_payment_before_insert",
		"validate": "crm_integration.crm_integration.delivery_note.validate_sales_order_process_status",
		"before_submit": "crm_integration.crm_integration.delivery_note.validate_sales_order_deliverable_before_submit",
		"on_submit": "crm_integration.crm_integration.delivery_note.mark_sales_orders_completed_on_submit",
		"on_cancel": "crm_integration.crm_integration.delivery_note.update_sales_orders_delivery_process_status_on_cancel",
		"on_trash": "crm_integration.crm_integration.delivery_note.rollback_pending_final_payment_on_trash",
	},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"crm_integration.tasks.all"
# 	],
# 	"daily": [
# 		"crm_integration.tasks.daily"
# 	],
# 	"hourly": [
# 		"crm_integration.tasks.hourly"
# 	],
# 	"weekly": [
# 		"crm_integration.tasks.weekly"
# 	],
# 	"monthly": [
# 		"crm_integration.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "crm_integration.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "crm_integration.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
override_whitelisted_methods = {
	"erpnext.manufacturing.doctype.work_order.work_order.query_sales_order": "crm_integration.crm_integration.work_order.query_sales_order",
	"erpnext.manufacturing.doctype.production_plan.production_plan.sales_order_query": "crm_integration.crm_integration.production_plan.sales_order_query",
	"erpnext.selling.doctype.sales_order.sales_order.make_production_plan": "crm_integration.crm_integration.production_plan.make_production_plan",
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "crm_integration.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["crm_integration.utils.before_request"]
# after_request = ["crm_integration.utils.after_request"]

# Job Events
# ----------
# before_job = ["crm_integration.utils.before_job"]
# after_job = ["crm_integration.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"crm_integration.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
