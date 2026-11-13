package platform

import "strings"

func (a *App) tables(ws string) []M {
	return a.rows(`SELECT t.*,s.name AS schema_name,ca.name AS catalog_name,ca.physical_prefix,ca.name||'.'||s.name||'.'||t.name AS ref FROM tables t JOIN schemas s ON s.id=t.schema_id JOIN catalogs ca ON ca.id=s.catalog_id WHERE ca.workspace_id=? ORDER BY ref`, ws)
}
func (a *App) resolve(ws, ref string) M {
	var found M
	for _, t := range a.tables(ws) {
		if ref == t["ref"] || ref == t["name"] {
			if found != nil {
				fail(400, "表名不唯一，请使用三段名")
			}
			found = t
		}
	}
	if found == nil {
		fail(404, "表不存在："+ref)
	}
	return found
}
func physical(t M) string {
	return quote(lakeNamespace(str(t["physical_prefix"]), str(t["catalog_name"]), str(t["schema_name"]))) + "." + quote(str(t["name"]))
}

func (a *App) dataRoutes() []route {
	return []route{
		{"GET", "/api/catalog/tables", false, false, func(c *request) any { return M{"tables": a.tables(c.WS)} }},
		{"GET", "/api/catalog/tree", false, false, func(c *request) any {
			cc := a.list("catalogs", c.WS)
			for _, cat := range cc {
				ss := a.rows("SELECT * FROM schemas WHERE catalog_id=?", cat["id"])
				for _, s := range ss {
					s["tables"] = a.rows("SELECT * FROM tables WHERE schema_id=?", s["id"])
				}
				cat["schemas"] = ss
			}
			return M{"catalogs": cc}
		}},
		{"GET", "/api/catalog/table", false, false, func(c *request) any {
			t := a.resolve(c.WS, c.q("ref"))
			cols := a.rows("SELECT * FROM columns WHERE table_id=? ORDER BY ordinal", t["id"])
			for _, col := range cols {
				col["tags"] = a.rows("SELECT * FROM column_tags WHERE column_id=?", col["id"])
			}
			t["columns"] = cols
			return M{"table": t, "columns": cols, "lineage": a.list("lineage", c.WS)}
		}},
		{"GET", "/api/catalog/preview", false, false, func(c *request) any {
			t := a.resolve(c.WS, c.q("ref"))
			return a.queryAs(c.WS, c.User, "SELECT * FROM "+physical(t)+" LIMIT 100", 100)
		}},
		{"POST", "/api/catalog/catalogs", false, false, func(c *request) any {
			validName(c.s("name"))
			r := a.insert("catalogs", M{"id": id(), "workspace_id": c.WS, "name": c.s("name"), "physical_prefix": "odb_" + c.WS + "_" + c.s("name"), "description": c.s("description"), "created_at": now()})
			return M{"catalog": r}
		}},
		{"POST", "/api/catalog/schemas", false, false, func(c *request) any {
			validName(c.s("name"))
			cat := a.row("SELECT * FROM catalogs WHERE workspace_id=? AND (id=? OR name=?)", c.WS, c.s("catalog_id"), c.s("catalog"))
			if cat == nil {
				fail(404, "Catalog 不存在")
			}
			_, e := a.Lake.Exec("CREATE SCHEMA IF NOT EXISTS " + quote(lakeNamespace(str(cat["physical_prefix"]), str(cat["name"]), c.s("name"))))
			must(e)
			r := a.insert("schemas", M{"id": id(), "catalog_id": cat["id"], "name": c.s("name"), "description": c.s("description"), "created_at": now()})
			return M{"schema": r}
		}},
		{"DELETE", "/api/catalog/tables/{table_id}", false, false, func(c *request) any {
			var t M
			for _, r := range a.tables(c.WS) {
				if r["id"] == c.p("table_id") {
					t = r
				}
			}
			if t == nil {
				fail(404, "表不存在")
			}
			_, e := a.Lake.Exec("DROP TABLE " + physical(t))
			must(e)
			a.exec("DELETE FROM column_tags WHERE column_id IN (SELECT id FROM columns WHERE table_id=?)", t["id"])
			a.exec("DELETE FROM columns WHERE table_id=?", t["id"])
			a.exec("DELETE FROM tables WHERE id=?", t["id"])
			return M{"ok": true}
		}},
		{"POST", "/api/sql/run", false, false, func(c *request) any {
			r := a.queryAs(c.WS, c.User, c.s("sql"), int(num(def(c.Body, "limit", 1000))))
			r["chart"] = chart(r)
			return r
		}},
		{"GET", "/api/overview", false, false, func(c *request) any {
			tt := a.tables(c.WS)
			count := int64(0)
			for _, t := range tt {
				count += int64(num(t["row_count"]))
			}
			flows := a.list("flows", c.WS)
			enabled := 0
			for _, f := range flows {
				if num(f["enabled"]) > 0 {
					enabled++
				}
			}
			return M{"stats": M{"tables": len(tt), "rows": count, "metrics": len(a.list("metrics", c.WS)), "flows": len(flows), "enabled_flows": enabled, "sensitive_columns": len(a.rows("SELECT * FROM column_tags")), "quality_pass_rate": nil, "conversations": len(a.list("conversations", c.WS)), "dashboards": len(a.list("dashboards", c.WS))}, "tables": tt, "recent_runs": a.rows("SELECT r.*,f.name AS flow_name FROM flow_runs r JOIN flows f ON f.id=r.flow_id WHERE f.workspace_id=? ORDER BY started_at DESC LIMIT 6", c.WS), "trace": a.traces(c.WS), "llm_ready": a.llm("api_key", "") != ""}
		}},
	}
}
func chart(r M) M {
	cols, _ := r["columns"].([]string)
	rr, _ := r["rows"].([][]any)
	if len(cols) == 0 || len(rr) == 0 {
		return M{"type": "empty"}
	}
	numeric := []int{}
	for i := range cols {
		for _, row := range rr {
			switch row[i].(type) {
			case int64, int32, int, float64, float32:
				numeric = append(numeric, i)
			}
			if len(numeric) > 0 && numeric[len(numeric)-1] == i {
				break
			}
		}
	}
	if len(numeric) == 0 {
		return M{"type": "table"}
	}
	if len(rr) == 1 && len(cols) <= 3 {
		return M{"type": "kpi", "value_index": numeric[0], "label": cols[numeric[0]]}
	}
	if len(cols) < 2 {
		return M{"type": "table"}
	}
	x, y := 0, numeric[0]
	if y == 0 && len(cols) > 1 {
		x = 1
	}
	typ := "bar"
	if len(rr) <= 8 {
		typ = "pie"
	}
	if strings.Contains(strings.ToLower(cols[x]), "date") || strings.Contains(strings.ToLower(cols[x]), "time") {
		typ = "line"
	}
	return M{"type": typ, "x": x, "y": y, "x_label": cols[x], "y_label": cols[y]}
}

func lakeNamespace(prefix, catalog, schema string) string {
	return firstNonempty(prefix, catalog) + "__" + schema
}
