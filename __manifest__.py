# -*- coding: utf-8 -*-
{
    'name': 'Ecuador - Declaración IVA Formulario 104 (Community)',
    'version': '17.0.1.0.0',
    'category': 'Accounting/Localizations/Reporting',
    'summary': 'Formulario 104 SRI Ecuador — compatible con Odoo 17 Community',
    'description': """
        Reporte del Formulario 104 (Declaración de IVA) para el SRI de Ecuador.
        Compatible con Odoo 17 Community Edition (NO requiere account_reports Enterprise).

        Estrategia Community:
        - Consulta directa a account.move.line filtrando por account.account.tag (tax tags)
        - Wizard TransientModel para selección de período y exportación
        - PDF via QWeb (ir.actions.report) — disponible en Community
        - XLSX via xlsxwriter
        - XML para DIMM del SRI

        Dependencias:
        - account (Community)
        - l10n_ec (localización oficial — Community desde v16)
    """,
    'author': 'Tu Empresa',
    'license': 'LGPL-3',
    'depends': [
        'account',    # Community
        'l10n_ec',    # Localización oficial Ecuador (Community desde v16)
        'l10n_ec_ats',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/account_tax_tag_data.xml',
        'wizard/ec_iva_wizard_views.xml',
        'report/ec_iva_report_pdf.xml',
        'views/ec_iva_report_run_views.xml',
        'views/ec_iva_report_views.xml',
    ],
    'external_dependencies': {
        'python': ['xlsxwriter'],
    },
    'application': True,
    'auto_install': False,
    'installable': True,
}
