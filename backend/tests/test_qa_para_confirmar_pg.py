"""«Para confirmar» sobre un Postgres 16 descartable: el LIKE sobre similar_listings (VARCHAR con el JSON) y los
contadores con el SQL de Postgres. Mismos casos que test_qa_para_confirmar.py. Se salta sola sin HUGO_TEST_PG_URL:

    docker run --rm -d --name hugo-pg -e POSTGRES_PASSWORD=pw -e POSTGRES_DB=hugo_test -p 55541:5432 postgres:16-alpine
    HUGO_TEST_PG_URL=postgresql+psycopg://postgres:pw@localhost:55541/hugo_test \\
        VENDURE_API_URL=https://example.invalid/admin-api pytest backend/tests/test_qa_para_confirmar_pg.py
"""

from __future__ import annotations

import os

os.environ.setdefault("VENDURE_API_URL", "https://example.invalid/admin-api")

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.test_qa_para_confirmar import _get, _post, check_counter, check_pao_flow, check_rule, seed  # noqa: E402
from tests.test_qa_siempre_trae_algo_pg import PG_URL, api, pg  # noqa: E402,F401

pytestmark = pytest.mark.skipif(not PG_URL, reason="falta HUGO_TEST_PG_URL (Postgres descartable)")


def test_rule_and_counter_on_postgres(api, pg):
    _, old_id = seed(pg)
    with pg.connect() as c:
        # el JSON se guarda como texto: el LIKE no depende de un cast a json
        assert c.execute(text("SELECT data_type FROM information_schema.columns WHERE table_schema = current_schema() "
                              "AND table_name = 'market_price_snapshot' AND column_name = 'similar_listings'")
                         ).scalar() in ("character varying", "text")
    check_rule(_get(api))
    check_counter(_get(api), old_id)


def test_same_and_not_same_take_it_out_of_the_list_on_postgres(api, pg):
    seed(pg)
    check_pao_flow(_get(api), _post(api))
