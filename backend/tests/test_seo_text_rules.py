"""HG1: reglas de texto sin IA, contra los casos reales de la sección 2.3 del diseño
(/_notas-b2box/seo-palabras-diseno.md) y contra bordes que el diseño no cubre."""

from __future__ import annotations

import itertools
import os
import random

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402

from app.seo import lists as seo_lists  # noqa: E402
from app.seo import text_rules as r  # noqa: E402
from app.seo.lists import TextLists  # noqa: E402

LISTS = TextLists()


def audit(name, slug="", description="", **kw):
    return r.audit_translation(name, slug, description, product_code=kw.pop("product_code", None),
                               lists=kw.pop("lists", LISTS), matcher=kw.pop("matcher", None))


def rules_of(name, **kw):
    return set(audit(name, **kw))


# ─── Los 40 de la sección 2.3 (id de la fila → título tal cual) ────

OK_ROWS = {
    1: "Mopa Giratoria con Balde Centrifugado Mango Extensible",
    2: "Arenero Inteligente Gatos Autolimpiante con Control App Automático",
    3: "Estante Organizador Multifunción para Cocina Acero Inoxidable",
    4: "Picador de Hielo Eléctrico 200W Uso Intensivo",
    6: "Caja Organizadora de Herramientas Apilable con Bandejas",
    7: "Enrollador de Manguera Compacto con Pico Multichorro",
    8: "Linterna LED COB Recargable con Mosquetón y Abrebotellas",
    9: "Masajeador Facial y Cuello Antiedad Terapia LED",
    12: "Organizador Colgante para Pileta con Desagüe",
    13: "Saca Pelusa de Bolsillo – Rodillo Portátil para Ropa",
    14: "Organizador de Huevos Automático Transparente para Heladera",
    15: "Cortador de Verduras Multifuncional con Diseño Ergonómico",
}

# Las 12 filas de acuerdo del criterio de aceptación de HG1: (fila, título, regla esperada)
AGREEMENT = [
    (21, " Juego de pinza perforadora y broches metálicos – Kit completo para costura y DIY", "ESPACIOS"),
    (30, "Auto de Carrera a Control Remoto Drift C64 Escala 176", "COD"),
    (31, "Cámara Térmica Profesional H6S Guide para Inspecciones", "COD"),
    (32, "Protector Pantalla Vidrio Templado SG con Aplicador Fácil", "COD"),
    (33, "Bolsas Urinarias Descartables Unisex x4u Viajes y Emergencias", "COD"),
    (34, "Globo Metálico 98*56cm  Torre de Pelotas de Fútbol para Fiestas", "COD"),
    (36, "BX00717", "NOMBRE_ES_CODIGO"),
    (37, "Botella Plegable de Silicona Flexible Gris", "SLUG_NO_COINCIDE"),
    (38, "Handy de Juguete para Niños Kuromi Walkie Talkie Divertido", "MAR"),
    (39, "Mangas Elásticas Let's Slim: Protección Solar y Comodidad sin Límites", "MAR"),
    (40, "Funda iPhone Efecto Líquido Transparente para Smartphone", "MAR"),
]
SLUG_OF = {
    36: "bx00717-609",
    37: "colador-plegable-de-silicona-rojo-para-cocina",
}


@pytest.mark.parametrize("row,title,expected", AGREEMENT, ids=[f"fila{a[0]}-{a[2]}" for a in AGREEMENT])
def test_acuerdo_con_la_clasificacion_manual(row, title, expected):
    got = audit(title, slug=SLUG_OF.get(row, ""), description="x" * 200)
    assert expected in got, f"fila {row}: esperaba {expected}, salió {sorted(got)}"


def test_fila_31_tambien_marca_la_marca_del_fabricante_guide():
    got = audit(AGREEMENT[2][1])
    assert "COD" in got and "MAR" in got and "Guide" in got["MAR"]


def test_fila_34_marca_asterisco_y_espacio_doble():
    got = audit(AGREEMENT[5][1])
    assert "98*56cm" in got["COD"]
    assert "ESPACIOS" in got


def test_fila_30_la_escala_mal_copiada_se_explica():
    got = audit("Auto de Carrera a Control Remoto Drift C64 Escala 176")
    assert "C64" in got["COD"] and "1:76" in got["COD"]


def test_fila_36_el_nombre_es_el_codigo_y_no_se_marca_ni_cod_ni_slug():
    got = audit("BX00717", slug="bx00717-609", description="B2BOX")
    assert "NOMBRE_ES_CODIGO" in got
    assert "COD" not in got and "SLUG_NO_COINCIDE" not in got
    assert "SIN_DESCRIPCION" in got   # la descripción «B2BOX» no es una descripción


@pytest.mark.parametrize("row,title", sorted(OK_ROWS.items()))
def test_las_filas_ok_no_disparan_las_reglas_de_formato(row, title):
    got = audit(title, slug=r.fold(title).replace(" ", "-"), description="x" * 100)
    # LARGO es otra cosa: la fila 2 mide 66 y la clasificación manual igual la da por buena.
    assert not (set(got) & {"COD", "MAR", "FAB", "RELLENO", "ESPACIOS", "SLUG_NO_COINCIDE",
                            "NOMBRE_ES_CODIGO"}), f"fila {row}: {sorted(got)}"


def test_fila_5_es_larga_y_nada_mas():
    got = audit("Tabla de Cocina Redonda Antibacterial con Soporte Desmontable Flexible", slug="tabla-de-cocina-redonda-antibacterial-con-soporte-desmontable-flexible", description="x" * 100)
    assert set(got) == {"LARGO"}
    assert got["LARGO"].startswith("70 caracteres")


# ─── LARGO ─────────────────────────────────────────────────────────

def test_largo_bordes():
    assert r.check_largo("a" * 60) is None
    assert r.check_largo("a" * 61) == "61 caracteres (tope 60)"
    assert r.check_largo(" " + "a" * 60 + " ") is None                 # los bordes no suman (eso lo dice ESPACIOS)
    assert r.check_largo("a" * 29 + "      " + "b" * 30) is None       # 60 reales: los espacios de más no suman


# ─── RELLENO ───────────────────────────────────────────────────────

RELLENO_ROWS = {
    23: "Sillón Reclinable Hinchable: Tu Oasis de Confort en Cualquier Lugar",
    24: "Mochila versátil con estilo clásico o moderno según tu vibe",
    25: "Lámpara LED Cristal Aura: Iluminá tus Espacios con Magia",
    26: "Organizador de pared con cajón transparente, sin perforaciones y súper práctico",
    27: "Elegante Reloj Estilo Deportivo con Esfera de Llanta y Caliper",
    28: "Vaso Mágico para Granizados Disfrutá Bebidas Congeladas al instante",
    29: "Humificador Difusor de Aroma con Efecto Llama Ambiente Relajante",
}


@pytest.mark.parametrize("row,title", sorted(RELLENO_ROWS.items()))
def test_relleno_filas_rel(row, title):
    assert "RELLENO" in rules_of(title)


def test_relleno_no_salta_en_titulos_sobrios():
    for title in OK_ROWS.values():
        assert "RELLENO" not in rules_of(title), title


def test_relleno_es_por_palabra_entera_y_sin_tildes():
    assert "RELLENO" in rules_of("Taza SUPER resistente")
    assert "RELLENO" not in rules_of("Supermercado Carrito de Compras")     # «super» dentro de otra palabra
    assert "RELLENO" in rules_of("Lámpara que te hace DISFRUTA")             # sin tilde, mayúsculas
    assert "RELLENO" in rules_of("Guantes Perfectos")                         # plural


# ─── COD ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Linterna LED COB Recargable", "Lámpara 3D Luna", "Anteojos UV400 Polarizados",
    "Cámara IP67 Resistente al Agua", "Hoja A4 Papel Fotográfico", "Foco E27 Cálido",
    "Tornillo M8 Acero", "Barbijo N95 Pack", "Cable USB-C Reforzado", "Protector SPF50 Facial",
    "Remera Talle XL Algodón", "Kit de Costura y DIY", "Pilas AAA Alcalinas",
])
def test_cod_no_marca_datos_tecnicos(title):
    assert "COD" not in rules_of(title), title


@pytest.mark.parametrize("title,token", [
    ("Auto Drift C64", "C64"), ("Cámara H6S Térmica", "H6S"), ("Vidrio SG Templado", "SG"),
    ("Soporte XK9 Metálico", "XK9"), ("Reloj Smart Z12 Deportivo", "Z12"),
])
def test_cod_marca_codigos(title, token):
    assert token in audit(title)["COD"]


def test_cod_no_confunde_un_titulo_todo_en_mayusculas_con_siglas():
    got = audit("ORGANIZADOR DE COCINA CON TAPA")
    assert "COD" not in got


def test_cod_no_corta_palabras_con_tilde_en_mayusculas_parciales():
    assert "COD" not in rules_of("Kit de ÁRBOL de Navidad")


def test_cod_cantidad_pegada_y_codigo_interno_embebido():
    assert "x4u" in audit("Bolsas x4u para viaje")["COD"]
    assert "BX01234" in audit("Organizador BX01234 de cocina")["COD"]
    assert "COD" not in rules_of("Bolsas Pack x4 para viaje")        # «x4» a secas es común y no se marca


def test_cod_respeta_la_lista_editable_de_datos_tecnicos():
    custom = TextLists(technical=seo_lists.DEFAULT_TECHNICAL + ("SG",))
    assert "COD" not in audit("Vidrio SG Templado", lists=custom)
    assert "COD" in audit("Vidrio SG Templado")


# ─── MAR ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,brand", [
    ("Funda iPhone Efecto Líquido", "iPhone"), ("Taza Kuromi Rosa", "Kuromi"),
    ("Cámara Térmica Guide Pro", "Guide"), ("Mangas Let’s Slim Protección", "Let's Slim"),
    ("Cápsulas para Nespresso Reutilizables", "Nespresso"), ("Mochila de HELLO KITTY", "Hello Kitty"),
    ("Muñeco Spider-Man Articulado", "Spider-Man"), ("Fundas iPhones Pack", "iPhone"),
])
def test_mar_detecta_marcas_y_personajes(title, brand):
    assert brand in audit(title)["MAR"]


def test_mar_por_palabra_entera():
    assert "MAR" not in rules_of("Guiderail Metálico Cortina")
    assert "MAR" not in rules_of("Manzana Roja Decorativa")


def test_mar_mira_tambien_la_descripcion_y_dice_donde():
    got = audit("Funda Transparente", description="<p>Compatible con Samsung y con Kuromi</p>")
    assert "Kuromi (descripción)" in got["MAR"] and "Samsung (descripción)" in got["MAR"]


def test_mar_distingue_la_compatibilidad():
    assert "solo como «para…»" in audit("Funda para iPhone 15")["MAR"]
    assert "solo como" not in audit("Funda iPhone Efecto Líquido")["MAR"]


def test_mar_la_lista_es_editable():
    custom = TextLists(brands=("Foobar",))
    assert "MAR" not in audit("Funda iPhone", lists=custom)
    assert "Foobar" in audit("Taza Foobar Premium", lists=custom)["MAR"]


# ─── FAB ───────────────────────────────────────────────────────────

def matcher(**kw):
    return r.SupplierMatcher(r.SupplierRefs(**kw), frozenset(t.upper() for t in LISTS.technical))


def test_fab_nombre_de_fabrica_en_titulo_y_descripcion():
    m = matcher(business="Yiwu Yongkang Hardware Tools Co., Ltd.")
    got = audit("Pinza Yongkang Profesional", description="<p>Fabricada por Yongkang.</p>", matcher=m)
    assert got["FAB"] == "coincide con nombre de fábrica: sí (título, descripción)"


def test_fab_dos_palabras_seguidas_del_nombre():
    m = matcher(business="Golden Sunrise Plastic Manufacturing Co")
    assert "FAB" in audit("Cesto Sunrise Plastic Apilable", matcher=m)
    assert "FAB" not in audit("Cesto Apilable de Plástico", matcher=m)


def test_fab_no_salta_por_palabras_genericas_del_nombre_de_empresa():
    m = matcher(business="Shenzhen Smart Home Technology Co., Ltd.")
    assert "FAB" not in audit("Cerradura Smart para Home Office", matcher=m)
    assert "FAB" not in audit("Cámara de Tecnología Compacta", matcher=m)


def test_fab_nombre_en_chino():
    m = matcher(business="义乌市永康五金工具有限公司")
    assert "FAB" in audit("Pinza 永康五金工具 Profesional", matcher=m)
    assert "FAB" not in audit("Pinza Profesional", matcher=m)


def test_fab_codigo_de_proveedor_del_modelo():
    m = matcher(size_model="XK-900 / Negro / 30cm")
    got = audit("Soporte XK-900 para Auto", matcher=m)
    assert got["FAB"] == "coincide con código de proveedor: sí (título)"
    assert "FAB" in audit("Soporte xk900 para Auto", matcher=m)        # con o sin guion, sin mayúsculas


def test_fab_codigo_de_proveedor_solo_sigla():
    m = matcher(size_model="SG")
    assert "FAB" in audit("Vidrio Templado SG con Aplicador", matcher=m)
    assert "FAB" not in audit("Vidrio Templado sg", matcher=m)         # una sigla se compara en mayúsculas


def test_fab_no_toma_medidas_ni_datos_tecnicos_por_codigo():
    m = matcher(size_model="200W / 5m / 30cm / LED / XL / Negro")
    assert "FAB" not in audit("Picador de Hielo Eléctrico 200W con Cable de 5m LED XL Negro", matcher=m)


def test_fab_link_del_proveedor_por_id_de_oferta_y_por_tienda():
    m = matcher(link="https://detail.1688.com/offer/612345678901.html?spm=a")
    assert "link" in audit("Taza ref 612345678901", matcher=m)["FAB"]
    m2 = matcher(link="https://xyzfactory.1688.com/page/offerlist.htm")
    assert "FAB" in audit("Taza de la tienda xyzfactory", matcher=m2)
    assert "FAB" not in audit("Taza de la tienda", matcher=m2)


def test_fab_menciona_un_sitio_de_proveedor_sin_necesitar_datos():
    got = audit("Taza", description="Importada de AliExpress y de 1688", matcher=matcher())
    assert got["FAB"] == "menciona un sitio de proveedor"
    assert "FAB" not in rules_of("Estufa 1688W Cuarzo")                  # «1688W» no es el sitio


def test_fab_sin_datos_de_proveedor_no_marca_nada():
    assert "FAB" not in audit("Pinza Yongkang XK-900", matcher=matcher())
    assert "FAB" not in audit("Pinza Yongkang XK-900", matcher=matcher(business="", size_model="  ", link=None))


def test_fab_el_detalle_nunca_trae_el_valor_del_proveedor():
    refs = dict(business="Yiwu Yongkang Hardware Co", size_model="XK-900", link="https://detail.1688.com/offer/612345678901.html")
    got = audit("Pinza Yongkang XK-900 612345678901", description="Yongkang", matcher=matcher(**refs))
    detail = got["FAB"].casefold()
    for secret in ("yongkang", "xk-900", "xk900", "612345678901", "1688", "hardware"):
        assert secret not in detail, secret
    assert "nombre de fábrica: sí" in detail and "código de proveedor: sí" in detail and "link de proveedor: sí" in detail


def test_fab_los_objetos_no_muestran_los_valores_en_repr():
    refs = r.SupplierRefs(business="Yongkang Secret Factory", size_model="XK-900", link="https://x.1688.com/o/1234567890")
    for obj in (refs, matcher(business="Yongkang Secret Factory", size_model="XK-900")):
        text = repr(obj) + str(obj)
        assert "Yongkang" not in text and "XK-900" not in text and "1234567890" not in text


# ─── SLUG_NO_COINCIDE ──────────────────────────────────────────────

@pytest.mark.parametrize("name,slug", [
    ("Botella Plegable de Silicona Flexible Gris", "colador-plegable-de-silicona-rojo"),
    ("Botella Plegable de Silicona Flexible Gris", "colador-plegable-de-silicona-rojo-para-cocina-y-bazar"),
    ("Organizador Colgante para Pileta con Desagüe", "contenedor-de-cocina-con-drenaje-para-lavaplatos"),   # fila 12
    ("Organizador de pared con cajón transparente, sin perforaciones y súper práctico",
     "organizador-adhesivo-para-ropa-interior"),                                                             # fila 26
])
def test_slug_no_coincide(name, slug):
    assert r.check_slug(name, slug) is not None


@pytest.mark.parametrize("name,slug", [
    ("Mopa Giratoria con Balde Centrifugado", "mopa-giratoria-con-balde-centrifugado"),
    ("Mopa Giratoria con Balde", "mopa-giratoria-con-balde-centrifugado-mango-extensible"),          # nombre acortado
    ("Organizador Colgante para Pileta", "organizador-colgante-para-cocina-con-drenaje"),             # cambio parcial
    ("Organizadores de Huevos", "organizador-de-huevo-184"),                                          # plural y sufijo -184
    ("Cinta Métrica Láser Digital", "cinta-metrica-laser-digital"),                                   # tildes
    ("BX00717", "bx00717-609"),
])
def test_slug_si_coincide(name, slug):
    assert r.check_slug(name, slug) is None


def test_slug_vacio_no_se_compara():
    assert r.check_slug("Mopa", "") is None
    assert r.check_slug("De la", "mopa-giratoria") is None


# ─── ESPACIOS / NOMBRE_ES_CODIGO ───────────────────────────────────

@pytest.mark.parametrize("name,ok", [
    ("Taza Blanca", True), (" Taza Blanca", False), ("Taza Blanca ", False),
    ("Taza  Blanca", False), ("Taza Blanca", False), ("Taza\nBlanca", False),
])
def test_espacios(name, ok):
    assert (r.check_espacios(name) is None) is ok


@pytest.mark.parametrize("name", ["BX00717", "bx02316", " PA1234 ", "BX-00717", "PA 0099"])
def test_nombre_es_codigo(name):
    assert r.check_nombre_es_codigo(name, None) is not None


def test_nombre_es_codigo_tambien_si_coincide_con_el_codigo_del_producto():
    assert r.check_nombre_es_codigo("ZZ-123", "zz-123") is not None
    assert r.check_nombre_es_codigo("Taza BX00717", None) is None
    assert r.check_nombre_es_codigo("Pasta de Dientes", "PA1234") is None


# ─── Descripción / meta description ────────────────────────────────

def test_sin_descripcion():
    assert "SIN_DESCRIPCION" in audit("Taza", description="")
    assert "SIN_DESCRIPCION" in audit("Taza", description="  <p> </p> &nbsp; ")
    assert "SIN_DESCRIPCION" in audit("Taza", description="B2BOX")
    assert "SIN_DESCRIPCION" not in audit("Taza", description="Una taza de cerámica de 300 ml.")


def test_html_y_emojis_en_la_meta():
    got = audit("Taza", description="<p>Taza de <b>cerámica</b> 😍 para el desayuno</p>")
    assert got["DESC_CON_HTML_EN_META"] == (
        "trae etiquetas HTML y emojis: la ficha lo copia tal cual a la meta description")
    assert "entidades" in audit("Taza", description="Taza de cerámica&nbsp;para el desayuno diario")["DESC_CON_HTML_EN_META"]
    assert "DESC_CON_HTML_EN_META" not in audit("Taza", description="Taza de cerámica para el desayuno diario.")
    assert "DESC_CON_HTML_EN_META" not in audit("Taza", description="Pack 2 x 1 < 3 y 5 > 4 tazas de cerámica blanca")


def test_meta_larga():
    assert "META_LARGA" not in audit("Taza", description="a" * 160)
    got = audit("Taza", description="<p>" + "palabra " * 40 + "</p>")
    assert "META_LARGA" in got and got["META_LARGA"].startswith("319 caracteres")


def test_los_tags_no_cuentan_para_el_largo_de_la_meta():
    desc = "<p><strong>" + "a" * 100 + "</strong></p>" * 3
    assert "META_LARGA" not in audit("Taza", description=desc)


# ─── Duplicados ────────────────────────────────────────────────────

def dups(rows):
    return r.find_duplicates([(f"{pid}|es_AR", pid, name) for pid, name in rows])


def test_dup_exacto_es_el_caso_3217_y_3204():
    got = dups([("3217", "Organizador de Basura para Fregadero de Cocina"),
                ("3204", "Organizador de basura  para fregadero de cocina."),
                ("1", "Mopa Giratoria con Balde Centrifugado")])
    assert got["3217|es_AR"]["DUP_EXACTO"] == "mismo nombre que el producto 3204"
    assert got["3204|es_AR"]["DUP_EXACTO"] == "mismo nombre que el producto 3217"
    assert "1|es_AR" not in got


def test_dup_exacto_ignora_nombres_muy_cortos_y_vacios():
    assert dups([("1", "ab"), ("2", "ab"), ("3", " "), ("4", " ")]) == {}


def test_dup_exacto_cuenta_productos_distintos_no_filas_del_mismo_producto():
    got = r.find_duplicates([("7|es", "7", "Taza Blanca Grande"), ("7|es_AR", "7", "Taza Blanca Grande")])
    assert got == {}


def test_dup_casi_grupo_de_hueveras():
    got = dups([
        ("1472", "Organizador de Huevos Automático Transparente para Heladera"),
        ("3192", "Organizador de Huevos Automático para Heladera"),
        ("1604", "Organizador de Huevos Transparente para Heladera"),
        ("968", "Mopa Giratoria con Balde Centrifugado Mango Extensible"),
    ])
    assert set(got) == {"1472|es_AR", "3192|es_AR", "1604|es_AR"}
    assert all(set(v) == {"DUP_CASI"} for v in got.values())
    assert "3192" in got["1472|es_AR"]["DUP_CASI"] and "1604" in got["1472|es_AR"]["DUP_CASI"]


def test_dup_casi_no_junta_productos_distintos_con_palabras_en_comun():
    got = dups([
        ("1", "Set de Herramientas Carraca Encaje y Mango Deslizante"),
        ("2", "Set de Herramientas Eléctricas Inalámbricas con Maletín"),
        ("3", "Organizador de Cocina Plástico Apilable"),
        ("4", "Organizador de Cocina Acero Inoxidable Colgante"),
    ])
    assert got == {}


def test_dup_casi_no_repite_los_exactos():
    got = dups([("1", "Organizador de Huevos para Heladera Grande"),
                ("2", "Organizador de Huevos para Heladera Grande"),
                ("3", "Organizador de Huevos para Heladera Grande Blanco")])
    assert "DUP_EXACTO" in got["1|es_AR"] and "DUP_CASI" in got["1|es_AR"]
    assert got["1|es_AR"]["DUP_CASI"].startswith("parecido al producto 3")   # no nombra al 2, que ya es el exacto


def test_dup_casi_coincide_con_la_fuerza_bruta():
    """El filtro por prefijo es exacto: encuentra los mismos pares que comparar todos contra todos."""
    rng = random.Random(7)
    vocab = [f"palabra{i}" for i in range(40)] + ["organizador", "cocina", "set", "kit"] * 3
    rows = []
    for i in range(160):
        rows.append((str(i), " ".join(rng.sample(vocab, rng.randint(3, 7)))))
    got = dups(rows)
    expected: dict[str, set[str]] = {}
    for (pa, na), (pb, nb) in itertools.combinations(rows, 2):
        ta, tb = set(r.name_tokens(na)), set(r.name_tokens(nb))
        if len(ta) >= r.DUP_CASI_MIN_TOKENS and len(tb) >= r.DUP_CASI_MIN_TOKENS \
                and r._name_key(na) != r._name_key(nb) \
                and len(ta & tb) / len(ta | tb) >= r.DUP_CASI_JACCARD:
            expected.setdefault(pa, set()).add(pb)
            expected.setdefault(pb, set()).add(pa)
    found = {k.split("|")[0] for k, v in got.items() if "DUP_CASI" in v}
    assert found == set(expected)
    assert expected, "el escenario aleatorio tiene que tener pares parecidos para que la prueba valga"


def test_dup_con_miles_de_productos_no_se_vuelve_cuadratico():
    import time

    rng = random.Random(3)
    vocab = [f"w{i}" for i in range(3000)]
    rows = [(str(i), " ".join(rng.sample(vocab, 6))) for i in range(4000)]
    t0 = time.monotonic()
    dups(rows)
    assert time.monotonic() - t0 < 5


def test_sin_es_ar_detalle():
    assert r.missing_es_ar_detail(["es", "en"]) == "sin traducción es_AR (tiene: en, es)"
    assert r.missing_es_ar_detail([]) == "sin ninguna traducción"
    assert r.normalize_lang("es-ar") == "es_AR" and r.normalize_lang("ES_ar") == "es_AR" and r.normalize_lang("es") == "es"


def test_el_orden_de_las_reglas_es_el_del_catalogo():
    got = audit(" Taza C64 Kuromi  ", slug="otra-cosa", description="")
    assert list(got) == r.sort_rules(got)
    assert set(r.RULE_IDS) >= set(got)


# ─── Listas editables ──────────────────────────────────────────────

def test_sanitize_items():
    assert seo_lists.sanitize_items(["  Hello   Kitty ", "hello kitty", "", "Kuromi"]) == ["Hello Kitty", "Kuromi"]
    for bad in ("x", [1], ["a" * 61], [f"m{i}" for i in range(501)], ["a\x00b"]):
        with pytest.raises(ValueError):
            seo_lists.sanitize_items(bad)


def test_la_lista_de_fabrica_trae_los_casos_sembrados_del_diseno():
    folded = {r.fold(b) for b in seo_lists.DEFAULT_BRANDS}
    assert {"iphone", "kuromi", "guide", "let's slim", "nespresso"} <= folded
