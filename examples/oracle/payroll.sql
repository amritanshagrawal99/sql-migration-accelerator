-- Representative Oracle PL/SQL-era batch script
CREATE TABLE payroll_stage (
    emp_id    NUMBER(10),
    emp_name  VARCHAR2(100),
    dept_id   NUMBER(6),
    salary    NUMBER(12,2),
    paid_on   DATE DEFAULT SYSDATE
) TABLESPACE payroll_ts PCTFREE 10 NOLOGGING;

-- latest 100 hires with their department (old-style outer join)
SELECT e.emp_id, e.emp_name, d.dept_name
FROM employees e, departments d
WHERE e.dept_id = d.dept_id(+)
  AND ROWNUM <= 100;

SELECT emp_id, NVL(bonus, 0) AS bonus, DECODE(status, 'A', 'Active', 'Inactive') AS status_label
FROM employees
WHERE hire_date >= ADD_MONTHS(TRUNC(SYSDATE), -12);

-- org chart
SELECT emp_id, manager_id, LEVEL AS depth
FROM employees
START WITH manager_id IS NULL
CONNECT BY PRIOR emp_id = manager_id;

MERGE INTO payroll p
USING payroll_stage s ON (p.emp_id = s.emp_id)
WHEN MATCHED THEN UPDATE SET p.salary = s.salary DELETE WHERE p.salary = 0
WHEN NOT MATCHED THEN INSERT (emp_id, salary) VALUES (s.emp_id, s.salary);
