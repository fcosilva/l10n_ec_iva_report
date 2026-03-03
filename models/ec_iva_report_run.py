# -*- coding: utf-8 -*-
import base64
import io
import json
import xml.etree.ElementTree as ET
from calendar import monthrange
from datetime import date, datetime
from html import escape
from xml.dom import minidom

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EcIvaReportRun(models.Model):
    _name = 'ec.iva.report.run'
    _description = 'Declaración IVA 104 - Ejecución'
    _order = 'id desc'

    name = fields.Char(string='Descripción', required=True, default='Nueva ejecución IVA 104')
    state = fields.Selection(
        [('draft', 'Borrador'), ('generated', 'Generado')],
        string='Estado',
        default='draft',
        required=True,
    )
    generated_at = fields.Datetime(string='Generado en', readonly=True)

    tipo_periodo = fields.Selection([
        ('mensual', 'Mensual'),
        ('semestral_1', 'Semestral - 1er Semestre (Ene-Jun)'),
        ('semestral_2', 'Semestral - 2do Semestre (Jul-Dic)'),
    ], string='Tipo de período', default='mensual', required=True)

    anio = fields.Selection(
        selection='_year_selection',
        string='Año',
        default=lambda self: str(fields.Date.today().year),
        required=True,
    )
    mes = fields.Selection([
        ('01', 'Enero'), ('02', 'Febrero'), ('03', 'Marzo'),
        ('04', 'Abril'), ('05', 'Mayo'), ('06', 'Junio'),
        ('07', 'Julio'), ('08', 'Agosto'), ('09', 'Septiembre'),
        ('10', 'Octubre'), ('11', 'Noviembre'), ('12', 'Diciembre'),
    ], string='Mes', default=lambda self: f'{fields.Date.today().month:02d}')

    company_id = fields.Many2one(
        'res.company',
        string='Empresa',
        default=lambda self: self.env.company,
        required=True,
    )

    credito_mes_anterior = fields.Float(
        string='Crédito tributario mes anterior (casillero 605)',
        digits=(16, 2),
        default=0.0,
        help='Valor a trasladar al casillero 605 desde la declaración del período anterior.',
    )

    fecha_desde = fields.Date(compute='_compute_fechas', store=True, string='Desde')
    fecha_hasta = fields.Date(compute='_compute_fechas', store=True, string='Hasta')

    estado = fields.Char(string='Mensaje', readonly=True)
    vista_previa_html = fields.Html(readonly=True)
    resultado_json = fields.Text(readonly=True)

    archivo_nombre = fields.Char(readonly=True)
    archivo_datos = fields.Binary(readonly=True, attachment=True)

    @api.depends('tipo_periodo', 'anio', 'mes')
    def _compute_fechas(self):
        for rec in self:
            anio = self._safe_int(rec.anio, fields.Date.today().year)
            if rec.tipo_periodo == 'mensual':
                mes = self._safe_int(rec.mes, 1)
                ultimo = monthrange(anio, mes)[1]
                rec.fecha_desde = f'{anio}-{mes:02d}-01'
                rec.fecha_hasta = f'{anio}-{mes:02d}-{ultimo:02d}'
            elif rec.tipo_periodo == 'semestral_1':
                rec.fecha_desde = f'{anio}-01-01'
                rec.fecha_hasta = f'{anio}-06-30'
            elif rec.tipo_periodo == 'semestral_2':
                rec.fecha_desde = f'{anio}-07-01'
                rec.fecha_hasta = f'{anio}-12-31'
            else:
                rec.fecha_desde = False
                rec.fecha_hasta = False

    @api.constrains('anio')
    def _check_anio(self):
        for rec in self:
            anio = int(rec.anio or 0)
            if not (2000 <= anio <= 2099):
                raise UserError(_('El año debe estar entre 2000 y 2099.'))

    def action_generar(self):
        for rec in self:
            resultado = rec._compute_resultado()
            period_label = resultado.get('meta', {}).get('period_label', '')
            rec.write({
                'name': f"F104 - {period_label} - {rec.company_id.name}",
                'state': 'generated',
                'generated_at': fields.Datetime.now(),
                'estado': '✓ Vista generada correctamente.',
                'vista_previa_html': rec._build_preview_html(resultado),
                'resultado_json': json.dumps(rec._json_safe(resultado), ensure_ascii=False),
                'archivo_nombre': False,
                'archivo_datos': False,
            })
        return True

    def action_exportar_pdf(self):
        self.ensure_one()
        return self.env.ref('l10n_ec_iva_report.action_ec_iva_104_pdf').report_action(self)

    def action_exportar_xlsx(self):
        self.ensure_one()
        resultado = self._get_resultado_cached()
        date_from = str(self.fecha_desde)
        mes_str = date_from[5:7]
        anio_str = self._to_plain_year(date_from[:4])
        filename, binary = self._build_xlsx(resultado, mes_str, anio_str)
        self.write({
            'archivo_nombre': filename,
            'archivo_datos': base64.b64encode(binary),
            'estado': '✓ XLSX generado correctamente.',
        })
        return self._download_file_action()

    def action_exportar_xml(self):
        self.ensure_one()
        resultado = self._get_resultado_cached()
        date_from = str(self.fecha_desde)
        mes_str = date_from[5:7]
        anio_str = self._to_plain_year(date_from[:4])
        filename, binary = self._build_xml(resultado, mes_str, anio_str)
        self.write({
            'archivo_nombre': filename,
            'archivo_datos': base64.b64encode(binary),
            'estado': '✓ XML generado correctamente.',
        })
        return self._download_file_action()

    def _compute_resultado(self):
        self.ensure_one()
        return self.env['ec.iva.engine'].calcular_104(
            date_from=str(self.fecha_desde),
            date_to=str(self.fecha_hasta),
            company_id=self.company_id.id,
            credito_mes_anterior=self.credito_mes_anterior,
        )

    def _json_safe(self, value):
        """Convierte estructuras complejas (recordsets, fechas, etc.) a JSON serializable."""
        if isinstance(value, models.BaseModel):
            if len(value) == 1:
                return value.id
            return value.ids
        if isinstance(value, dict):
            return {str(k): self._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._json_safe(v) for v in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    def _get_resultado_cached(self):
        self.ensure_one()
        if self.resultado_json:
            return self._normalize_resultado(json.loads(self.resultado_json))
        return self._compute_resultado()

    def _build_xlsx(self, resultado, mes_str, anio_str):
        try:
            import xlsxwriter
        except ImportError:
            raise UserError(_('Instale xlsxwriter: pip install xlsxwriter'))

        secciones = resultado['secciones']
        dynamic_sections = secciones.get('dynamic', [])
        meta = resultado['meta']

        output = io.BytesIO()
        wb = xlsxwriter.Workbook(output, {'in_memory': True})

        f_title = wb.add_format({'bold': True, 'font_size': 13, 'align': 'center',
                                 'bg_color': '#1F4E79', 'font_color': '#FFFFFF', 'font_name': 'Arial'})
        f_sec = wb.add_format({'bold': True, 'bg_color': '#2E75B6', 'font_color': '#FFFFFF',
                               'border': 1, 'font_name': 'Arial', 'font_size': 10})
        f_cas = wb.add_format({'bold': True, 'bg_color': '#D6E4F0', 'border': 1,
                               'align': 'center', 'font_name': 'Arial', 'font_size': 9})
        f_lbl = wb.add_format({'border': 1, 'font_name': 'Arial', 'font_size': 9, 'text_wrap': True})
        f_num = wb.add_format({'border': 1, 'num_format': '#,##0.00', 'align': 'right',
                               'font_name': 'Arial', 'font_size': 9})
        f_tot = wb.add_format({'bold': True, 'border': 1, 'num_format': '#,##0.00',
                               'align': 'right', 'bg_color': '#FFF2CC', 'font_name': 'Arial'})
        f_hdr = wb.add_format({'bold': True, 'bg_color': '#D6E4F0', 'border': 1,
                               'font_name': 'Arial', 'font_size': 9})
        f_dat = wb.add_format({'border': 1, 'font_name': 'Arial', 'font_size': 9})

        ws = wb.add_worksheet('Formulario 104')
        ws.set_column('A:A', 10)
        ws.set_column('B:B', 36)
        ws.set_column('C:C', 48)
        ws.set_column('D:D', 16)

        row = 0
        ws.merge_range(row, 0, row, 3, 'FORMULARIO 104 - DECLARACION DE IVA', f_title)
        ws.set_row(row, 22)
        row += 1
        ws.merge_range(row, 0, row, 3, 'SRI Ecuador', f_title)
        row += 2

        for label, val in [('RUC:', meta['ruc']), ('Razon Social:', meta['razon_social']),
                           ('Periodo:', meta['period_label'])]:
            ws.write(row, 0, label, f_hdr)
            ws.merge_range(row, 1, row, 3, val, f_dat)
            row += 1
        row += 1

        for i, h in enumerate(['Casillero', 'Descripcion', 'Cuentas relacionadas', 'Valor (USD)']):
            ws.write(row, i, h, f_sec)
        row += 1

        def write_section(titulo):
            nonlocal row
            ws.merge_range(row, 0, row, 3, titulo, f_sec)
            row += 1

        def write_linea(line):
            nonlocal row
            cod, desc, val, accounts_summary = self._line_parts(line)
            f_v = f_tot if self._is_total_label(desc) else f_num
            ws.write(row, 0, cod, f_cas)
            ws.write(row, 1, desc, f_lbl)
            ws.write(row, 2, accounts_summary, f_lbl)
            ws.write(row, 3, val, f_v)
            row += 1

        for sec in dynamic_sections:
            row += 1
            write_section(sec.get('title', 'Sección'))
            for line in sec.get('lines', []):
                write_linea(line)

        ws_det = wb.add_worksheet('Detalle cuentas')
        ws_det.set_column('A:A', 10)
        ws_det.set_column('B:B', 42)
        ws_det.set_column('C:C', 14)
        ws_det.set_column('D:D', 42)
        ws_det.set_column('E:E', 12)
        ws_det.set_column('F:F', 16)
        det_row = 0
        headers = ['Casillero', 'Descripción', 'Cuenta', 'Nombre cuenta', '# Líneas', 'Monto ref.']
        for i, h in enumerate(headers):
            ws_det.write(det_row, i, h, f_sec)
        det_row += 1

        for line in self._detail_rows(dynamic_sections):
            ws_det.write(det_row, 0, line['code'], f_cas)
            ws_det.write(det_row, 1, line['label'], f_lbl)
            ws_det.write(det_row, 2, line['account_code'], f_lbl)
            ws_det.write(det_row, 3, line['account_name'], f_lbl)
            ws_det.write(det_row, 4, line['line_count'], f_num)
            ws_det.write(det_row, 5, line['amount_ref'], f_num)
            det_row += 1

        wb.close()
        return f'F104_{anio_str}{mes_str}.xlsx', output.getvalue()

    def _build_xml(self, resultado, mes_str, anio_str):
        casilleros = resultado['casilleros']
        meta = resultado['meta']

        root = ET.Element('declaracionImpuestosSRI')
        root.set('version', '1.0')

        cab = ET.SubElement(root, 'cabecera')
        ET.SubElement(cab, 'tipoFormulario').text = '104'
        ET.SubElement(cab, 'ruc').text = meta['ruc']
        ET.SubElement(cab, 'razonSocial').text = meta['razon_social']
        ET.SubElement(cab, 'mes').text = mes_str
        ET.SubElement(cab, 'anio').text = self._to_plain_year(anio_str)
        ET.SubElement(cab, 'declaracionSustitutiva').text = '0'

        orden = [
            '411', '412', '413', '414', '415', '416',
            '407', '408', '409', '410', '417', '420',
            '500', '501', '502', '503', '504', '505',
            '506', '507', '508', '509', '510',
            '511', '512', '513', '514', '515', '516',
            '517', '518',
            '601', '602', '603', '604', '605',
            '721', '723', '725', '727', '729',
        ]

        detalle = ET.SubElement(root, 'detalle')
        for cod in orden:
            val = casilleros.get(cod, 0.0)
            if val != 0.0:
                campo = ET.SubElement(detalle, 'campo')
                campo.set('nombre', f'cas{cod}')
                campo.text = f'{abs(val):.2f}'

        raw = ET.tostring(root, encoding='unicode')
        pretty = minidom.parseString(
            f'<?xml version="1.0" encoding="UTF-8"?>{raw}'
        ).toprettyxml(indent='  ', encoding='UTF-8')
        bytes_xml = pretty if isinstance(pretty, bytes) else pretty.encode()
        return f'F104_{anio_str}{mes_str}.xml', bytes_xml

    def _build_preview_html(self, resultado):
        meta = resultado.get('meta', {})
        secciones = resultado.get('secciones', {})
        dynamic_sections = secciones.get('dynamic', [])

        def fmt(v):
            return f"{float(v or 0.0):,.2f}"

        def section_table(title, rows):
            body = []
            for line in rows or []:
                cod, desc, val, accounts_summary = self._line_parts(line)
                body.append(
                    "<tr>"
                    f"<td>{escape(str(cod))}</td>"
                    f"<td>{escape(str(desc))}</td>"
                    f"<td>{escape(accounts_summary)}</td>"
                    f"<td style='text-align:right'>{fmt(val)}</td>"
                    "</tr>"
                )
            body_html = "".join(body) or "<tr><td colspan='4'>Sin datos</td></tr>"
            return (
                f"<h4>{escape(title)}</h4>"
                "<table class='table table-sm table-bordered'>"
                "<thead><tr><th>Casillero</th><th>Descripción</th><th>Cuentas relacionadas</th><th style='text-align:right'>Valor</th></tr></thead>"
                f"<tbody>{body_html}</tbody></table>"
            )

        detail_rows = self._detail_rows(dynamic_sections)
        detail_body = "".join(
            "<tr>"
            f"<td>{escape(r['code'])}</td>"
            f"<td>{escape(r['label'])}</td>"
            f"<td>{escape(r['account_code'])}</td>"
            f"<td>{escape(r['account_name'])}</td>"
            f"<td style='text-align:right'>{int(r['line_count'])}</td>"
            f"<td style='text-align:right'>{fmt(r['amount_ref'])}</td>"
            "</tr>"
            for r in detail_rows
        ) or "<tr><td colspan='6'>Sin detalle de cuentas</td></tr>"

        return (
            "<div>"
            "<h3>Vista previa Formulario 104</h3>"
            "<table class='table table-sm table-bordered'>"
            f"<tr><td><b>RUC</b></td><td>{escape(str(meta.get('ruc', '')))}</td></tr>"
            f"<tr><td><b>Razón social</b></td><td>{escape(str(meta.get('razon_social', '')))}</td></tr>"
            f"<tr><td><b>Período</b></td><td>{escape(str(meta.get('period_label', '')))}</td></tr>"
            "</table>"
            + "".join(section_table(sec.get('title', 'Sección'), sec.get('lines', [])) for sec in dynamic_sections)
            +
            "<h4>Detalle por cuenta contable</h4>"
            "<table class='table table-sm table-bordered'>"
            "<thead><tr><th>Casillero</th><th>Descripción</th><th>Cuenta</th><th>Nombre cuenta</th><th style='text-align:right'># Líneas</th><th style='text-align:right'>Monto ref.</th></tr></thead>"
            f"<tbody>{detail_body}</tbody></table>"
            +
            f"<p><i>Generado: {escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</i></p>"
            "</div>"
        )

    @staticmethod
    def _is_total_label(label):
        txt = str(label or '').lower()
        return ('total' in txt) or ('subtotal' in txt)

    @api.model
    def _year_selection(self):
        company_id = self.env.context.get('default_company_id') or self.env.company.id
        self.env.cr.execute("""
            SELECT DISTINCT EXTRACT(YEAR FROM m.date)::int AS y
            FROM account_move m
            WHERE m.state = 'posted'
              AND m.company_id = %s
              AND m.date IS NOT NULL
              AND m.move_type IN ('out_invoice', 'out_refund', 'in_invoice', 'in_refund')
            ORDER BY y DESC
        """, (company_id,))
        years = [str(row[0]) for row in self.env.cr.fetchall() if row and row[0]]
        if not years:
            years = [str(fields.Date.today().year)]
        return [(y, y) for y in years]

    @staticmethod
    def _to_plain_year(value):
        try:
            return str(int(float(str(value).strip())))
        except (TypeError, ValueError):
            return str(value).strip()

    @staticmethod
    def _safe_int(value, default):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return int(default)

    def _normalize_resultado(self, resultado):
        """Compatibilidad: normaliza snapshots antiguos (líneas tipo lista/tupla)."""
        secciones = (resultado or {}).get('secciones', {})
        dynamic = secciones.get('dynamic', [])
        changed = False
        for sec in dynamic:
            normalized = []
            for line in sec.get('lines', []):
                if isinstance(line, dict):
                    normalized.append(line)
                    continue
                code, label, value, accounts_summary = self._line_parts(line)
                normalized.append({
                    'code': code,
                    'label': label,
                    'value': value,
                    'accounts_summary': accounts_summary,
                    'accounts': [],
                })
                changed = True
            sec['lines'] = normalized
        if changed and 'details' not in secciones:
            secciones['details'] = [
                line for sec in dynamic for line in sec.get('lines', [])
            ]
        return resultado

    @staticmethod
    def _line_parts(line):
        if isinstance(line, dict):
            return (
                str(line.get('code', '')),
                str(line.get('label', '')),
                float(line.get('value', 0.0) or 0.0),
                str(line.get('accounts_summary', '') or ''),
            )
        if isinstance(line, (list, tuple)) and len(line) >= 3:
            return str(line[0]), str(line[1]), float(line[2] or 0.0), ''
        return '', '', 0.0, ''

    @classmethod
    def _detail_rows(cls, dynamic_sections):
        rows = []
        for sec in dynamic_sections or []:
            for line in sec.get('lines', []):
                code, label, _, _ = cls._line_parts(line)
                accounts = line.get('accounts', []) if isinstance(line, dict) else []
                if not accounts:
                    continue
                for acc in accounts:
                    rows.append({
                        'code': code,
                        'label': label,
                        'account_code': str(acc.get('account_code', '') or ''),
                        'account_name': str(acc.get('account_name', '') or ''),
                        'line_count': int(acc.get('line_count', 0) or 0),
                        'amount_ref': float(acc.get('amount_ref', 0.0) or 0.0),
                    })
        return rows

    def _download_file_action(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/{}/{}/archivo_datos/{}?download=true'.format(
                self._name, self.id, self.archivo_nombre or 'reporte'
            ),
            'target': 'self',
        }
