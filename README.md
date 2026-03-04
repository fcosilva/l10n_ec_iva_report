# l10n_ec_iva_report

Reporte de Declaración de IVA Formulario 104 para Ecuador en Odoo 17 Community.

## Objetivo

Generar información del Formulario 104 del SRI usando tax tags contables, con salida en PDF, XLSX y XML.

## Dependencias

- `account`
- `l10n_ec`
- `l10n_ec_ats`

## Instalación / actualización

```bash
docker-compose run --rm web-dev odoo -d openlab-dev -u l10n_ec_iva_report --stop-after-init
docker-compose restart web-dev
```

## Licencia

AGPL-3 (ver archivo `LICENSE`).
