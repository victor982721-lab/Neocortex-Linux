"""Built-in electrical-sector vocabulary for document classification."""
# region [00] Contexto del módulo
# Módulo: _04_Nucleo_Operativo/document_taxonomy_vocabulary.py
# Propósito: documentación embebida y separación visual de regiones.
# endregion [00]


# region [01] Dependencias del módulo
from __future__ import annotations

from neocortex.platform import preserve_legacy_module as _preserve_legacy_module

from typing import Mapping

from .document_taxonomy_models import (
    AuthoritySpec,
    ClientSpec,
    OrganizationSpec,
    ProjectSpec,
    TechnicalTaxonomy,
)
# endregion [01]

# region [02] Implementación


BUILTIN_TAXONOMY_VERSION = "electrical-document-taxonomy-v13"

_SHORT_EN_INDUSTRIAL_STANDARDS = frozenset(
    {149, 166, 170, 172, 360, 361, 362, 363, 365, 388, 397, 420, 590, 795}
)


_BUILTIN_AUTHORITIES = (
    AuthoritySpec(
        "ISO/IEC",
        ("ISO/IEC", "ISO IEC"),
        (r"\bISO\s*/\s*IEC\s+\d{3,5}(?:[.\-]\d+)*(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec(
        "IEC/IEEE",
        ("IEC/IEEE", "IEC IEEE"),
        (r"\bIEC\s*/\s*IEEE\s+\d{3,5}(?:[.\-]\d+)*(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec(
        "IEEE",
        ("IEEE", "Institute of Electrical and Electronics Engineers"),
        (
            r"(?<![/\w])(?:ANSI\s*/\s*)?IEEE(?:\s+STD\.?)?\s+"
            r"(?:C\d{2,3}(?:\.\d+)+|\d{2,5}(?:[.\-]\d+)*)"
            r"(?:\s*TM)?\s*(?:[-:]\d{4})?\b",
        ),
    ),
    AuthoritySpec(
        "IEC",
        ("IEC", "International Electrotechnical Commission"),
        (
            r"(?<![/\w])IEC(?:\s+(?:TR|TS|PAS))?\s+\d{3,5}(?:[.\-]\d+)*"
            r"\s+EDITION\s+\d+(?:\.\d+)?\s+(?:19|20)\d{2}(?:-\d{2})?\b",
            r"(?<![/\w])IEC(?:\s+(?:TR|TS|PAS))?\s+\d{3,5}(?:[.\-]\d+)*(?:[-:]\d{4})?\b",
        ),
    ),
    AuthoritySpec(
        "ISO",
        ("ISO", "International Organization for Standardization"),
        (r"\bISO(?!\s*/\s*IEC)\s+\d{3,5}(?:[.\-]\d+)*(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec(
        "NMX",
        ("NMX", "Norma Mexicana"),
        (
            r"\bNMX(?:-[A-Z0-9]+(?:/[A-Z0-9]+)?){2,7}\b",
            r"\bNMX\s+[A-Z]{1,5}\s+[A-Z0-9]+(?:/[A-Z0-9]+)?"
            r"(?:\s+(?:ANCE|IMNC|SCFI|NYCE|ONNCCE|NORMEX))?(?:\s+\d{4})?\b",
        ),
    ),
    AuthoritySpec(
        "NOM",
        ("NOM", "Norma Oficial Mexicana"),
        (r"\bNOM(?:-[A-Z0-9]+){2,7}\b",),
    ),
    AuthoritySpec(
        "NRF",
        ("NRF", "Norma de Referencia"),
        (r"\bNRF(?:-[A-Z0-9]+){2,7}\b",),
    ),
    AuthoritySpec(
        "CFE",
        (
            "CFE",
            "Comision Federal de Electricidad",
            "Comisión Federal de Electricidad",
        ),
        (
            r"\bCFE\s+(?=[A-Z0-9_-]{3,18}\b)(?=[A-Z0-9_-]*\d)"
            r"[A-Z0-9]+(?:[-_][A-Z0-9]+)*\b",
            r"\bCFE\s+(?:DCCAMBT|DCCSSUBT)\b",
            r"\b(?:SOM|M)[-\s]+\d{3,5}(?:[-\s]+[A-Z0-9]{2,8})?\b",
        ),
    ),
    AuthoritySpec(
        "ANSI",
        ("ANSI",),
        (r"\bANSI\s+[A-Z]?[0-9]{1,4}(?:[.\-][A-Z0-9]+)+(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec("NFPA", ("NFPA",), (r"\bNFPA\s+\d{1,4}(?:[A-Z])?(?:[-:]\d{4})?\b",)),
    AuthoritySpec("NEMA", ("NEMA",), (r"\bNEMA\s+[A-Z]{1,5}\s*\d+(?:[.\-]\d+)*\b",)),
    AuthoritySpec(
        "ASTM",
        ("ASTM", "ASTM International"),
        (
            r"\bASTM\s+[A-Z]\d{1,5}(?:/[A-Z]\d{1,5}[A-Z]?)?"
            r"(?:[-:]\d{2,4}(?:\(\d{4}\))?)?\b",
        ),
    ),
    AuthoritySpec(
        "UL", ("UL", "Underwriters Laboratories"), (r"\bUL\s+\d{2,5}(?:[-:]\d{4})?\b",)
    ),
    AuthoritySpec("ANCE", ("ANCE", "Normalizacion y Certificacion NYCE"), ()),
    AuthoritySpec("CENACE", ("CENACE", "Centro Nacional de Control de Energia"), ()),
    AuthoritySpec("CRE", ("CRE", "Comision Reguladora de Energia"), ()),
    AuthoritySpec("STPS", ("STPS", "Secretaria del Trabajo y Prevision Social"), ()),
    AuthoritySpec(
        "LAPEM",
        ("LAPEM", "Laboratorio de Pruebas de Equipos y Materiales"),
        (),
    ),
    AuthoritySpec(
        "CIGRE",
        ("CIGRE", "Conseil International des Grands Reseaux Electriques"),
        (r"\bCIGRE(?:\s+(?:TB|BROCHURE))?\s+\d{2,4}\b",),
    ),
    AuthoritySpec(
        "CSA",
        ("CSA", "Canadian Standards Association"),
        (r"\b(?:CAN/)?CSA[-\s][A-Z0-9.]{1,16}(?:[-:]\d{2,4})?\b",),
    ),
    AuthoritySpec(
        "EN",
        ("European Standard", "Norma Europea"),
        (r"\bEN\s+\d{3,6}(?:[-.]\d+)*(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec(
        "BS",
        ("British Standard", "British Standards Institution"),
        (r"\bBS(?:\s+EN)?\s+\d{3,6}(?:[-.]\d+)*(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec(
        "DIN",
        ("DIN", "Deutsches Institut fur Normung"),
        (r"\bDIN(?:\s+EN|\s+IEC|\s+ISO)?\s+\d{3,6}(?:[-.]\d+)*\b",),
    ),
    AuthoritySpec(
        "ASME",
        ("ASME", "American Society of Mechanical Engineers"),
        (r"\bASME\s+[A-Z]{1,4}\d*(?:\.\d+)+(?:[-:]\d{2,4})?\b",),
    ),
    AuthoritySpec(
        "API",
        ("American Petroleum Institute",),
        (r"\bAPI\s+(?:STD|RP|SPEC)?\s*\d{2,4}[A-Z]?(?:[-:]\d{2,4})?\b",),
    ),
    AuthoritySpec(
        "OSHA",
        ("OSHA", "Occupational Safety and Health Administration"),
        (r"\bOSHA\s+(?:29\s+CFR\s+)?\d{3,4}(?:\.\d+)+\b",),
    ),
    AuthoritySpec(
        "NETA",
        ("NETA", "InterNational Electrical Testing Association"),
        (
            r"\b(?:ANSI\s*(?:/|\s)\s*)?NETA[-\s](?:ATS|MTS|ECS|ETT|EMW)"
            r"(?:[-:\s]\d{4})?\b",
        ),
    ),
    AuthoritySpec(
        "NERC",
        ("NERC", "North American Electric Reliability Corporation"),
        (
            r"\bNERC[-\s](?:BAL|CIP|COM|EOP|FAC|INT|IRO|MOD|NUC|PER|PRC|TOP|TPL|VAR|VIC)-\d{3}(?:-\d+)?[A-Z]?\b",
        ),
    ),
    AuthoritySpec(
        "EPRI",
        ("EPRI", "Electric Power Research Institute"),
        (r"\bEPRI\s+(?:TR[-\s])?\d{5,}\b",),
    ),
    AuthoritySpec(
        "ISA",
        ("International Society of Automation",),
        (r"\b(?:ANSI[/\s])?ISA[-\s]\d{1,3}(?:\.\d+)+(?:[-:]\d{4})?\b",),
    ),
    AuthoritySpec(
        "ACI",
        ("American Concrete Institute",),
        (r"\bACI\s+\d{3}[A-Z]?(?:[-:]\d{2,4})?\b",),
    ),
    AuthoritySpec(
        "AISC",
        ("American Institute of Steel Construction",),
        (r"\bAISC\s+\d{3}(?:[-:]\d{2,4})?\b",),
    ),
    AuthoritySpec(
        "AWS",
        ("American Welding Society",),
        (r"\bAWS\s+D\d+(?:\.\d+)+(?:[-:]\d{2,4})?\b",),
    ),
    AuthoritySpec(
        "ASCE",
        ("American Society of Civil Engineers",),
        (r"\bASCE[/\s]+SEI\s+\d+(?:[-:]\d{2,4})?\b", r"\bASCE\s+\d+(?:[-:]\d{2,4})?\b"),
    ),
    AuthoritySpec(
        "SSPC",
        ("Society for Protective Coatings",),
        (r"\bSSPC[-\s](?:AB|Guide|PA|QP|SP)\s*\d+(?:\.\d+)?\b",),
    ),
    AuthoritySpec(
        "NEC",
        ("National Electrical Code",),
        (r"\bNEC\s+(?:ARTICLE\s+)?\d{3}(?:\.\d+)?\b",),
    ),
    AuthoritySpec(
        "PEMEX",
        ("Norma PEMEX", "Especificacion PEMEX", "Especificación PEMEX"),
        (r"\bPEMEX[-\s](?:EST|NRF|PROY|REF)[-\s][A-Z0-9][A-Z0-9.\-]{2,}\b",),
    ),
)

_BUILTIN_ORGANIZATIONS = (
    OrganizationSpec("ANDRITZ", ("ANDRITZ", "ANDRITZ HYDRO")),
    OrganizationSpec("SERINTRA", ("SERINTRA",)),
    OrganizationSpec(
        "CFE",
        ("CFE", "Comision Federal de Electricidad", "Comisión Federal de Electricidad"),
    ),
    OrganizationSpec("OMICRON", ("OMICRON", "OMICRON ELECTRONICS")),
    OrganizationSpec("MEGGER", ("MEGGER",)),
    OrganizationSpec("DOBLE", ("DOBLE", "DOBLE ENGINEERING")),
    OrganizationSpec("FLUKE", ("FLUKE",)),
    OrganizationSpec("VANGUARD", ("VANGUARD INSTRUMENTS", "VANGUARD")),
    OrganizationSpec("DV POWER", ("DV POWER",)),
    OrganizationSpec("ISA ALTANOVA", ("ISA ALTANOVA", "ALTANOVA", "ISA ADVANCED TEST")),
    OrganizationSpec("QUALITROL", ("QUALITROL",)),
    OrganizationSpec("SEL", ("SCHWEITZER ENGINEERING LABORATORIES", "SEL")),
    OrganizationSpec("SIEMENS", ("SIEMENS",)),
    OrganizationSpec("HITACHI ENERGY", ("HITACHI ENERGY", "ABB POWER GRIDS")),
    OrganizationSpec("ABB", ("ABB", "ASEA BROWN BOVERI")),
    OrganizationSpec("GE VERNOVA", ("GE VERNOVA", "GENERAL ELECTRIC", "GE GRID")),
    OrganizationSpec("SCHNEIDER ELECTRIC", ("SCHNEIDER ELECTRIC", "SCHNEIDER")),
    OrganizationSpec("EATON", ("EATON", "COOPER POWER SYSTEMS")),
    OrganizationSpec(
        "INGENIERÍA ANALÍTICA & MANTENIMIENTO ELÉCTRICO",
        (
            "INGENIERIA ANALITICA & MANTENIMIENTO ELECTRICO",
            "INGENIERIA ANALITICA Y MANTENIMIENTO ELECTRICO",
            "IAME",
        ),
    ),
    OrganizationSpec("ARBEIT INGENIERÍA", ("ARBEIT INGENIERIA", "ARBEIT")),
    OrganizationSpec(
        "COMUNICACIONES Y CONTROL",
        ("COMUNICACIONES Y CONTROL", "COMUNICACIONES & CONTROL"),
    ),
    OrganizationSpec("CYMI", ("CYMI", "CONTROL Y MONTAJES INDUSTRIALES")),
    OrganizationSpec("SAAVI ENERGÍA", ("SAAVI ENERGIA", "SAAVI")),
    OrganizationSpec("CHINT", ("CHINT", "CHINT ELECTRIC")),
    OrganizationSpec("VITRO", ("VITRO", "VITRO ENERGIA")),
    OrganizationSpec("PROLEC GE", ("PROLEC GE", "PROLEC")),
    OrganizationSpec("IEM", ("IEM", "INDUSTRIAS IEM")),
    OrganizationSpec("WEG", ("WEG", "WEG ELECTRIC")),
    OrganizationSpec("TOSHIBA", ("TOSHIBA", "TOSHIBA ENERGY SYSTEMS")),
    OrganizationSpec(
        "MITSUBISHI ELECTRIC",
        ("MITSUBISHI ELECTRIC", "MITSUBISHI POWER"),
    ),
    OrganizationSpec("HYOSUNG", ("HYOSUNG", "HYOSUNG HEAVY INDUSTRIES")),
    OrganizationSpec("TBEA", ("TBEA",)),
    OrganizationSpec("S&C ELECTRIC", ("S&C ELECTRIC", "S AND C ELECTRIC")),
    OrganizationSpec("G&W ELECTRIC", ("G&W ELECTRIC", "G AND W ELECTRIC")),
    OrganizationSpec("POWELL", ("POWELL INDUSTRIES", "POWELL")),
    OrganizationSpec("BASLER ELECTRIC", ("BASLER ELECTRIC", "BASLER")),
    OrganizationSpec("BECKWITH ELECTRIC", ("BECKWITH ELECTRIC", "BECKWITH")),
    OrganizationSpec("DRANETZ", ("DRANETZ",)),
    OrganizationSpec("ELSPEC", ("ELSPEC",)),
    OrganizationSpec("HAEFELY", ("HAEFELY", "HAEFELY HIPOTRONICS")),
    OrganizationSpec("HIGHVOLT", ("HIGHVOLT",)),
    OrganizationSpec("SEBA KMT", ("SEBA KMT", "MEGGER SEBA")),
    OrganizationSpec(
        "GRUPO DE METROLOGÍA CLAM",
        (
            "GRUPO DE METROLOGIA CLAM",
            "GRUPO DE METROLOGÍA CLAM",
            "CLAM S.A. DE C.V.",
            "CLAM SA DE CV",
        ),
    ),
    OrganizationSpec("PEMEX", ("PEMEX", "PETROLEOS MEXICANOS", "PETRÓLEOS MEXICANOS")),
    OrganizationSpec(
        "INEEL",
        (
            "INEEL",
            "INSTITUTO NACIONAL DE ELECTRICIDAD Y ENERGIAS LIMPIAS",
            "INSTITUTO NACIONAL DE ELECTRICIDAD Y ENERGÍAS LIMPIAS",
            "INSTITUTO DE INVESTIGACIONES ELECTRICAS",
            "INSTITUTO DE INVESTIGACIONES ELÉCTRICAS",
        ),
    ),
    OrganizationSpec(
        "LAPEM",
        ("LAPEM", "LABORATORIO DE PRUEBAS DE EQUIPOS Y MATERIALES"),
    ),
)

_BUILTIN_CLIENTS = (
    ClientSpec("ANDRITZ", ("ANDRITZ", "ANDRITZ HYDRO", "ANDRITZ CHINA")),
)

_BUILTIN_PROJECTS = (
    ProjectSpec(
        "Malpaso",
        "ANDRITZ",
        (
            "MALPASO",
            "MAL PASO",
            "CH MALPASO",
            "C.H. MALPASO",
            "CENTRAL HIDROELECTRICA MALPASO",
            "MALPASO HYDROELECTRIC POWER PLANT",
            "FIEL/10670-005/2021",
            "LH-MALPASO",
        ),
    ),
)

_KIND_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "reunion_grabada": (
        r"\bgrabaci[oó]n\s+de\s+(?:la\s+)?reuni[oó]n\b",
        r"\breuni[oó]n\s+(?:de\s+)?(?:trabajo|seguimiento|coordinaci[oó]n|arranque)\b",
        r"\borden\s+del\s+d[ií]a\b",
        r"\bmeeting\s+(?:recording|discussion|minutes)\b",
    ),
    "instruccion_verbal": (
        r"\binstrucci[oó]n\s+(?:verbal|de\s+voz|operativa)\b",
        r"\bindicaciones?\s+(?:de\s+trabajo|para\s+(?:realizar|ejecutar))\b",
        r"\bnota\s+de\s+voz\s+(?:con\s+)?instrucciones\b",
        r"\bvoice\s+(?:note|message)\s+with\s+instructions\b",
    ),
    "entrevista_grabada": (
        r"\bentrevista\s+(?:grabada|con|a)\b",
        r"\bpregunta\s*(?:uno|1)?\s*[:.-]",
        r"\brecorded\s+interview\b",
    ),
    "manual_equipo": (
        r"\bmanual\s+(?:de\s+)?(?:usuario|operacion|operación|servicio|mantenimiento|instrucciones?)\b",
        r"\b(?:user|instruction|operation|service|maintenance)\s+manual\b",
        r"\bguia\s+(?:de\s+)?(?:usuario|operacion|operación)\b",
    ),
    "manual_sistema_gestion": (
        r"\bmanual\s+(?:del?\s+)?(?:sistema\s+de\s+gesti[oó]n|calidad|"
        r"seguridad|ambiental)\b",
        r"\b(?:quality|management\s+system|safety|environmental)\s+manual\b",
    ),
    "procedimiento": (
        r"\bprocedimientos?\b",
        r"\bmanual\s+de\s+procedimientos\b",
        r"\bmetodo\s+de\s+trabajo\b",
        r"\bmethod\s+statement\b",
        r"\bprocedimiento\s+de\s+prueba\b",
    ),
    "instructivo_trabajo": (
        r"\binstructivo\s+(?:de\s+)?trabajo\b",
        r"\bmanual\s+de\s+instructivos\b",
        r"\b(?:nombre|desarrollo)\s+del\s+instructivo\b",
        r"\binstrucci[oó]n\s+de\s+trabajo\b",
        r"\bwork\s+instruction\b",
    ),
    "curso_capacitacion": (
        r"\bcurso\b",
        r"\bcapacitaci[oó]n\b",
        r"\bmaterial\s+did[aá]ctico\b",
        r"\bmanual\s+del\s+participante\b",
        r"\btraining\s+(?:course|material|manual)\b",
    ),
    "formato_empresa": (
        r"\bformato\s*[:#-]\s*[A-Z0-9][A-Z0-9._/-]{2,}\b",
        r"\b(?:c[oó]digo|clave)\s+(?:del?\s+)?formato\b",
        r"\bplantilla\b",
        r"\bformulario\b",
    ),
    "formato_inspeccion": (
        r"\b(?:hoja|formato)\s+de\s+inspecci[oó]n\b",
        r"\binspecci[oó]n\s+para\s+(?:el|la|los|las)\b",
        r"\binspection\s+(?:form|sheet)\b",
    ),
    "lista_verificacion": (
        r"\blista\s+de\s+verificaci[oó]n\b",
        r"\bcheck\s*list\b",
        r"\bverification\s+checklist\b",
    ),
    "ficha_tecnica": (
        r"\bficha\s+t[eé]cnica\b",
        r"\btechnical\s+data\s+sheet\b",
        r"\bdatasheet\b",
        r"\bespecificaciones\s+t[eé]cnicas\b",
    ),
    "hoja_datos_seguridad": (
        r"\bhoja\s+(?:de\s+datos\s+)?de\s+seguridad\b",
        r"\b(?:material\s+)?safety\s+data\s+sheet\b",
    ),
    "catalogo_equipo": (r"\bcat[aá]logo\b", r"\bproduct\s+catalog\b"),
    "informe_tecnico": (
        r"\binforme\s+t[eé]cnico\b",
        r"\breporte\s+(?:de\s+)?(?:pruebas?|servicio|mantenimiento)\b",
        r"\btest\s+report\b",
        r"\bservice\s+work\s+report\b",
    ),
    "informe_inspeccion": (
        r"\b(?:informe|reporte)\s+de\s+inspecci[oó]n\b",
        r"\binspection\s+report\b",
    ),
    "informe_auditoria": (
        r"\b(?:informe|reporte)\s+de\s+auditor[ií]a\b",
        r"\baudit\s+report\b",
    ),
    "dossier_calidad": (
        r"\bdossier\s+de\s+calidad\b",
        r"\bquality\s+dossier\b",
    ),
    "reporte_anomalias": (
        r"\b(?:informe|reporte|levantamiento)\s+de\s+anomal[ií]as\b",
        r"\b(?:technical\s+)?findings?\s+report\b",
    ),
    "registro_fotografico": (
        r"\b(?:informe|reporte|registro)\s+fotogr[aá]fico\b",
        r"\bphotographic\s+(?:report|record)\b",
    ),
    "reporte_no_conformidad": (
        r"\b(?:informe|reporte)\s+de\s+no\s+conformidad\b",
        r"\bnon[-\s]?conform(?:ance|ity)\s+report\b",
        r"\bNCR\s+(?:report|reporte|no\.?|number)\b",
    ),
    "accion_correctiva_preventiva": (
        r"\bacci[oó]n\s*:\s*(?:correctiva|preventiva)\b",
        r"\bacci[oó]n\s+(?:correctiva|preventiva)\b",
        r"\bcorrective\s+(?:and\s+preventive\s+)?action\b",
    ),
    "reporte_resultados_pruebas": (
        r"\breporte\s+de\s+resultados\s+(?:de|del)\b",
        r"\bresultados\s+de\s+la\s+prueba\b",
        r"\btest\s+results?\s+report\b",
    ),
    "reporte_inventario_archivo": (
        r"\breporte\s+de\s+archivo\s*:",
        r"\bruta\s+relativa\s*:\s*[`.]",
        r"\btimestamp\s*\(UTC\)\s*:",
    ),
    "plano_diagrama": (
        r"\bplano(?:s)?\b",
        r"\bdiagrama\s+(?:unifilar|trifilar|esquem[aá]tico|de\s+control)\b",
        r"\b(?:single|three)[-\s]?line\s+diagram\b",
        r"\bwiring\s+diagram\b",
        r"\bdrawing\s+(?:number|no\.?|list)\b",
    ),
    "memoria_calculo": (
        r"\bmemoria\s+de\s+c[aá]lculo\b",
        r"\bcalculation\s+(?:report|note|sheet)\b",
        r"\bc[aá]lculo\s+(?:de\s+)?(?:cortocircuito|corto\s+circuito|coordinaci[oó]n|carga|conductores?)\b",
    ),
    "especificacion_tecnica": (
        r"\bespecificaci[oó]n(?:es)?\s+t[eé]cnica(?:s)?\b",
        r"\btechnical\s+specification(?:s)?\b",
        r"\brequerimientos?\s+t[eé]cnicos?\b",
        r"\bdata\s+requirement\s+sheet\b",
    ),
    "descripcion_tecnica_sistema": (
        r"\bdescripci[oó]n\s+general\s+(?:del?\s+)?sistema\b",
        r"\bdescripci[oó]n\s+funcional\s+(?:del?\s+)?sistema\b",
        r"\b(?:general|functional)\s+system\s+description\b",
    ),
    "protocolo_pruebas": (
        r"\bprotocolo\s+(?:de\s+)?pruebas?\b",
        r"\btest\s+protocol\b",
        r"\bhoja\s+de\s+pruebas?\b",
        r"\bregistros?\s+de\s+(?:mediciones|pruebas|resultados)\b",
        r"\bresultados?\s+de\s+pruebas?\b",
        r"\btest\s+results?\b",
    ),
    "registro_bitacora": (
        r"\bbit[aá]cora\b",
        r"\blog\s*book\b",
        r"\bregistro\s+(?:diario|de\s+actividades|de\s+mantenimiento)\b",
    ),
    "reporte_actividades": (
        r"\b(?:reporte|informe)\s+(?:diario\s+)?de\s+actividades\b",
        r"\breporte\s+diario\s+de\s+campo\b",
        r"\bdaily\s+activit(?:y|ies)\s+report\b",
        r"\bactivity\s+report\b",
    ),
    "control_metrologico": (
        r"\bbit[aá]cora\s+y\s+control\s+de\s+equipos\s+de\s+inspecci[oó]n[,]?\s+medici[oó]n\s+y\s+pruebas\b",
        r"\bcontrol\s+metrol[oó]gico\s+de\s+equipos\b",
        r"\b(?:programa|calendario)\s+de\s+calibraci[oó]n\s+de\s+equipos\b",
        r"\bcalibration\s+(?:equipment\s+)?(?:register|schedule)\b",
    ),
    "reporte_laboratorio": (
        r"\binforme\s+de\s+laboratorio\b",
        r"\breporte\s+de\s+laboratorio\b",
        r"\blaboratory\s+(?:test\s+)?report\b",
        r"\b(?:informe|reporte)\s+de\s+(?:an[aá]lisis|cromatograf[ií]a)\s+(?:de\s+)?(?:aceite|gases?)\b",
        r"\b(?:informe|reporte)\s+(?:dga|de\s+dga)\b",
        r"\b(?:dga|dissolved\s+gas\s+analysis)\s+(?:report|reporte|informe)\b",
    ),
    "informe_analisis": (
        r"\b(?:informe|reporte)\s+de\s+an[aá]lisis\b",
        r"\b(?:informe|reporte)\s+(?:de\s+)?trazabilidad\s+normativa\b",
        r"\ban[aá]lisis\s+t[eé]cnico\b",
        r"\bdiagnostic\s+report\b",
    ),
    "reporte_fat_sat": (
        r"\b(?:fat|sat)\s+(?:report|protocol|test|reporte|protocolo)\b",
        r"\b(?:reporte|informe)\s+(?:de\s+pruebas?\s+)?(?:fat|sat)\b",
        r"\bfactory\s+acceptance\s+test\b",
        r"\bsite\s+acceptance\s+test\b",
        r"\bsite\s+test\s+(?:and|&)\s+commissioning\s+report\b",
        r"\b(?:informe|reporte|protocolo)\s+(?:de\s+)?puesta\s+en\s+servicio\b",
        r"\bcommissioning\s+report\b",
    ),
    "certificado_calidad": (
        r"\bcertificado\s+de\s+(?:calidad|conformidad|inspecci[oó]n)\b",
        r"\bconstancia\s+de\s+aceptaci[oó]n\s+de\s+prototipo\b",
        r"\bcertificate\s+of\s+(?:quality|conformity|compliance|analysis)\b",
        r"\binspection\s+certificate\b",
        r"\babnahmepr(?:[uü]|ii)fzeugnis\b",
        r"\bmaterial\s+test\s+certificate\b",
    ),
    "constancia_capacitacion": (
        r"\bconstancia\s+de\s+(?:capacitaci[oó]n|competencias?|habilidades?)\b",
        r"\bformato\s+dc[-\s]?3\b",
        r"\bdc[-\s]?3\b",
        r"\btraining\s+certificate\b",
    ),
    "minuta_acta": (
        r"\bminuta\s+(?:de\s+)?(?:reuni[oó]n|trabajo)?\b",
        r"\bacta\s+(?:de\s+)?(?:reuni[oó]n|entrega|recepci[oó]n|cierre|inicio)\b",
        r"\bminutes\s+of\s+(?:meeting|session)\b",
    ),
    "contrato_legal": (
        r"\bcontrato\s+de\s+.{2,80}?\s+que\s+celebran\b",
        r"\bconvenio\s+.{0,80}?\s+que\s+celebran\b",
        r"\b(?:contract|agreement)\s+(?:number|no\.?|between)\b",
        r"\bcl[aá]usula(?:s)?\b",
    ),
    "cotizacion_propuesta": (
        r"\b(?:COT|CTZ)[-\s]+[A-Z0-9][A-Z0-9/-]{2,}\b",
        r"\b(?:n[o°.]?\s*(?:de\s+)?)?cotizaci[oó]n\s*[:#-]\s*"
        r"[A-Z0-9][A-Z0-9/-]{2,}\b",
        r"\bpropuesta\s+(?:t[eé]cnica|econ[oó]mica|t[eé]cnico[-\s]econ[oó]mica)\b",
        r"\b(?:quotation|commercial\s+offer|technical\s+proposal)\b",
    ),
    "compra_requisicion": (
        r"\borden\s+de\s+compra\b",
        r"\brequisici[oó]n\s+de\s+(?:compra|materiales?|servicios?)\b",
        r"\bpurchase\s+order\b",
        r"\bsolicitud\s+de\s+pedido\b",
    ),
    "factura_comprobante": (
        r"\bfactura\b",
        r"\bcomprobante\s+fiscal\b",
        r"\bCFDI\b",
        r"\btimbre\s+fiscal\s+digital\b",
        r"\binvoice\s+(?:number|no\.?|date)\b",
    ),
    "licitacion": (
        r"\bconvocatoria\s+(?:p[uú]blica|a\s+cuando\s+menos)\b",
        r"\bbases\s+de\s+(?:la\s+)?licitaci[oó]n\b",
        r"\bfallo\s+de\s+licitaci[oó]n\b",
        r"\btender\s+document\b",
    ),
    "programa_cronograma": (
        r"\bcronograma\b",
        r"\bprograma\s+de\s+(?:obra|trabajo|mantenimiento|pruebas)\b",
        r"\bplan\s+de\s+(?:trabajo|actividades)\b",
        r"\bwork\s+schedule\b",
    ),
    "programa_seguridad_salud": (
        r"\bprograma\s+de\s+(?:seguridad|seguridad\s+e\s+higiene|"
        r"seguridad\s+y\s+salud)\b",
        r"\bcomisi[oó]n\s+de\s+seguridad\s+e\s+higiene\b",
        r"\boccupational\s+(?:health\s+and\s+)?safety\s+program\b",
    ),
    "programa_gestion_ambiental": (
        r"\bprograma\s+(?:de\s+gesti[oó]n\s+)?ambiental\b",
        r"\baspecto\s+ambiental\b.{0,80}\bobjetivo\b.{0,40}\bmeta\b",
        r"\benvironmental\s+management\s+program\b",
    ),
    "lista_materiales": (
        r"\blista\s+de\s+materiales\b",
        r"\blista\s+de\s+(?:herramientas?|consumibles|accesorios)\b",
        r"\blistado\s+de\s+equipos\b",
        r"\baccesorios\s+y\s+consumibles\b",
        r"\bbill\s+of\s+materials?\b",
        r"\bsupply\s+list\b",
        r"\bequipment\s+list\b",
        r"\bBOM\b",
    ),
    "orden_trabajo": (
        r"\borden\s+de\s+(?:trabajo|servicio)\b",
        r"\bwork\s+order\b",
        r"\bservice\s+order\b",
    ),
    "correspondencia": (
        r"\bcorreo\s+electr[oó]nico\b",
        r"\bcarta\s+(?:externa|interna)\b",
        r"\bmemor[aá]ndum\b",
        r"\boficio\s+(?:n[uú]mero|no\.?)\b",
        r"\bemail\s+(?:from|subject)\b",
    ),
    "hoja_asignacion_proyecto": (
        r"\bhoja\s+de\s+asignaci[oó]n\s+de\s+proyecto\b",
        r"\bproject\s+assignment\s+sheet\b",
    ),
    "instruccion_cuenta_bancaria": (
        r"\bcarta\s+instrucci[oó]n\s+para\s+registro\s+de\s+cuenta\s+bancaria\b",
        r"\bregistro\s+de\s+cuenta\s+bancaria\b",
        r"\bformato\s+de\s+solicitud\s+de\s+pago\s+mediante\s+"
        r"transferencia\s+electr[oó]nica\s+bancaria\b",
    ),
    "comprobante_viaje": (
        r"\bgracias\s+por\s+elegir\s+Uber\b",
        r"\b(?:UberX|Uber\s+Comfort)\b.{0,120}\bkil[oó]metros\b",
        r"\btravel\s+ride\s+receipt\b",
    ),
    "plan_tecnico": (
        r"\bplan\s+de\s+(?:atenci[oó]n\s+y\s+respuesta\s+a\s+)?emergencias\b",
        r"\bplan\s+de\s+(?:seguridad|calidad|inspecci[oó]n\s+y\s+pruebas)\b",
        r"\b(?:emergency\s+response|inspection\s+and\s+test|quality)\s+plan\b",
    ),
    "registro_asistencia": (
        r"\b(?:control|lista|registro)\s+de\s+asistencia\b",
        r"\battendance\s+(?:record|register|sheet)\b",
    ),
    "registro_auditores": (
        r"\b(?:lista|registro)\s+de\s+auditores\s+internos\b",
        r"\binternal\s+auditor\s+(?:list|register)\b",
    ),
    "registro_entrega_epp": (
        r"\bentrega\s+de\s+EPP\b",
        r"\bentrega\s+de\s+equipo\s+de\s+protecci[oó]n\s+personal\b",
        r"\bPPE\s+issue\s+record\b",
    ),
    "registro_incidencias": (
        r"\bincidencias\s+y\s+novedades\b",
        r"\bregistro\s+de\s+incidencias\b",
        r"\bincident\s+(?:log|register)\b",
    ),
    "credencial_visitante": (
        r"\bcredencial\s+para\s+visitantes\b",
        r"\bvisitor\s+(?:pass|badge)\b",
    ),
    "viaticos_gastos": (
        r"\b(?:solicitud|formato|comprobaci[oó]n|relaci[oó]n)\s+de\s+vi[aá]ticos\b",
        r"\bsolicitud\s+de\s+gastos\b",
        r"\bcomprobaci[oó]n\s+de\s+gastos\b",
        r"\breembolso\s+de\s+(?:gastos|caja\s+chica)\b",
        r"\btravel\s+expenses?\b",
    ),
    "expediente_personal": (
        r"\bcurr[ií]culum\s+vitae\b",
        r"\bexpediente\s+(?:de\s+)?personal\b",
        r"\brecibo\s+de\s+n[oó]mina\b",
        r"\b(?:CURP|NSS)\b",
    ),
    "referencia_tecnica": (
        r"\breferencia\s+t[eé]cnica\b",
        r"\bart[ií]culo\s+t[eé]cnico\b",
        r"\bgu[ií]a\s+t[eé]cnica\b",
        r"\b(?:bolet[ií]n|nota)\s+t[eé]cnic[ao]\b",
        r"\btechnical\s+(?:reference|article|paper|guide|note)\b",
        r"\bwhite\s+paper\b",
    ),
}

_TOPIC_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "subestaciones": (r"\bsubestaci[oó]n(?:es)?\b", r"\bsubstation(?:s)?\b"),
    "transformadores": (r"\btransformador(?:es)?\b", r"\btransformer(?:s)?\b"),
    "interruptores_potencia": (
        r"\binterruptor(?:es)?\s+de\s+potencia\b",
        r"\bcircuit\s+breaker(?:s)?\b",
    ),
    "switchgear": (r"\bswitchgear\b", r"\btablero(?:s)?\s+de\s+media\s+tensi[oó]n\b"),
    "proteccion_control": (
        r"\bprotecci[oó]n\s+y\s+control\b",
        r"\bprotection\s+(?:and|&)\s+control\b",
        r"\brelevador(?:es)?\b",
        r"\brel[eé](?:s)?\s+de\s+protecci[oó]n\b",
        r"\bprotection\s+relays?\b",
        r"\b(?:differenzial|schutz)relais\b",
    ),
    "pruebas_electricas": (
        r"\bpruebas?\s+el[eé]ctricas?\b",
        r"\belectrical\s+test(?:ing)?\b",
    ),
    "puesta_tierra": (
        r"\bpuesta\s+a\s+tierra\b",
        r"\bsistema(?:s)?\s+de\s+tierras\b",
        r"\bgrounding\b",
        r"\bearth(?:ing)?\b",
    ),
    "aislamiento": (r"\baislamiento\b", r"\binsulation\b", r"\bdiel[eé]ctric[oa]\b"),
    "calidad_energia": (
        r"\bcalidad\s+de\s+(?:la\s+)?energ[ií]a\b",
        r"\bpower\s+quality\b",
    ),
    "seguridad_electrica": (
        r"\bseguridad\s+el[eé]ctrica\b",
        r"\barc\s+flash\b",
        r"\brel[aá]mpago\s+de\s+arco\b",
    ),
    "instrumentos_prueba": (
        r"\bequipo(?:s)?\s+de\s+prueba\b",
        r"\btest\s+(?:set|equipment|instrument)\b",
    ),
    "transformadores_instrumento": (
        r"\btransformador(?:es)?\s+de\s+(?:corriente|potencial|tensi[oó]n)\b",
        r"\binstrument\s+transformer(?:s)?\b",
        r"\b(?:current|voltage)\s+transformer(?:s)?\b",
    ),
    "cables_potencia": (
        r"\bcable(?:s)?\s+de\s+potencia\b",
        r"\bpower\s+cable(?:s)?\b",
        r"\bampacidad\b",
    ),
    "generadores_motores": (
        r"\b(?:generador|motor)(?:es)?\s+(?:el[eé]ctrico|s[ií]ncrono|de\s+inducci[oó]n)\b",
        r"\b(?:generator|motor)\s+(?:winding|stator|rotor)\b",
    ),
    "baterias_corriente_directa": (
        r"\bbanco(?:s)?\s+de\s+bater[ií]as\b",
        r"\bbattery\s+(?:bank|charger)\b",
        r"\bsistema\s+de\s+corriente\s+directa\b",
    ),
    "aceite_dga": (
        r"\baceite\s+(?:diel[eé]ctrico|aislante)\b",
        r"\b(?:dga|dissolved\s+gas\s+analysis)\b",
        r"\bcromatograf[ií]a\s+de\s+gases\b",
    ),
    "sf6": (
        r"\bSF6\b",
        r"\bhexafluoruro\s+de\s+azufre\b",
        r"\bsulphur\s+hexafluoride\b",
    ),
    "descargas_parciales": (
        r"\bdescargas?\s+parciales?\b",
        r"\bpartial\s+discharge(?:s)?\b",
    ),
    "termografia": (
        r"\btermograf[ií]a\b",
        r"\bthermal\s+imaging\b",
        r"\binfrarrojo(?:s)?\b",
    ),
    "automatizacion_scada": (
        r"\bSCADA\b",
        r"\bautomatizaci[oó]n\s+de\s+subestaciones\b",
        r"\bIEC\s*61850\b",
        r"\bRTU\b",
    ),
    "medicion_instrumentacion": (
        r"\bmedici[oó]n\s+(?:el[eé]ctrica|de\s+energ[ií]a)\b",
        r"\bmetering\b",
        r"\binstrumentaci[oó]n\b",
    ),
    "mantenimiento": (
        r"\bmantenimiento\s+(?:preventivo|predictivo|correctivo)\b",
        r"\bcondition[-\s]based\s+maintenance\b",
    ),
    "puesta_servicio": (
        r"\bpuesta\s+en\s+servicio\b",
        r"\bcomisionamiento\b",
        r"\bcommissioning\b",
    ),
    "hidroelectricas": (
        r"\bcentral\s+hidroel[eé]ctrica\b",
        r"\bhydroelectric\s+power\s+plant\b",
        r"\bturbina\s+hidr[aá]ulica\b",
    ),
    "seguridad_trabajo": (
        r"\bbloqueo\s+y\s+etiquetado\b",
        r"\bLOTO\b",
        r"\bequipo\s+de\s+protecci[oó]n\s+personal\b",
        r"\btrabajo\s+en\s+altura\b",
    ),
}

_EQUIPMENT_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "transformadores_potencia": (
        r"\btransformadores?\s+(?:y\s+autotransformadores?\s+)?de\s+potencia\b",
        r"\btransformadores?\s+y\s+autotransformadores?\s+de\s+distribucion\s+y\s+potencia\b",
        r"\bpower\s+(?:and\s+auto)?transformers?\b",
        r"\btransformer\s+(?:main\s+body|units?)\b",
    ),
    "transformadores_distribucion": (
        r"\btransformadores?\s+de\s+distribucion\b",
        r"\bdistribution\s+transformers?\b",
        r"\btransformadores?\s+(?:tipo\s+)?(?:poste|pedestal|sumergible)\b",
    ),
    "autotransformadores": (r"\bautotransformadores?\b", r"\bautotransformers?\b"),
    "reactores_potencia": (
        r"\breactores?\s+(?:de\s+potencia|en\s+derivacion|de\s+neutro)\b",
        r"\b(?:power|shunt|neutral)\s+reactors?\b",
    ),
    "boquillas": (
        r"\bboquillas?\s+(?:de\s+)?(?:alta\s+tension|para\s+transformadores?)\b",
        r"\btransformer\s+bushings?\b",
    ),
    "cambiadores_derivaciones": (
        r"\bcambiadores?\s+de\s+derivaciones?\b",
        r"\b(?:on[-\s]?load\s+)?tap[-\s]?changers?\b",
        r"\bOLTC\b",
    ),
    "interruptores_potencia": (
        r"\binterruptores?\s+de\s+potencia\b",
        r"\bhigh[-\s]?voltage\s+circuit\s+breakers?\b",
        r"\bcircuit\s+breakers?\b",
    ),
    "cuchillas_desconectadores": (
        r"\bcuchillas?\s+(?:desconectadoras?|seccionadoras?)\b",
        r"\bdesconectadores?\b",
        r"\bdisconnect(?:ing)?\s+switches?\b",
        r"\bdisconnectors?\b",
    ),
    "transformadores_corriente": (
        r"\btransformadores?\s+de\s+corriente\b",
        r"\bcurrent\s+transformers?\b",
    ),
    "transformadores_potencial": (
        r"\btransformadores?\s+de\s+(?:potencial|tension)\b",
        r"\b(?:voltage|potential)\s+transformers?\b",
        r"\bcapacitive\s+voltage\s+transformers?\b",
    ),
    "apartarrayos": (
        r"\bapartarrayos\b",
        r"\bsurge\s+arresters?\b",
        r"\bmetal[-\s]?oxide\s+arresters?\b",
    ),
    "bancos_capacitores": (
        r"\bbancos?\s+de\s+capacitores\b",
        r"\bcapacitor\s+banks?\b",
        r"\bunidades?\s+capacitivas?\b",
    ),
    "switchgear_media_tension": (
        r"\btableros?\s+(?:metalicos?\s+)?(?:blindados?\s+)?(?:de\s+)?media\s+tension\b",
        r"\bmedium[-\s]?voltage\s+switchgear\b",
        r"\bmetal[-\s]?clad\s+switchgear\b",
    ),
    "gis": (
        r"\bsubestaciones?\s+(?:blindadas?|encapsuladas?)\s+en\s+(?:gas|SF6)\b",
        r"\bgas[-\s]?insulated\s+(?:switchgear|substations?)\b",
        r"\bGIS\b",
    ),
    "cables_potencia": (r"\bcables?\s+de\s+potencia\b", r"\bpower\s+cables?\b"),
    "sistemas_tierra": (
        r"\bsistemas?\s+de\s+(?:puesta\s+a\s+)?tierra\b",
        r"\bmallas?\s+de\s+tierra\b",
        r"\bgrounding\s+(?:grid|system)\b",
        r"\bearthing\s+(?:grid|system)\b",
    ),
    "barras_bus": (
        r"\bbarras?\s+(?:colectoras?|principales?|de\s+transferencia)\b",
        r"\bbusbars?\b",
        r"\bbus\s+ducts?\b",
    ),
    "aisladores": (r"\baisladores?\b", r"\binsulators?\b"),
    "aceite_aislante": (
        r"\baceites?\s+(?:minerales?\s+)?aislantes?\b",
        r"\binsulating\s+(?:mineral\s+)?oils?\b",
        r"\binsulating\s+liquids?\b",
    ),
    "gas_sf6": (r"\bSF6\b", r"\bhexafluoruro\s+de\s+azufre\b"),
    "baterias_cargadores": (
        r"\bbancos?\s+de\s+baterias\b",
        r"\bcargadores?\s+de\s+baterias\b",
        r"\bbattery\s+(?:banks?|chargers?)\b",
    ),
    "proteccion_control": (
        r"\bproteccion,?\s+control\s+y\s+medicion\b",
        r"\bproteccion\s+y\s+control\b",
        r"\bprotection\s+(?:and|&)\s+control\b",
        r"\brelevadores?\s+de\s+proteccion\b",
    ),
    "equipo_primario_subestacion": (
        r"\bequipos?\s+primarios?\s+(?:de\s+)?subestaciones?\b",
        r"\bequipos?\s+electricos?\s+primarios?\b",
        r"\bsubstation\s+primary\s+equipment\b",
        r"\bSOM\s*[- ]\s*3531\b",
    ),
    "subestaciones": (r"\bsubestaciones?\b", r"\bsubstations?\b"),
}

_ACTIVITY_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "diseno_ingenieria": (
        r"\bdiseno\s+(?:electrico|de\s+subestaciones?)\b",
        r"\bingenieria\s+de\s+detalle\b",
        r"\bdesign\s+(?:requirements?|criteria)\b",
    ),
    "construccion_montaje": (
        r"\bconstruccion\s+de\s+subestaciones?\b",
        r"\bmontaje\s+(?:electromecanico|de\s+equipos?)\b",
        r"\berection\s+and\s+installation\b",
    ),
    "pruebas_fabrica": (
        r"\bpruebas?\s+(?:de\s+)?(?:fabrica|rutina|prototipo|tipo)\b",
        r"\bfactory\s+(?:acceptance\s+)?tests?\b",
        r"\bFAT\b",
    ),
    "pruebas_campo": (
        r"\bpruebas?\s+(?:de\s+)?campo\b",
        r"\bfield\s+(?:testing|tests?)\b",
        r"\bpruebas?\s+locales?\s+de\s+recepcion\b",
        r"\bSOM\s*[- ]\s*3531\b",
    ),
    "recepcion_aceptacion": (
        r"\brecepcion\s+(?:y\s+puesta\s+en\s+servicio|de\s+equipos?)\b",
        r"\bpruebas?\s+de\s+aceptacion\b",
        r"\bacceptance\s+(?:testing|tests?)\b",
        r"\bSAT\b",
    ),
    "puesta_punto": (r"\bpuesta\s+a\s+punto\b", r"\bpre[-\s]?commissioning\b"),
    "puesta_servicio": (
        r"\bpuesta\s+en\s+servicio\b",
        r"\bcomisionamiento\b",
        r"\bcommissioning\b",
    ),
    "mantenimiento": (
        r"\bmantenimiento(?:s)?\b",
        r"\bmaintenance\b",
        r"\bcondition[-\s]?based\s+maintenance\b",
    ),
    "diagnostico_condicion": (
        r"\bdiagnostico\b",
        r"\bevaluacion\s+de\s+(?:la\s+)?condicion\b",
        r"\bcondition\s+assessment\b",
        r"\bdiagnostic\s+(?:field\s+)?testing\b",
    ),
    "monitoreo_condicion": (
        r"\bmonitoreo\s+en\s+linea\b",
        r"\bcondition\s+monitoring\b",
        r"\bonline\s+monitoring\b",
    ),
    "muestreo_laboratorio": (
        r"\bmuestreo\b",
        r"\banalisis\s+de\s+(?:gases|aceite|liquidos?)\b",
        r"\blaboratory\s+analysis\b",
        r"\bdissolved\s+gas\s+analysis\b",
    ),
    "operacion": (
        r"\boperacion\s+de\s+(?:equipos?|subestaciones?)\b",
        r"\boperation\b",
    ),
    "reparacion": (r"\breparacion\b", r"\brepair\b", r"\boverhaul\b"),
    "transporte_almacenamiento": (
        r"\b(?:embarque|transporte|almacenamiento)\b",
        r"\bshipping,?\s+transport(?:ation)?\s+and\s+storage\b",
        r"\bpacking\s+lists?\b",
        r"\bshipment\s+(?:no|number|type)\b",
    ),
    "seguridad": (
        r"\bseguridad\s+(?:electrica|industrial)\b",
        r"\barc\s+flash\b",
        r"\blockout[/\s-]*tagout\b",
    ),
    "proteccion_ambiental": (
        r"\bproteccion\s+ambiental\b",
        r"\bcontingencia\s+ambiental\b",
        r"\benvironmental\s+protection\b",
    ),
}

_DOCUMENT_SUBTYPE_PATTERNS: Mapping[str, tuple[str, ...]] = {
    "metodo_prueba": (
        r"\bmetodos?\s+de\s+prueba\b",
        r"\bstandard\s+test\s+method\b",
        r"\btest\s+method\b",
    ),
    "practica_recomendada": (
        r"\bpractica\s+recomendada\b",
        r"\brecommended\s+practice\b",
    ),
    "guia": (r"\bguia\b", r"\bguide\s+for\b"),
    "codigo": (
        r"\bcodigo\s+(?:de\s+red|electrico|nacional|de\s+seguridad|"
        r"de\s+instalaciones)\b",
        r"\b(?:national\s+electrical|electrical|safety)\s+code\b",
        r"\bcode\s+for\b",
    ),
    "especificacion": (
        r"\bespecificacion\s+CFE\b",
        r"\btechnical\s+specification\b",
        r"\btesting\s+specifications?\b",
    ),
    "regulacion_obligatoria": (
        r"\bnorma\s+oficial\s+mexicana\b",
        r"\bregulation\b",
    ),
    "manual_mantenimiento": (
        r"\bmanual\s+de\s+mantenimiento\b",
        r"\bmaintenance\s+manual\b",
    ),
    "manual_operacion": (r"\bmanual\s+de\s+operacion\b", r"\boperation\s+manual\b"),
    "manual_usuario": (r"\bmanual\s+(?:de\s+)?usuario\b", r"\buser\s+manual\b"),
    "manual_servicio": (r"\bmanual\s+de\s+servicio\b", r"\bservice\s+manual\b"),
    "manual_instalacion": (
        r"\bmanual\s+de\s+instalacion\b",
        r"\binstallation\s+manual\b",
    ),
    "procedimiento_pruebas": (
        r"\bprocedimientos?\s+de\s+pruebas?\b",
        r"\bmanual\s+de\s+procedimientos?\s+de\s+pruebas?\b",
        r"\btest\s+procedure\b",
    ),
    "procedimiento_mantenimiento": (r"\bprocedimiento\s+de\s+mantenimiento\b",),
    "procedimiento_puesta_servicio": (
        r"\bprocedimiento\b.{0,100}\bpuesta\s+en\s+servicio\b",
        r"\bpuesta\s+a\s+punto\s+y\s+puesta\s+en\s+servicio\b",
    ),
    "procedimiento_recepcion": (r"\bprocedimiento\b.{0,100}\brecepcion\b",),
    "procedimiento_seguridad": (r"\bprocedimiento\b.{0,100}\bseguridad\b",),
    "procedimiento_ambiental": (r"\bprocedimiento\s+de\s+proteccion\s+ambiental\b",),
}

_EQUIPMENT_SPECIFICITY = {
    "transformadores_potencia": frozenset({"autotransformadores", "subestaciones"}),
    "transformadores_distribucion": frozenset({"subestaciones"}),
    "interruptores_potencia": frozenset({"subestaciones", "switchgear_media_tension"}),
    "transformadores_corriente": frozenset({"subestaciones"}),
    "transformadores_potencial": frozenset({"subestaciones"}),
    "gis": frozenset({"subestaciones", "switchgear_media_tension", "gas_sf6"}),
    "equipo_primario_subestacion": frozenset({"subestaciones"}),
}


def builtin_taxonomy() -> TechnicalTaxonomy:
    return TechnicalTaxonomy(
        signature=BUILTIN_TAXONOMY_VERSION,
        authorities=_BUILTIN_AUTHORITIES,
        organizations=_BUILTIN_ORGANIZATIONS,
        clients=_BUILTIN_CLIENTS,
        projects=_BUILTIN_PROJECTS,
    )


def semantic_label_inventory() -> Mapping[str, tuple[str, ...]]:
    """Expose deterministic labels to the shared ontology without regex coupling."""

    return {
        "document_kind": tuple(_KIND_PATTERNS),
        "topic": tuple(_TOPIC_PATTERNS),
        "equipment": tuple(_EQUIPMENT_PATTERNS),
        "activity": tuple(_ACTIVITY_PATTERNS),
    }
# endregion [02]


_preserve_legacy_module(globals(), "_04_Nucleo_Operativo.document_taxonomy_vocabulary")
