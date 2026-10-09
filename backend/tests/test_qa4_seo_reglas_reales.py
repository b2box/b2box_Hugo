"""QA independiente de HG1: las reglas contra títulos REALES de https://b2box.app (catálogo público, 09-oct-2026).

Los nombres y slugs de abajo son literales del listado público `/ar/api/latest-products` (1.077 productos, bajados
completos para medir). Cada caso trae el id público para poder buscarlo.

  * Parte 1: acuerdo con la clasificación manual del diseño (filas 21, 30-34, 36, 37, 38-40) sobre los títulos vivos.
  * Parte 2: falsos positivos y falsos negativos encontrados. Son `xfail(strict=True)`: el día que se ajusten las listas
    pasan a XPASS, fallan y obligan a sacar la marca.

Cifras sobre los 1.077 títulos vivos con las listas de fábrica: LARGO 286 (26,6 %), RELLENO 94, COD 10, MAR 7,
SLUG_NO_COINCIDE 49, DUP_EXACTO 4 (2 pares), DUP_CASI 60, ESPACIOS 7, NOMBRE_ES_CODIGO 1.
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.seo import text_rules as R  # noqa: E402
from app.seo.lists import TextLists  # noqa: E402

L = TextLists()


def audit(name: str, slug: str = "", desc: str = "") -> dict[str, str]:
    return R.audit_translation(name, slug or R.fold(name).replace(" ", "-"), desc, product_code=None, lists=L)


# ─── Parte 1: acuerdo con el diseño sobre los títulos vivos ────────

AGREEMENT = [
    # (id, nombre vivo, slug vivo, regla esperada, fila del diseño)
    ("163", " Juego de pinza perforadora y broches metálicos – Kit completo para costura y DIY",
     "kit-de-instalacion-de-ojales-con-alicates-y-organizador", "ESPACIOS", 21),
    ("1275", "Auto de Carrera a Control Remoto Drift C64 Escala 176", "auto-de-carrera-a-control-remoto-drift-c64-escala-176", "COD", 30),
    ("4443", "Cámara Térmica Profesional H6S Guide para Inspecciones", "camara-termica-profesional-h6s-guide-para-inspecciones", "COD", 31),
    ("4442", "Protector Pantalla Vidrio Templado SG con Aplicador Fácil", "protector-pantalla-vidrio-templado-sg-con-aplicador-facil", "COD", 32),
    ("1085", "Bolsas Urinarias Descartables Unisex x4u Viajes y Emergencias", "bolsas-urinarias-descartables-unisex-x4u-viajes-y-emergencias", "COD", 33),
    ("2337", "Globo Metálico 98*56cm  Torre de Pelotas de Fútbol para Fiestas ", "globo-metalico-9856cm-torre-de-pelotas-de-futbol-para-fiestas", "COD", 34),
    ("609", "BX00717", "bx00717-609", "NOMBRE_ES_CODIGO", 36),
    ("94", "Botella Plegable de Silicona Flexible Gris", "colador-plegable-de-silicona-rojo-versatilidad-para-tu-cocina", "SLUG_NO_COINCIDE", 37),
    ("1404", "Handy de Juguete para Niños Kuromi Walkie Talkie Divertido", "handy-de-juguete-para-ninos-kuromi-walkie-talkie-divertido", "MAR", 38),
    ("809", "Mangas Elásticas Let's Slim: Protección Solar y Comodidad sin Límites",
     "mangas-elasticas-lets-slim-proteccion-solar-y-comodidad-sin-limites", "MAR", 39),
    ("1283", "Funda iPhone Efecto Líquido Transparente para Smartphone", "funda-iphone-efecto-liquido-transparente-para-smartphone", "MAR", 40),
    ("4443", "Cámara Térmica Profesional H6S Guide para Inspecciones", "camara-termica-profesional-h6s-guide-para-inspecciones", "MAR", 31),
]


@pytest.mark.parametrize("pid,name,slug,rule,row", AGREEMENT, ids=[f"{a[0]}-{a[3]}" for a in AGREEMENT])
def test_los_12_casos_del_diseno_se_marcan_con_la_regla_esperada_sobre_el_titulo_vivo(pid, name, slug, rule, row):
    assert rule in audit(name, slug), f"fila {row} del diseño (id {pid}): se esperaba {rule}"


def test_el_detalle_de_los_casos_del_diseno_dice_que_se_encontro():
    a = audit("Auto de Carrera a Control Remoto Drift C64 Escala 176")
    assert "C64" in a["COD"] and "escala 176" in a["COD"]
    assert "x4u" in audit("Bolsas Urinarias Descartables Unisex x4u Viajes y Emergencias")["COD"]
    assert "98*56cm" in audit("Globo Metálico 98*56cm  Torre de Pelotas de Fútbol para Fiestas ")["COD"]
    assert "Let's Slim" in audit("Mangas Elásticas Let's Slim: Protección Solar")["MAR"]


def test_duplicados_exactos_vivos_los_dos_pares_y_solo_esos():
    live = [
        ("a|es_AR", "3217", "Organizador de Basura para Fregadero de Cocina"),
        ("b|es_AR", "3204", "Organizador de Basura para Fregadero de Cocina"),
        ("c|es_AR", "1471", "Organizador Ropero Plegable de Tela con Múltiples Estantes"),
        ("d|es_AR", "1613", "Organizador Ropero Plegable de Tela con Múltiples Estantes"),
        ("e|es_AR", "968", "Mopa Giratoria con Balde Centrifugado Mango Extensible"),
    ]
    dups = R.find_duplicates(live)
    assert {k for k, v in dups.items() if "DUP_EXACTO" in v} == {"a|es_AR", "b|es_AR", "c|es_AR", "d|es_AR"}
    assert "e|es_AR" not in dups


def test_productos_que_solo_difieren_en_tamano_o_color_se_marcan_como_casi_iguales():
    sizes = [(f"{pid}|es_AR", pid, f"Manta con Diseño de Cancha de Fútbol {size}")
             for pid, size in (("2362", "100x130cm"), ("2363", "130x150cm"), ("2364", "135x200cm"), ("2365", "150x200cm"))]
    d = R.find_duplicates(sizes)
    assert set(d) == {k for k, *_ in sizes} and all("DUP_CASI" in v for v in d.values())
    color = [("771|es_AR", "771", "Set de Organizadores de Equipaje Plegables para Viajes"),
             ("1513|es_AR", "1513", "Set de Organizadores de Equipaje Rosa para Viajes")]
    assert all("DUP_CASI" in v for v in R.find_duplicates(color).values())


def test_titulos_distintos_con_las_palabras_en_comun_de_siempre_no_se_marcan():
    other = [("1|es_AR", "1", "Mopa Giratoria con Balde Centrifugado Mango Extensible"),
             ("2|es_AR", "2", "Estante Organizador Multifunción para Cocina Acero Inoxidable"),
             ("3|es_AR", "3", "Picador de Hielo Eléctrico 200W Uso Intensivo"),
             ("4|es_AR", "4", "Linterna LED COB Recargable con Mosquetón y Abrebotellas")]
    assert R.find_duplicates(other) == {}


def test_el_largo_vivo_mide_lo_que_dice_el_diseno_con_el_tope_de_60():
    assert "LARGO" in audit("Cajonera Plástica de Escritorio - 3, 4, 6 o 9 Compartimientos para una Organización Perfecta")
    assert "LARGO" not in audit("Mopa Giratoria con Balde Centrifugado Mango Extensible")        # 54


@pytest.mark.parametrize("name", [
    "Linterna LED COB Recargable con Mosquetón y Abrebotellas", "Lámpara de Mesa de Arena con Fluidez LED RGB Decorativa",
    "Ventilador doble para auto con carga USB y conector 12V", "Cabina Mini LED UV Portátil para Uñas Semipermanentes Secado Rápido",
    "Anteojos Deportivos Urbanos con Lentes Espejados UV400", "Visera Protectora Solar Plegable UPF50 para Mujer",
    "Cuadro decorativo 3D estilo Caja Diorama Arte en Profundidad", "Sillón Puff Corduroy XL Gris Confort y Estilo para Tu Espacio",
    "Secador de Uñas LED/UV con Temporizador Inteligente", "Balanza de Bioimpedancia Digital Blanca con Pantalla LCD",
    "Cartera de hombro minimalista de cuero PU con correa ajustable", "Kit Limpiador Teclado Multifunción para PC y Monitor",
    "Arnés para Mascota de PVC, Seguridad y Estilo", "Aspiradora de Mano Portátil 16000PA Batería Recargable",
    "Picador de Hielo Eléctrico 200W Uso Intensivo", "Rollo de Papel Térmico Blanco para Cámara de Niños Pack x10",
    "Gazebo Plegable 2x2m con Altura Ajustable", "Botellón Plegable con Grifo Dispensador - Capacidad 5L",
    "Tijeras eléctricas de precisión para DIY y manualidades", "Adaptador USB a Tipo C Lightning con Llavero Portátil",
])
def test_datos_tecnicos_reales_no_son_codigos(name):
    assert "COD" not in audit(name)


@pytest.mark.parametrize("name", [
    "Mopa Giratoria con Balde Centrifugado Mango Extensible", "Arenero Inteligente Gatos Autolimpiante con Control App Automático",
    "Estante Organizador Multifunción para Cocina Acero Inoxidable", "Caja Organizadora de Herramientas Apilable con Bandejas",
    "Enrollador de Manguera Compacto con Pico Multichorro", "Cortador de Verduras Multifuncional con Diseño Ergonómico",
    "Saca Pelusa de Bolsillo – Rodillo Portátil para Ropa", "Masajeador Facial y Cuello Antiedad Terapia LED",
])
def test_los_titulos_ok_de_la_muestra_del_diseno_no_saltan_ninguna_regla_de_titulo(name):
    got = set(audit(name)) - {"SIN_DESCRIPCION"}
    assert got <= {"LARGO"}, got


# ─── Parte 2: falsos positivos (xfail estricto) ────────────────────

_FP_COD = [
    ("IA", "Impresora Térmica Infantil Portátil con IA- Imprime tus Dibujos", "IA = inteligencia artificial (real, id 5037)"),
    ("USBC", "Depiladora Facial Portátil Indolora con Carga USBC", "USBC = USB-C sin guion (real, id 1573)"),
    ("VESA", "Soporte de TV Articulado Reforzado VESA", "VESA = estándar de montaje (real, id 1576)"),
    ("SK5", "Cuchillo de Cocina Acero SK5 Profesional", "SK5 = tipo de acero"),
    ("V8", "Aspiradora Inalámbrica V8 Potente", "V8 = modelo/serie de aspiradora"),
    ("DPI", "Mouse Gamer RGB 7200 DPI", "DPI"),
    ("PIR", "Sensor PIR Pared", "PIR = sensor infrarrojo"),
    ("HSS", "Mecha HSS 6mm", "HSS = acero rápido (Ferretería)"),
    ("SDS", "Taladro SDS 800W", "SDS = portabrocas (Ferretería)"),
    ("MDF", "Mueble Organizador MDF Blanco", "MDF = material (Bazar/Hogar)"),
    ("BPA", "Botella Libre de BPA 500ml", "BPA (Bazar)"),
    ("FDA", "Tupper Apto FDA Hermético", "FDA (Bazar)"),
    ("BBQ", "Set BBQ Parrilla Acero", "BBQ (Bazar)"),
    ("ISO", "Cinta ISO Aislante", "ISO"),
    ("TPR", "Bolsa TPR Silicona", "TPR (material)"),
    ("BMX", "Bicicleta BMX Rodado 20", "BMX"),
    ("PD", "Cargador Rápido PD 20W", "PD = Power Delivery"),
    ("RC", "Auto RC 4WD Drift", "RC = radio control (Regalería)"),
    ("SPA", "Kit SPA Facial", "SPA (Regalería)"),
    ("DC", "Bomba de Agua 12V DC", "DC = corriente continua"),
]


@pytest.mark.parametrize("label,name,why", _FP_COD, ids=[x[0] for x in _FP_COD])
@pytest.mark.xfail(strict=True, reason="Falso positivo de COD: sigla técnica legítima que falta en DEFAULT_TECHNICAL / TECH_PATTERN")
def test_sigla_tecnica_legitima_no_es_un_codigo_de_proveedor(label, name, why):
    assert "COD" not in audit(name), why


@pytest.mark.xfail(strict=True, reason="Falso positivo de RELLENO: «Elegante» es un adjetivo de producto en «Traje Elegante» (id 1095)")
def test_traje_elegante_es_una_prenda_no_relleno():
    assert "RELLENO" not in audit("Bolso de Viaje Plegable con Compartimento para Traje Elegante")


@pytest.mark.xfail(strict=True, reason=(
    "Falso positivo de DUP_CASI: tres juguetes distintos (ids 4909, 4570, 4415) comparten 3 de 5 palabras genéricas "
    "(juguete, interactivo, mascotas) y llegan justo al umbral de 0,6"))
def test_juguetes_distintos_para_mascotas_no_son_casi_duplicados():
    names = [("4909|es_AR", "4909", "Juguete Interactivo Tambaleante Para Mascotas"),
             ("4570|es_AR", "4570", "Juguete Interactivo de Colores para Mascotas"),
             ("4415|es_AR", "4415", "Pez Juguete Interactivo para Mascotas")]
    assert R.find_duplicates(names) == {}


@pytest.mark.xfail(strict=True, reason=(
    "Falso positivo de FAB: la medida «25x30cm» (dimensión) está en supplierSizeModel Y en el título, de forma legítima; "
    "_MEASURE_RE solo reconoce una medida simple («25cm»), no «25x30cm» ni «5V2A»"))
def test_una_medida_compuesta_en_el_modelo_del_proveedor_no_delata_al_proveedor():
    m = R.SupplierMatcher(R.SupplierRefs(size_model="25x30cm"), frozenset())
    assert R.check_proveedor("Bolsa de Tela 25x30cm", "bolsa-de-tela", "", m) is None


# ─── Parte 2: falsos negativos (xfail estricto) ────────────────────

@pytest.mark.parametrize("pid,name,brand", [
    ("4422", "Conservadora al Vacío Inteligente everyU", "everyU (marca china, id 4422)"),
    ("649", "Masajeador Íntimo Rosen bienestar y placer en tus manos", "Rosen (marca, id 649)"),
    ("2206", "Juguete Musical Otamatone Melodía Lúdica Interactivo", "Otamatone (marca registrada, id 2206)"),
    ("58", "Estante Organizador B2BOX para Baño - Practicidad y Orden sin Esfuerzo", "B2BOX: nombre propio en el título (id 58)"),
    (None, "Funda iPhone15 Transparente", "iPhone pegado al número"),
    (None, "Mangas Lets Slim Protección Solar", "Let's Slim sin apóstrofo"),
])
@pytest.mark.xfail(strict=True, reason="Falso negativo de MAR: marca real que no está en DEFAULT_BRANDS (o variante ortográfica)")
def test_marca_real_en_el_titulo_se_detecta(pid, name, brand):
    assert "MAR" in audit(name), brand


@pytest.mark.parametrize("pid,name", [
    ("4573", "Cepillo de baño para mascotas práctico y cómodo"),
    ("61", "Árbol Organizador de Joyas - Estilo y Practicidad para su Espacio"),
    ("820", "Almohadilla Inflable para Lavacabezas: Comodidad Sin Igual en Cada Lavado"),
    ("639", "Rizador de Pestañas Eléctrico Térmico Mirada Cautivadora"),
    ("33", "Organizador de Brochas 360°: Elegancia y Orden para tu Tocador"),
    ("67", "Cajón Oculto Bajo Escritorio - Solución Práctica y Discreta para Organizar"),
])
@pytest.mark.xfail(strict=True, reason=(
    "Falso negativo de RELLENO: práctico/a, practicidad, comodidad, elegancia, «sin igual», solución, cautivadora, "
    "impactante, novedoso no están en DEFAULT_FILLER (≈40 títulos vivos más)"))
def test_relleno_de_marketing_real_se_detecta(pid, name):
    assert "RELLENO" in audit(name)


# ─── Correctitud del filtro por prefijo de DUP_CASI contra la fuerza bruta ──

def _brute_force_dup_casi(entries):
    toks = {rk: frozenset(R.name_tokens(name)) for rk, _pid, name in entries}
    pid = {rk: p for rk, p, _ in entries}
    out: dict[str, set[str]] = {}
    keys = [rk for rk, _p, name in entries if len(toks[rk]) >= R.DUP_CASI_MIN_TOKENS]
    exact = {}
    for rk, _p, name in entries:
        exact.setdefault(R._name_key(name), []).append(rk)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if pid[a] == pid[b]:
                continue
            same_exact = len(R._name_key(next(n for k, _p, n in entries if k == a)) or "") >= 4 and \
                R._name_key(next(n for k, _p, n in entries if k == a)) == R._name_key(next(n for k, _p, n in entries if k == b))
            if same_exact:
                continue
            inter, union = len(toks[a] & toks[b]), len(toks[a] | toks[b])
            if union and inter / union >= R.DUP_CASI_JACCARD:
                out.setdefault(a, set()).add(b)
                out.setdefault(b, set()).add(a)
    return out


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_el_filtro_por_prefijo_de_DUP_CASI_encuentra_exactamente_lo_mismo_que_la_fuerza_bruta(seed):
    import random

    rnd = random.Random(seed)
    vocab = ("organizador huevos heladera plegable cocina mini ventilador usb portátil recargable mopa balde "
             "silicona vidrio acero baño adhesivo pared colgante transparente set kit giratorio apilable "
             "negro rosa 2 3 360 con de para y el").split()
    entries = []
    for i in range(350):
        words = [rnd.choice(vocab) for _ in range(rnd.randint(2, 9))]
        entries.append((f"{i}|es_AR", str(i), " ".join(words).title()))
    got = R.find_duplicates(entries)
    flagged = {rk for rk, v in got.items() if "DUP_CASI" in v}
    want = _brute_force_dup_casi(entries)
    assert flagged == set(want), (sorted(flagged ^ set(want))[:10])
    assert all("parecido al producto" in v["DUP_CASI"] for v in got.values() if "DUP_CASI" in v)
