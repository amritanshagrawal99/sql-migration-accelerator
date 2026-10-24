/* Nightly customer load - representative Teradata BTEQ-style script */
DATABASE edw_stage;

CREATE MULTISET VOLATILE TABLE vt_customer_delta, NO FALLBACK, NO LOG AS (
    SEL c.customer_id,
        TRIM(c.first_name) || ' ' || TRIM(c.last_name) AS full_name,
        ZEROIFNULL(c.credit_limit) AS credit_limit,
        CAST(c.signup_dt AS DATE FORMAT 'YYYY-MM-DD') AS signup_date,
        c.updated_ts
    FROM edw_stage.customer_raw c
    WHERE c.updated_ts > (SEL MAX(load_ts) FROM edw_ctl.load_audit WHERE job_name = 'customer')
    QUALIFY ROW_NUMBER() OVER (PARTITION BY c.customer_id ORDER BY c.updated_ts DESC) = 1
) WITH DATA PRIMARY INDEX (customer_id) ON COMMIT PRESERVE ROWS;

COLLECT STATISTICS ON vt_customer_delta COLUMN (customer_id);

LOCKING ROW FOR ACCESS
SEL TOP 5 customer_id, full_name FROM vt_customer_delta ORDER BY updated_ts DESC;

UPDATE tgt
FROM edw_core.dim_customer tgt, vt_customer_delta src
SET full_name = src.full_name,
    credit_limit = src.credit_limit
WHERE tgt.customer_id = src.customer_id;

INSERT INTO edw_core.dim_customer (customer_id, full_name, credit_limit, signup_date)
SEL src.customer_id, src.full_name, src.credit_limit, src.signup_date
FROM vt_customer_delta src
WHERE NOT EXISTS (SEL 1 FROM edw_core.dim_customer d WHERE d.customer_id = src.customer_id);

DEL FROM edw_stage.customer_raw WHERE updated_ts < ADD_MONTHS(CURRENT_DATE, -3);

INSERT INTO edw_ctl.load_audit (job_name, load_ts) VALUES ('customer', CURRENT_TIMESTAMP);
