"""Provision native quantity precision before opening stock on a dedicated ERP site."""

SETTINGS = 'System Settings'
LOAD_DEFAULTS = 'frappe.core.doctype.system_settings.system_settings.load'
QUANTITY_PRECISION = '8'


def provision_stock_precision(admin):
    saved = admin.get(SETTINGS, SETTINGS).get('float_precision')
    effective = admin.call(LOAD_DEFAULTS)['defaults'].get('float_precision')
    if str(saved) == str(effective) == QUANTITY_PRECISION:
        return
    if str(saved) == QUANTITY_PRECISION:
        # Frappe synchronizes only changed fields on save. A direct singleton
        # write can leave the stored setting ahead of the runtime default.
        admin.update(SETTINGS, SETTINGS, {'float_precision': '7'})
    admin.update(SETTINGS, SETTINGS, {'float_precision': QUANTITY_PRECISION})
    if str(admin.call(LOAD_DEFAULTS)['defaults'].get('float_precision')) != QUANTITY_PRECISION:
        raise RuntimeError('ERPNext did not apply the stock quantity precision')
