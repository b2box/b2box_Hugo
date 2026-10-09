# Hugo — Agente de control de calidad de catálogo (B2Box)

Hugo es el tercer agente del ecosistema B2Box. Su responsabilidad es **mantener la
base de datos de productos limpia y sin duplicados, y avisar cuando el precio del
proveedor cambia**. Hugo no modifica precios de venta en Vendure.

## El ecosistema

```
       ┌──────────┐         ┌──────────┐         ┌──────────┐
       │  Luis    │ ──────▶ │  Hugo    │ ──────▶ │  Paco    │
       │ descubre │  check  │ verifica │  ok     │ enriquece│
       │ virales  │         │ duplicado│         │ y sube   │
       └──────────┘         └────┬─────┘         └────┬─────┘
                                 │                    │
                                 │                    ▼
                                 │              ┌──────────┐
                                 └─────────────▶│ Vendure  │
                                  audit + price │   (DB)   │
                                                └──────────┘
```

- **Luis** → busca productos virales (Alibaba/AliExpress/etc).
- **Paco** → enriquece datos y sube el producto a Vendure.
- **Hugo** → verifica que no se duplique y vigila el precio del proveedor (1688) para avisar cuando cambia.

## Qué hace Hugo

### 1. Anti-duplicados (3 capas, en orden de confianza)

1. **Source URL match** — si dos productos vienen del mismo `sourceUrl` (custom field en Vendure), son el mismo. Match exacto.
2. **Image perceptual hash** — `pHash` sobre las imágenes principales. Pesca duplicados aunque vengan de fuentes distintas.
3. **Fuzzy text** — similitud (`rapidfuzz`) sobre `name + description`. Última red.

El módulo `dedup/orchestrator.py` combina las tres y devuelve un score de confianza (0-1).

### 2. Vigilancia de precios fuente

- **Fuente original**: re-fetch del precio del proveedor (1688 vía OTAPI;
  Alibaba/AliExpress por JSON-LD) en `pricing/source_check.py`, con un budget
  diario de llamadas a OTAPI.
- **Diff**: cada precio se guarda como snapshot (`PriceHistory`); si la variación
  contra el snapshot anterior supera `PRICE_DRIFT_THRESHOLD`, se crea un evento
  `price_flagged` y se manda la alerta.

Hugo **no toca el precio de venta** en Vendure: solo avisa. La comparación
contra Mercado Libre la hace el semáforo (ver [Semáforo de precios (modo
sombra)](#semáforo-de-precios-modo-sombra)) por la API oficial;
`pricing/competitor_check.py` (scraping del listado) sigue sin llamador.

### 3. Cuándo actúa

- **Tiempo real (webhook)**: Paco/Luis le pegan a `POST /verify` antes de subir/descubrir.
- **Programado (scheduler)**: APScheduler corre auditorías cada
  `AUDIT_INTERVAL_HOURS` (default 336 h = 14 días). La última corrida de cada
  auditoría se guarda en la DB (`settings`, clave `_meta:last_run:<job>`), así
  un redeploy **no reinicia el reloj**: al arrancar, la próxima corrida se
  calcula como última + intervalo (si ya venció, corre 5 min después de
  levantar). La primera vez, sin marcador, espera un intervalo completo.
- **On-demand**: `POST /audit` para correr una auditoría completa manualmente.

### 4. Búsqueda por imagen del b2box app (`POST /app/lookup`)

El app manda **una URL** y Hugo responde si el producto está en el catálogo.

```
URL del cliente (MercadoLibre / Alibaba / 1688 / AliExpress / foto propia)
   │
   ▼  ingest/image_from_url.py — saca la foto (JSON-LD → og:image → regex de CDN)
   ▼  match contra el catálogo:
        1. source URL exacta          (gratis, seguro)
        2. embeddings CLIP            (mismo producto aunque sea otra foto)
        3. pHash                      (fallback si el modelo no está)
   │
   ├── match → PA (código de variante) + precio + botón "comprar ahora"
   └── sin match → se abre el formulario del app en Cloud_B2BOX
```

Sin match, Hugo abre **el mismo formulario que ya usa el app**: la edge function
`form-app-submit` de `b2b-flow-pro`, que escribe en `form_app_consultations` +
`form_app_consultation_products`. Aparece en la sección Forms del tablero, igual
que si el cliente lo hubiera cargado a mano. El link original va en
`reference_link`, la foto en `image_urls`, y Hugo agrega en `notes` por qué no lo
encontró y cuál fue el mejor candidato del catálogo.

Como el formulario exige **nombre, email y teléfono**, el app tiene que mandarlos
en `client`. Si faltan, Hugo no llama a Cloud y devuelve `cloud_request.missing_fields`
para que el app los pida y reintente.

> **Dos límites de `form-app-submit` que hay que mirar antes de escalar:**
>
> * `checkRateLimit` permite **5 submissions cada 10 min por IP**. Está pensado
>   para un browser, pero Hugo es un solo servidor: del 6º lookup sin match en
>   10 minutos en adelante, Cloud responde 429. Para volumen real hace falta un
>   bypass server-to-server en la edge function.
> * `verifyRecaptcha` corre en modo `monitor` por defecto y deja pasar a Hugo.
>   Si alguien pone `RECAPTCHA_MODE=enforce`, Hugo empieza a comer 403: no puede
>   generar un token de reCAPTCHA v3.
>
> Hugo detecta los dos casos y devuelve el error explicado en `cloud_request.error`.

**Por qué CLIP y no solo pHash.** pHash compara píxeles: sirve cuando la
publicación reusa la foto oficial del proveedor, pero se cae con la foto propia
del cliente. Medido sobre la misma foto transformada (recorte + brillo, rotación
18°, espejo + desaturado):

| variante                      | CLIP  | pHash |
|-------------------------------|-------|-------|
| misma foto                    | 1.000 | 1.000 |
| recorte + brillo              | 0.923 | 0.500 |
| rotada 18°                    | 0.929 | 0.594 |
| espejo + fondo desaturado     | 0.982 | 0.500 |
| **otro producto sin relación**| 0.544 | 0.625 |

pHash puntúa **más bajo** al mismo producto que a uno sin relación. Por eso el
threshold por defecto (`EMBED_MATCH_THRESHOLD=0.88`) es sobre CLIP.

El modelo es la torre visual de CLIP ViT-B/32 en ONNX (~350 MB), bakeada en la
imagen Docker; corre en CPU sin torch (~24 ms/imagen). Los embeddings del
catálogo se precalculan en un índice en memoria y se persisten en
`image_embed_cache`, así un reinicio no vuelve a descargar ni inferir nada.

El Dockerfile baja una **revisión fija** del repo de Hugging Face
(`Qdrant/clip-ViT-B-32-vision @ e0c24ed0`) y verifica el **sha256** del archivo:
si no coincide, el build falla. Subir de versión implica cambiar los dos `ARG`
juntos, recalibrar los thresholds y vaciar `image_embed_cache`.

Medido en producción (ago 2026): el primer build del índice tardó **~19 min**
para 2052 imágenes (~1026 productos × 2), a ~1.8 img/s — el cuello es la
descarga, no la inferencia. Los rebuilds posteriores salen del cache. Mientras
construye, `/app/lookup` devuelve `status:"indexing"`; seguilo con
`GET /app/index-status`.

> **Memoria**: el modelo suma ~500 MB de RSS. El límite del container pasó de
> 512M a **2G** — en Coolify hay que subirlo en el panel del servicio.

### 5. Acción

- **Duplicados: solo flaguea.** Nada se deshabilita en Vendure sin que alguien
  apriete "Confirmar duplicado" en el dashboard (o `bulk-confirm`). Y confirmar
  solo apaga algo cuando el flag vino de la **auditoría de catálogo** (los dos
  productos están en Vendure: se apaga el más nuevo y se conserva el canónico).
  Un flag de `/verify` no tiene nada que apagar — el candidato de Luis/Cloud
  nunca entró a Vendure y el único id de la fila es el del producto **original**
  — así que confirmar solo registra y archiva. Cada fila dice explícitamente qué
  se apagaría (`disable_target_id`) y cuál es el original (`canonical_product_id`).
- Precios: snapshot + alerta. No modifica Vendure.
- Loguea TODO en `AuditLog` (Postgres en Supabase; SQLite en local).
- Manda email a `tech@b2box.pro` con resumen diario y alertas críticas.

## Semáforo de precios (modo sombra)

Cada noche Hugo compara el catálogo publicado contra Mercado Libre y calcula
cuánto ganaría un revendedor que nos compra y revende en ML. **En esta etapa
solo mide y guarda**: no toca Vendure.

### Qué hace

Por cada producto del catálogo (job `price_monitor`, `pricing/price_monitor.py`).
Con `pm_include_disabled = 1` (default) entran también los **deshabilitados**
(Nico tiene ~1.800 productos en Vendure y antes solo se medían los ~1.088
habilitados): quedan marcados en el snapshot y en la API, se pueden filtrar en
el dashboard, y **ni ellos ni nadie llevan a escribir nada en Vendure**. Sus
fotos no están en el índice CLIP del app (a propósito: un deshabilitado no debe
devolverse como "lo tenemos"), así que se embeben al vuelo y se comparan en el
mismo espacio centrado.

1. Lee **nuestro precio fresco** de Vendure (`variantList` + `bulkPriceTiers`,
   concurrencia 2; no usa el cache del catálogo, que puede tener 12 h). La
   variante representativa es la primera con precio; el tramo sale de
   `pm_tier_policy`. En la misma query lee las **medidas de esa variante**
   (custom fields `length`, `width`, `height`, `weight` y `boxLength`,
   `boxWidth`, `boxHeight`, `boxWeight`; cm y kg: son columnas de la misma fila,
   no suman consultas del lado de Vendure). Si el schema no tuviera esos
   campos, la query se repite sin ellos y el semáforo sigue sin medidas.
2. **Fuente 1, API de ML**: busca (`/products/search`, fichas de catálogo). La API
   de ML no tiene búsqueda por foto: la foto se usa para **filtrar**. Busca con
   hasta `pm_ml_query_variants` consultas por producto (default 3), ver
   ["Variantes de búsqueda"](#variantes-de-búsqueda-en-la-api-de-ml).
3. **Fuente 2, web de ML** (solo si la API no dio un IGUAL con precio): el mismo
   título en `listado.mercadolibre.com.ar` con el navegador de Hugo. Ver
   ["Búsqueda web de ML"](#búsqueda-web-de-ml-fuente-2).
   Si la Mac de la oficina ya buscó ese producto (resultado fresco), se usan esas
   publicaciones en lugar de buscar desde el servidor: ver
   ["Buscador de la oficina"](#buscador-de-la-oficina-fuente-2b-la-búsqueda-web-de-ml-hecha-desde-la-mac-de-la-oficina).
4. Filtro "mismo producto" (`pricing/market_match.py`), igual para las dos
   fuentes: CLIP contra las fotos del producto (escala centrada) + similitud de
   nombre. Vetos, match por imagen fuerte o por imagen+nombre; lo que queda en
   el medio es la **banda ambigua** (sin juez, sin cupo o sin respuesta queda como
   SIMILAR "sin confirmar": se muestra, nunca es idéntico).
   Después, el veredicto **igual / similar / diferente**:
   - **IGUAL** es lo único que entra a mediana, mínimo, ganancia y color real.
     Pedido de Nico: el color real cuenta SOLO lo idéntico.
   - **SIMILAR** es el mismo tipo de producto con una diferencia que importa (o
     algo parecido en foto y nombre que nadie pudo confirmar: "sin confirmar").
     Se guarda aparte (`similar_listings`) y **nunca toca el color real**; sin
     idénticos da un color **estimado** (ver "Siempre trae algo").
   - **DIFERENTE** (otro producto) **también se guarda** (`other_listings`) con su
     motivo y su precio, solo para mostrar.

   Reglas de Nico (08-oct-2026): marca genérica o inventada = igual; marca
   conocida con valor propio (Stanley, Philips, Samsung…) = similar; pack o
   cantidad distinta = similar; color distinto = igual. Las aplican dos cosas:
   el **juez IA** (marca, modelo, diseño; solo con `pm_vision_max_calls` > 0) y
   el **chequeo de medidas** (`pricing/market_specs.py`, `pm_spec_check`), que
   sin IA compara con el título de la publicación la **cantidad** ("x3", "pack
   de 6", "4 unidades"), la **capacidad** ("500 ml" contra "1 L") y las
   **medidas** contra las de Vendure (±`pm_dim_tol_pct` = 10 % por lado, sin
   importar cuál es el largo) y el **peso** (±`pm_weight_tol_pct` = 15 %). Un
   IGUAL que difiere en algo de eso baja a SIMILAR con la diferencia anotada.
   Sin número no se inventa una diferencia, salvo la cantidad: un pack
   explícito contra un título sin cantidad cuenta como pack distinto ("x 3"
   después de un código como "E27" o "talle 42" es un pack; "30 x 40 cm" es una
   medida). Las medidas de caja (`box*`) se guardan y se muestran, pero no
   deciden (una caja puede traer varias unidades); de los atributos de una ficha
   de la API también se descartan los de la caja de envío (`PACKAGE_*`). **Tope
   de cordura:** si la medida de Vendure es absurda (lados fuera de 0,1 a 300 cm,
   peso fuera de 1 g a 200 kg) o está a más de 10 veces de la de la publicación
   (mm cargados como cm, gramos como kilos), no decide SIMILAR: la publicación
   queda con el aviso "medida dudosa en Vendure" y no se penaliza. **Sin juez prendido la regla de marca no se
   aplica**: una publicación con marca conocida sale IGUAL si la foto y el
   nombre coinciden.
5. Para las fichas de la API aceptadas trae los vendedores
   (`/products/{id}/items`) y descarta los de pocas ventas (`sold_quantity` si
   viene; si no, `/users/{id}`, cacheado 30 días). Solo pesos. Para la web, el
   precio y las ventas del ítem vienen en el propio resultado (ML las publica en
   baldes: 100, 1.000, 5.000…); se descarta lo que tiene ventas conocidas por
   debajo de `pm_min_seller_sales` y lo que no está en pesos.
6. Guarda **mediana, mínimo, cantidad de publicaciones y vendedores**, links,
   el **origen** del precio (`api` | `web`), de dónde vino cada match (`clip`,
   `clip+nombre`, `llm`, `specs`, `manual`, `ambiguo`) con sus % de foto y de
   nombre, y, con nuestro precio, la ganancia y el color real.

### Variantes de búsqueda en la API de ML

Muchos títulos son largos o genéricos ("Organizador Doble Ajustable 3 Niveles 40x30
Blanco") y `/products/search?q=<título completo>` no encuentra ficha. Por producto se
arman, sin IA y de forma determinista (`pricing/market_query.py`), hasta
`pm_ml_query_variants` consultas, de la más específica a la más general:

| # | Etiqueta | Qué es | Ejemplo |
|---|---|---|---|
| 1 | `titulo` | el título como siempre (sin códigos BX/PA) | `Organizador Doble Ajustable 3 Niveles 40x30 Blanco` |
| 2 | `corto` | sin medidas, cantidades, códigos, colores ni relleno; las primeras 5 palabras con contenido | `Organizador Doble Ajustable Niveles` |
| 3 | `claves` | el sustantivo principal (el primero, salvo kit/set/juego) y hasta 2 atributos, sin modificadores flojos (doble, mini…) | `Organizador Ajustable Niveles` |

Lo que identifica al producto **se conserva**: el número de modelo o de tamaño (`iPhone 13`, `Número 5`), los códigos con
letras y dígitos (`i12`, `PH2`, `T6`, `A5`, `3D`) y los conectores con guion (`USB-C`); en `claves` esos códigos tienen
prioridad sobre los atributos comunes. Lo que se saca son las medidas y cantidades (`40x30`, `500 ml`, `20W`, `3 Niveles`,
`100 Piezas`, `Pack x6`, `1.5 L`), los colores, el relleno de marketing, los códigos internos BX/PA y los números de más de 4
dígitos. **Nunca se busca una variante de una sola palabra** (`Juego`, `Limpieza`): trae de todo; el título sí se busca siempre,
aunque sea corto. Las repetidas (después de normalizar mayúsculas y tildes) se buscan una sola vez: un título ya corto y limpio
hace una sola búsqueda.

- Se prueban **en orden y se corta en la primera que da un IGUAL con precio**. Las fichas
  de todas las variantes probadas se juntan sin repetir por id y pasan por el **mismo**
  filtro (CLIP, nombre, juez, medidas); cada ficha se juzga UNA vez, en la variante que
  la trajo (el juez no se llama dos veces por la misma). Un IGUAL sin vendedores que
  cuenten no corta: sigue con la variante siguiente.
- **Cada búsqueda cuenta contra `pm_ml_daily_budget`** (la reserva atómica de siempre). Sin
  cupo a mitad de camino el producto queda `skipped`; un fallo de ML en una variante lo
  deja `failed`, igual que con la primera.
- Para medir: el snapshot guarda en `ml_variant` qué búsqueda encontró el match
  (`titulo` | `corto` | `claves` | `inicio`) y la corrida suma en `variant_stats` cuántos
  productos resolvió cada una (el dashboard lo muestra debajo de la última corrida).
  `ml_requests_used` sube en lo que cuestan las variantes (ver "Costo real" abajo).
- **`pm_ml_query_variants = 1` es exactamente el comportamiento anterior**: el título y,
  solo si no trajo ninguna ficha, sus primeras 4 palabras (`inicio`). Hay un test dorado
  que corre el mundo de 16 productos de origin/main con 1 variante y compara TODAS las
  columnas del snapshot y de la corrida. Con 2 o 3 el respaldo de "4 palabras" no se usa
  (lo cubre la variante corta).
- **Costo real.** Un producto sin ficha en ningún lado gasta 3 requests (una búsqueda por variante) en vez de 1 o 2. Pero cada
  ficha IGUAL pide además sus vendedores (`/items`, hasta 4 fichas **por variante**) y, si la mitad de los vendedores no trae
  `sold_quantity`, `/users/{id}` (cacheado 30 días): el peor caso de UN producto, con 4 fichas IGUAL sin vendedores en cada
  variante, son 3 + 3×4 = **15 requests**. Con el juez de IA prendido (`pm_vision_max_calls` > 0), cada variante que trae fichas
  dudosas (o una IGUAL con marca conocida) es **una llamada más al juez**: hasta 3 por producto en vez de 1, más los `/items` que se
  piden para mostrarle el precio; el tope diario del juez acota el gasto. Referencia: la corrida #4 (con 1 variante) usó **2.359
  requests**; con 1.857 productos "sin dato" y 3 variantes el consumo esperado es de ~6.000 (2.359 + 2 × 1.857 en el peor caso de
  "ninguna ficha"), contra el `pm_ml_daily_budget` de 15.000. **No hace falta subirlo**; sin cupo el producto queda `skipped` y la
  noche siguiente empieza por esos. Conviene mirar `ml_requests_used` las primeras noches: si pasara de ~10.000, bajar
  `pm_ml_query_variants` a 2.
- La búsqueda web de ML del servidor (fuente 2) no cambia: título y, si no hay
  resultados, las primeras 4 palabras.

### Siempre trae algo

Pedido de Nico: "en ML no es solo lo idéntico, tiene que buscar similar, idéntico o
diferente pero traer algo". **Nada de lo que devolvió ML se descarta.** Por producto
se guardan hasta `pm_ml_keep_listings` (8) publicaciones, **las más parecidas
primero** (foto y después nombre), en tres listas:

| Lista (columna) | Qué es | Entra al color real |
|---|---|---|
| idénticos (`matched_listings`, con precio; `unpriced_listings`, sin precio que cuente) | el mismo producto | **sí** (solo los que tienen precio) |
| similares (`similar_listings`) | mismo tipo con otra marca, pack, medida o capacidad, o parecido sin confirmar | no; dan el color **estimado** |
| diferentes (`other_listings`) | otro producto (la foto o el nombre no se parecen, el juez dice que no) | no |

Cada publicación lleva veredicto, **motivo corto** ("difiere en cantidad", "otro
producto: la foto no se parece", lo que contestó el juez, "una persona la marcó…"),
% de foto, % de nombre, precio en pesos (si lo tiene), link, origen (ficha API / web)
y cómo se decidió. Los idénticos nunca se recortan (definen el precio), ni tampoco lo
que **una persona marcó** ("Es el mismo" / "No es el mismo": esa card tiene que seguir
visible para poder darla vuelta); el tope reparte lo que queda entre similares y
diferentes. No cuesta requests extra a la API
de ML: el precio de una ficha de la API solo se conoce si ya se había pedido (el
juez lo pide); las de la web siempre traen precio.

**Estado de cada producto** (`match_state`):

| Estado | Qué se ve | Color real | Color estimado |
|---|---|---|---|
| `igual` | color real, mediana, ganancia | verde / amarillo / rojo | no hay |
| `igual_sin_precio` | "Idéntico sin precio" (hay idénticos, ninguno con vendedores que cuenten) | sin dato | por similares, si hay |
| `similar` | "Solo similares" | sin dato | **sí**, si algún similar **confirmado** tiene precio |
| `diferente` | "Solo diferentes": las más parecidas con su precio | sin dato | no |
| `ninguno` | "Sin dato": **ML no devolvió ningún resultado** (ni API ni web) | sin dato | no |

Fallar o no poder evaluar (`failed` / `skipped`) sigue siendo aparte y no tiene estado.

**Color estimado.** Sin idéntico pero con similares **confirmados** con precio (en
pesos y, en la web, con ventas suficientes), se calcula con la **mediana de esos
similares y la misma fórmula de ganancia y los mismos cortes** que el color real. Un
similar está confirmado si lo decidió el **juez**, el **chequeo de medidas** o **una
persona**, y su diferencia **no es la cantidad (pack) ni la capacidad** (su precio no
es comparable con el nuestro). Los "sin confirmar" (banda ambigua sin juez), los de
otro pack y los de otra capacidad **se siguen mostrando en la lista** con su etiqueta
("no cuenta para el estimado: …") y su precio, pero no mueven el estimado; si no queda
ningún similar confirmado no hay color estimado y el producto muestra la lista igual. Va en campos aparte
(`estimated_color`, `estimated_margin_pct`, `estimated_median_cents`,
`estimated_listing_count`, `estimated_from = 'similar'`): `color`, `est_margin_pct`,
la mediana real y **los contadores del color real (`n_verde`, `n_sin_dato`…) no
cambian nunca por esto**; la corrida suma `n_est_verde/amarillo/rojo` y
`n_solo_diferentes`. En el dashboard se ve con punto hueco, en cursiva y "estimado
por similares". Hay un test que compara el semáforo real con el de `main` columna por
columna (web apagada): los campos nuevos son solo aditivos.

**"Es el mismo" / "No es el mismo".** Una persona puede corregir en los dos sentidos
(ver más abajo): promover un similar o un diferente a idéntico, o pasar un idéntico a
diferente. La marca se guarda por producto e id de ML (`market_match_feedback.label`,
0 o 1) y la próxima corrida la respeta, sin que el juez ni el chequeo de medidas la
cambien.

Ganancia estimada (en % sobre lo que nos paga el revendedor):

```
(mediana_ML − comisión_ML − envío_ML − nuestro_precio_con_IVA) / nuestro_precio_con_IVA × 100
```

Color: **verde** ≥ `pm_green_min_pct` (30 %), **amarillo** ≥ `pm_yellow_min_pct`
(10 %), **rojo** si es menos o si nuestro precio supera la mediana de ML,
**sin dato** si no hay match (no es malo: puede no haber llegado a ML).

Historial: tablas propias `market_price_snapshot` (una fila por producto y
corrida, siempre, con `ml_status` = `ok` | `no_data` | `failed` | `skipped`) y
`price_monitor_run` (una por corrida: estado, totales por color, requests a ML,
llamadas/tokens/costo del juez, y los bytes y búsquedas de la web). Retención:
`prune_price_history` borra los snapshots de más de `PRICE_MONITOR_RETENTION_DAYS`
(180; 0 = nunca) salvo **el último de cada producto**, y las corridas viejas que
se quedan sin snapshots (una en curso no se toca). También poda los embeddings de
fotos de ML (`mlstatic`) más viejos que `pm_embed_cache_days`.

Robustez:

- Budget diario de requests a ML con reserva atómica (`pm_ml_daily_budget`):
  si se acaba a mitad de corrida, lo que falta queda `skipped`; si la corrida
  arranca sin cupo, se cierra `skipped` sin tocar ningún producto. Cada corrida
  empieza por los productos que hace más que no se miden (último dato `ok` o
  `no_data`), así con un budget corto todo el catálogo rota. 429/5xx/red →
  backoff exponencial; si persiste, ese producto queda `failed` y la corrida
  **sigue**.
- El consumo (requests a ML, llamadas al juez) se suma a la corrida en la
  misma transacción que reserva el cupo: después de un corte, la corrida
  refleja exactamente lo gastado.
- Si más del 20 % quedó `failed`, la corrida es `degraded`.
- Solo corre en el líder, con lock. Si el proceso se reinicia a mitad, al
  levantar **retoma la misma corrida** (mismo `run_id`, solo los productos sin
  snapshot). Si el cron no disparó porque el proceso estaba caído, corre al
  levantar. Una corrida sin terminar de más de 20 h se cierra como `failed`.
- Primera noche: dos sondas de 1 request que quedan en `settings`
  (`_meta:pm_probe_listing_prices`, `_meta:pm_probe_sold_quantity`) para
  decidir si ML nos da comisión/envío por categoría y ventas por publicación.

### Búsqueda web de ML (fuente 2)

**Por qué.** La API de ML solo deja buscar **fichas de catálogo**
(`/products/search`); las publicaciones comunes (`/sites/MLA/search`,
`/items/{id}`) dan 403 por política de ML y un token de usuario no lo cambia.
Lo importado de China casi nunca tiene ficha: en la primera corrida, 1.021 de
1.088 productos quedaron "sin dato". La web pública de ML sí lista esas
publicaciones.

**Cómo.** `pricing/market_ml_web.py` abre `https://listado.mercadolibre.com.ar/<título>`
(el mismo nombre que la API, sin códigos BX/PA; si no hay resultados, una
segunda búsqueda con las primeras 4 palabras) con **el mismo navegador y el
mismo proxy que Hugo ya usa para leer links de ML** (`ingest/browser_fetch.py`:
Camoufox, `BROWSER_PROXY`, el mismo guard anti-SSRF; no hay otro lanzador).
Para ~1.000 búsquedas por noche se reusa **un** navegador
(`browser_fetch.ListingBrowser`, se relanza cada `BROWSER_LISTING_RECYCLE_AFTER`
páginas, 75; con concurrencia 2 espera a las páginas en vuelo antes de
relanzar) en vez de uno por búsqueda (~50 s de reloj cada uno).

Seguridad y memoria del navegador (el guard de `browser_fetch.py` es el mismo
para `render()` y para el listado):

- Cada request del navegador se valida contra red pública, con la cache DNS
  vencida (5 min si resuelve a una IP pública, 30 s si no; los fallos de DNS no
  se cachean) y el listado **solo puede hablar con `*.mercadolibre.com.ar`,
  `*.mercadolibre.com` y `*.mlstatic.com`** (con chequeo de borde: ni
  `evilmercadolibre.com.ar` ni `mercadolibre.com.ar.evil.com`); la búsqueda
  además tiene que terminar en un host de ML o no se lee.
- Hay **un solo Firefox a la vez en todo el proceso** (el container es de 3 GB y
  lo comparte CLIP). Un `render()` con un cliente esperando (/verify, /app/lookup)
  le pide al listado que suelte el suyo y el listado se relanza en la búsqueda
  siguiente; nunca quedan dos.
- Cerrar una página o Firefox espera como máximo 10 a 15 s; si no contesta se
  sigue y el navegador se relanza.
- `BROWSER_PROXY` va como `http://usuario:clave@host:puerto` con los caracteres
  especiales de la clave codificados (`/` = `%2F`, `#` = `%23`, `?` = `%3F`,
  `@` = `%40`). Si está mal formado Hugo lo ignora, la web queda apagada con el
  motivo "BROWSER_PROXY mal formado" y no pasa nada más (antes tiraba un 500).
  Usuario, clave y host del proxy se tapan en los errores, en `ml_error`, en
  `run.error` y en los logs; el log nunca imprime el valor.

**Qué lee.** No parsea HTML: lee lo que la página trae embebido.

1. `_n.ctx.r = {...}` (script `__NORDIC_RENDERING_CTX__`): el estado con el que ML
   renderiza la página. Cada resultado es una "polycard" con id, título, precio
   en pesos, link, id de la foto (la URL sale de `D_NQ_NP_<id>-F.jpg` en
   `mlstatic.com`), vendedor y ventas del ítem. Se toma `appProps.pageProps.
   initialState.results`; si ML mueve el árbol, se busca la primera lista
   `results` de polycards.
2. El JSON-LD (`application/ld+json`, schema.org `Product`): de ahí sale la
   **marca**, que la polycard no trae, y es el respaldo si el estado cambia.

Si la página no trae ninguno de los dos queda como "formato desconocido" y
cuenta como fallo (ver abajo). Ids, links y fotos pasan por las mismas listas
blancas que la API (`market_ml.safe_*`: https, hosts de ML, `*.mlstatic.com`).
Se leen como máximo `pm_ml_web_max_results` (8) resultados por búsqueda, los
primeros de ML; todos pasan por el filtro "mismo producto".

**Cuánto pesa.** Medido el 08-oct-2026 con Camoufox contra 4 búsquedas reales
(desde una Mac, sin proxy; el cable es el mismo, un proxy suma algo de
overhead de túnel):

| | por búsqueda |
|---|---|
| HTML de la búsqueda (2,4 a 3,6 MB sin comprimir) | **170 a 210 KB** por el cable |
| scripts + estilos de ML (no se reusan: Playwright apaga la cache HTTP si hay interceptación) | ~1,5 MB |
| total sin bloquear nada más que imágenes/fuentes/media | ~1,7 MB |

Por eso `pm_ml_web_block_scripts` = 1 (default): el navegador corta imágenes,
fuentes, media, **scripts y estilos** (el estado viene en el HTML; verificado en
las 4 búsquedas) y cada búsqueda baja **~0,2 MB**. La corrida guarda los bytes
reales (`Request.sizes`: cabeceras + cuerpo de lo completado) por producto y
por corrida (`web_bytes`; el dashboard muestra el promedio por búsqueda).

Costo estimado de proxy residencial (a USD 2,75 a 4 por GB, precios publicados
de Decodo; **estimación**, no hay proxy contratado medido): hasta 2.500
búsquedas por noche son ~0,5 GB (≈ USD 1,4 a 2 por noche, USD 41 a 60 por mes).
Sin bloquear scripts serían ~4,3 GB por noche (USD 350 a 510 por mes).

**Cuándo corre y cuánto.**

- Solo para el producto que la API no resolvió (sin IGUAL con precio). Tope
  diario `pm_ml_web_daily_budget` (2500) con la reserva atómica de
  `daily_budget.py`: nunca se pasa, ni con reinicios a mitad.
- Concurrencia `pm_ml_web_concurrency` (1; máx. 2) y pausa de
  `pm_ml_web_pause_s` (4 s, ±30 % al azar) después de cada búsqueda. La carga
  tardó 2 a 4 s medida sin proxy (con proxy residencial será más); con la pausa
  son unos 7 a 10 s por búsqueda (estimación): 1.700 búsquedas son ~3 a 5 horas,
  que se suman a lo que ya tardaba la corrida. Además, hasta 8 fotos de
  `mlstatic.com` por producto para CLIP (directo desde Hugo, sin proxy; quedan
  en el cache de embeddings, así que pesan sobre todo la primera noche).
- **Sin `BROWSER_PROXY` la fuente queda apagada** (no se intenta desde la IP del
  datacenter: ML la bloquea). También si el navegador no está disponible
  (`BROWSER_FETCH_ENABLED`, Camoufox) o el cupo es 0. Queda un aviso en el log,
  en `price_monitor_run.web_status` ("apagada: falta BROWSER_PROXY…") y en el
  dashboard; los productos se evalúan solo con la API.
- **Bloqueos.** Captcha, redirect a verificación de cuenta, HTTP 403/429, proxy
  caído o página ilegible: ese producto queda **sin dato** con el motivo en
  `ml_error` ("ML web: ML pidió verificación anti-bot…") y `web_state`
  (`blocked` | `error`), no se reintenta y la corrida sigue. Con
  `pm_ml_web_block_streak` (5) fallos seguidos la fuente se **corta por esa
  noche**: lo que falta queda con `web_state = off` y la corrida termina `ok`
  (no es culpa de ML: no hay `degraded`). El corta-circuito por host de
  `browser_fetch` (3 fallos, 15 min) también cuenta. Un producto cuya web no
  pudo correr (bloqueo, cupo, proxy) **no cuenta como medido** y vuelve a la
  cabeza de la fila la noche siguiente.
- Un listado válido sin resultados NO es un fallo: es "ML no tiene nada para ese
  título" y corta la racha de fallos.

**Riesgos.** Es una fuente que depende de cómo ML arma su página y de que no la
bloquee: si ML cambia el estado embebido el JSON-LD sirve de respaldo, y si
cambia los dos, la fuente degrada a "formato desconocido" y se corta sola.
Términos de uso: el `robots.txt` de `listado.mercadolibre.com.ar` (consultado el
08-oct-2026) permite a los agentes genéricos las búsquedas por palabra (sin
filtros ni ordenamientos en la URL, que es lo que usa Hugo) y prohíbe las
páginas de ítem (`*/mla-`, que Hugo no abre), y bloquea por nombre a varios
rastreadores de IA (incluidos `ClaudeBot` y `Claude-User`); Hugo es una
herramienta interna, no se identifica como ninguno de ellos y no entrena
modelos, pero **no se revisaron los Términos y Condiciones de ML**: antes de
prender la fuente en producción conviene que Nico y Gabriel den el OK explícito
(poner `pm_ml_web_daily_budget` en 0 la apaga sin redeploy). Una IP residencial
es la única forma de que ML responda; no se intenta evadir captchas.

**"No es el mismo" y "Es el mismo".** En el detalle de cada producto, cada
publicación idéntica tiene "No es el mismo" y cada similar o diferente tiene "Es el
mismo"; los dos **piden confirmación**.
- *No es el mismo* (`POST /api/price-monitor/snapshots/{id}/not-same`): pasa a la
  lista de **diferentes** (no se descarta: se ve con su precio y se puede dar
  vuelta), **recalcula** mediana, mínimo, ganancia y color real con los idénticos que
  quedan (y el estimado, si ya no queda ninguno) y rehace los contadores de la
  corrida. Queda como diferente para ese producto en las próximas corridas.
- *Es el mismo* (`POST /api/price-monitor/snapshots/{id}/same`): pasa a **idéntica**,
  su precio cuenta, **recalcula el color real** (y el estimado desaparece), rehace los
  contadores y la próxima corrida la toma como IGUAL aunque el juez o el chequeo de
  medidas dijeran otra cosa. Si no tenía precio, queda como idéntica sin precio.

Las dos se guardan en la tabla `market_match_feedback` (`label` 0 / 1, con los
puntajes, el origen que tenía y **quién la marcó**: el usuario de la sesión), en una
sola transacción con el snapshot y los contadores, y son idempotentes. Una persona
puede cambiar de opinión (la fila se da vuelta y guarda la marca anterior en
`previous_label`). "Deshacer"
(`DELETE /api/price-monitor/products/{id}/feedback/{ml_id}`, también sirve el path
viejo `…/not-same/{ml_id}`) **vuelve a la marca anterior** si hubo un cambio de opinión
(`{"removed": false, "restored": 0|1}`) y, si no, borra la marca (`{"removed": true}`):
la próxima corrida vuelve a juzgar sola; el detalle de hoy no se reconstruye. Un "Es el
mismo" sobre una publicación sin precio la deja como idéntica sin precio: "cuenta para
el color desde la próxima corrida". Esas filas son etiquetas para calibrar:
`calibrate_market_match export` las saca ya etiquetadas (0 y 1).

**Pendiente (B3, fuera de este PR): CSRF.** Los POST/DELETE del dashboard
(`run`, `not-same`) se apoyan en la cookie de sesión `SameSite=Lax` y no llevan
token CSRF. `Lax` ya frena los POST entre sitios, pero conviene sumar un token (o
validar `Origin`) para todos los endpoints que escriben, no solo estos.

### Buscador de la oficina (fuente 2b: la búsqueda web de ML hecha desde la Mac de la oficina)

**Por qué.** Desde el servidor, ML contesta con captcha a la IP del proxy residencial y Hugo corta la búsqueda web
(eso está bien y **no se esquiva**). Desde la conexión de la oficina ML responde la búsqueda normal (probado 4 de 4
con `ListingBrowser` sin proxy). Entonces: **la Mac busca de a poco y Hugo hace el matching**.

```
Mac de la oficina (01:00 ART)                       Hugo
backend/tools/oficina_ml_search.py
  GET  /api/oficina/ml-queue?limit=N   ───────▶  productos sin idéntico con precio + UNA consulta cada uno
  busca esa consulta en listado.mercadolibre.com.ar
  (sin proxy, 1 página por producto, pausa 8-15 s)
  POST /api/oficina/ml-results         ───────▶  re-sanea TODO y guarda en `ml_web_result`
                                                  el semáforo de las 03:00 ART lo usa como fuente "web"
                                                  (origen `oficina`, "Web (oficina)" en el dashboard)
```

**Lado Hugo** (`pricing/oficina_ml.py`, `api/oficina_routes.py`):

- **Prender/apagar.** Variable `OFICINA_SEARCH_KEY` (**>= 32 caracteres** y >= 12 distintos: `aaaa…` no vale; **usá la que genera
  `oficina_ml_search.py --init`**, 43 caracteres aleatorios). El largo importa: la key correcta nunca se bloquea (ver abajo), así que
  lo único que frena la fuerza bruta es que la key sea imposible de adivinar. Sin la variable, o con una key floja/placeholder, los
  dos endpoints dan **404** y no existen en la práctica (la causa se loguea UNA vez, sin la key). `OFICINA_RESULT_TTL_DAYS` (default 7): cuánto vale un resultado.
- **Auth.** Header `x-oficina-key` contra la variable, comparado en tiempo constante (se comparan los SHA-256). No usan la
  cookie del dashboard (`/api/oficina/` está en las rutas públicas del middleware; la sesión del dashboard tampoco los abre).
  Rate limit por IP (30 pedidos por minuto) y bloqueo de 5 minutos tras 10 intentos con key mala (`429`). Las tablas por IP tienen **tope duro de 5.000 entradas (LRU)**, el barrido de lo vencido corre como mucho cada 30 s y las IPv6 se
  agrupan por **/64** (quien tiene una red IPv6 tiene 2^64 direcciones): cada request cuesta O(1) aunque inventen 100.000 IPs.
  **El bloqueo frena fallos, nunca a quien trae la key correcta**: detrás de un CDN la IP puede ser un borde compartido y un tercero no puede dejar
  sin cola al runner bloqueándola. La auth está declarada UNA vez, en el `APIRouter`: una ruta nueva no puede olvidarse de ella.
- **La cola** (`GET /api/oficina/ml-queue?limit=N`, N de 1 a 500), en este orden: (1) los productos cuya última medición no tuvo un
  IDÉNTICO con precio (`no_data` o `failed`) y que la oficina todavía no buscó, habilitados antes que deshabilitados — **los
  deshabilitados se buscan también**: Nico pidió medirlos (`pm_include_disabled`) y su nombre viaja en la consulta, como el de
  cualquier otro producto del catálogo —; (2) los que la oficina buscó con una consulta **vacía** y todavía tienen otra variante
  por probar; (3) los que tienen resultado **por vencer**, el más viejo primero (siguen sin idéntico, o hoy tienen precio gracias a
  la oficina). **Cada item trae UNA sola consulta**: una página de ML por producto por noche como tope. Si la del título vino
  vacía, la variante siguiente (corta, después claves) se da recién la noche siguiente (12 horas después como mínimo); si las tres
  vinieron vacías el producto espera a que venza el resultado y empieza de nuevo por el título. Solo viaja `product_id` y `queries`
  (con un único elemento): nada de costos, proveedor ni precios nuestros. **Se vuelve a pedir un producto un día antes de que su
  resultado venza** (a los 6 días con el TTL de 7): si no, la corrida de las 03:00 lo encontraría vencido y ese producto perdería el
  dato un día por semana. **Los productos que el semáforo ya no mide no entran**: se mira cuán viejo es su último snapshot respecto
  del más nuevo de todos (más de 3 días: borrado de Vendure, o deshabilitado con `pm_include_disabled = 0`), así que si el semáforo
  estuvo caído una semana nadie queda afuera.
- **Los resultados** (`POST /api/oficina/ml-results`, `{"results": [{product_id, query, fetched_at, status, reason, candidates}]}`).
  Hugo no confía en lo que llega: cada campo se vuelve a sanear con las listas blancas de la búsqueda web del servidor:

  | Campo | Regla |
  |---|---|
  | `product_id` | `[A-Za-z0-9_-]{1,64}` y tiene que existir en el semáforo (producto desconocido: rechazado) |
  | `id` de la publicación | `MLA<dígitos>` / `MLAU<dígitos>`, solo ASCII (`re.ASCII`: `MLA١٢٣` no pasa) |
  | `permalink` | https y host de Mercado Libre, sin usuario ni puerto raro ni click-trackers, **hasta 512 caracteres** (un link real de ML mide menos de 100; el tope vale para todos los usos de `safe_permalink` / `safe_image_url`, que son de ML); si no cumple se reemplaza por el link canónico del id |
  | fotos | solo `*.mlstatic.com` (http se sube a https), hasta 512 caracteres; cualquier otro host o largo se descarta. Una URL con un carácter que no se puede escribir (un surrogate suelto) se descarta: no tira el lote |
  | precio | entero en centavos, `0 < p < 10^11`; fuera de rango (o bool, NaN, texto) se descarta el precio, no la publicación; una moneda que no son 3 letras descarta el precio |
  | título, vendedor, marca | una sola línea: sin controles, sin caracteres de formato/inversión de texto, sin las marcas `{…}` de ML; cortados a 200 / 60 / 40 |
  | `fetched_at` | ISO 8601 (con o sin zona); en el futuro (más de 5 min) se rechaza: no puede mantener un resultado fresco para siempre |
  | `status` | `ok` / `empty` / `blocked` / `error`; solo `ok` guarda publicaciones (un `ok` sin ninguna válida se guarda como `empty`) |

  Topes: **512 KB** de body (también sin `Content-Length`), **50 productos** por lote, **`pm_ml_web_max_results`** publicaciones por
  producto, sin repetir id. **Idempotente por `(product_id, fetched_at)`**: mandar dos veces el mismo lote no duplica nada. Un
  resultado malo (o que revienta al procesarlo) se rechaza solo, con el motivo en la respuesta, sin tirar el lote. **Retención:** de
  cada producto se conservan los últimos 5 resultados (`ok` / `empty`) y, aparte, los últimos 5 bloqueos / errores —una racha de
  bloqueos no desplaza el último resultado bueno que sigue dentro del TTL—, y todo lo que tiene más de 30 días se borra.
- **La corrida del semáforo.** Si un producto sin IGUAL de la API tiene un resultado `ok` o `empty` de la oficina de menos de
  `OFICINA_RESULT_TTL_DAYS`, se usan esos candidatos **en lugar** de buscar desde el servidor, por el mismo filtro que la web
  (CLIP, nombre, juez, medidas, "No es el mismo" / "Es el mismo"). Un `empty` fresco dice "la oficina buscó y ML no tiene nada" y
  tampoco dispara la búsqueda del servidor. Un `blocked` / `error` no es un resultado. El color real sigue saliendo solo de
  idénticos. **Plausibilidad:** un candidato de la oficina con un precio 10 veces más chico o más grande que el nuestro (el mismo
  criterio que las tiendas) es un precio dudoso: se ve como idéntico sin precio, con el aviso, y no cuenta para el color ni para el
  estimado. Es la defensa contra una key robada o una Mac comprometida (una publicación con nuestro título, una foto real de
  mlstatic y el precio que quiera quien manda no puede fijar el color); una persona que marcó «Es el mismo» manda sobre el
  chequeo. **Alcance:** el rango [0,1×, 10×] es contra precios *absurdos* (un dato malo, una moneda mal leída, un atacante torpe), **no
  contra una manipulación dirigida**: quien tenga la key y elija un precio dentro del rango (por ejemplo 5× el nuestro) sí puede mover
  el color de ese producto. Por eso la key es larga, está solo en la Mac y en Coolify, y todo lo que entra queda guardado con su
  origen (`oficina`) en el snapshot. (No hay «mediana de la API» contra la que comparar: a la oficina solo se llega cuando la API NO tuvo un IGUAL con
  precio.) Si no hay resultado fresco, todo sigue como antes (el servidor busca solo si tiene proxy). El snapshot guarda
  `match_origin = oficina` y `web_via = oficina`; la corrida, `oficina_fresh` (productos con resultado fresco al empezar) y
  `n_oficina_ok`. En el dashboard: "Web (oficina)", el filtro de origen y una línea en Salud (productos frescos, última carga,
  últimas 24 h con cuántas bloqueadas).

**Lado Mac** (`backend/tools/oficina_ml_search.py`, se corre con el venv del repo; el Dockerfile no lo copia):

- Lee `OFICINA_SEARCH_KEY` y `HUGO_URL` de `~/.config/b2box-bench/.env` (se niega si el archivo no es 600; la key **nunca** se
  imprime ni se loguea; `HUGO_URL` tiene que ser `https://`).
- `ListingBrowser` **sin proxy** (`BROWSER_PROXY` forzado a vacío antes de importar nada), scripts bloqueados, de a una página,
  **pausa al azar de 8 a 15 s** entre búsquedas (no se puede bajar), tope `--max` (default 250; el piloto, `--max 50`).
  Parsea con `market_ml_web.parse_search` y `page_problem`. **Abre UNA página por producto por noche**: la consulta que Hugo le da
  (si Hugo mandara más de una, se usa solo la primera); lo vacío no se reintenta con otra variante esa misma noche.
- **Una sola instancia a la vez**: candado `flock` en `~/.config/b2box-bench/oficina-ml-search.lock`. Un piloto manual a la hora del
  launchd (o dos Macs con el mismo archivo) buscaría los mismos productos al doble de ritmo desde la misma IP; la segunda corrida
  dice «Ya hay otra corrida…» y sale con código 6. `--check` no lo necesita. El candado y el log se abren con `O_NOFOLLOW`: un link
  simbólico plantado en su lugar es un error de configuración (código 2), no se escribe en el archivo al que apunta.
- **`fetched_at` sale del reloj de Hugo** (header `Date` de la respuesta de la cola), no del de la Mac: con el reloj corrido más de
  5 minutos Hugo rechazaría todo por «fecha en el futuro». Si el reloj está desfasado más de 90 s lo avisa en el log. Un `Date` que
  difiere **más de un día** de la hora de la Mac (o roto, o del año 9999) no se cree: se avisa y se usa el reloj de la Mac.
- **Al primer captcha o bloqueo (`page_problem` = `blocked`: captcha, verificación de cuenta, 403/429, redirect a otro sitio) frena la
  noche entera**, lo reporta a Hugo (`status: blocked`) y termina con código 3. No reintenta, no espera para probar de nuevo, no
  cambia nada para esquivarlo. Cinco errores de lectura seguidos (página ilegible, navegador caído) también la frenan (código 5).
- Manda lotes de 10 productos. **Tanto el GET de la cola como el POST se reintentan** 3 veces con espera creciente (5 / 15 s; un 429
  respeta su `Retry-After`, con tope de 2 minutos) cuando es algo transitorio (red, 502/503 de un redeploy de Coolify): un lote que
  igual no se puede entregar se cuenta como perdido y sigue (código 4). Key mala o endpoint apagado (401 / 404): frena sin
  reintentar (código 2). **Si Hugo rechaza TODO un lote**, el log dice `ERROR … rechazó TODO el lote` con el motivo y la corrida
  termina con código 4; si todos los rechazos tienen la misma causa se frena la noche en vez de seguir gastando páginas de ML para
  tirarlas. Un rechazo parcial es un `WARNING` con los motivos.
- `--dry-run` busca de verdad pero **no manda nada** a Hugo. `--check` solo prueba la configuración y que Hugo acepte la key.
  `caffeinate -i` atado al proceso mientras corre. Log en `~/Library/Logs/b2box-oficina-ml-search.log` (permisos 600, sin la key,
  sin headers, sin los textos de las consultas). `--init` crea `~/.config/b2box-bench/` con permisos 700 (si no existía) y el
  `.env` con 600.

**Instalación en la Mac de la oficina** (una sola vez; pasos para Nico):

1. En la Terminal: `cd ~/Documents/GitHub/b2box_Hugo && git pull && cd backend`.
2. `uv sync --locked --extra dev --extra browser` y después `.venv/bin/python -m camoufox fetch` (baja el Firefox, ~150 MB).
3. `.venv/bin/python tools/oficina_ml_search.py --init`: genera la key y la guarda en `~/.config/b2box-bench/.env`. **No la
   muestra**: te dice que abras el archivo (`open -e ~/.config/b2box-bench/.env`) y copies el valor de `OFICINA_SEARCH_KEY`.
4. En Coolify → aplicación Hugo → Environment Variables → agregar `OFICINA_SEARCH_KEY` con ese valor → Redeploy.
5. En el mismo archivo, `HUGO_URL=https://<dominio de Hugo>`.
6. `.venv/bin/python tools/oficina_ml_search.py --check` → tiene que decir `OK`.
7. Prueba chica sin mandar nada: `… --dry-run --max 3` (3 búsquedas reales a ML, tarda ~1 minuto).
8. **Piloto de 50:** `… tools/oficina_ml_search.py --max 50` (~15 minutos, una página por producto). En el dashboard, Semáforo, aparecen "Web (oficina)" a la
   noche siguiente, cuando corre el semáforo.
9. **Automático a la 01:00 ART:** copiar `backend/tools/com.b2box.oficina-ml-search.plist` a `~/Library/LaunchAgents/`, cambiar
   `/Users/USUARIO/...` por la carpeta y el usuario de esa Mac, y `launchctl bootstrap gui/$(id -u)
   ~/Library/LaunchAgents/com.b2box.oficina-ml-search.plist`. La Mac tiene que estar en la zona horaria de Buenos Aires.
10. **La Mac tiene que estar despierta a la 01:00**: launchd no despierta una Mac dormida. El despertar programado lo configura una
    persona con su clave de administrador: `sudo pmset repeat wakeorpoweron MTWRFSU 00:55:00` (y la Mac enchufada, con la tapa
    abierta o en modo clamshell con monitor). Ningún script del repo lo ejecuta.

**Migración y rollback.** Es automática en el arranque (`init_db`): la tabla `ml_web_result` (con su índice único
`ix_mwr_product_fetched`) y cinco columnas nuevas **sin NOT NULL** (`market_price_snapshot.ml_variant` y `web_via`;
`price_monitor_run.variant_stats`, `oficina_fresh` y `n_oficina_ok`, estas dos con backfill a 0). El código anterior sigue
insertando sobre el esquema nuevo (hay un test sobre Postgres 16). Para volver el esquema atrás, si hiciera falta:

```sql
DROP TABLE IF EXISTS ml_web_result;
ALTER TABLE market_price_snapshot DROP COLUMN IF EXISTS ml_variant, DROP COLUMN IF EXISTS web_via;
ALTER TABLE price_monitor_run DROP COLUMN IF EXISTS variant_stats, DROP COLUMN IF EXISTS oficina_fresh,
    DROP COLUMN IF EXISTS n_oficina_ok;
```

**Cobertura y tiempos.** Con UNA página por producto por noche, un producto cuya primera variante viene vacía tarda hasta 3 noches
en probar las tres. Con 250 productos por noche y 1.800 "sin dato", una vuelta de primeras variantes son ~8 noches; con el TTL de 7
días el régimen no alcanza a refrescar todo antes de que venza (necesitaría ~265 por noche). Subir `--max` en el plist (hasta 500) o
`OFICINA_RESULT_TTL_DAYS` lo resuelve. Tiempo medido en la prueba real (5 búsquedas desde esta Mac, sin proxy): 3 a 7 s de carga + la
pausa de 8 a 15 s (promedio 11,5) = **~17-18 s por producto**: 50 productos son ~15 minutos y 250 son ~1 hora 15.

**Riesgos.** Es la misma fuente que la búsqueda web del servidor (`robots.txt` de `listado.mercadolibre.com.ar`: permite las
búsquedas por palabra a los agentes genéricos) pero desde una IP de oficina, así que si ML la bloquea esa IP queda marcada: por eso
el corte al primer bloqueo y el ritmo lento. **No se revisaron los Términos y Condiciones de ML**: conviene el OK de Nico y Gabriel.
La key vive en el `.env` de la Mac y en Coolify; rotarla es borrar la línea, `--init` y actualizar Coolify.

### Tiendas (Gadnic, Casa Perfecta…)

Además de Mercado Libre, la **misma corrida** del semáforo compara cada producto contra las
tiendas argentinas que estén cargadas en Configuración. Hoy: **Gadnic** y **Casa Perfecta**.
Para cada producto y cada tienda Hugo trae hasta 6 candidatos, **siempre** (aunque ninguno se
parezca) y los clasifica igual que a ML: **idéntico / similar / diferente**, con precio, link,
% de foto, % de nombre y motivo.

**Un idéntico de tienda cambia el color real, igual que uno de ML** (decisión de Nico, 08-oct-2026;
ajuste `pm_stores_affect_color`, default 1; en 0 las tiendas son solo referencia). El color usa la
**mediana de los idénticos de ML y de las tiendas**. Para que el precio de un idéntico de tienda
cuente tiene que cumplir TODO esto:

- estar confirmado por la **foto + el nombre**, por el **chequeo de medidas** o por **una persona**
  («Es el mismo»). El juez IA solo no alcanza: se ve como idéntico, pero su precio no mueve el color
  (el título y la foto de una tienda son texto de terceros y el modelo los lee);
- tener **stock**: lo agotado se muestra con la etiqueta «sin stock» (celda, detalle y «Más barato
  afuera»), pero no cuenta para «Más barato afuera» ni para la mediana del color, real o estimado. Si
  lo único idéntico de afuera está agotado, «Más barato afuera» lo muestra marcado como dato;
- tener un **precio creíble** (no «dudoso»).

Los similares de tienda dan el color *estimado* con las reglas de ML (`in_estimate`: confirmados por
juez, medidas o una persona, sin diferencia de pack ni de capacidad, y con stock). La marca «Gadnic»
es de importador, como la nuestra: cuenta como **genérica en todas las fuentes** (en ML y en las
otras tiendas también, no solo en la tienda Gadnic): un producto «Gadnic» que coincide por foto y
nombre es idéntico, no «marca conocida».

En el dashboard (Semáforo): una columna por fuente (**Mercado Libre | Gadnic | Casa Perfecta**) con el
mejor resultado de cada una (el precio del idéntico; si no hay, el del similar marcado «similar»; si
no, el más parecido marcado «diferente»), la columna **Más barato afuera** (la fuente con el precio
más bajo entre los idénticos), filtros **Fuente** y **Tiene idéntico en**, y en el detalle de cada
producto un bloque por tienda con «Es el mismo» / «No es el mismo» (con Deshacer, que vuelve a la
marca anterior). En Salud: cuántos productos tienen, por fuente, un idéntico, solo similares, solo
diferentes o nada, y el estado del índice de cada tienda.

#### Cómo funciona

1. **Indexador** (`pricing/store_catalog.py`), job aparte de madrugada (`STORE_INDEX_CRON_UTC`,
   03:20 UTC = 00:20 ART, antes del semáforo de las 06:00 UTC). No usa el buscador de la tienda:
   lee su `sitemap.xml`, baja cada ficha de producto y guarda en `store_catalog_item`: título, SKU,
   precio en centavos, foto, marca, stock, y las marcas `precio_dudoso` y `dead`. El semáforo compara
   contra esa tabla local: **cero requests a la tienda por producto nuestro**.
2. **Incremental**: primero las URLs nuevas, después las que hace más que no se miran, hasta llenar el
   tope diario de la tienda (`max_pages_per_day`). Gadnic tiene ~22.000 URLs y se leen 2.000 por día:
   el catálogo rota entero en ~11 días (`refresh_days`). Casa Perfecta (~150 productos) se lee entero
   cada 7 días. Las URLs que salieron del sitemap dejan de leerse.
3. **Al empezar la corrida** del semáforo, si el índice de una tienda está viejo (hay páginas vencidas
   y queda cupo hoy) se refresca lo que entre, hasta `pm_stores_topup_minutes` (15), antes de comparar.
   «Correr ahora» también compara las tres fuentes.
4. **Matching** (`pricing/store_match.py`): por producto y tienda, prefiltro por nombre (rapidfuzz
   sobre todos los títulos indexados, K = 6) → CLIP con la foto del candidato (embedding cacheado) →
   el mismo veredicto de tres valores que ML: reglas por foto + nombre, juez IA solo para la banda
   ambigua y para las marcas conocidas, y chequeo de medidas/cantidad/capacidad. Lo dudoso que nadie
   confirma (sin juez, sin cupo) queda **similar «sin confirmar»**, igual que en ML. La marca propia de
   la tienda (Gadnic) se trata como genérica en todas las fuentes (regla de Nico: genérica = idéntico,
   conocida = similar).
   Los 6 candidatos quedan en `store_match`, también los diferentes. Lo que una persona marca «No es
   el mismo» sigue visible al día siguiente como diferente (sin volver a bajar su foto) y se puede
   dar vuelta; lo que marca «Es el mismo» entra como idéntico.

#### Respeto por las tiendas

- **robots.txt**: se baja y se parsea (con comodines `*` y `$`: `urllib.robotparser` no los entiende)
  y se respeta para el sitemap y para **cada** página. Gadnic prohíbe las URLs con `?` (no se usa su
  buscador); Casa Perfecta prohíbe `/search/` (el sitemap de Tiendanube trae `/ar/search/?q=…`: se
  descartan). Si el robots.txt no se puede bajar (5xx, 429, 401 o 403), no se rastrea nada; si no
  existe (404), todo permitido. Un `Crawl-delay` del robots alarga la pausa. El patrón se compara sin
  regex (un robots hostil no puede colgar el proceso). **Tope de reglas**: 2.000 por tienda, contando
  solo las de los grupos que nos aplican (las de otros bots no cuentan y no pueden desplazar las
  nuestras); si lo nuestro se pasa, no se rastrea nada (fail-closed) y el motivo queda en el estado de la
  pasada. Un `Disallow` de más de 400 caracteres se acorta (prohíbe lo mismo y algo más). **Qué grupo es el nuestro**: se compara el *token* del User-Agent declarado en el robots
  (las letras, `_` y `-` del principio, hasta la primera `/`, espacio, `(` o `;`). `HugoPriceBot`,
  `HugoPriceBot/1.0` y `HugoPriceBot (+https://b2box.pro)` son nuestros; `HugoPriceBot2` o `o` no. Si
  no hay grupo nuestro vale el `*`.
- **Ritmo**: una página cada 2-3 segundos **por tienda**, el tope diario (se mira ANTES de bajar
  robots o sitemaps) y un tope de tiempo por pedido (robots 30 s, ficha 60 s, sitemap 180 s).
  `net_guard.safe_get` (httpx, sin navegador y sin proxy, anti-SSRF, tope de bytes: 3 MB por página,
  25 MB por sitemap, ya descomprimido). **Compresión**: solo se acepta sin comprimir o con UNA capa de
  gzip; el gzip lo descomprime Hugo con tope de tamaño. Un gzip roto, truncado, apilado o de varios
  miembros (y `br`, `zstd` o `deflate`) se rechaza con un error propio (`BadEncoding`), y un cuerpo
  vacío con `Content-Encoding: gzip` (un 204, 304 o 404) es válido. Los headers de la respuesta se
  conservan como bytes: uno con acentos o latin-1 ya no rompe la descarga. Cada redirect se valida
  contra el sitio de la tienda y contra robots.txt.
- **Cupo y descanso por sitio, no por tienda**: el cupo diario de páginas y los 10 minutos de descanso
  de «Indexar ahora» se cuentan por el dominio del sitio (sin el `www.`). Borrar y volver a crear la
  tienda no los reinicia, aunque se la borre en medio de una pasada (esa pasada igual queda anotada). El
  candado de «ya hay una pasada en curso» también es por sitio: la tienda recreada con otro id no puede
  correr en paralelo con la que se estaba leyendo.
- **Topes de tiempo en la comparación**: cada tienda tiene 120 s por producto y el producto entero
  300 s (`STORE_MATCH_TIMEOUT_S` / `PRODUCT_MATCH_TIMEOUT_S` en `store_match.py`). Si una foto gotea o
  el juez se cuelga, ese producto sigue sin esa tienda y la corrida no se traba. Cada foto tiene además
  un tope **total** de 30 s (12 s cuando hay un cliente esperando, como en `/app/lookup`), no solo por
  chunk.
  **Corta-circuito**: si una tienda se pasa de esos 120 s tres productos seguidos (`STORE_TIMEOUT_STREAK`),
  se la saltea el resto de esa corrida y el motivo aparece en Salud, debajo de su nombre en «Productos
  por fuente». Un producto que sí contesta reinicia la cuenta, y la corrida siguiente vuelve a probarla.
- **Una ficha rara no corta la pasada**: un charset inválido, un parser que falla o un error de red
  inesperado en UNA ficha es el resultado de esa ficha (error de red = transitorio, no se guarda; error
  de lectura = un fallo de esa ficha, con el motivo) y la pasada sigue con la siguiente. Si no se puede
  leer el robots, la pasada de esa tienda queda «abortada» (no se rastrea) y no en «error». Si falla un
  sitemap, queda anotado y la pasada sigue sin dar ninguna baja. Un sitemap (o uno hijo) sin ninguna
  URL tampoco cuenta como completo: nunca da una URL por «ya no está».
- **User-Agent honesto**: `HugoPriceBot/1.0 (+https://b2box.pro)` (`STORE_USER_AGENT`), sin
  disfrazarse de navegador y sin cookies.
- **GET condicional**: se manda `If-None-Match` / `If-Modified-Since` con lo que dio la tienda. (Hoy
  ninguna de las dos lo aprovecha: Casa Perfecta no manda validadores y el ETag de Gadnic cambia en
  cada respuesta, así que contestan 200. Queda listo por si lo arreglan.)
- **`dead`**: un 404/410 (o una página que ya no es un producto) dos veces la deja muerta por 30
  días (`STORE_DEAD_RETRY_DAYS`). Un **5xx** puede ser una caída de la tienda: se reintenta a 1, 2 y 4
  días y recién queda `dead` con dos fallos y 3 días desde el primero (`STORE_DEAD_MIN_DAYS_5XX`).
  Las fechas se comparan con un **margen de 6 horas** (`DUE_MARGIN`): el job corre una vez por día y
  la pasada de hoy empieza unos minutos antes que la de ayer; sin margen, un reintento que vence «a las
  24 h» no estaba vencido todavía y se corría un día entero. Con el job diario las lecturas caen los
  días 0, 1 y 3 y la ficha muere el día 3. Si TODAS las fichas dan 5xx (25 seguidas) y no hubo una bien en la última semana, la pasada se corta
  («caída») y no se marca nada muerto; en Gadnic, que tiene muchas muertas sueltas pero siempre alguna
  viva, no se corta. Salud muestra por tienda las fichas fallando, cuántas con 5xx y la salud
  (ok / degradada / caída). Un 429 corta la pasada de esa tienda por hoy; tres 403 o cinco errores de
  red seguidos, también. Nada de eso tira el job ni a las otras tiendas. Si borrás o apagás una
  tienda mientras se la lee, la pasada se corta en la página siguiente. (Caso real que motivó esta
  tolerancia: el 08-oct-2026 las fichas de Casa Perfecta devolvieron 500 por un rato, del lado de la
  tienda; el 09-oct volvían a responder 200.)

#### Precios dudosos

Cada tienda trae el precio de una manera y a veces no es el que se ve:

- **Tiendanube**: la página trae un JSON-LD `Product` por cada producto *relacionado*, y en una
  promoción el `price` del JSON-LD es el precio **tachado**. Se usa el JSON-LD que coincide con la URL
  y el precio de `data-variants` del bloque `#single-product`. Se ignora el peso del JSON-LD (0,111 kg
  es un default).
- **Gadnic**: el precio del JSON-LD se contrasta con el `finalPrice` del estado de Next.js (lo que se
  ve en pantalla). Si no coinciden manda el visible y queda marcado. Un precio menor a ARS 1.000
  también (el mini teclado a ARS 249 es un dato viejo que la página muestra igual).
- Además, un precio 10 veces menor o mayor que el nuestro se marca al comparar.

Un precio dudoso se muestra con el aviso «precio dudoso» y **no** cuenta para «Más barato afuera» ni
para el color.

#### Agregar otra tienda

Dashboard → **Configuración → Tiendas de comparación → Agregar tienda**: nombre, dirección
(`https://…`) y plataforma. **Tiendanube** sirve para cualquier tienda hecha con Tiendanube (sitemap
con `/productos/…`, `data-variants`, fotos en `mitiendanube.com`); **Sitio propio** lee el sitemap y
el JSON-LD `Product` de cualquier otro sitio. La dirección tiene que ser un dominio común y hay una
tienda por sitio (no se repite el dominio, con o sin `www.`). No se aceptan IPs, `localhost`, nombres
de una sola etiqueta, plataformas multi-inquilino (`github.io`, `myshopify.com`, `mitiendanube.com`…)
ni **sufijos públicos**: `com`, `com.ar`, `com.uy`, `com.pe`, `co.nz`, `org.uk`, `gob.ar`, `co.jp`…
Como Hugo no trae la Public Suffix List completa (no hay una dependencia para eso en el lock), la regla
es estructural: es un sufijo público un TLD solo, o `<etiqueta genérica>.<país de 2 letras>` (`com`,
`co`, `net`, `org`, `gob`, `gov`, `edu`, `ac`, `or`, `ne`…). `tienda.com.uy` y `mitienda.uy` sí son
válidos. Un sufijo público de otro estilo que no calce con esa regla habría que sumarlo a `_TOO_BROAD`
en `pricing/store_urls.py`.

Opcionales: sitemap (del mismo sitio), dominios de las fotos, marca propia, días de relectura y páginas
por día.

**Fotos (`image_hosts`)**: lista separada por comas (o espacios) donde cada entrada tiene una de tres
formas:

| Entrada | Qué acepta |
|---|---|
| `gadnic.com.ar` | ese host exacto y su `www.` |
| `*.bidcom.com.ar` | el dominio y todos sus subdominios |
| `acdn*.mitiendanube.com` | un patrón con `*` **en la primera etiqueta**: `acdn-us.mitiendanube.com` sí; `acdn.evil.mitiendanube.com` no. El comodín no cruza puntos |

La primera etiqueta del patrón lleva entre 3 y 30 letras, números o guiones, y el resto del dominio
tiene que ser válido (no un sufijo público ni una plataforma). Siempre se aceptan las fotos de la
tienda misma (su host y su `www.`) y las de su plataforma (Tiendanube: `acdn*.mitiendanube.com`). Un
dominio extra solo si es de la tienda misma, de su plataforma o si un administrador lo autorizó en
`STORE_TRUSTED_IMAGE_HOSTS` (default `*.bidcom.com.ar`, el CDN de Gadnic): quien carga la tienda no puede
sumar cualquier dominio (le abriría las fotos al juez a medio internet). Lo que no se puede autorizar
se rechaza al guardar, con el nombre de los dominios. Texto con caracteres de control (NUL) en el
nombre, la marca propia o las notas se limpia al guardar. «Indexar ahora» arranca la primera
pasada sin esperar a la madrugada. No hace falta deploy. Apagar una tienda la saca de la tabla y de la
comparación; borrarla borra también lo que se leyó de ella (y quién la borró queda en el log).

#### Costos y tiempos

| | Casa Perfecta | Gadnic |
|---|---|---|
| URLs de producto | ~150 | ~22.300 (más de la mitad muertas: 500) |
| Páginas por día (default) | 1.000 (se lee todo) | 2.000 |
| Tiempo de la pasada | ~8 min | ~1 h 45 min (2,5 s de pausa + ~0,7 s la página) |
| Rotación del catálogo | cada 7 días | ~11 días |
| Peso por página | ~0,6 MB | ~0,7 MB (descomprimido) |

Sin proxy ni navegador: no gasta Decodo ni Camoufox. El job de madrugada tiene que terminar antes de
las 06:00 UTC; con Gadnic en 2.000 páginas termina cerca de las 05:10 UTC. La primera noche, CLIP baja
las fotos de los candidatos (unas 1,8 por segundo; después quedan en el cache y se podan a los
`pm_embed_cache_days`). El juez IA de las tiendas tiene su **propio tope diario**
(`pm_stores_vision_max_calls`, 500) y solo corre si el juez de ML está prendido: comparar tiendas no le
saca llamadas al de ML. Cada producto puede sumar una consulta por tienda.

### Qué NO hace todavía

- No pasa los rojos a inactivo ni escribe nada en Vendure (`pm_mode=1` existe
  pero solo loguea "modo activo todavía no implementado"). Tampoco con los
  productos deshabilitados que ahora se miden.
- No hay diagnóstico automático, bandeja de revisión de Pao, histéresis,
  alertas (26 h sin correr, >20 % del catálogo cambia de color) ni campos en
  Vendure para la web o para pauta. Eso es la etapa siguiente.

### Settings (dashboard → Configuración → "Semáforo de precios")

| Setting | Default | Qué hace |
|---|---|---|
| `pm_mode` | 0 | 0 sombra; 1 activo (todavía no implementado) |
| `pm_green_min_pct` | 30 | ganancia mínima para verde |
| `pm_yellow_min_pct` | 10 | ganancia mínima para amarillo |
| `pm_ml_commission_pct` | 13 | comisión de ML, % de la mediana (a definir por Gabriel) |
| `pm_ml_shipping_cents` | 0 | envío de ML, centavos ARS (a definir) |
| `pm_min_seller_sales` | 50 | ventas mínimas del vendedor para contar |
| `pm_image_threshold` | 0.65 | imagen mínima (junto con el nombre) |
| `pm_name_threshold` | 0.60 | nombre mínimo (junto con la imagen) |
| `pm_image_strong` | 0.80 | imagen que alcanza sola |
| `pm_image_veto` / `pm_name_veto` | 0.40 / 0.30 | por debajo, descarte directo |
| `pm_ml_daily_budget` | 15000 | requests a ML por día (UTC) |
| `pm_ml_concurrency` | 4 | productos en paralelo contra ML |
| `pm_ml_query_variants` | 3 | búsquedas por producto en la API de ML (1 = título y 4 primeras palabras, como antes; 2 = + título corto; 3 = + palabras clave) |
| `pm_tier_policy` | 0 | 0 tramo mínimo (compra chica); 1 tramo más barato |
| `pm_vision_max_calls` | 0 | tope diario del juez IA; 0 = apagado |
| `pm_embed_cache_days` | 60 | poda de embeddings de fotos de ML |
| `pm_manual_cooldown_min` | 30 | minutos mínimos entre corridas para "Correr ahora" |
| `pm_ml_keep_listings` | 8 | publicaciones de ML que se guardan por producto (idénticas + similares + diferentes) |
| `pm_include_disabled` | 1 | también mide los productos deshabilitados (filtro en el dashboard) |
| `pm_spec_check` | 1 | chequeo de cantidad, capacidad, medidas y peso (IGUAL → SIMILAR) |
| `pm_dim_tol_pct` / `pm_weight_tol_pct` | 10 / 15 | tolerancia de medidas por lado y de peso, en % |
| `pm_ml_web_daily_budget` | 2500 | búsquedas web de ML por día (UTC); 0 = fuente apagada |
| `pm_ml_web_max_results` | 8 | resultados de cada búsqueda web que se comparan |
| `pm_ml_web_concurrency` | 1 | búsquedas web en paralelo (1-2) |
| `pm_ml_web_pause_s` | 4 | pausa entre búsquedas web, en segundos |
| `pm_ml_web_block_streak` | 5 | fallos seguidos que cortan la web por esa noche |
| `pm_ml_web_block_scripts` | 1 | no bajar scripts ni estilos (~0,2 MB en vez de ~1,7 MB por búsqueda) |
| `pm_stores_affect_color` | 1 | 1 = el color usa la mediana de los idénticos de ML y de las tiendas (con stock, precio creíble y confirmados por foto + nombre, medidas o una persona); 0 = solo referencia |
| `pm_stores_vision_max_calls` | 500 | tope diario del juez IA de las tiendas (contador propio; 0 = sin juez) |
| `pm_stores_topup_minutes` | 15 | minutos para refrescar el índice viejo de las tiendas al empezar la corrida; 0 = solo el job de la madrugada |

Los umbrales se validan al guardar: amarillo ≤ verde, veto de imagen ≤
umbral de imagen ≤ imagen sola, veto de nombre ≤ umbral de nombre.

Cambiar un corte recolorea en la **próxima corrida**, sin redeploy. El horario
es env: `PRICE_MONITOR_CRON_UTC` (default `0 6 * * *` = 03:00 ART).

Calibrar los umbrales con un set etiquetado (criterio: precisión ≥ 90 %,
recall ≥ 60 %):

```bash
# en el container de prod (usa ML y CLIP; gasta ~1-2 requests por producto)
python -m app.pricing.calibrate_market_match export --sample 100 --out pares.csv
# completar la columna same_product (1/0) a mano y después, offline:
python -m app.pricing.calibrate_market_match evaluate pares.csv --grid
```

### Juez IA para la banda ambigua (opcional)

Un modelo multimodal barato mira nuestras fotos y las de las publicaciones
dudosas y contesta, por publicación, `{ml_id, verdict, confidence,
differences, reason}` en JSON, con `verdict` = `igual` | `similar` |
`diferente` (el formato viejo `same_product` sí/no sigue funcionando). Una
`igual` con confianza ≥ 0,60 es IGUAL; con 0,50 a 0,59 baja a SIMILAR (ante la
duda no se contamina el precio); una `similar` con confianza ≥ 0,50 es SIMILAR;
`differences` solo admite un vocabulario cerrado (marca, modelo, medida,
capacidad, cantidad, funcion, accesorio). En la web el juez también revisa los
matches por reglas que declaran una marca (para aplicar "marca conocida =
similar"). Solo se consulta si `pm_vision_max_calls` > 0, con tope diario atómico; una
respuesta ilegible, un timeout o la falta de cupo = sin veredicto. Tokens y
costo estimado quedan en `price_monitor_run`. Se configura con tres variables
(API OpenAI-compatible):

```env
PM_LLM_BASE_URL=...
PM_LLM_API_KEY=...
PM_LLM_MODEL=qwen3-vl-plus
```

- **Qwen (default)** — Alibaba Cloud Model Studio, región internacional
  (Singapur). Según la doc oficial de Model Studio ("OpenAI 兼容", consultada
  el 08-oct-2026) la URL es
  `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`
  (el `WorkspaceId` está en el detalle del espacio de trabajo de la consola);
  el dominio anterior `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`
  sigue funcionando. Modelo `qwen3-vl-plus`.
- **Xiaomi MiMo** — `https://api.xiaomimimo.com/v1`, modelo `mimo-v2.6-flash`
  (multimodal; `mimo-v2-omni` y `mimo-v2.5` están dados de baja). Probado
  contra la API real el 08-oct-2026:

  ```env
  PM_LLM_BASE_URL=https://api.xiaomimimo.com/v1
  PM_LLM_API_KEY=...
  PM_LLM_MODEL=mimo-v2.6-flash
  # opcionales: precio de mimo-v2.6-flash para el costo estimado
  PM_LLM_PRICE_IN_PER_M=0.14
  PM_LLM_PRICE_OUT_PER_M=0.28
  ```

  Con ese host el juez apaga solo el pensamiento y manda las fotos en
  base64 (ver abajo); no hace falta configurar nada más.
- **OpenRouter** — `https://openrouter.ai/api/v1` con el slug del modelo de su
  catálogo.

Opcionales: `PM_LLM_TIMEOUT_S` (30) y, para estimar el costo,
`PM_LLM_PRICE_IN_PER_M` / `PM_LLM_PRICE_OUT_PER_M` (USD por millón de tokens;
0.20 / 1.60 por default). La base URL tiene que ser `https://` (si no, el
juez queda apagado); el cliente no reintenta solo y es uno por corrida.

**Pensamiento apagado.** MiMo v2.6 y los Qwen híbridos piensan por default:
gastan todo `max_tokens` razonando y devuelven el contenido vacío (MiMo pro
tardó 163 s). Según el host de `PM_LLM_BASE_URL` el juez manda en el body:

| host | campo extra |
|---|---|
| `api.xiaomimimo.com` | `{"thinking": {"type": "disabled"}}` |
| `dashscope*.aliyuncs.com`, `*.maas.aliyuncs.com`, `qwencloudapi.com` | `{"enable_thinking": false}` |
| cualquier otro (OpenRouter…) | nada |

`PM_LLM_EXTRA_BODY` (objeto JSON) reemplaza ese default entero; `{}` no manda
nada. Si no es un objeto JSON válido se loguea un aviso y se usa el default.
Es una lista blanca: solo pasan `thinking`, `enable_thinking`,
`thinking_budget`, `reasoning`, `reasoning_effort`, `top_p`, `seed` y
`response_format`. Cualquier otra clave (`model`, `messages`, `max_tokens`,
`temperature`, `stream`, `tools`…) se descarta, con un aviso por clave y una
sola vez que dice el nombre y nunca el valor.
Si igual llega una respuesta con `reasoning_tokens` > 0 o vacía, cuenta como
sin veredicto y queda un aviso "el modelo está pensando, revisá
PM_LLM_EXTRA_BODY" en el log.

**Fotos: url o base64** (`PM_LLM_IMAGE_MODE`, opcional). En `url` viaja la
URL pública y la baja el proveedor. En `base64` las baja Hugo y viajan dentro
del request. Vacío = `base64` con MiMo y `url` con el resto. MiMo baja bien
las URLs de mlstatic y de nuestro Vendure, pero con otros hosts falló (400
"failed to download or process media content"); en base64 además las fotos
van achicadas y el mismo veredicto salió con ~45 % menos tokens de entrada.
En base64:

- solo se bajan fotos `https://` de `*.mlstatic.com`, de los hosts de foto de
  las tiendas activas (ver "Tiendas") y del host de
  `VENDURE_API_URL` (de ahí salen las de nuestro catálogo); cada redirect se
  valida igual y el host tiene que resolver a una IP pública. Ya conectado y
  antes de leer el body se vuelve a validar la IP real del servidor (DNS
  rebinding); el cliente ignora `HTTP(S)_PROXY` y pide el body sin comprimir
  (un `content-encoding` distinto de `identity` se rechaza);
- hasta 5 MB por foto, `image/jpeg|png|webp|gif|bmp`, y un tope de 15 s para
  todas las fotos de la consulta juntas (las que no llegan se omiten);
- se reducen a 768 px de lado y se re-encodean JPEG, sin metadata (ni EXIF,
  ni comentario, ni perfil de color);
- memoria acotada (el container es de 3 GB y lo comparte Camoufox): PNG, WebP,
  GIF y BMP hasta 16 MP, JPEG hasta 40 MP (se decodifica ya reducido); el
  tamaño se mira en el header, antes de decodificar, y hay un solo decode a la
  vez en todo el proceso;
- una foto que falla se omite y el veredicto sigue; si no se pudo bajar
  ninguna foto nuestra, ese producto queda sin veredicto (no gasta cupo).

Si mlstatic empezara a cortar las descargas desde el servidor, las fichas
van sin foto: probar con `PM_LLM_IMAGE_MODE=url`.

#### Privacidad del juez

Qué sale hacia el proveedor en cada llamada:

- el **nombre** de nuestro producto (hasta 200 caracteres);
- la **foto destacada de nuestro catálogo** (solo https, una sola versión:
  la preview): en modo `url` el proveedor la descarga y ve esa URL; en modo
  `base64` le llega la imagen (achicada a 768 px), sin la URL;
- hasta **6 publicaciones públicas de Mercado Libre** (fichas de la API o
  resultados de la web): id, título (hasta 160 caracteres), la **marca que
  declara** la publicación (hasta 40 caracteres: en la web sale del JSON-LD; en
  las fichas de la API, del atributo `BRAND` si lo tienen), una foto
  de `mlstatic.com` (URL o imagen, según el modo) y el precio publicado.

Qué **no** sale: nuestros precios, costos, tramos o márgenes, datos de
clientes o pedidos, ni ninguna credencial salvo la API key del propio
proveedor.

Dónde se procesa:

- **Qwen (Model Studio)**: en la región de la URL que se configure; con la
  URL internacional de arriba, Singapur.
- **Xiaomi MiMo**: en servidores de Xiaomi; región y retención **sin
  verificar**.
- **OpenRouter**: enruta a terceros; la retención depende del proveedor final
  que elija.

Antes de subir `pm_vision_max_calls` por encima de 0, revisar los términos de
retención y de uso de datos para entrenamiento del proveedor elegido.

### Cómo dispararlo a mano

- Dashboard → **Semáforo** → "Correr ahora" (o `POST /api/price-monitor/run`
  con la sesión del dashboard). Corre en background. Devuelve 409 si ya hay
  una corrida en curso y 429 si no queda cupo de ML hoy o si la última
  corrida arrancó hace menos de `pm_manual_cooldown_min` (con `Retry-After`).
- Endpoints (sesión del dashboard): `GET /api/price-monitor/runs`,
  `GET /api/price-monitor/snapshots?run_id=&color=&status=&q=&enabled=&match=&origin=&estimated=&page=&page_size=`
  (`enabled`: `enabled` | `disabled` | `all`; `match`, qué devolvió ML: `igual` (tiene
  idéntico) | `similar` | `solo_similar` | `diferente` | `solo_diferente` | `ninguno`
  (sin resultados); `origin`: `api` | `web`; `estimated`: `verde` | `amarillo` | `rojo` |
  `any`, el color estimado por similares; todo por whitelist). Además de `colors` (el
  real, sin cambios) devuelve `estimated_colors` y `states` (conteos aparte), y cada
  item suma `other_listings`, `other_count`, `unpriced_listings`, `match_state` y los
  `estimated_*`, con links, fotos y precios saneados como los demás.
  `GET /api/price-monitor/products/{id}/history`, `GET /api/price-monitor/summary`,
  `POST /api/price-monitor/snapshots/{id}/not-same` y `…/same` (`{"ml_id": "MLA…"}`) y
  `DELETE /api/price-monitor/products/{id}/feedback/{ml_id}`.
- El dashboard (Semáforo) muestra, en cada producto, **las tres listas** (idénticos /
  similares / diferentes) con precio, link, % de foto y de nombre, origen (ficha API
  / web), cómo se decidió (foto + nombre, juez IA con su confianza, medidas, una
  persona, sin confirmar) y el motivo, más las medidas nuestras junto a las de la
  publicación y los botones "No es el mismo" / "Es el mismo" (con confirmación y
  "Deshacer"). El color real es un punto lleno; el **estimado por similares** va con
  punto hueco, en cursiva y "estimado por similares", con chips aparte (Estimado
  verde / amarillo / rojo); "Solo diferentes" tiene su etiqueta y "Sin dato" es solo
  cuando ML no devolvió nada. Filtros: producto (habilitados / deshabilitados /
  todos), lo que devolvió ML (tiene idéntico / solo similares / solo diferentes / sin
  resultados, con su conteo) y origen. Las chips del color real y sus conteos no
  cambian.
- La card "Semáforo de precios (ML)" en **Salud** muestra la última corrida,
  su estado, requests a ML, % sin dato, % fallado y llamadas/costo del juez.

## Auditoría de textos del catálogo (SEO, solo lectura)

HG1 del diseño `seo-palabras-diseno.md`. Hugo recorre **todos** los productos de Vendure
(habilitados y deshabilitados), en el canal Argentina (`VENDURE_CHANNEL_TOKEN`) y en el canal
por defecto, con **todas** sus traducciones (`es_AR`, `es`, las que haya), y por cada producto e
idioma marca problemas de texto con reglas fijas, **sin IA**. El resultado queda en la base de
Hugo (`text_audit_run` y `text_audit_item`) y se ve en el dashboard → **Textos (SEO)**.

**Nunca escribe en Vendure.** Lee con una `query` por página de 100 productos y por canal, sin
variantes (no dispara el N+1 de `variantList`): unas 22 lecturas para 1.100 productos, unos segundos.
Cada canal se lee aparte: si uno falla, la corrida queda **incompleta** con el motivo a la vista
y el otro se audita igual. Tiene un tope de 270 s por canal.

### Reglas

| Regla | Qué marca |
|---|---|
| `LARGO` | Nombre de más de 60 caracteres |
| `RELLENO` | Frases de marketing que no aportan búsqueda («Iluminá tus espacios con magia», «súper práctico», «práctico», «comodidad», «sin igual»). «Elegante», «mágico» y «mágica» solo cuentan si encabezan el título: en el medio suelen ser el producto («Traje Elegante», «Cubo Mágico») |
| `COD` | Código de modelo (`C64`, `H6S`, `ZK-7731`), sigla suelta (`SG`), cantidad pegada (`x4u`), medida con asterisco (`98*56cm`), escala mal copiada (`Escala 176`) |
| `MAR` | Marca o personaje de terceros de la lista (`iPhone`, `Kuromi`, `Guide`, `Let's Slim`, `Nespresso`, `everyU`, `Rosen`, `Otamatone`…), en título o descripción; avisa si está solo como «para iPhone». Se compara sin tildes, mayúsculas ni apóstrofos (`Lets Slim`), con el número pegado (`iPhone15`) y por palabra entera. «One Piece» no está: también es la malla enteriza |
| `MARCA_PROPIA` | Informativa: el título nombra a B2BOX (el storefront ya agrega « - B2BOX») |
| `FAB` | El texto coincide con el nombre de fábrica, el modelo o el link del proveedor **de ese producto**, o nombra un sitio de proveedor (1688, Alibaba, AliExpress…). Las medidas del modelo (`25x30cm`, `500 ml`, `5V2A`) no cuentan |
| `SLUG_NO_COINCIDE` | Nombre y URL comparten la mitad o menos de sus palabras (se reescribió uno y quedó el otro) |
| `NOMBRE_ES_CODIGO` | El nombre es `BX…`/`PA…` o el código del producto |
| `ESPACIOS` | Espacio al inicio o al final, espacios dobles, saltos de línea |
| `DUP_EXACTO` | Mismo nombre que otro producto **habilitado** (en el mismo idioma) |
| `DUP_CASI` | Nombre casi igual (Jaccard ≥ 0,65 sobre palabras con contenido) a otro producto habilitado. En una familia de más de 100 nombres casi iguales cada producto se compara con sus 100 vecinos por id: el conteo de esa familia es aproximado |
| `SIN_DESCRIPCION` | Descripción vacía o de menos de 20 caracteres |
| `DESC_CON_HTML_EN_META` | La descripción trae etiquetas HTML, entidades o emojis: la ficha la copia tal cual a la meta description |
| `META_LARGA` | Esa descripción, ya en texto plano, pasa de 160 caracteres |
| `SIN_ES_AR` | El producto no tiene traducción `es_AR` (el canal Argentina no cae a `es`) |

Los duplicados se buscan solo entre productos habilitados: un duplicado que Hugo ya apagó no
cuenta contra el original. `META_LARGA` describe la ficha **de hoy**; cuando el storefront
recorte la meta (SF1) deja de ser un problema real, pero el conteo sirve de línea base.

**Datos del proveedor.** `FAB` compara los textos contra `supplierBusiness`, `supplierSizeModel`
y `supplierLink` del propio producto (el modelo se compara sin medidas ni datos técnicos como
`200W`, `25x30cm` o `XL`). Esos valores **no se guardan, no se loguean y no salen por la API ni por el CSV**:
la regla solo dice «coincide con nombre de fábrica: sí» (o código / link) y en qué parte del
texto. Los objetos que los cargan ocultan los valores en `repr`. Con `LOG_LEVEL=DEBUG` los loggers `gql`, `httpx` y `httpcore`
quedan igual en WARNING: sin eso loguearían la respuesta completa de Vendure (con esos campos) y los headers del login.

### Cómo usarlo

- Dashboard → **Textos (SEO)** → «Auditar ahora» (o `POST /api/seo/text-audit/run`, con la sesión
  del dashboard). Corre en background; 409 si ya hay una, 429 si la última arrancó hace menos de un minuto.
- Corre sola los **lunes 07:30 UTC (04:30 ART)** con reloj persistente (si el proceso estaba caído a esa hora, se
  recupera al arrancar). `SEO_TEXT_AUDIT_CRON_UTC` la cambia; **vacía = sin corrida programada**. En APScheduler el
  `1` del día de la semana es martes: escribir el día con su nombre (`mon`, `tue`…).
- La vista tiene un chip por regla con la cantidad de **productos distintos** (respeta los demás filtros), filtros
  de habilitados / canal / idioma / «solo con problemas», búsqueda por nombre, URL o código, y exporta **CSV** con los
  mismos filtros: UTF-8 con BOM y **`;` como separador** (la coma es el separador decimal en es-AR y Excel abría todo en
  una columna; `?sep=,` o `?sep=tab` para otras herramientas). Las celdas que empiezan con `=`, `+`, `-` o `@` (aunque
  antes tengan espacios o caracteres de control) salen escapadas. Máximo 20.000 filas: si hay más, el dashboard avisa
  (`X-Export-Truncated`, `X-Export-Total`).
- Las listas de **marcas**, **relleno** y **datos técnicos permitidos** se editan en el panel «Listas editables» (sin
  redeploy). Lo guardado reemplaza a la lista de fábrica entera y vale desde la próxima corrida; «Restablecer» vuelve
  a la de fábrica. Cada término tiene hasta 6 palabras. Dejar una lista **vacía** apagaría la regla: el servidor lo
  rechaza (422) salvo `allow_empty: true`, y el dashboard pide confirmación. Cada cambio, restablecimiento y disparo
  manual queda en el `AuditLog` (sección «Todo») con quién lo hizo y qué se agregó o quitó.
- Endpoints (sesión del dashboard): `GET /api/seo/text-audit/summary`, `GET /api/seo/text-audit/items?rule=&enabled=&lang=&channel=&q=&only_issues=&run_id=&page=&page_size=`
  (`enabled`: `all` | `enabled` | `disabled`; `channel`: `all` | `ar` | `solo_default`; `lang`: `-` = sin traducciones),
  `GET /api/seo/text-audit/export.csv` (mismos filtros, `sep=;|,|tab`), `GET|PUT|DELETE /api/seo/text-audit/lists[/{marcas|relleno|tecnicos}]`.
- Se conservan las últimas 8 corridas. Si algo falla, el dashboard muestra el tipo de la excepción y una frase fija
  («TimeoutError: Vendure no respondió en 270 s (canal default)»); el detalle va solo al log del servidor. Un producto con
  datos que rompen una regla se saltea (queda un aviso en la corrida) y una traducción con el idioma repetido se ignora:
  no tiran la corrida. El schema de Vendure sin `supplierBusiness`/`supplierSizeModel` se vuelve a probar en cada corrida.

**Qué sigue (no está hecho):** HG2 en adelante (diccionario, reescritura, aplicar a Vendure). Los typos
(«Biométricacon») y las traducciones literales («fregadero», «flexómetro») quedan para HG4: necesitan
diccionario o corrector.

**Migración y rollback.** Es automática en el arranque (`init_db`): crea las tablas `text_audit_run` y
`text_audit_item` (con su índice único `ix_text_audit_item_run_prod_lang`) y no toca ninguna otra (hay un test
sobre Postgres 16). Las listas editables viven en `settings` (`seo:lista:*`). Para volver el esquema atrás:

```sql
DROP TABLE IF EXISTS text_audit_item;
DROP TABLE IF EXISTS text_audit_run;
DELETE FROM settings WHERE key LIKE 'seo:lista:%' OR key = '_meta:last_run:seo_text_audit';
```

## Estructura

```
backend/
├── app/
│   ├── main.py              # Entry point FastAPI
│   ├── config.py            # Settings (pydantic-settings)
│   ├── vendure/
│   │   └── client.py        # Cliente GraphQL Vendure Admin API
│   ├── dedup/
│   │   ├── url_match.py
│   │   ├── image_hash.py     # pHash (píxeles)
│   │   ├── image_embed.py    # CLIP ONNX (semántico)
│   │   ├── catalog_index.py  # índice vectorial del catálogo
│   │   ├── fuzzy_text.py
│   │   └── orchestrator.py
│   ├── ingest/
│   │   ├── image_from_url.py # saca la foto de una URL de marketplace
│   │   └── browser_fetch.py  # Camoufox: render de una ficha y ListingBrowser (listados)
│   ├── pricing/
│   │   ├── source_check.py   # precio del proveedor (OTAPI) + budget diario
│   │   ├── daily_budget.py   # contador diario con reserva atómica (OTAPI, ML, juez)
│   │   ├── price_monitor.py  # job del semáforo contra ML (modo sombra)
│   │   ├── semaforo.py       # reglas puras: ganancia, color, tramo
│   │   ├── market_ml.py      # API de ML: budget, backoff, vendedores
│   │   ├── market_ml_web.py  # búsqueda web de ML (estado embebido) + cupo y cortes
│   │   ├── market_query.py   # variantes de búsqueda (título, corto, palabras clave)
│   │   ├── oficina_ml.py     # buscador de la oficina: saneo, cola, resultados frescos
│   │   ├── market_match.py   # filtro "mismo producto" (CLIP + nombre)
│   │   ├── market_specs.py   # cantidad, capacidad, medidas y peso (IGUAL → SIMILAR)
│   │   ├── match_feedback.py # "No es el mismo": exclusiones y etiquetas negativas
│   │   ├── market_judge.py   # juez IA opcional (OpenAI-compatible)
│   │   ├── store_catalog.py  # indexador incremental de tiendas (sitemap, robots, cupo, dead)
│   │   ├── store_parse.py    # fichas de Tiendanube / JSON-LD y sitemaps (sin red)
│   │   ├── store_robots.py   # robots.txt con comodines
│   │   ├── store_urls.py     # links y fotos de tiendas saneados
│   │   ├── store_match.py    # producto vs tienda: nombre → CLIP → juez → medidas
│   │   ├── judge_images.py   # fotos del juez en base64 (descarga acotada)
│   │   ├── calibrate_market_match.py  # precisión/recall del filtro
│   │   ├── competitor_check.py  # SIN USO: no tiene llamador
│   │   └── diff.py
│   ├── seo/
│   │   ├── text_rules.py     # reglas de texto sin IA (puras)
│   │   ├── lists.py          # marcas, relleno y datos técnicos (editables)
│   │   └── text_audit.py     # corrida: lee Vendure, evalúa, guarda y consulta
│   ├── scheduler/
│   │   └── jobs.py          # APScheduler
│   ├── notifier/
│   │   └── email.py         # SMTP a tech@b2box.pro
│   ├── api/
│   │   ├── routes.py        # /verify, /audit, /products/{id}/check
│   │   ├── oficina_routes.py # /api/oficina/*: cola y resultados de la Mac (x-oficina-key)
│   │   └── seo_routes.py     # /api/seo/text-audit/*: auditoría de textos (solo lectura)
│   └── db/
│       ├── models.py        # SQLModel: PriceHistory, AuditLog
│       └── session.py
├── tools/
│   ├── oficina_ml_search.py                 # runner de la Mac de la oficina (no va en la imagen)
│   └── com.b2box.oficina-ml-search.plist    # launchd, 01:00 ART (ejemplo)
├── scripts/
│   └── check_lock.sh        # verifica que uv.lock esté al día con pyproject.toml
├── uv.lock                  # versiones exactas de producción (ver "Dependencias y lock")
└── tests/
    ├── test_dedup_orchestrator.py
    └── test_pricing_diff.py
```

## Setup local (sin Docker)

```bash
cd backend
uv sync --locked --extra dev      # crea .venv con las versiones EXACTAS de uv.lock
source .venv/bin/activate
cp ../.env.example ../.env
# editar .env con credenciales reales
uvicorn app.main:app --reload
```

(Sin `uv`: `python -m venv .venv && pip install -e ".[dev]"` anda, pero resuelve
las últimas versiones y no las del lock; ver [Dependencias y lock](#dependencias-y-lock).)

Tests: `pytest` desde `backend/` o desde la raíz del repo (hay un `pytest.ini`
que apunta a `backend/tests` con `asyncio_mode=auto`). Solo necesitan
`VENDURE_API_URL` en el entorno (alcanza `https://example.invalid/admin-api`) o
un `backend/.env`; no tocan red ni Vendure.

## Dependencias y lock

`backend/pyproject.toml` dice qué rangos acepta la app; `backend/uv.lock` fija la
versión exacta (y el sha256) de cada paquete, incluidas las transitivas. **El
build de Docker instala solo desde el lock**: `uv export --locked` genera la
lista, `pip install --no-deps --require-hashes` la instala. Sin lock, cada
rebuild resolvía las últimas versiones y el 08-oct-2026 eso rompió producción
dos veces (sqlmodel 0.0.48 con las fechas naive, gql 4.4 + httpx2 con Vendure).

- El build **falla** si `uv.lock` no coincide con `pyproject.toml`
  (`The lockfile at uv.lock needs to be updated`). Se arregla con `uv lock`.
- Versiones clave fijadas hoy: gql 4.4.0, anthropic 1.12.1, openai 3.26.1,
  httpx 0.28.1, httpx2 2.13.1, sqlmodel 0.0.44, camoufox 0.5.6.
- Los topes de `pyproject.toml` (`sqlmodel<0.0.45`, `camoufox<0.5.7`) siguen
  valiendo: el lock los respeta, no los reemplaza.
- `uv` se instala solo en el stage de build, desde `ghcr.io/astral-sh/uv` con
  versión y digest fijos (ARG en el Dockerfile; para subirla se cambian los dos).

### Actualizar dependencias

Requiere [uv](https://docs.astral.sh/uv/). Todo desde `backend/`:

```bash
# 1) Subir UN paquete (o varios) y re-resolver solo lo necesario
uv lock --upgrade-package gql            # a la última que permita pyproject
uv lock --upgrade-package "gql==4.4.0"   # a una versión puntual

#    Agregar o cambiar un rango: editar pyproject.toml (o `uv add paquete`) y
#    después `uv lock`. Subir TODO de golpe (`uv lock --upgrade`) casi nunca
#    conviene: es justo lo que rompió el 08-oct.

# 2) Instalar lo nuevo y correr la suite completa
uv sync --locked --extra dev
VENDURE_API_URL=https://example.invalid/admin-api pytest -q

# 3) Chequear que el lock quedó al día (es la misma verificación que hace el build)
scripts/check_lock.sh

# 4) Commitear pyproject.toml y uv.lock JUNTOS, PR, merge a main, redeploy
git add pyproject.toml uv.lock
```

Revisá el diff de `uv.lock` en el PR: un `--upgrade-package` de un paquete que
mueve diez más (por ejemplo `anthropic` arrastrando `httpx2`) es la señal para
probar con más cuidado.

Para probar el build como lo haría Coolify, sin el extra de Camoufox (más
rápido): `docker build -f backend/Dockerfile -t hugo-test .` desde la raíz; con
Camoufox, agregar `--build-arg INSTALL_BROWSER=true`.

Lo que **todavía no** está fijado: la imagen base (`python:3.11-slim`), los
paquetes de apt, el `npm install` del frontend (hay `package-lock.json`, pero el
Dockerfile no usa `npm ci`), el binario de Firefox que baja `camoufox fetch` y
el `hatchling` que empaqueta el proyecto. Ninguno cambia las versiones de las
deps de Python.

## Run con Docker (recomendado para producción)

Requisitos: Docker + Docker Compose v2.

```bash
# 1) Asegurate que .env existe en la raíz del proyecto (mismo nivel que docker-compose.yml)
cp .env.example .env
# editar .env con las credenciales reales

# 2) Build + up en segundo plano
docker compose up -d --build

# 3) Ver logs en vivo
docker compose logs -f hugo

# 4) Verificar que está sano
curl http://localhost:8000/health
# → {"status":"ok","agent":"hugo"}

# 5) Disparar una auditoría on-demand (sin esperar al scheduler)
curl -X POST http://localhost:8000/audit
```

**Persistencia**: la DB vive en **Supabase** (Postgres managed). Sobrevive a
`docker compose down`, rebuilds, redeploys y a borrar el container completo.

**Updates** (cuando haya código nuevo):

```bash
git pull
docker compose up -d --build
```

Las migraciones de schema (columnas nuevas) son automáticas: al arrancar, Hugo
detecta columnas faltantes en las tablas existentes y hace `ALTER TABLE ADD COLUMN`.
No vas a tener que borrar la DB cada vez que crezca el modelo.

## Deploy en Coolify

1. **Conectar el repo** a Coolify (Settings → Sources → tu GitHub).
2. **Crear nuevo Resource** → "Application" → seleccionar este repo.
3. **Build pack**: Docker Compose (Coolify detecta el `docker-compose.yml` solo).
4. **Variables de entorno**: copiar el contenido de tu `.env` local en
   Coolify → Environment Variables. Las críticas:
   - `HUGO_ENV=production` (activa los chequeos estrictos de seguridad)
   - `DATABASE_URL` (Supabase Session Pooler, ver más abajo)
   - `VENDURE_API_URL`, `VENDURE_BEARER`, `VENDURE_CHANNEL_TOKEN`
   - `HUGO_API_KEYS` y `SUPABASE_ALLOWED_EMAILS` (ver *Seguridad*)
   - `RAPIDAPI_KEY`
   - `ALERT_SMTP_*` y `ALERT_EMAIL_TO`

   **Usuario de Vendure.** Lo único que Hugo escribe en Vendure es el flag
   `enabled` de un producto (`updateProduct`); el resto es lectura del catálogo.
   `VENDURE_BEARER` / `VENDURE_USER` deberían ser de un administrador con un
   **rol acotado al catálogo** (permisos `ReadCatalog` + `UpdateCatalog` del
   canal), no un SuperAdmin: si la credencial se filtra, el daño queda en
   productos y no llega a pedidos, clientes ni configuración. Esto es
   configuración en el admin de Vendure (Settings → Roles), no código de Hugo.
5. **Domain**: asignar un dominio (ej. `hugo.b2box.app`) en Coolify.
6. **Deploy**.

Updates futuros: cada `git push` a `main` puede gatillar redeploy automático
si activás el webhook en Coolify.

## Connection string de Supabase

Para `DATABASE_URL`, ir a Supabase Dashboard:

1. Project Settings → Database → **Connection pooling**
2. Modo: **Session** (puerto `5432` vía pooler — soporta DDL para migraciones)
3. Copiar el URI y reemplazar `[YOUR-PASSWORD]` con la pass de la DB
4. Cambiar el prefijo `postgresql://` por `postgresql+psycopg://`

Resultado típico:

```
postgresql+psycopg://postgres.<project>:<pass>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

## Endpoints

- `GET  /health` — liveness probe (lo usa el healthcheck de Docker)
- `POST /verify` — Paco/Luis preguntan si un candidato es duplicado
- `POST /audit?target=all|duplicates|prices` — auditoría on-demand
- `GET  /products/{id}/check` — chequea un producto puntual (precio fuente)
- `GET  /audit-log?limit=N` — últimas N acciones (para dashboard)
- `POST /app/lookup` — el b2box app manda una URL: PA + comprar ahora, o pedido a Cloud
- `GET  /app/index-status` — si el índice de imágenes ya está listo
- `POST /app/index-rebuild` — fuerza la reconstrucción del índice
- `/api/price-monitor/*` — semáforo de precios contra ML (ver su sección)
- `/api/seo/text-audit/*` — auditoría de textos del catálogo, solo lectura (ver su sección)
- `GET /api/oficina/ml-queue`, `POST /api/oficina/ml-results` — buscador de la oficina; se autentican con `x-oficina-key`
  (`OFICINA_SEARCH_KEY`), no con la cookie del dashboard; 404 sin la variable (ver su sección)

Los tres `/app/*` se autentican con `X-API-Key` (igual que `/verify`). Hay una
key **por cliente** en `HUGO_API_KEYS="luis:xxx,cloud:yyy,b2box-app:zzz"`: Hugo
loguea el nombre del cliente en cada request autenticado y cada key se rota por
separado. `HUGO_API_KEY` (una sola key compartida) sigue valiendo como cliente
`legacy`; `GET /api/debug-config` lista los nombres configurados (nunca las keys).

### `POST /app/lookup`

```jsonc
// request — alcanza con `url`; `image_url` es para cuando el app ya subió la foto.
// `client` solo se usa si NO lo tenemos (es lo que pide el formulario de Cloud).
{
  "url": "https://articulo.mercadolibre.com.ar/MLA-123-lampara-led",
  "image_url": null,
  "note": "lo quiero en negro",
  "client": {
    "name": "Juan Pérez", "email": "juan@ejemplo.com", "phone": "+5491155551234",
    "country": "Argentina", "quantity": "200 u"
  }
}
```

```jsonc
// response — lo tenemos
{
  "status": "found", "found": true,
  "confidence": 0.94, "matched_by": ["image_embed"],
  "image_url": "https://http2.mlstatic.com/…jpg",
  "product": {
    "product_id": "42", "name": "Lámpara LED táctil",
    "product_code": "BX-1001",          // código del producto
    "pa": "PA-1001-BL",                 // PA = código de la 1ra variante
    "price_cents": 189900, "currency": "ARS",
    "variants": [{ "id": "101", "name": "Blanco", "pa": "PA-1001-BL", "price_cents": 189900 }],
    "buy_now_url": "https://b2box.app/ar/products/lampara-led-tactil"
  }
}
```

```jsonc
// response — no lo tenemos: se abrió la consulta en Cloud
{ "status": "not_found", "found": false,
  "cloud_request": { "sent": true, "request_id": "<uuid de form_app_consultations>" },
  "suggestion": { "product_id": "42", "score": 0.82 } }  // casi-match, si lo hubo

// response — no lo tenemos pero faltan datos del cliente: el app los pide y reintenta
{ "status": "not_found",
  "cloud_request": { "sent": false, "missing_fields": ["client.email", "client.phone"] } }
```

### Integración del app: mirá `action`, no `status`

La respuesta trae `action` y `message`. El app decide la pantalla con `action`
solo — no hace falta interpretar combinaciones de `status` + `cloud_request` +
`product`. `message` ya viene redactado para mostrárselo al cliente.

| `action` | Qué hace el app |
|---|---|
| `show_product` | Muestra el producto: PA, precio y botón "comprar ahora" |
| `confirm_product` | Muestra `suggestion` y pregunta "¿es este?". Si el cliente dice que no, repetir el lookup con `reject_suggestion: true` |
| `ask_photo` | Pide una foto del producto y reintenta el lookup con `image_url` |
| `ask_client_data` | Pide nombre, email y teléfono, y reintenta con `client` |
| `retry_later` | Avisa que reintente en unos minutos |
| `none` | Muestra `message` y listo |

**Por qué se pregunta en vez de afirmar.** Medido con productos reales del
catálogo: los aciertos caen entre 0.84 y 0.87 y el ruido en 0.80. Cuatro
centésimas de margen no alcanzan para decirle a un cliente "sí, lo tenemos" —
equivocarse ahí es peor que no encontrarlo. Por eso arriba de
`EMBED_MATCH_THRESHOLD` (0.88) se afirma, y entre `EMBED_SUGGEST_THRESHOLD`
(0.82) y ese valor se pregunta. El cliente resuelve en un toque lo que el modelo
no puede decidir solo.

Mientras hay una pregunta abierta **no se abre la consulta en Cloud**: si el
candidato resulta ser el correcto, ese pedido nacería muerto y alguien tendría
que descartarlo a mano. Cuando el cliente dice que no, el app repite el lookup
con `reject_suggestion: true` y ahí sí se abre — con el candidato descartado
anotado, para que nadie vuelva a proponer lo mismo.

**MercadoLibre necesita el paso de la foto.** ML no le contesta a un servidor y su
API no ofrece leer publicaciones de otros vendedores — no es falta de permisos ni
de certificación: ese scope no existe. Ver `ingest/meli.py`. Los links de ML
devuelven `action: "ask_photo"`, y con la foto del cliente el flujo sigue normal.
Las fichas de catálogo de ML (`/p/MLA…`) sí se leen por la API oficial.

El flujo completo del caso ML queda así:

```
cliente pega link de ML  → action: "ask_photo"
cliente sube una foto    → mismo lookup + image_url → action: "show_product"
```

Otros `status`: `"indexing"` (el índice se está construyendo — reintentar, **no**
se abre pedido), `"no_image"` (no se pudo sacar ninguna foto) y `"site_blocked"`
(el sitio bloquea a los servidores).

## Seguridad

- **`HUGO_ENV=production` es el interruptor.** Activa todo lo que sigue:
  secretos obligatorios al arranque (login del dashboard + al menos una API key
  que parsee), allowlist fail-closed, `503` en `/verify` y `/app/*` si ninguna
  key es válida, y descarte de keys placeholder o de menos de 24 caracteres.
  Con `development` (el default del código) todo eso es solo un warning. Por
  eso `docker-compose.yml` —lo que deploya Coolify— lo define con default
  `production`; en local el `.env` lo pisa con `development`.
- **Login del dashboard**: Supabase Auth de Cloud_B2BOX (mismos usuarios que
  Paco). `SUPABASE_ALLOWED_EMAILS` es la allowlist de quién entra y es
  **fail-closed en producción**: con `HUGO_ENV=production` y la variable vacía,
  todo login responde `403` con un texto genérico ("login deshabilitado por
  configuración"); el motivo exacto queda en el log del servidor. Hugo
  arranca igual (no es un restart loop) y `/verify` y `/app/*` siguen andando
  con su API key. Para abrir a todos los usuarios de Cloud_B2BOX hay que
  decirlo a propósito: `SUPABASE_ALLOWED_EMAILS=*`. En development, vacía =
  abierta, con warning.
- **Clientes máquina** (`/verify`, `/app/*`): una API key por cliente en
  `HUGO_API_KEYS` (ver arriba). Comparación en tiempo constante; el nombre del
  cliente queda en el log de cada request.
- **IP del cliente** (rate limit de `/verify`, lockout del login): se toma del
  último hop confiable de `X-Forwarded-For` según `TRUSTED_PROXY_HOPS`
  (1 = Traefik de Coolify, 0 = sin proxy). El primer valor del header lo
  escribe el cliente y no se le cree; si la cadena viene más corta que lo
  configurado se usa la IP del socket. `2` (Cloudflare delante de Traefik)
  **solo** si Traefik acepta tráfico únicamente desde los rangos de Cloudflare;
  si no, quien le pegue directo con un header armado elige su propia IP. No
  subirlo "por las dudas".
- **Descargas a servidores de terceros** (`net_guard.safe_get`: fotos de
  catálogo, de ML y de tiendas para pHash y CLIP, y las páginas de las tiendas):
  anti-SSRF (el host tiene que resolver a una IP pública, también en cada
  redirect), tope de bytes leído en streaming, y solo se acepta sin comprimir o
  con UNA capa de gzip (descomprimida por Hugo con tope; un gzip roto, truncado
  o de varios miembros da `BadEncoding`). Los headers se conservan en bytes: uno
  no ASCII ya no rompe la descarga. Las fotos pesan hasta 8 MB, se rechazan las
  de más de 16 MP (JPEG: 40 MP, decodificado ya reducido) y cada una tiene un
  tope **total** de 30 s (12 s con un cliente esperando, como `/app/lookup`);
  el timeout de httpx es por chunk y un servidor que gotea un byte cada tanto
  lo esquivaba.

## Variables de entorno

Ver `.env.example`. Las críticas:

- `VENDURE_API_URL`, `VENDURE_BEARER`, `VENDURE_CHANNEL_TOKEN` — Vendure Admin API.
- `RAPIDAPI_KEY` — proxy a 1688 vía OTAPI (sin esto Hugo no puede consultar precios fuente).
- `ALERT_SMTP_*`, `ALERT_EMAIL_TO` — notificaciones por email.
- `ALERT_WEBHOOK_URL` — opcional, Slack/Discord/n8n/CallMeBot.
- `HUGO_ENV=production` — activa los chequeos estrictos (secretos obligatorios, allowlist fail-closed).
- `HUGO_API_KEYS` — una API key por cliente (`luis:…,cloud:…,b2box-app:…`).
- `SUPABASE_ALLOWED_EMAILS` — quién entra al dashboard; `*` para todos. Vacía en producción = nadie.
- `TRUSTED_PROXY_HOPS` — proxies confiables delante de Hugo (default 1).
- `DEDUP_*_THRESHOLD` — umbrales de confianza de cada estrategia (0-1).
- `PRICE_DRIFT_THRESHOLD` — % mínimo de variación que dispara alerta.
- `AUDIT_INTERVAL_HOURS` — cada cuánto corre la auditoría completa.
- `STORE_INDEX_CRON_UTC` (default `20 3 * * *`), `STORE_USER_AGENT`, `STORE_REQUEST_DELAY_MIN_S` /
  `STORE_REQUEST_DELAY_MAX_S` (2 / 3), `STORE_DEAD_RETRY_DAYS` (30), `STORE_DEAD_MIN_DAYS_5XX` (3),
  `STORE_MAX_STORES` (20), `STORE_TRUSTED_IMAGE_HOSTS` (default `*.bidcom.com.ar`; dominios de fotos extra
  que un administrador autoriza, separados por comas, con la misma sintaxis que `image_hosts`:
  `host`, `*.dominio` o `acdn*.dominio`) — indexado de las tiendas.
- `MELI_CLIENT_ID`, `MELI_CLIENT_SECRET` — app de Mercado Libre (el semáforo no corre sin esto).
- `PRICE_MONITOR_CRON_UTC` — horario del semáforo (default `0 6 * * *`).
- `SEO_TEXT_AUDIT_CRON_UTC` — horario de la auditoría de textos (default `30 7 * * mon`, lunes 04:30 ART;
  vacía = sin corrida programada). El día de la semana con su nombre: en APScheduler `1` es martes.
- `PRICE_MONITOR_RETENTION_DAYS` — días de historial del semáforo que se conservan
  (default 180; siempre queda el último snapshot de cada producto; 0 = nunca).
- `BROWSER_LISTING_RECYCLE_AFTER` — páginas de listado antes de relanzar Firefox (default 75).
- `OFICINA_SEARCH_KEY` — key del buscador de la oficina (>= 32 caracteres; usá la que genera `tools/oficina_ml_search.py --init`). Sin ella
  los endpoints `/api/oficina/*` no existen (404). `OFICINA_RESULT_TTL_DAYS` (default 7): cuánto vale un resultado de la oficina.
- `BROWSER_PROXY` (`http://user:pass@host:port`, residencial), `BROWSER_FETCH_ENABLED=true`
  e `INSTALL_BROWSER=true` (build) — el navegador (Camoufox) que usan /verify, /app/lookup
  y la búsqueda web de ML del semáforo. **Sin `BROWSER_PROXY` la búsqueda web queda apagada.**
- `PM_LLM_BASE_URL`, `PM_LLM_API_KEY`, `PM_LLM_MODEL` — juez IA opcional del semáforo
  (opcionales: `PM_LLM_IMAGE_MODE`, `PM_LLM_EXTRA_BODY`; ver "Juez IA").
