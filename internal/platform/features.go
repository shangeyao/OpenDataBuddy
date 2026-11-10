package platform

import (
	"fmt"
	"regexp"
	"strings"
)

var aggregateExpr = regexp.MustCompile(`(?i)\b(sum|avg|mean|count|min|max|stddev|stddev_pop|stddev_samp|median|quantile_cont|quantile_disc|approx_count_distinct|any_value|array_agg|list)\s*\(`)

func (a *App) featureRevision(ws string, f M) {
	m := M{}
	for k, v := range f {
		m[k] = v
	}
	m["id"] = id()
	m["feature_id"] = f["id"]
	m["workspace_id"] = ws
	m["created_at"] = now()
	a.insert("feature_revisions", m)
	a.recordEdge(ws, M{"upstream": f["table_ref"], "downstream": "feature:" + str(f["name"]), "kind": "feature", "transform": f["expr"]})
}
func (a *App) entityKey(t M, entity string) string {
	cols := a.rows("SELECT name FROM columns WHERE table_id=? ORDER BY ordinal", t["id"])
	for _, candidate := range []string{entity, entity + "_id"} {
		for _, c := range cols {
			if c["name"] == candidate {
				return candidate
			}
		}
	}
	for _, c := range cols {
		if strings.HasSuffix(str(c["name"]), "_id") {
			return str(c["name"])
		}
	}
	return ""
}
func (a *App) featureSQL(ws string, f M, ids []any) (string, string, M) {
	t := a.resolve(ws, str(f["table_ref"]))
	key := a.entityKey(t, str(f["entity"]))
	expr := str(f["expr"])
	guard("SELECT " + expr)
	selects := []string{}
	if key != "" {
		selects = append(selects, quote(key)+" AS entity_id")
	}
	selects = append(selects, expr+" AS "+quote(str(f["name"])))
	q := "SELECT " + strings.Join(selects, ",") + " FROM " + physical(t)
	if len(ids) > 0 && key != "" {
		values := []string{}
		for _, v := range ids {
			values = append(values, "'"+strings.ReplaceAll(str(v), "'", "''")+"'")
		}
		q += " WHERE " + quote(key) + " IN (" + strings.Join(values, ",") + ")"
	}
	if aggregateExpr.MatchString(expr) && key != "" {
		q += " GROUP BY " + quote(key)
	}
	return q, key, t
}
func (a *App) computeFeatures(ws string, m M) M {
	names := arr(m["names"])
	if len(names) == 0 {
		for _, f := range a.list("features", ws) {
			names = append(names, f["name"])
		}
	}
	computed := []M{}
	for _, name := range names {
		f := a.row("SELECT * FROM features WHERE workspace_id=? AND (name=? OR id=?)", ws, name, name)
		if f == nil {
			fail(404, "特征不存在")
		}
		q, key, t := a.featureSQL(ws, f, arr(m["entity_ids"]))
		preview := a.query(ws, q, int(num(def(m, "limit", 20))))
		col := quote(str(f["name"]))
		stats := rows(a.Lake, "SELECT count(*) AS count,count(*)-count("+col+") AS null_count,count(DISTINCT "+col+") AS distinct,min("+col+") AS min,max("+col+") AS max FROM ("+q+") native_feature")[0]
		if num(stats["count"]) > 0 {
			stats["null_rate"] = num(stats["null_count"]) / num(stats["count"])
		} else {
			stats["null_rate"] = 0
		}
		a.update("features", str(f["id"]), M{"stats": stats, "updated_at": now()})
		item := decoded(f)
		item["stats"] = stats
		item["entity_key"] = key
		item["preview"] = preview
		item["sql"] = q
		item["aggregate"] = aggregateExpr.MatchString(str(f["expr"]))
		if boolOf(m["materialize"]) {
			table := a.importSQL(ws, str(t["catalog_name"])+"."+str(t["schema_name"]), "_feature_"+str(f["name"]), q, "特征物化快照")
			item["materialized"] = M{"table": table["ref"], "rows": table["row_count"]}
		}
		computed = append(computed, item)
	}
	return M{"features": computed, "count": len(computed), "computed_at": now()}
}
func (a *App) trainingSet(ws string, m M) M {
	names := arr(m["feature_names"])
	if len(names) == 0 {
		fail(422, "请选择特征")
	}
	queries := []string{}
	ff := []M{}
	var firstTable M
	for _, name := range names {
		f := a.row("SELECT * FROM features WHERE workspace_id=? AND name=?", ws, name)
		if f == nil {
			fail(404, "特征不存在")
		}
		q, key, t := a.featureSQL(ws, f, arr(m["entity_ids"]))
		if key == "" {
			fail(422, "特征来源表没有可关联的实体键")
		}
		if firstTable == nil {
			firstTable = t
		}
		queries = append(queries, q)
		ff = append(ff, f)
	}
	selects := []string{}
	keys := []string{}
	from := ""
	for i, q := range queries {
		alias := fmt.Sprintf("f%d", i)
		keys = append(keys, alias+".entity_id")
		selects = append(selects, alias+"."+quote(str(ff[i]["name"])))
		if i == 0 {
			from = "(" + q + ") " + alias
		} else {
			from += " FULL OUTER JOIN (" + q + ") " + alias + " USING (entity_id)"
		}
	}
	entity := "coalesce(" + strings.Join(keys, ",") + ")"
	if len(keys) == 1 {
		entity = keys[0]
	}
	selects = append([]string{entity + " AS entity_id"}, selects...)
	if expr := str(m["label_expr"]); expr != "" {
		guard("SELECT " + expr)
		t := firstTable
		if refOf(m) != "" {
			t = a.resolve(ws, refOf(m))
		}
		key := a.entityKey(t, str(ff[0]["entity"]))
		labelExpr := expr
		if !aggregateExpr.MatchString(expr) {
			labelExpr = "max(" + expr + ")"
		}
		from += " LEFT JOIN (SELECT " + quote(key) + " AS entity_id," + labelExpr + " AS label FROM " + physical(t) + " GROUP BY " + quote(key) + ") labels USING (entity_id)"
		selects = append(selects, "labels.label")
	}
	q := "SELECT " + strings.Join(selects, ",") + " FROM " + from
	out := a.query(ws, q, int(num(def(m, "limit", 500))))
	out["feature_count"] = len(ff)
	out["features"] = ff
	out["entity_key"] = "entity_id"
	out["name"] = def(m, "name", "训练集")
	if boolOf(m["materialize"]) {
		name := str(m["name"])
		if name == "" {
			name = "_training_" + id()[:8]
		}
		t := a.importSQL(ws, str(firstTable["catalog_name"])+"."+str(firstTable["schema_name"]), validName(name), q, "训练集物化")
		out["materialized"] = M{"table": t["ref"], "rows": t["row_count"]}
	}
	return out
}
