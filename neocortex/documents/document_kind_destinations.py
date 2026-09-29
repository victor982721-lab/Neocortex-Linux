"""Controlled physical taxonomy shared by planning and Curation validation.

These are routing identifiers, not inferred facts or another classifier. Model
concepts can select only an explicit directory mapping after the single current
calibrated decision passes the owner gate.
"""

from __future__ import annotations

KIND_DESTINATION_VERSION = "neocortex.document-kind-destinations/v1"

COMPACT_KIND_DIRECTORIES: dict[str, tuple[str, ...]] = {
    "accion_correctiva_preventiva": ("Pruebas_y_calidad", "Calidad"),
    "catalogo_equipo": ("Ingenieria_y_documentacion", "Manuales_catalogos_y_fichas"),
    "certificado_calibracion": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "certificado_calidad": ("Pruebas_y_calidad", "Calidad"),
    "comprobante_viaje": ("Gestion_y_administracion", "Administracion"),
    "constancia_capacitacion": ("Capacitacion",),
    "control_metrologico": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "correspondencia": ("Gestion_y_administracion", "Proyecto_y_correspondencia"),
    "credencial_visitante": ("Seguridad_y_ambiente", "Seguridad"),
    "curso_capacitacion": ("Capacitacion",),
    "descripcion_tecnica_sistema": (
        "Ingenieria_y_documentacion",
        "Ingenieria_y_calculos",
    ),
    "documento_empresa": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "dossier_calidad": ("Pruebas_y_calidad", "Calidad"),
    "especificacion_tecnica": ("Ingenieria_y_documentacion", "Ingenieria_y_calculos"),
    "etiqueta_muestra_laboratorio": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "factura_comprobante": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "ficha_tecnica": ("Ingenieria_y_documentacion", "Manuales_catalogos_y_fichas"),
    "formato_empresa": ("Gestion_y_administracion", "Formatos_y_registros"),
    "formato_inspeccion": ("Pruebas_y_calidad", "Inspecciones"),
    "hoja_asignacion_proyecto": (
        "Gestion_y_administracion",
        "Proyecto_y_correspondencia",
    ),
    "hoja_datos_seguridad": ("Seguridad_y_ambiente", "Seguridad"),
    "informe_analisis": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "informe_auditoria": ("Pruebas_y_calidad", "Calidad"),
    "informe_inspeccion": ("Pruebas_y_calidad", "Inspecciones"),
    "informe_tecnico": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "instructivo_trabajo": (
        "Operacion_y_mantenimiento",
        "Procedimientos_e_instructivos",
    ),
    "lista_empaque_embarque": ("Logistica_y_embarques",),
    "lista_materiales": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "lista_verificacion": ("Pruebas_y_calidad", "Inspecciones"),
    "manual_equipo": ("Ingenieria_y_documentacion", "Manuales_catalogos_y_fichas"),
    "manual_sistema_gestion": ("Pruebas_y_calidad", "Calidad"),
    "memoria_calculo": ("Ingenieria_y_documentacion", "Ingenieria_y_calculos"),
    "minuta_acta": ("Gestion_y_administracion", "Proyecto_y_correspondencia"),
    "orden_trabajo": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "plan_tecnico": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "plano_diagrama": ("Ingenieria_y_documentacion", "Planos_y_diagramas"),
    "procedimiento": ("Operacion_y_mantenimiento", "Procedimientos_e_instructivos"),
    "programa_cronograma": ("Operacion_y_mantenimiento", "Planeacion_y_ordenes"),
    "programa_gestion_ambiental": ("Seguridad_y_ambiente", "Ambiente"),
    "programa_seguridad_salud": ("Seguridad_y_ambiente", "Seguridad"),
    "protocolo_pruebas": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
    "referencia_tecnica": ("Ingenieria_y_documentacion", "Informes_y_referencias"),
    "registro_asistencia": ("Gestion_y_administracion", "Formatos_y_registros"),
    "registro_auditores": ("Pruebas_y_calidad", "Calidad"),
    "registro_bitacora": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "registro_entrega_epp": ("Seguridad_y_ambiente", "Seguridad"),
    "registro_fotografico": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "registro_incidencias": ("Seguridad_y_ambiente", "Seguridad"),
    "registro_mediciones": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
    "registro_tiempo_personal": ("Gestion_y_administracion", "Formatos_y_registros"),
    "reporte_actividades": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "reporte_anomalias": ("Operacion_y_mantenimiento", "Bitacoras_y_reportes"),
    "reporte_entrega_embarque": ("Logistica_y_embarques",),
    "reporte_fat_sat": ("Pruebas_y_calidad", "FAT_SAT"),
    "reporte_laboratorio": ("Pruebas_y_calidad", "Laboratorio_y_metrologia"),
    "reporte_no_conformidad": ("Pruebas_y_calidad", "Calidad"),
    "reporte_resultados_pruebas": ("Pruebas_y_calidad", "Pruebas_y_resultados"),
    "viaticos_gastos": ("Gestion_y_administracion", "Administracion"),
    "compra_requisicion": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "contrato_legal": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "cotizacion_propuesta": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "licitacion": ("Gestion_y_administracion", "Comercial_y_contratos"),
    "entrevista_grabada": ("Reuniones_y_entrevistas",),
    "instruccion_verbal": ("Reuniones_y_entrevistas",),
    "reunion_grabada": ("Reuniones_y_entrevistas",),
}
REVIEW_ONLY_KINDS = frozenset(
    {
        "audio_transcrito",
        "expediente_personal",
        "instruccion_cuenta_bancaria",
        "otro",
        "registro_log",
        "reporte_inventario_archivo",
    }
)

# The packaged compact calibration has coarser document-kind axes than the
# legacy domain catalog. Preserve its concept ID in evidence, and map only
# these explicit IDs to the existing physical bucket (no invented folders).
FAST_KIND_ROUTING_ALIASES = {
    "contract": "contrato_legal",
    "design": "memoria_calculo",
    "email": "correspondencia",
    "invoice": "factura_comprobante",
    "manual": "manual_equipo",
    "procedure": "procedimiento",
    "report": "informe_tecnico",
}
