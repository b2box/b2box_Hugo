SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

CREATE TABLE audit_log (
    id integer NOT NULL,
    action character varying NOT NULL,
    source character varying,
    product_id character varying NOT NULL,
    related_product_id character varying,
    detail character varying NOT NULL,
    before character varying,
    after character varying,
    confidence double precision,
    created_at timestamp without time zone NOT NULL,
    notified boolean NOT NULL,
    product_name character varying,
    product_code character varying,
    product_image_url character varying,
    product_source_url character varying,
    related_product_name character varying,
    related_product_code character varying,
    dismissed boolean NOT NULL,
    dismissed_at timestamp without time zone,
    note character varying,
    disable_target_id character varying,
    canonical_product_id character varying,
    verify_ctx character varying
);

CREATE SEQUENCE audit_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE audit_log_id_seq OWNED BY audit_log.id;

CREATE TABLE image_embed_cache (
    url character varying(1024) NOT NULL,
    model character varying(64) NOT NULL,
    dim integer NOT NULL,
    vector_b64 character varying NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE image_hash_cache (
    url character varying(1024) NOT NULL,
    phash character varying NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

CREATE TABLE market_match_feedback (
    id integer NOT NULL,
    product_id character varying(64) NOT NULL,
    ml_id character varying(64) NOT NULL,
    created_at timestamp without time zone NOT NULL,
    category character varying(12),
    origin character varying(8),
    source character varying(16),
    image_score double precision,
    name_score double precision,
    confidence double precision,
    title character varying,
    permalink character varying,
    snapshot_id integer,
    product_name character varying,
    actor character varying(120),
    label integer NOT NULL,
    previous_label integer
);

CREATE SEQUENCE market_match_feedback_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE market_match_feedback_id_seq OWNED BY market_match_feedback.id;

CREATE TABLE market_price_snapshot (
    id integer NOT NULL,
    run_id integer NOT NULL,
    product_id character varying(64) NOT NULL,
    variant_id character varying(64),
    captured_at timestamp without time zone NOT NULL,
    ml_status character varying(16) NOT NULL,
    ml_error character varying,
    ml_median_cents integer,
    ml_min_cents integer,
    ml_listing_count integer NOT NULL,
    ml_seller_count integer NOT NULL,
    ml_currency character varying(8),
    matched_listings character varying,
    match_source character varying,
    match_confidence double precision,
    image_score_max double precision,
    name_score_max double precision,
    candidates_count integer NOT NULL,
    ambiguous_count integer NOT NULL,
    our_price_cents integer,
    tier_used character varying,
    commission_pct double precision,
    shipping_cents integer,
    est_margin_pct double precision,
    color character varying(16) NOT NULL,
    prev_color character varying,
    product_name character varying,
    product_code character varying,
    product_image_url character varying,
    product_slug character varying,
    product_enabled boolean NOT NULL,
    match_origin character varying(8),
    similar_count integer NOT NULL,
    similar_listings character varying,
    web_searches integer NOT NULL,
    web_bytes integer NOT NULL,
    web_state character varying(8),
    our_specs character varying,
    other_listings character varying,
    other_count integer NOT NULL,
    unpriced_listings character varying,
    match_state character varying(16),
    estimated_color character varying(16),
    estimated_margin_pct double precision,
    estimated_median_cents integer,
    estimated_listing_count integer NOT NULL,
    estimated_from character varying(8)
);

CREATE SEQUENCE market_price_snapshot_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE market_price_snapshot_id_seq OWNED BY market_price_snapshot.id;

CREATE TABLE ml_seller_cache (
    seller_id character varying(32) NOT NULL,
    completed_sales integer,
    fetched_at timestamp without time zone NOT NULL
);

CREATE TABLE price_history (
    id integer NOT NULL,
    product_id character varying NOT NULL,
    variant_id character varying,
    source character varying NOT NULL,
    price_cents integer NOT NULL,
    currency character varying(8) NOT NULL,
    captured_at timestamp without time zone NOT NULL,
    extra character varying
);

CREATE SEQUENCE price_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE price_history_id_seq OWNED BY price_history.id;

CREATE TABLE price_monitor_run (
    id integer NOT NULL,
    started_at timestamp without time zone NOT NULL,
    finished_at timestamp without time zone,
    status character varying NOT NULL,
    mode integer NOT NULL,
    trigger character varying NOT NULL,
    total_products integer NOT NULL,
    processed integer NOT NULL,
    n_ok integer NOT NULL,
    n_no_data integer NOT NULL,
    n_failed integer NOT NULL,
    n_skipped integer NOT NULL,
    n_verde integer NOT NULL,
    n_amarillo integer NOT NULL,
    n_rojo integer NOT NULL,
    n_sin_dato integer NOT NULL,
    ml_requests_used integer NOT NULL,
    llm_calls integer NOT NULL,
    llm_input_tokens integer NOT NULL,
    llm_output_tokens integer NOT NULL,
    llm_cost_usd double precision NOT NULL,
    resumed_count integer NOT NULL,
    error character varying,
    web_status character varying,
    web_searches integer NOT NULL,
    web_bytes bigint NOT NULL,
    web_blocked integer NOT NULL,
    n_web_ok integer NOT NULL,
    n_con_similares integer NOT NULL,
    n_est_verde integer NOT NULL,
    n_est_amarillo integer NOT NULL,
    n_est_rojo integer NOT NULL,
    n_solo_diferentes integer NOT NULL
);

CREATE SEQUENCE price_monitor_run_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE price_monitor_run_id_seq OWNED BY price_monitor_run.id;

CREATE TABLE settings (
    key character varying NOT NULL,
    value character varying NOT NULL,
    updated_at timestamp without time zone NOT NULL
);

ALTER TABLE ONLY audit_log ALTER COLUMN id SET DEFAULT nextval('audit_log_id_seq'::regclass);

ALTER TABLE ONLY market_match_feedback ALTER COLUMN id SET DEFAULT nextval('market_match_feedback_id_seq'::regclass);

ALTER TABLE ONLY market_price_snapshot ALTER COLUMN id SET DEFAULT nextval('market_price_snapshot_id_seq'::regclass);

ALTER TABLE ONLY price_history ALTER COLUMN id SET DEFAULT nextval('price_history_id_seq'::regclass);

ALTER TABLE ONLY price_monitor_run ALTER COLUMN id SET DEFAULT nextval('price_monitor_run_id_seq'::regclass);

ALTER TABLE ONLY audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY image_embed_cache
    ADD CONSTRAINT image_embed_cache_pkey PRIMARY KEY (url);

ALTER TABLE ONLY image_hash_cache
    ADD CONSTRAINT image_hash_cache_pkey PRIMARY KEY (url);

ALTER TABLE ONLY market_match_feedback
    ADD CONSTRAINT market_match_feedback_pkey PRIMARY KEY (id);

ALTER TABLE ONLY market_price_snapshot
    ADD CONSTRAINT market_price_snapshot_pkey PRIMARY KEY (id);

ALTER TABLE ONLY ml_seller_cache
    ADD CONSTRAINT ml_seller_cache_pkey PRIMARY KEY (seller_id);

ALTER TABLE ONLY price_history
    ADD CONSTRAINT price_history_pkey PRIMARY KEY (id);

ALTER TABLE ONLY price_monitor_run
    ADD CONSTRAINT price_monitor_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY settings
    ADD CONSTRAINT settings_pkey PRIMARY KEY (key);

CREATE INDEX ix_audit_dismissed_action_time ON audit_log USING btree (dismissed, action, created_at);

CREATE INDEX ix_audit_dismissed_source_time ON audit_log USING btree (dismissed, source, created_at);

CREATE INDEX ix_audit_log_action ON audit_log USING btree (action);

CREATE INDEX ix_audit_log_created_at ON audit_log USING btree (created_at);

CREATE INDEX ix_audit_log_dismissed ON audit_log USING btree (dismissed);

CREATE INDEX ix_audit_log_product_id ON audit_log USING btree (product_id);

CREATE INDEX ix_audit_log_source ON audit_log USING btree (source);

CREATE INDEX ix_audit_product_created ON audit_log USING btree (product_id, created_at);

CREATE UNIQUE INDEX ix_mmf_product_ml ON market_match_feedback USING btree (product_id, ml_id);

CREATE INDEX ix_mps_product_time ON market_price_snapshot USING btree (product_id, captured_at);

CREATE INDEX ix_mps_run_color ON market_price_snapshot USING btree (run_id, color);

CREATE UNIQUE INDEX ix_mps_run_product ON market_price_snapshot USING btree (run_id, product_id);

CREATE INDEX ix_price_history_captured_at ON price_history USING btree (captured_at);

CREATE INDEX ix_price_history_prod_source_time ON price_history USING btree (product_id, source, captured_at);

CREATE INDEX ix_price_history_product_id ON price_history USING btree (product_id);

CREATE INDEX ix_price_history_source ON price_history USING btree (source);

CREATE INDEX ix_price_history_variant_id ON price_history USING btree (variant_id);

CREATE INDEX ix_price_monitor_run_started_at ON price_monitor_run USING btree (started_at);

CREATE INDEX ix_price_monitor_run_status ON price_monitor_run USING btree (status);
