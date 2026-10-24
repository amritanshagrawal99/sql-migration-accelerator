/* Daily load macros, called from the scheduler as EXEC edw.load_day(...) */
REPLACE MACRO edw.load_day (run_dt DATE, region VARCHAR(10) DEFAULT 'IN') AS (
  DEL FROM edw.daily_sales WHERE sale_dt = :run_dt AND region = :region;
  INSERT INTO edw.daily_sales
  SEL sale_dt, region, SUM(amount), COUNT(*)
  FROM edw.fact_sales
  WHERE sale_dt = :run_dt AND region = :region
  GROUP BY 1, 2;
  COLLECT STATISTICS ON edw.daily_sales COLUMN (sale_dt);
);

REPLACE MACRO edw.top_customers (n INTEGER) AS (
  SEL customer_id, SUM(amount) AS revenue
  FROM edw.fact_sales
  GROUP BY 1
  QUALIFY ROW_NUMBER() OVER (ORDER BY SUM(amount) DESC) <= :n;
);

EXEC edw.load_day (DATE '2024-01-01', 'US');
EXEC edw.top_customers (20);
