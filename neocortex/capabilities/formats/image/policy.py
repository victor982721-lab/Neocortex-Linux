"""Versioned constants and conservative policy tables for image analysis."""

from __future__ import annotations
import re

from neocortex.semantic.semantic_ontology import (
    INDUSTRIAL_ACTIVITY_HINTS as INDUSTRIAL_ACTIVITY_HINTS,
    INDUSTRIAL_ENTITY_HINTS as INDUSTRIAL_ENTITY_HINTS,
    OPERATIONAL_CONTEXT_HINTS as OPERATIONAL_CONTEXT_HINTS,
    SAFETY_CONDITION_HINTS as SAFETY_CONDITION_HINTS,
)


# region [01] Processing versions and bounded samples

IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
SAMPLE_SIDE = 448
MIB = 1024 * 1024
DOCUMENT_OCR_TEXT_MAX_UTF8_BYTES = 16 * 1024
FEATURE_VERSION = "image-features-v4"
DECISION_VERSION = "image-decisions-v10"
ANALYSIS_VERSION = f"{FEATURE_VERSION}|{DECISION_VERSION}"
LEGACY_FEATURE_SIGNATURES = frozenset({"image-route-v2|image-analysis-v3"})
COMPATIBLE_FEATURE_PREFIXES = (
    "image-route-v3|image-features-v3|",
    "image-route-v4|image-features-v4|",
)
PROFILE_EXCLUDED_DIRS = (".codex", "AppData")


# endregion [01]


# region [02] Structural classification policy

CATEGORY_DIRS = {
    "documento_pagina": "01_documentos_y_paginas",
    "plano_diagrama": "02_planos_y_diagramas",
    "captura_pantalla": "03_capturas_de_pantalla",
    "logo_icono": "04_logos_e_iconos",
    "recurso_transparente": "05_recursos_transparentes",
    "grafico_ilustracion": "06_graficos_e_ilustraciones",
    "foto": "07_fotografias",
    "animada": "08_imagenes_animadas",
    "baja_resolucion": "09_baja_resolucion",
    "otro": "10_otros_no_determinados",
}
GENERATED_DIRS = frozenset(CATEGORY_DIRS.values()) | {
    "01_documentos_escaneados",
    "01_documentos_y_paginas",
    "02_logos_candidatos",
    "02_planos_y_diagramas",
    "03_electrico_subestaciones",
    "03_capturas_de_pantalla",
    "04_equipos_electricos",
    "04_placas_y_datos_tecnicos",
    "04_logos_e_iconos",
    "05_maquinaria_y_campo",
    "05_subestaciones",
    "05_recursos_transparentes",
    "06_otros",
    "06_transformadores",
    "06_graficos_e_ilustraciones",
    "07_proteccion_y_control",
    "07_fotografias",
    "07_fotos_horizontales",
    "08_pruebas_y_mediciones",
    "08_fotos_verticales",
    "08_imagenes_animadas",
    "09_equipo_electrico",
    "09_fotos_cuadradas",
    "09_baja_resolucion",
    "10_herramientas_y_maquinaria",
    "10_imagenes_animadas",
    "10_otros_no_determinados",
    "11_obra_y_mantenimiento",
    "11_panoramicas",
    "12_seguridad_y_epp",
    "12_baja_resolucion",
    "13_logos_e_iconos",
    "13_otros_no_determinados",
    "14_graficos_e_ilustraciones",
    "15_personas",
    "16_fotografias_generales",
    "17_imagenes_animadas",
    "18_baja_resolucion",
    "19_panoramicas",
    "20_otros_no_determinados",
}

NAME_HINTS = {
    "documento_pagina": (
        "acta",
        "certificado",
        "certificate",
        "contrato",
        "document",
        "documento",
        "factura",
        "invoice",
        "informe",
        "manual",
        "pagina",
        "page",
        "recibo",
        "receipt",
        "reporte",
        "report",
        "scan",
        "scanned",
        "escaneo",
        "hoja",
    ),
    "plano_diagrama": (
        "blueprint",
        "croquis",
        "diagram",
        "diagrama",
        "drawing",
        "esquema",
        "layout",
        "plano",
        "planos",
        "schematic",
        "wiring",
        "unifilar",
        "dwg",
    ),
    "captura_pantalla": (
        "captura de pantalla",
        "captura",
        "screenshot",
        "screen shot",
        "screensnap",
        "snipping tool",
        "pantallazo",
        "screencap",
    ),
    "logo_icono": ("favicon", "icon", "icono", "logo", "logotipo", "watermark"),
    "grafico_ilustracion": (
        "banner",
        "chart",
        "grafica",
        "grafico",
        "graph",
        "illustration",
        "ilustracion",
        "infographic",
        "infografia",
        "poster",
        "render",
        "vector",
    ),
}

NAME_HINT_POINTS = {
    "documento_pagina": 2.0,
    "plano_diagrama": 3.5,
    "captura_pantalla": 5.5,
    "logo_icono": 5.5,
    "grafico_ilustracion": 4.0,
}

PHOTO_NAME_RE = re.compile(
    r"^(?:img|dsc|dscf|pxl|mvimg|photo|foto|camera|wp)[-_ ]?\d{3,}", re.IGNORECASE
)
SCREEN_DIMENSIONS = {
    (1024, 768),
    (1280, 720),
    (1280, 800),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
    (1920, 1200),
    (2160, 1080),
    (2560, 1440),
    (2560, 1600),
    (2880, 1800),
    (3440, 1440),
    (3840, 2160),
}
TOKEN_RE = re.compile(r"[a-z0-9]+")


# endregion [02]


# region [03] Industrial semantic vocabulary
# The legacy names above are imported from the shared stable ontology so OCR,
# path evidence, visual prototypes and document semantics use one vocabulary.

# endregion [03]
