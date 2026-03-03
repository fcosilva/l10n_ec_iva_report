# -*- coding: utf-8 -*-
"""
report/ec_iva_report_abstract.py
=================================
En Odoo Community, para pasar datos personalizados a un template QWeb PDF
se usa el patrón AbstractModel con _get_report_values().

CONVENCIÓN DE NOMBRES (crítica):
    El nombre del AbstractModel DEBE ser:
    'report.' + nombre_del_template_qweb

    Template: 'l10n_ec_iva_report.ec_iva_104_pdf_template'
    AbstractModel: 'report.l10n_ec_iva_report.ec_iva_104_pdf_template'

_get_report_values() recibe:
    - docids: lista de IDs del modelo del reporte (ec.iva.wizard)
    - data: dict extra que se puede pasar desde report_action()

Retorna un dict que estará disponible en el template QWeb como variables.
"""
from odoo import models


class EcIva104PdfReport(models.AbstractModel):
    _name = 'report.l10n_ec_iva_report.ec_iva_104_pdf_template'
    _description = 'AbstractModel para PDF del Formulario 104'

    def _get_report_values(self, docids, data=None):
        """
        Prepara los datos para el template QWeb.

        Se accede al wizard (ec.iva.wizard) por docids y se calculan
        los casilleros del 104 con el motor ec.iva.engine.

        El resultado se pasa al template en 'resultados_por_doc', indexado
        por ID del wizard.
        """
        runs = self.env['ec.iva.report.run'].browse(docids)

        # Para cada ejecución, usar snapshot; si no existe, recalcular.
        resultados_por_doc = {}
        for run in runs:
            if run.resultado_json:
                resultados_por_doc[run.id] = run._get_resultado_cached()
                continue
            resultados_por_doc[run.id] = self.env['ec.iva.engine'].calcular_104(
                date_from=str(run.fecha_desde),
                date_to=str(run.fecha_hasta),
                company_id=run.company_id.id,
                credito_mes_anterior=run.credito_mes_anterior,
            )

        return {
            'doc_ids': docids,
            'doc_model': 'ec.iva.report.run',
            'docs': runs,
            'resultados_por_doc': resultados_por_doc,
        }
