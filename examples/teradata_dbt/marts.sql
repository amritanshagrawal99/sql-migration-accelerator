CREATE MULTISET VOLATILE TABLE vt_active AS (
    SEL c.customer_id, c.region FROM edw_core.dim_customer c WHERE c.status = 'A'
) WITH DATA PRIMARY INDEX (customer_id) ON COMMIT PRESERVE ROWS;

CREATE SET TABLE edw_mart.customer_revenue AS (
    SEL a.customer_id, a.region, SUM(f.amount) AS revenue
    FROM vt_active a
    JOIN edw_core.fact_sales f ON f.customer_id = a.customer_id
    GROUP BY 1, 2
) WITH DATA UNIQUE PRIMARY INDEX (customer_id);

CREATE VIEW edw_mart.v_region_revenue AS
SEL region, SUM(revenue) AS revenue FROM edw_mart.customer_revenue GROUP BY region;

INSERT INTO edw_ctl.load_audit (job_name, load_ts) VALUES ('marts', CURRENT_TIMESTAMP);
