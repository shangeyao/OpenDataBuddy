package platform

import (
	"fmt"
	"math"
	"sort"
)

func numbers(v []any) []float64 {
	out := []float64{}
	for _, v := range v {
		if v != nil {
			out = append(out, num(v))
		}
	}
	sort.Float64s(out)
	return out
}
func ks(a, b []float64) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	i, j := 0, 0
	best := float64(0)
	for i < len(a) || j < len(b) {
		x := math.Inf(1)
		if i < len(a) {
			x = a[i]
		}
		if j < len(b) && b[j] < x {
			x = b[j]
		}
		for i < len(a) && a[i] <= x {
			i++
		}
		for j < len(b) && b[j] <= x {
			j++
		}
		best = math.Max(best, math.Abs(float64(i)/float64(len(a))-float64(j)/float64(len(b))))
	}
	return best
}
func distances(metric string, a, b []any) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	aa, bb := numbers(a), numbers(b)
	if metric == "ks" {
		return ks(aa, bb)
	}
	if metric == "wasserstein" {
		sum := float64(0)
		for i := 0; i < 1000; i++ {
			x := aa[i*(len(aa)-1)/999]
			y := bb[i*(len(bb)-1)/999]
			sum += math.Abs(x - y)
		}
		return sum / 1000
	}
	pa, pb := map[string]float64{}, map[string]float64{}
	numeric := metric == "psi"
	lo, hi := aa[0], aa[len(aa)-1]
	for _, v := range a {
		k := str(v)
		if numeric {
			bin := 0
			if hi > lo {
				bin = int((num(v) - lo) / (hi - lo) * 10)
			}
			if bin < 0 {
				bin = 0
			}
			if bin > 9 {
				bin = 9
			}
			k = fmt.Sprint(bin)
		}
		pa[k] += 1 / float64(len(a))
	}
	for _, v := range b {
		k := str(v)
		if numeric {
			bin := 0
			if hi > lo {
				bin = int((num(v) - lo) / (hi - lo) * 10)
			}
			if bin < 0 {
				bin = 0
			}
			if bin > 9 {
				bin = 9
			}
			k = fmt.Sprint(bin)
		}
		pb[k] += 1 / float64(len(b))
	}
	keys := map[string]bool{}
	for k := range pa {
		keys[k] = true
	}
	for k := range pb {
		keys[k] = true
	}
	score := float64(0)
	for k := range keys {
		p, q := math.Max(pa[k], 1e-9), math.Max(pb[k], 1e-9)
		switch metric {
		case "psi":
			score += (p - q) * math.Log(p/q)
		case "js":
			m := (p + q) / 2
			score += (p*math.Log2(p/m) + q*math.Log2(q/m)) / 2
		case "chi2":
			score += (p - q) * (p - q) / p
		default:
			fail(422, "未知漂移指标")
		}
	}
	return score
}
func (a *App) drift(ws string, m M) M {
	t := a.resolve(ws, refOf(m))
	col := colOf(m)
	var baseline M
	if key := str(m["baseline_id"]); key != "" {
		baseline = a.owned("profile_baselines", key, ws)
		if baseline["table_ref"] != t["ref"] {
			fail(422, "基线属于其他表")
		}
	} else {
		baseline = a.row("SELECT * FROM profile_baselines WHERE workspace_id=? AND table_ref=? ORDER BY captured_at DESC LIMIT 1", ws, t["ref"])
	}
	created := false
	if baseline == nil {
		baseline = a.baseline(ws, a.profile(ws, M{"table_ref": t["ref"]}))
		created = true
	}
	stats := obj(baseline["stats"])
	cols := obj(stats["column_stats"])
	if len(cols) == 0 {
		cols = obj(stats["columns"])
	}
	entry := obj(cols[col])
	if len(entry) == 0 {
		fail(422, "基线不包含该字段")
	}
	baseValues := arr(entry["sample"])
	if len(baseValues) == 0 {
		hist := obj(entry["histogram"])
		counts := arr(hist["counts"])
		for i, c := range counts {
			center := num(hist["min"]) + (num(hist["max"])-num(hist["min"]))*float64(i)/math.Max(1, float64(len(counts)-1))
			for j := 0; j < int(num(c)) && len(baseValues) < 5000; j++ {
				baseValues = append(baseValues, center)
			}
		}
		if len(baseValues) == 0 {
			for _, v := range arr(entry["top_values"]) {
				p := obj(v)
				for j := 0; j < int(num(p["count"])) && len(baseValues) < 5000; j++ {
					baseValues = append(baseValues, p["value"])
				}
			}
		}
	}
	colFound := false
	for _, c := range a.rows("SELECT name FROM columns WHERE table_id=?", t["id"]) {
		if c["name"] == col {
			colFound = true
		}
	}
	if !colFound {
		fail(404, "字段不存在")
	}
	current := []any{}
	for _, r := range rows(a.Lake, "SELECT "+quote(col)+" AS v FROM "+physical(t)+" WHERE "+quote(col)+" IS NOT NULL LIMIT 5000") {
		current = append(current, r["v"])
	}
	metric := str(m["metric"])
	if metric == "" {
		metric = "chi2"
		if entry["kind"] == "numeric" {
			metric = "ks"
		}
	}
	score := distances(metric, baseValues, current)
	thresholds := obj(m["thresholds"])
	warn, crit := num(def(thresholds, "warn", 0.1)), num(def(thresholds, "crit", 0.2))
	if metric == "psi" {
		crit = num(def(thresholds, "crit", 0.25))
	}
	normalized := score
	if metric == "wasserstein" {
		spread := num(entry["max"]) - num(entry["min"])
		if spread > 0 {
			normalized = score / spread
		}
	}
	level := "stable"
	if normalized >= warn {
		level = "warning"
	}
	if normalized >= crit {
		level = "critical"
	}
	return M{"table_ref": t["ref"], "column": col, "column_name": col, "metric": metric, "score": score, "normalized_score": normalized, "value": score, "level": level, "drifted": level != "stable", "thresholds": M{"warn": warn, "crit": crit}, "baseline_id": baseline["id"], "baseline_created": created, "baseline_count": len(baseValues), "current_count": len(current), "kind": entry["kind"]}
}
func (a *App) anomaly(ws string, m M) M {
	t := a.resolve(ws, refOf(m))
	current := a.profile(ws, M{"table_ref": t["ref"]})
	baseline := a.row("SELECT * FROM profile_baselines WHERE workspace_id=? AND table_ref=? ORDER BY captured_at DESC LIMIT 1", ws, t["ref"])
	findings := []M{}
	if baseline == nil {
		a.baseline(ws, current)
		return M{"table_ref": t["ref"], "baseline_created": true, "findings": findings, "anomalies": findings, "count": 0}
	}
	old := obj(baseline["stats"])
	if num(old["row_count"]) > 0 && math.Abs(num(current["row_count"])-num(old["row_count"]))/num(old["row_count"]) > 0.3 {
		findings = append(findings, M{"kind": "volume", "severity": "warning", "detail": "行数变化超过 30%"})
	}
	if num(old["column_count"]) != num(current["column_count"]) {
		findings = append(findings, M{"kind": "schema", "severity": "warning", "detail": "字段数量发生变化"})
		// P2-3：结构漂移归档为结构演进事件，供 /api/catalog/evolve 显式补列
		a.triggerEvolutionFromDrift(ws, str(t["ref"]), "字段数量由 "+str(old["column_count"])+" 变为 "+str(current["column_count"]))
	}
	for _, v := range arr(current["columns"]) {
		col := obj(v)
		d := a.drift(ws, M{"table_ref": t["ref"], "column_name": col["name"], "baseline_id": baseline["id"]})
		if d["drifted"] == true {
			findings = append(findings, M{"kind": "distribution", "column_name": col["name"], "severity": d["level"], "detail": js(d)})
		}
	}
	for _, f := range findings {
		f["id"] = id()
		f["workspace_id"] = ws
		f["table_ref"] = t["ref"]
		f["detected_at"] = now()
		a.insert("anomaly_findings", f)
	}
	return M{"table_ref": t["ref"], "baseline_created": false, "findings": findings, "anomalies": findings, "count": len(findings)}
}
func (a *App) driftRoutes() []route {
	return []route{
		{"GET", "/api/drift/metrics", true, true, func(c *request) any { return M{"metrics": metadata["drift_metrics"]} }},
		{"POST", "/api/drift/detect", false, false, func(c *request) any { return a.drift(c.WS, c.Body) }},
		{"POST", "/api/drift/batch", false, false, func(c *request) any {
			t := a.resolve(c.WS, refOf(c.Body))
			cols := arr(c.Body["columns"])
			if len(cols) == 0 {
				for _, r := range a.rows("SELECT name FROM columns WHERE table_id=?", t["id"]) {
					cols = append(cols, r["name"])
				}
			}
			rr := []M{}
			drifted := 0
			for _, col := range cols {
				r := a.drift(c.WS, M{"table_ref": t["ref"], "column_name": col})
				rr = append(rr, r)
				if r["drifted"] == true {
					drifted++
				}
			}
			return M{"table_ref": t["ref"], "results": rr, "columns": rr, "drifted_count": drifted, "total": len(rr)}
		}},
		{"POST", "/api/drift/anomalies", false, false, func(c *request) any { return a.anomaly(c.WS, c.Body) }},
		{"GET", "/api/anomalies", false, false, func(c *request) any {
			rr := []M{}
			unacked := 0
			for _, r := range a.list("anomaly_findings", c.WS) {
				if !boolOf(r["acknowledged"]) {
					unacked++
				}
				if c.q("table") != "" && r["table_ref"] != c.q("table") {
					continue
				}
				if c.q("acknowledged") == "0" && boolOf(r["acknowledged"]) {
					continue
				}
				rr = append(rr, r)
			}
			return M{"anomalies": rr, "unacked": unacked}
		}},
		{"POST", "/api/anomalies/{anomaly_id}/ack", false, false, func(c *request) any {
			a.owned("anomaly_findings", c.p("anomaly_id"), c.WS)
			a.update("anomaly_findings", c.p("anomaly_id"), M{"acknowledged": true})
			return M{"ok": true}
		}},
	}
}
