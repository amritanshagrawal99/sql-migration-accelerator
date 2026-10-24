import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sma.cli import main
from sma.converter import convert_script
from sma.dbt_export import collect, render_model, write

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "teradata_dbt"


@pytest.fixture
def export():
    res = convert_script((EXAMPLE / "marts.sql").read_text(), "teradata", "snowflake", "marts.sql")
    return collect([res], "teradata", "snowflake")


def flat(s):
    return " ".join(s.split())


def test_models_and_materializations(export):
    assert {m.name: m.materialized for m in export.models} == {
        "vt_active": "ephemeral", "customer_revenue": "table", "v_region_revenue": "view"}
    assert export.sources == {"edw_core": {"dim_customer", "fact_sales"}}
    assert len(export.skipped) == 1 and "INSERT" in export.skipped[0]


def test_refs_and_sources(export):
    names = {m.name for m in export.models}
    rev = next(m for m in export.models if m.name == "customer_revenue")
    sql = flat(render_model(rev, names, "snowflake"))
    assert sql.startswith("{{ config(materialized='table') }}")
    assert "FROM {{ ref('vt_active') }} AS a JOIN {{ source('edw_core', 'fact_sales') }} AS f" in sql


def test_unique_primary_index_becomes_tests(export, tmp_path):
    rev = next(m for m in export.models if m.name == "customer_revenue")
    assert rev.unique_key == ["customer_id"]
    write(export, tmp_path, "snowflake")
    schema = (tmp_path / "models" / "schema.yml").read_text()
    assert "- name: customer_id\n        data_tests: [unique, not_null]" in schema
    assert (tmp_path / "dbt_project.yml").exists() and (tmp_path / "models" / "sources.yml").exists()


def test_structure_only_ctas_is_skipped():
    res = convert_script("CREATE TABLE m AS (SEL a FROM t) WITH NO DATA;", "teradata", "snowflake")
    ex = collect([res], "teradata", "snowflake")
    assert not ex.models and "LIMIT 0" in ex.skipped[0]


def test_cli_dbt_flag(tmp_path, capsys):
    assert main(["convert", str(EXAMPLE), "-o", str(tmp_path / "conv"), "--dbt", str(tmp_path / "dbt")]) == 0
    assert "dbt: 3 models, 2 sources" in capsys.readouterr().out
    assert sorted(p.name for p in (tmp_path / "dbt" / "models").glob("*.sql")) == [
        "customer_revenue.sql", "v_region_revenue.sql", "vt_active.sql"]


DBT = shutil.which("dbt") or str(Path(sys.executable).with_name("dbt"))


@pytest.mark.skipif(not Path(DBT).exists() and shutil.which("dbt") is None, reason="dbt not installed")
def test_generated_project_parses_with_real_dbt(tmp_path):
    pytest.importorskip("dbt.adapters.duckdb")
    out = tmp_path / "dbt"
    main(["convert", str(EXAMPLE), "-o", str(tmp_path / "conv"), "--dbt", str(out)])
    (out / "profiles.yml").write_text('migrated:\n  target: dev\n  outputs:\n    dev: {type: duckdb, path: ":memory:"}\n')
    r = subprocess.run([DBT, "ls", "--profiles-dir", ".", "--output", "name"], cwd=out, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    listed = set(r.stdout.split())
    assert {"customer_revenue", "v_region_revenue", "vt_active", "unique_customer_revenue_customer_id"} <= listed
