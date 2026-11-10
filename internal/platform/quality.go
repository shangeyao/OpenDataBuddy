package platform

import (
	"fmt"
	"gopkg.in/yaml.v3"
	"math"
	"regexp"
	"strings"
	"time"
)

func refOf(m M) string  { return str(def(m, "table_ref", def(m, "table", ""))) }
func colOf(m M) string  { return str(def(m, "column_name", def(m, "column", ""))) }
func boolOf(v any) bool { return v == true || num(v) > 0 || v == "true" }
func ruleType(s string) string {
	switch s {
	case "not_null", "null", "null_rate":
		return "not_null"
	case "unique", "uniqueness":
		return "unique"
	case "sql", "custom_sql":
		return "custom_sql"
	case "enum", "accepted_values":
		return "enum"
	}
	return s
}
func ruleOut(r M) M {
	decoded(r)
	cfg := obj(r["config"])
	r["enabled"] = boolOf(r["enabled"])
	r["severity"] = def(cfg, "severity", "warning")
	r["blocking"] = boolOf(cfg["blocking"])
	for _, v := range arr(metadata["rule_types"]) {
		m := obj(v)
		if ruleType(str(m["type"])) == ruleType(str(r["rule_type"])) {
			r["rule_type_label"] = m["label"]
		}
	}
	return r
}
func (a *App) newRule(ws string, m M) M {
	r := a.prepareRule(ws, m)
	a.insert("quality_rules", r)
	return ruleOut(a.owned("quality_rules", str(r["id"]), ws))
}
func (a *App) prepareRule(ws string, m M) M {
	t := a.resolve(ws, refOf(m))
	typ := ruleType(str(def(m, "rule_type", m["type"])))
	if !strings.Contains("|not_null|unique|range|enum|regex|custom_sql|freshness|", "|"+typ+"|") {
		fail(422, "未知规则类型")
	}
	col := colOf(m)
	if typ != "custom_sql" {
		cols := a.rows("SELECT name FROM columns WHERE table_id=? AND name=?", t["id"], col)
		if len(cols) == 0 {
			fail(422, "字段不存在")
		}
	}
	cfg := obj(m["config"])
	for _, k := range []string{"severity", "blocking"} {
		if v, ok := m[k]; ok {
			cfg[k] = v
		}
	}
	r := M{"id": id(), "workspace_id": ws, "table_ref": t["ref"], "column_name": col, "rule_type": typ, "config": cfg, "enabled": def(m, "enabled", true), "created_at": now()}
	if typ == "custom_sql" {
		a.qualify(ws, guard(str(cfg["sql"])))
	}
	if typ == "regex" {
		if _, e := regexp.Compile(str(cfg["pattern"])); e != nil {
			fail(422, "正则表达式无效")
		}
	}
	if typ == "enum" && len(arr(cfg["values"])) == 0 {
		fail(422, "枚举值不能为空")
	}
	return r
}
func (a *App) runRule(ws string, r M) (out M) {
	out = M{"rule_id": r["id"], "table_ref": r["table_ref"], "column_name": r["column_name"], "rule_type": r["rule_type"], "status": "error", "value": nil, "detail": "", "ran_at": now()}
	defer func() {
		if e := recover(); e != nil {
			out["status"] = "error"
			out["detail"] = fmt.Sprint(e)
		}
		a.insert("quality_rule_runs", M{"id": id(), "workspace_id": ws, "rule_id": r["id"], "table_ref": r["table_ref"], "status": out["status"], "value": js(out["value"]), "detail": out["detail"], "ran_at": now()})
		a.update("quality_rules", str(r["id"]), M{"last_status": out["status"], "last_value": js(out["value"]), "last_run_at": now()})
	}()
	t := a.resolve(ws, str(r["table_ref"]))
	cfg := obj(r["config"])
	col := quote(str(r["column_name"]))
	table := physical(t)
	q := ""
	threshold := num(def(cfg, "max_bad", 0))
	switch ruleType(str(r["rule_type"])) {
	case "not_null":
		q = "SELECT coalesce(avg(CASE WHEN " + col + " IS NULL THEN 1.0 ELSE 0 END),0) AS value FROM " + table
		threshold = num(def(cfg, "max_rate", 0))
		if cfg["max_null"] != nil {
			q = "SELECT count(*)-count(" + col + ") AS value FROM " + table
			threshold = num(cfg["max_null"])
		}
	case "unique":
		q = "SELECT count(" + col + ")-count(DISTINCT " + col + ") AS value FROM " + table
		threshold = num(def(cfg, "max_dup", 0))
		if cfg["max_dup_rate"] != nil {
			q = "SELECT (count(" + col + ")-count(DISTINCT " + col + "))*1.0/nullif(count(*),0) AS value FROM " + table
			threshold = num(cfg["max_dup_rate"])
		}
	case "range":
		min := num(def(cfg, "min", -math.MaxFloat64))
		max := num(def(cfg, "max", math.MaxFloat64))
		q = fmt.Sprintf("SELECT count(*) AS value FROM %s WHERE %s < %g OR %s > %g", table, col, min, col, max)
	case "enum":
		vv := arr(def(cfg, "values", cfg["allowed"]))
		literals := []string{}
		for _, v := range vv {
			literals = append(literals, "'"+strings.ReplaceAll(str(v), "'", "''")+"'")
		}
		if len(literals) == 0 {
			fail(422, "枚举值不能为空")
		}
		q = "SELECT count(*) AS value FROM " + table + " WHERE " + col + " IS NOT NULL AND CAST(" + col + " AS VARCHAR) NOT IN (" + strings.Join(literals, ",") + ")"
	case "regex":
		pattern := str(cfg["pattern"])
		if _, e := regexp.Compile(pattern); e != nil {
			fail(422, "正则表达式无效")
		}
		q = "SELECT count(*) AS value FROM " + table + " WHERE " + col + " IS NOT NULL AND NOT regexp_matches(CAST(" + col + " AS VARCHAR),'" + strings.ReplaceAll(pattern, "'", "''") + "')"
		if cfg["max_bad"] == nil {
			q = "SELECT (" + q + ")*1.0/nullif((SELECT count(*) FROM " + table + "),0) AS value"
			threshold = num(def(cfg, "max_mismatch_rate", 0))
		}
	case "custom_sql":
		q = a.qualify(ws, guard(str(cfg["sql"])))
		threshold = num(def(cfg, "max_value", def(cfg, "expected", 0)))
	case "freshness":
		q = "SELECT epoch(current_timestamp-max(CAST(" + col + " AS TIMESTAMP)))/3600 AS value FROM " + table
		threshold = num(def(cfg, "max_hours", 24))
	}
	rr := rows(a.Lake, q)
	value := float64(0)
	if len(rr) > 0 {
		for _, v := range rr[0] {
			value = num(v)
			break
		}
	}
	out["value"] = value
	out["status"] = "pass"
	if ruleType(str(r["rule_type"])) == "custom_sql" && cfg["expected"] != nil && value != num(cfg["expected"]) {
		out["status"] = "fail"
	}
	if value > threshold {
		out["status"] = "fail"
	}
	out["detail"] = fmt.Sprintf("实际值 %g，阈值 %g", value, threshold)
	out["severity"] = def(cfg, "severity", "warning")
	out["blocking"] = boolOf(cfg["blocking"])
	return
}
func (a *App) runRules(ws, ref string, blocking bool) M {
	if ref != "" {
		ref = str(a.resolve(ws, ref)["ref"])
	}
	results := []M{}
	passed := 0
	for _, r := range a.list("quality_rules", ws) {
		if !boolOf(r["enabled"]) || ref != "" && r["table_ref"] != ref {
			continue
		}
		if blocking && !boolOf(obj(r["config"])["blocking"]) {
			continue
		}
		out := a.runRule(ws, r)
		results = append(results, out)
		if out["status"] == "pass" {
			passed++
		}
	}
	rate := 1.0
	if len(results) > 0 {
		rate = float64(passed) / float64(len(results))
	}
	return M{"total": len(results), "checked": len(results), "passed": passed, "failed": len(results) - passed, "pass_rate": rate, "blocked": blocking && passed < len(results), "results": results}
}
func (a *App) qualitySummary(ws string) M {
	rr := a.list("quality_rules", ws)
	failing := []M{}
	enabled := 0
	byType := M{}
	for _, r := range rr {
		if boolOf(r["enabled"]) {
			enabled++
		}
		k := str(r["rule_type"])
		byType[k] = num(byType[k]) + 1
		if r["last_status"] == "fail" || r["last_status"] == "error" {
			failing = append(failing, ruleOut(r))
		}
	}
	runs := a.rows("SELECT * FROM quality_rule_runs WHERE workspace_id=? AND ran_at>=?", ws, now()[:10])
	passed, errors := 0, 0
	for _, r := range runs {
		if r["status"] == "pass" {
			passed++
		}
		if r["status"] == "error" {
			errors++
		}
	}
	var rate any
	if len(runs) > 0 {
		rate = float64(passed) / float64(len(runs))
	}
	return M{"rule_count": len(rr), "enabled_count": enabled, "today_runs": len(runs), "today_passed": passed, "today_failed": len(runs) - passed - errors, "today_errors": errors, "today_pass_rate": rate, "failing_count": len(failing), "failing_rules": failing, "unacked_anomalies": len(a.rows("SELECT id FROM anomaly_findings WHERE workspace_id=? AND acknowledged=0", ws)), "by_type": byType, "trend": a.rows("SELECT substr(ran_at,1,10) AS date,avg(CASE WHEN status='pass' THEN 1.0 ELSE 0 END) AS pass_rate FROM quality_rule_runs WHERE workspace_id=? GROUP BY date ORDER BY date DESC LIMIT 7", ws)}
}
func (a *App) profile(ws string, m M) M {
	started := time.Now()
	t := a.resolve(ws, refOf(m))
	mode := str(def(m, "mode", "snapshot"))
	if mode != "snapshot" && mode != "inference" && mode != "timeseries" {
		fail(422, "未知剖析模式")
	}
	table := physical(t)
	source := table
	if mode == "inference" {
		source = "(SELECT * FROM " + table + " LIMIT 5000)"
	}
	desc := rows(a.Lake, "DESCRIBE "+table)
	count := rows(a.Lake, "SELECT count(*) AS n FROM "+source)[0]["n"]
	cols := M{}
	names := []string{}
	for _, d := range desc {
		col := str(d["column_name"])
		names = append(names, col)
		typ := str(d["column_type"])
		q := quote(col)
		stats := rows(a.Lake, "SELECT count(*)-count("+q+") AS null_count,count(DISTINCT "+q+") AS distinct_count,min("+q+") AS min,max("+q+") AS max FROM "+source)[0]
		stats["name"] = col
		stats["data_type"] = typ
		stats["kind"] = "string"
		if num(count) > 0 {
			stats["null_rate"] = num(stats["null_count"]) / num(count)
		} else {
			stats["null_rate"] = 0.0
		}
		if regexp.MustCompile(`(?i)int|double|float|decimal|real`).MatchString(typ) {
			stats["kind"] = "numeric"
			v := rows(a.Lake, "SELECT avg("+q+") AS mean,stddev_pop("+q+") AS stddev,quantile_cont("+q+",0.25) AS p25,quantile_cont("+q+",0.5) AS p50,quantile_cont("+q+",0.75) AS p75,quantile_cont("+q+",0.9) AS p90,quantile_cont("+q+",0.99) AS p99 FROM "+source)[0]
			for k, x := range v {
				stats[k] = x
			}
			samples := rows(a.Lake, "SELECT "+q+" AS v FROM "+source+" WHERE "+q+" IS NOT NULL LIMIT 5000")
			vals := []any{}
			for _, v := range samples {
				vals = append(vals, v["v"])
			}
			stats["sample"] = vals
			stats["quantiles"] = M{"p25": v["p25"], "p50": v["p50"], "p75": v["p75"], "p90": v["p90"], "p99": v["p99"]}
			lo, hi := num(stats["min"]), num(stats["max"])
			width := (hi - lo) / 10
			if width <= 0 {
				width = 1
			}
			bins := []M{}
			counts := []any{}
			for i := 0; i < 10; i++ {
				bins = append(bins, M{"lo": lo + float64(i)*width, "hi": lo + float64(i+1)*width, "count": 0})
				counts = append(counts, 0)
			}
			histSQL := fmt.Sprintf("SELECT least(9,greatest(0,CAST(floor((%s-%g)/%g) AS INTEGER))) AS bucket,count(*) AS n FROM %s WHERE %s IS NOT NULL GROUP BY 1", q, lo, width, source, q)
			for _, b := range rows(a.Lake, histSQL) {
				i := int(num(b["bucket"]))
				bins[i]["count"] = b["n"]
				counts[i] = b["n"]
			}
			stats["histogram"] = M{"min": lo, "max": hi, "width": width, "bins": bins, "counts": counts}

		}
		stats["top_values"] = rows(a.Lake, "SELECT CAST("+q+" AS VARCHAR) AS value,count(*) AS count FROM "+source+" GROUP BY "+q+" ORDER BY count DESC LIMIT 10")
		cols[col] = stats
	}
	columnList := []M{}
	for _, name := range names {
		stat := obj(cols[name])
		stat["count"] = count
		stat["distinct"] = stat["distinct_count"]
		stat["std"] = stat["stddev"]
		for _, v := range arr(stat["top_values"]) {
			row := obj(v)
			if num(count) > 0 {
				row["ratio"] = num(row["count"]) / num(count)
			}
		}
		columnList = append(columnList, stat)
	}
	out := M{"table_ref": t["ref"], "mode": mode, "row_count": count, "column_count": len(desc), "columns": columnList, "column_names": names, "column_stats": cols, "sampled": mode == "inference", "captured_at": now(), "timeseries": []M{}}
	if mode == "timeseries" {
		tc := str(m["time_column"])
		if tc == "" {
			for _, d := range desc {
				if strings.Contains(str(d["column_type"]), "DATE") || strings.Contains(str(d["column_type"]), "TIMESTAMP") {
					tc = str(d["column_name"])
					break
				}
			}
		}
		if tc == "" {
			fail(422, "请指定时间列")
		}
		grain := str(def(m, "granularity", "day"))
		if grain != "day" && grain != "week" && grain != "month" && grain != "hour" {
			fail(422, "时间粒度无效")
		}
		numeric := []string{}
		aggs := []string{"count(*) AS total"}
		for _, name := range names {
			if obj(cols[name])["kind"] == "numeric" {
				numeric = append(numeric, name)
				aggs = append(aggs, "avg("+quote(name)+") AS "+quote("mean_"+name))
			}
		}
		series := rows(a.Lake, "SELECT date_trunc('"+grain+"',CAST("+quote(tc)+" AS TIMESTAMP)) AS bucket,"+strings.Join(aggs, ",")+" FROM "+table+" GROUP BY bucket ORDER BY bucket")
		for _, row := range series {
			means := M{}
			for _, name := range numeric {
				means[name] = M{"avg": row["mean_"+name]}
				delete(row, "mean_"+name)
			}
			row["columns"] = means
		}
		out["series"] = series
		out["time_column"] = tc
		out["granularity"] = grain
		out["numeric_columns"] = numeric
		out["bucket_count"] = len(series)
	}
	out["elapsed_ms"] = time.Since(started).Milliseconds()
	if boolOf(m["save_baseline"]) {
		out["baseline"] = a.baseline(ws, out)
	}
	return out
}
func (a *App) baseline(ws string, p M) M {
	stats := M{}
	for k, v := range p {
		if k != "columns" && k != "column_stats" {
			stats[k] = v
		}
	}
	stats["columns"] = p["column_stats"]
	if stats["columns"] == nil {
		stats["columns"] = p["columns"]
	}
	r := M{"id": id(), "workspace_id": ws, "table_ref": p["table_ref"], "mode": def(p, "mode", "snapshot"), "column_name": "", "stats": stats, "captured_at": now()}
	a.insert("profile_baselines", r)
	return r
}
func parseRules(m M) []M {
	var doc M
	text := str(def(m, "yaml", def(m, "text", m["content"])))
	if len(text) > 1<<20 {
		fail(413, "YAML 过大")
	}
	if e := yaml.Unmarshal([]byte(text), &doc); e != nil {
		fail(422, "YAML 无效："+e.Error())
	}
	out := []M{}
	for _, v := range arr(doc["rules"]) {
		r := obj(v)
		if refOf(r) == "" {
			fail(422, "规则缺少 table")
		}
		out = append(out, r)
	}
	if len(out) == 0 {
		fail(422, "YAML 不包含规则")
	}
	return out
}
func (a *App) qualityRoutes() []route {
	r := []route{}
	for _, path := range []string{"/quality/rules", "/governance/quality"} {
		path := path
		r = append(r,
			route{"GET", "/api" + path, false, false, func(c *request) any {
				rr := []M{}
				for _, v := range a.list("quality_rules", c.WS) {
					if c.q("table") == "" || v["table_ref"] == c.q("table") {
						rr = append(rr, ruleOut(v))
					}
				}
				return M{"rules": rr, "types": metadata["rule_types"], "severities": []string{"info", "warning", "critical"}}
			}},
			route{"POST", "/api" + path, false, false, func(c *request) any { return M{"rule": a.newRule(c.WS, c.Body)} }},
			route{"PATCH", "/api" + path + "/{rule_id}", false, false, func(c *request) any {
				a.owned("quality_rules", c.p("rule_id"), c.WS)
				m := M{}
				for _, k := range []string{"config", "enabled", "rule_type", "column_name"} {
					if v, ok := c.Body[k]; ok {
						m[k] = v
					}
				}
				a.update("quality_rules", c.p("rule_id"), m)
				return M{"rule": ruleOut(a.owned("quality_rules", c.p("rule_id"), c.WS))}
			}},
			route{"DELETE", "/api" + path + "/{rule_id}", false, false, func(c *request) any {
				a.owned("quality_rules", c.p("rule_id"), c.WS)
				a.exec("DELETE FROM quality_rules WHERE id=?", c.p("rule_id"))
				return M{"ok": true}
			}},
			route{"GET", "/api" + path + "/{rule_id}", false, false, func(c *request) any {
				return M{"rule": ruleOut(a.owned("quality_rules", c.p("rule_id"), c.WS)), "runs": a.rows("SELECT * FROM quality_rule_runs WHERE rule_id=? ORDER BY ran_at DESC LIMIT 50", c.p("rule_id")), "stats": M{}}
			}},
			route{"POST", "/api" + path + "/run-all", false, false, func(c *request) any { return a.runRules(c.WS, c.q("table"), false) }},
			route{"POST", "/api" + path + "/{rule_id}/run", false, false, func(c *request) any { return a.runRule(c.WS, a.owned("quality_rules", c.p("rule_id"), c.WS)) }},
			route{"GET", "/api" + path + "/{rule_id}/runs", false, false, func(c *request) any {
				a.owned("quality_rules", c.p("rule_id"), c.WS)
				return M{"runs": a.rows("SELECT * FROM quality_rule_runs WHERE rule_id=? ORDER BY ran_at DESC LIMIT 50", c.p("rule_id"))}
			}},
		)
	}
	r = append(r,
		route{"GET", "/api/quality/summary", false, false, func(c *request) any { return a.qualitySummary(c.WS) }},
		route{"GET", "/api/quality/meta", false, false, func(c *request) any {
			return M{"rule_types": metadata["rule_types"], "severities": []M{{"value": "info", "label": "提示"}, {"value": "warning", "label": "告警"}, {"value": "critical", "label": "严重"}}, "profile_modes": metadata["profile_modes"], "drift_metrics": metadata["drift_metrics"], "histogram_bins": 10}
		}},
		route{"POST", "/api/quality/precheck", false, false, func(c *request) any { return a.runRules(c.WS, str(a.resolve(c.WS, refOf(c.Body))["ref"]), true) }},
		route{"POST", "/api/quality/rules/validate", false, false, func(c *request) any {
			rr := parseRules(c.Body)
			return M{"ok": true, "rules": rr, "count": len(rr), "issues": []M{}}
		}},
		route{"POST", "/api/quality/rules/import", false, false, func(c *request) any {
			rr := parseRules(c.Body)
			out := []M{}
			prepared := []M{}
			targets := map[string]bool{}
			for _, r := range rr {
				p := a.prepareRule(c.WS, r)
				prepared = append(prepared, p)
				targets[str(p["table_ref"])] = true
			}
			if boolOf(c.Body["replace"]) {
				for ref := range targets {
					a.exec("DELETE FROM quality_rules WHERE workspace_id=? AND table_ref=?", c.WS, ref)
				}
			}
			for _, p := range prepared {
				a.insert("quality_rules", p)
				out = append(out, ruleOut(a.owned("quality_rules", str(p["id"]), c.WS)))
			}
			return M{"created": len(out), "skipped": []M{}, "rules": out}
		}},
		route{"GET", "/api/quality/rules/export", false, false, func(c *request) any {
			rr := []M{}
			for _, r := range a.list("quality_rules", c.WS) {
				rr = append(rr, M{"table": r["table_ref"], "column": r["column_name"], "type": r["rule_type"], "config": obj(r["config"])})
			}
			b, e := yaml.Marshal(M{"version": 1, "rules": rr})
			must(e)
			return M{"yaml": string(b), "text": string(b)}
		}},
		route{"GET", "/api/profiling/modes", true, true, func(c *request) any { return M{"modes": metadata["profile_modes"]} }},
		route{"POST", "/api/profiling/profile", false, false, func(c *request) any { return a.profile(c.WS, c.Body) }},
		route{"POST", "/api/governance/profile", false, false, func(c *request) any { return a.profile(c.WS, c.Body) }},
		route{"GET", "/api/profiling/baselines", false, false, func(c *request) any {
			rr := a.list("profile_baselines", c.WS)
			for _, r := range rr {
				decoded(r)
				s := obj(r["stats"])
				r["row_count"] = s["row_count"]
				r["column_count"] = s["column_count"]
				r["column_names"] = s["column_names"]
			}
			return M{"baselines": rr}
		}},
		route{"POST", "/api/profiling/baselines", false, false, func(c *request) any {
			p := obj(def(c.Body, "stats", c.Body["profile"]))
			if len(p) == 0 {
				p = a.profile(c.WS, c.Body)
			}
			p["table_ref"] = a.resolve(c.WS, refOf(c.Body))["ref"]
			return M{"baseline": a.baseline(c.WS, p)}
		}},
		route{"DELETE", "/api/profiling/baselines/{baseline_id}", false, false, func(c *request) any {
			a.owned("profile_baselines", c.p("baseline_id"), c.WS)
			a.exec("DELETE FROM profile_baselines WHERE id=?", c.p("baseline_id"))
			return M{"ok": true}
		}},
	)
	return r
}
