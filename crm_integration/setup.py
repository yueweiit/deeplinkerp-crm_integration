"""Idempotent schema synchronization for CRM integration fields."""


def sync_custom_fields():
	from crm_integration.patches.add_company_integration_switches import execute as add_company_fields
	from crm_integration.patches.add_crm_custom_fields import execute as add_crm_fields
	from crm_integration.patches.add_item_tax_amount_field import execute as add_tax_fields
	from crm_integration.patches.add_direct_fulfillment_products import execute as add_direct_products

	add_company_fields()
	add_crm_fields()
	add_tax_fields()
	add_direct_products()


def after_install():
	sync_custom_fields()


def after_migrate():
	sync_custom_fields()
