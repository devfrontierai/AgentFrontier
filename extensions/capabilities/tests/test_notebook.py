"""Notebook cells: rendering to Python, the .ipynb round trip (edited cells become user code), outputs, and the
Markdown report."""

from __future__ import annotations

import json

import pytest

from agentfrontier_capabilities.notebook import (Cell, Output, cell_hash, dumps, from_ipynb, prelude, render,
                                                 report_markdown, to_ipynb)


def cells() -> list[Cell]:
    return [Cell("markdown", "# Monthly revenue\n\nRevenue excludes cancelled orders."),
            prelude("retail"),
            Cell("sql", "select date_trunc('month', order_date) as month, sum(total_amount) as revenue\n"
                        "from sales.orders where status <> 'cancelled' group by 1 order by 1", "revenue",
                 title="Revenue per month", id="c1"),
            Cell("chart", chart={"data": "revenue", "type": "line", "x": "month", "y": ["revenue"],
                                 "title": "Revenue per month"}, id="c2"),
            Cell("python", "print(revenue['revenue'].sum())", authored="llm", id="c3")]


def test_render() -> None:
    c = cells()
    assert render(c[2]).startswith('revenue = query("""\nselect date_trunc') and render(c[2]).endswith(
        "revenue.head(20)")
    assert render(c[3]) == ("ax = revenue.plot(kind='line', x='month', y='revenue', figsize=(9, 4), "
                            "title='Revenue per month', legend=False)\nplt.tight_layout()\nplt.show()")
    pie = Cell("chart", chart={"data": "d", "type": "pie", "x": "segment", "y": ["n"]})
    assert ".set_index('segment')['n'].plot.pie(" in render(pie)
    compile(render(c[1]), "<prelude>", "exec")
    tricky = Cell("sql", 'select \'a"\' as x', "t")
    compile(render(tricky), "<sql>", "exec")
    with pytest.raises(ValueError):
        Cell("sql", "select 1", "Bad Name")
    with pytest.raises(ValueError):
        Cell("chart", chart={"data": "d", "type": "radar", "x": "a", "y": ["b"]})


def test_ipynb_round_trip_and_edits() -> None:
    c = cells()
    out = {"c1": Output("ok", table={"columns": ["month", "revenue"], "rows": [["2025-01-01", 10.5]], "rowCount": 1}),
           "c2": Output("ok", image_png="iVBORw0KGgo="),
           "c3": Output("error", error={"type": "KeyError", "message": "'revenu'", "traceback": ["..."]})}
    nb = to_ipynb(c, "Monthly revenue", out, {"sourceId": "retail"})
    assert nb["nbformat"] == 4 and nb["metadata"]["avero"] == {"sourceId": "retail"}
    code = [x for x in nb["cells"] if x["cell_type"] == "code"]
    assert [x["execution_count"] for x in code] == [None, 2, 3, 4]
    assert code[1]["outputs"][0]["output_type"] == "execute_result"
    assert code[2]["outputs"][0]["data"]["image/png"] == "iVBORw0KGgo="
    assert code[3]["outputs"][0]["ename"] == "KeyError"
    json.loads(dumps(nb))
    back = from_ipynb(nb)
    assert [(x.kind, x.authored) for x in back] == [("markdown", "template"), ("python", "template"),
                                                     ("sql", "template"), ("chart", "template"), ("python", "llm")]
    assert back[2].source == c[2].source and back[3].chart == c[3].chart
    assert [cell_hash(x) for x in back] == [cell_hash(x) for x in c]
    nb["cells"][2]["source"] = ["revenue = query('select 1')\n"]  # a person edited the SQL cell
    edited = from_ipynb(nb)[2]
    assert (edited.kind, edited.authored) == ("python", "user")


def test_report() -> None:
    c = cells()
    out = {"c1": Output("ok", table={"columns": ["month", "revenue"], "rows": [["2025-01", 10.5]], "rowCount": 12}),
           "c2": Output("ok", image_png="x"), "c3": Output("error", error={"type": "KeyError", "message": "'revenu'"})}
    md = report_markdown("Monthly revenue", c, out, [{"name": "revenue", "definition": "order totals",
                                                      "expression": "sum(total_amount)", "source": "dbt"}],
                         {"c2": "report_files/c2.png"}, ["Cancelled orders excluded."])
    assert md.startswith("# Monthly revenue\n\n## Definitions\n\n* **revenue**: order totals (`sum(total_amount)`)")
    assert "| month | revenue |\n|---|---|\n| 2025-01 | 10.5 |" in md and "_12 rows; the first 10 are shown._" in md
    assert "![chart](report_files/c2.png)" in md and "> **Failed:** KeyError: 'revenu'" in md
    assert md.rstrip().endswith("* Cancelled orders excluded.") and "Setup" not in md
