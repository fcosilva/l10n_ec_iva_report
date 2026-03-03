# -*- coding: utf-8 -*-
"""
models/ec_iva_engine.py
=======================
Motor de cálculo del Formulario 104 para Odoo 17 COMMUNITY.

DIFERENCIA CLAVE vs Enterprise:
────────────────────────────────
Enterprise usa account_reports (account.report + account.report.line)
Community NO tiene ese módulo. Aquí hacemos la consulta directamente:

    account.move.line → filtrar por tag_ids que contengan +EC_XXX / -EC_XXX

FLUJO:
  1. Para cada casillero (411, 412, ...) obtener el ID del tag +EC_XXX
  2. Buscar account.move.line con ese tag en el período
  3. Sumar balance de las líneas con tag positivo
  4. Restar balance de las líneas con tag negativo (notas de crédito)
  5. Calcular casilleros derivados (601, 604, 605) por fórmula

NOTA SOBRE SIGNOS:
  En Odoo, las líneas de ventas tienen balance NEGATIVO (crédito).
  El motor invierte el signo para que los valores del formulario sean positivos.
  El campo `tax_tag_invert` en account.move.line indica si el signo debe invertirse.
"""
from odoo import models, api
import logging
import re

_logger = logging.getLogger(__name__)


class EcIvaEngine(models.AbstractModel):
    _name = 'ec.iva.engine'
    _description = 'Motor de cálculo IVA 104 - Community'

    @api.model
    def calcular_104(self, date_from, date_to, company_id, credito_mes_anterior=0.0):
        """
        Calcula todos los casilleros del Formulario 104.

        Args:
            date_from (str): 'YYYY-MM-DD'
            date_to (str): 'YYYY-MM-DD'
            company_id (int): ID de la empresa
            credito_mes_anterior (float): valor del casillero 605 del período anterior

        Returns:
            dict: {
                'casilleros': {'411': float, '412': float, ...},
                'secciones': {...},  # agrupado por sección para PDF/vista
                'meta': {...},
            }
        """
        company = self.env['res.company'].browse(company_id)

        # 1. Estructura oficial del formulario 104 desde la localización instalada
        report_lines = self._load_report_104_lines()

        # 2. Cargar tags oficiales del Reporte 104 (+/- XXX (Reporte 104))
        tag_map = self._load_tag_map_104()

        # 3. Calcular cada casillero con tags oficiales
        casilleros = {}
        cuentas_rel = {}
        codes = sorted(set(list(tag_map.keys()) + [r['code'] for r in report_lines]))
        for codigo in codes:
            ids_pos, ids_neg = tag_map.get(codigo, ([], []))
            valor = self._sum_tag(ids_pos, ids_neg, date_from, date_to, company_id)
            casilleros[codigo] = valor
            cuentas_rel[codigo] = self._get_related_accounts(ids_pos + ids_neg, date_from, date_to, company_id)

        # 4. Campo manual trasladado desde período anterior (según formulario oficial)
        if credito_mes_anterior:
            casilleros['605'] = credito_mes_anterior

        # 5. Armar resultado con metadatos
        return {
            'casilleros': casilleros,
            'secciones': self._agrupar_secciones(casilleros, report_lines, cuentas_rel),
            'meta': {
                'ruc': company.vat or '',
                'razon_social': company.name,
                'date_from': date_from,
                'date_to': date_to,
                'period_label': self._period_label(date_from, date_to),
                'company_id': company.id,
            },
        }

    def _merge_tax_line_fallback(self, casilleros, date_from, date_to, company_id):
        """
        Fallback inspirado en ATS: calcula casilleros base usando tax_ids en
        líneas de producto/impuesto para completar casilleros sin dato.
        """
        fallback = self._compute_from_move_lines(date_from, date_to, company_id)
        for code, value in fallback.items():
            if abs(casilleros.get(code, 0.0)) <= 1e-9 and abs(value) > 1e-9:
                casilleros[code] = value

        if any(abs(v) > 1e-9 for v in fallback.values()):
            _logger.info(
                "IVA 104 fallback por líneas aplicado para company=%s, %s..%s",
                company_id, date_from, date_to,
            )

    def _compute_from_move_lines(self, date_from, date_to, company_id):
        """
        Cálculo base (aproximado) por líneas, similar al enfoque ATS.
        """
        values = {
            '411': 0.0,  # Ventas gravadas 15% bienes
            '412': 0.0,  # IVA ventas bienes
            '415': 0.0,  # Ventas tarifa 0%
            '407': 0.0,  # Ventas no objeto
            '500': 0.0,  # Compras gravadas 15%
            '501': 0.0,  # IVA compras crédito
            '508': 0.0,  # Compras tarifa 0%
            '506': 0.0,  # Compras no objeto
        }

        moves = self.env['account.move'].search([
            ('state', '=', 'posted'),
            ('company_id', '=', company_id),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('move_type', 'in', ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')),
        ])

        for move in moves:
            sign = 1 if move.move_type in ('out_invoice', 'in_invoice') else -1
            is_sale = move.move_type in ('out_invoice', 'out_refund')
            is_purchase = move.move_type in ('in_invoice', 'in_refund')

            for line in move.line_ids:
                if line.display_type != 'product':
                    continue
                taxes = line.tax_ids
                if not taxes:
                    continue

                has_iva_pos = any(self._is_vat_tax(t) and (t.amount > 0) for t in taxes)
                has_iva_zero = any(self._is_vat_tax(t) and (t.amount == 0) for t in taxes)
                has_iva = any(self._is_vat_tax(t) for t in taxes)
                base = abs(line.price_subtotal) * sign

                if is_sale:
                    if has_iva_pos:
                        values['411'] += base
                    elif has_iva_zero:
                        values['415'] += base
                    elif not has_iva:
                        values['407'] += base
                elif is_purchase:
                    if has_iva_pos:
                        values['500'] += base
                    elif has_iva_zero:
                        values['508'] += base
                    elif not has_iva:
                        values['506'] += base

            for line in move.line_ids:
                if line.display_type != 'tax':
                    continue
                taxes = line.tax_ids
                if not taxes and line.tax_line_id:
                    taxes = line.tax_line_id
                if not taxes:
                    continue
                for tax in taxes:
                    if (not self._is_vat_tax(tax)) or tax.amount <= 0:
                        continue
                    amount = abs(line.balance) * sign
                    if is_sale:
                        values['412'] += amount
                    elif is_purchase:
                        values['501'] += amount

        return values

    @staticmethod
    def _is_vat_tax(tax):
        """Detecta impuestos IVA/VAT independientemente de idioma."""
        tax_name = str(tax.name or '').lower()
        group_name = str(tax.tax_group_id.name or '').lower()
        markers = ('iva', 'vat', 'igv')
        return any(m in tax_name for m in markers) or any(m in group_name for m in markers)

    # ─────────────────────────────────────────────────────────────
    # CONSULTA SQL — corazón del motor Community
    # ─────────────────────────────────────────────────────────────

    def _load_report_104_lines(self):
        """
        Lee la estructura oficial (secciones/códigos/etiquetas) desde account.report.line.
        """
        report = self.env['account.report'].search([
            ('country_id', '=', self.env.ref('base.ec').id),
            ('name', 'ilike', '104'),
        ], limit=1)
        if not report:
            return []

        lines = self.env['account.report.line'].search(
            [('report_id', '=', report.id)],
            order='sequence, id',
        )

        def root_section(line):
            cur = line
            while cur.parent_id:
                cur = cur.parent_id
            return cur

        result = []
        for line in lines:
            code = (line.code or '').strip()
            label = self._trans_text(line.name) or code

            # Odoo l10n_ec usa códigos tipo c401, c500, c425_104, etc.
            match = re.match(r'^c(\d{3,4})(?:_104)?$', code, flags=re.IGNORECASE)
            if not match:
                # Algunas líneas no tienen code, pero sí "(411)" en la etiqueta.
                match = re.search(r'\((\d{3,4})\)', label or '')
            if not match:
                continue
            cas_code = match.group(1)
            section = root_section(line)
            result.append({
                'code': cas_code,
                'label': label or cas_code,
                'section': self._trans_text(section.name) or 'Formulario 104',
                'section_seq': section.sequence or 0,
                'line_seq': line.sequence or 0,
            })
        return result

    def _load_tag_map_104(self):
        """
        Carga tags oficiales del Reporte 104 en dict:
        {'401': ([id_pos...], [id_neg...]), ...}
        """
        tags = self.env['account.account.tag'].search([
            ('applicability', '=', 'taxes'),
            ('country_id', '=', self.env.ref('base.ec').id),
        ])

        tag_map = {}
        for tag in tags:
            name = self._trans_text(tag.name)
            if 'Reporte 104' not in name:
                continue
            match = re.match(r'^([+-])\s*(\d{3,4})\b', name)
            if not match:
                continue

            sign, code = match.groups()
            entry = tag_map.setdefault(code, ([], []))
            if sign == '+':
                entry[0].append(tag.id)
            else:
                entry[1].append(tag.id)

        return tag_map

    def _sum_tag(self, tag_ids_pos, tag_ids_neg, date_from, date_to, company_id):
        """
        Suma el balance de las líneas de movimiento que tienen los tags dados.

        Lógica de signos en Odoo Community:
        - account.move.line.balance es positivo para débitos, negativo para créditos.
        - Las líneas de VENTAS (crédito) tienen balance negativo → invertir signo.
        - Las líneas de COMPRAS (débito del impuesto) tienen balance positivo.
        - El campo tax_tag_invert indica si la línea usa el tag en modo invertido
          (típicamente para notas de crédito).

        Fórmula:
            valor = Σ(balance con tag+ y NO invert) - Σ(balance con tag+ y SÍ invert)
                  - Σ(balance con tag- y NO invert) + Σ(balance con tag- y SÍ invert)

        Simplificado con el signo nativo de Odoo:
            valor = Σ(balance * (-1 si invert) para tag+) +
                    Σ(balance * (+1 si invert) para tag-)
        """
        if not tag_ids_pos and not tag_ids_neg:
            return 0.0

        total = 0.0

        # Tags positivos
        if tag_ids_pos:
            total += self._query_tag_sum(tag_ids_pos, date_from, date_to, company_id, sign_invert=False)

        # Tags negativos (notas de crédito, reversiones)
        if tag_ids_neg:
            total -= self._query_tag_sum(tag_ids_neg, date_from, date_to, company_id, sign_invert=False)

        return total

    def _query_tag_sum(self, tag_ids, date_from, date_to, company_id, sign_invert):
        """
        Ejecuta la consulta SQL para sumar balances de líneas con los tags dados.

        Usa SQL directo para máximo rendimiento (evita ORM overhead en períodos con
        miles de líneas).
        """
        if not tag_ids:
            return 0.0

        # La tabla de relación entre move.line y account.tag es:
        # account_account_tag_account_move_line_rel (en Odoo 17)
        # Columnas: account_move_line_id, account_account_tag_id
        query = """
            SELECT
                COALESCE(
                    SUM(
                        CASE
                            WHEN aml.tax_tag_invert = TRUE THEN -aml.balance
                            ELSE aml.balance
                        END
                    ), 0.0
                ) AS total
            FROM account_move_line aml
            JOIN account_move am ON am.id = aml.move_id
            JOIN account_account_tag_account_move_line_rel rel
                ON rel.account_move_line_id = aml.id
            WHERE
                rel.account_account_tag_id = ANY(%(tag_ids)s)
                AND am.state = 'posted'
                AND am.company_id = %(company_id)s
                AND am.date >= %(date_from)s
                AND am.date <= %(date_to)s
        """
        self.env.cr.execute(query, {
            'tag_ids': tag_ids,
            'company_id': company_id,
            'date_from': date_from,
            'date_to': date_to,
        })
        result = self.env.cr.fetchone()
        raw = result[0] if result else 0.0

        # Las ventas generan crédito (balance negativo en Odoo).
        # Para que el formulario muestre valores positivos, invertimos el signo.
        # Odoo aplica esta inversión automáticamente en Enterprise via tax_tag_invert.
        # En Community lo hacemos aquí: si el resultado es negativo, retornamos el absoluto.
        return abs(raw) if raw else 0.0

    # ─────────────────────────────────────────────────────────────
    # HELPERS
    # ─────────────────────────────────────────────────────────────

    def _agrupar_secciones(self, casilleros, report_lines, cuentas_rel):
        """Agrupa por secciones oficiales del Reporte 104 y devuelve estructura dinámica."""
        sections = {}
        section_order = {}
        detail_rows = []
        for row in report_lines:
            section = row['section']
            lines = sections.setdefault(section, [])
            related = cuentas_rel.get(row['code'], {})
            line = {
                'code': row['code'],
                'label': row['label'],
                'value': casilleros.get(row['code'], 0.0),
                'accounts_summary': related.get('summary', ''),
                'accounts': related.get('accounts', []),
            }
            lines.append(line)
            detail_rows.append(line)
            if section not in section_order:
                section_order[section] = row['section_seq']

        dynamic = []
        for section in sorted(sections.keys(), key=lambda s: section_order.get(s, 9999)):
            dynamic.append({'title': section, 'lines': sections[section]})

        return {
            'dynamic': dynamic,
            'details': detail_rows,
            'ventas': [],       # compatibilidad hacia atrás (wizard viejo)
            'compras': [],
            'liquidacion': [],
            'retenciones': [],
        }

    @staticmethod
    def _period_label(date_from, date_to):
        MESES = {
            '01': 'Enero', '02': 'Febrero', '03': 'Marzo', '04': 'Abril',
            '05': 'Mayo', '06': 'Junio', '07': 'Julio', '08': 'Agosto',
            '09': 'Septiembre', '10': 'Octubre', '11': 'Noviembre', '12': 'Diciembre',
        }
        m_ini = MESES.get(date_from[5:7], '')
        m_fin = MESES.get(date_to[5:7], '')
        anio = date_from[:4]
        if m_ini == m_fin:
            return f'{m_ini} {anio}'
        return f'{m_ini} – {m_fin} {anio}'

    @staticmethod
    def _trans_text(value):
        """Obtiene texto desde campos traducibles (str o dict jsonb)."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for lang in ('es_EC', 'es_419', 'es_ES', 'en_US'):
                if value.get(lang):
                    return value.get(lang)
            for txt in value.values():
                if txt:
                    return txt
        return str(value or '')

    def _get_related_accounts(self, tag_ids, date_from, date_to, company_id):
        """Obtiene cuentas relacionadas a los tags del casillero para trazabilidad."""
        if not tag_ids:
            return {'summary': '', 'accounts': []}

        query = """
            SELECT
                COALESCE(aa.code, '') AS account_code,
                COALESCE(
                    aa.name->>'es_EC',
                    aa.name->>'es_419',
                    aa.name->>'es_ES',
                    aa.name->>'en_US',
                    ''
                ) AS account_name,
                COUNT(*) AS line_count,
                COALESCE(
                    SUM(
                        ABS(
                            CASE
                                WHEN aml.tax_tag_invert = TRUE THEN -aml.balance
                                ELSE aml.balance
                            END
                        )
                    ),
                    0.0
                ) AS amount_ref
            FROM account_move_line aml
            JOIN account_move am ON am.id = aml.move_id
            JOIN account_account aa ON aa.id = aml.account_id
            JOIN account_account_tag_account_move_line_rel rel
                ON rel.account_move_line_id = aml.id
            WHERE
                rel.account_account_tag_id = ANY(%(tag_ids)s)
                AND am.state = 'posted'
                AND am.company_id = %(company_id)s
                AND am.date >= %(date_from)s
                AND am.date <= %(date_to)s
            GROUP BY 1, 2
            ORDER BY amount_ref DESC, line_count DESC, account_code
        """
        self.env.cr.execute(query, {
            'tag_ids': tag_ids,
            'company_id': company_id,
            'date_from': date_from,
            'date_to': date_to,
        })

        accounts = []
        for code, name, line_count, amount_ref in self.env.cr.fetchall():
            accounts.append({
                'account_code': code or '',
                'account_name': name or '',
                'line_count': int(line_count or 0),
                'amount_ref': float(amount_ref or 0.0),
            })

        summary_parts = []
        for row in accounts[:3]:
            label = f"{row['account_code']} {row['account_name']}".strip()
            if label:
                summary_parts.append(label)
        if len(accounts) > 3:
            summary_parts.append(f"+{len(accounts) - 3} más")

        return {
            'summary': '; '.join(summary_parts),
            'accounts': accounts,
        }
