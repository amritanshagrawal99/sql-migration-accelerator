CREATE SET TABLE edw_core.fact_sales, FALLBACK, NO BEFORE JOURNAL, NO AFTER JOURNAL, CHECKSUM = DEFAULT (
    sale_id      BIGINT NOT NULL,
    store_id     INTEGER NOT NULL,
    product_code CHAR(12),
    sale_dt      DATE,
    amount       DECIMAL(18,2),
    qty          INTEGER
) UNIQUE PRIMARY INDEX (sale_id);

CREATE MULTISET TABLE edw_core.fact_sales_bkp AS edw_core.fact_sales WITH NO DATA;

CREATE TABLE edw_mart.store_daily AS (
    SEL store_id, sale_dt, SUM(amount) AS revenue, SUM(qty) AS units
    FROM edw_core.fact_sales
    GROUP BY 1, 2
) WITH NO DATA PRIMARY INDEX (store_id, sale_dt);

SEL store_id,
    CAST(SUM(amount) AS DECIMAL(18,2) FORMAT 'ZZZ,ZZ9.99') AS revenue_fmt,
    INDEX(product_code, 'X') AS x_pos
FROM edw_core.fact_sales
WHERE sale_dt BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'
GROUP BY store_id
SAMPLE 100;

HELP TABLE edw_core.fact_sales;
