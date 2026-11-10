package platform

import (
	"fmt"
	"regexp"
	"strings"
	"time"
)

func securityPattern(pattern string) *regexp.Regexp {
	pattern = strings.ReplaceAll(pattern, `(?<!\d)`, ``)
	pattern = strings.ReplaceAll(pattern, `(?!\d)`, ``)
	pattern = strings.ReplaceAll(pattern, `(?<![\d.])`, ``)
	pattern = strings.ReplaceAll(pattern, `(?![\d.])`, ``)
	pattern = strings.ReplaceAll(pattern, `(?!数据|一个数据)`, ``)
	pattern = strings.ReplaceAll(pattern, `\u4e00`, `\x{4e00}`)
	pattern = strings.ReplaceAll(pattern, `\u9fa5`, `\x{9fa5}`)
	r, e := regexp.Compile(pattern)
	if e != nil {
		panic(fmt.Errorf("security rule: %w", e))
	}
	return r
}
func checkInput(text string) M {
	findings := []M{}
	action := "allow"
	for _, kind := range []string{"injection_patterns", "privilege_patterns", "sql_intent_patterns"} {
		for _, v := range arr(metadata[kind]) {
			p := arr(v)
			if securityPattern(str(p[0])).MatchString(text) {
				findings = append(findings, M{"category": kind, "label": p[1], "severity": p[2], "match": securityPattern(str(p[0])).FindString(text)})
				if p[2] == "high" {
					action = "block"
				} else if action == "allow" {
					action = "audit"
				}
			}
		}
	}
	return M{"action": action, "findings": findings, "allowed": action != "block", "safe": len(findings) == 0, "text": text, "risk": map[bool]string{true: "high", false: "low"}[action == "block"]}
}
func checkOutput(text string) M {
	findings := []M{}
	masked := text
	for _, v := range arr(metadata["pii_patterns"]) {
		p := arr(v)
		re := securityPattern(str(p[1]))
		matches := re.FindAllString(text, -1)
		for _, match := range matches {
			findings = append(findings, M{"category": "pii", "type": p[0], "label": p[2], "severity": p[3], "match": "[已脱敏]"})
			masked = strings.ReplaceAll(masked, match, "["+str(p[2])+"已脱敏]")
		}
	}
	for _, kind := range []string{"hallucination_patterns", "harmful_patterns"} {
		for _, v := range arr(metadata[kind]) {
			p := arr(v)
			if securityPattern(str(p[0])).MatchString(text) {
				findings = append(findings, M{"category": kind, "label": p[1], "severity": p[2]})
			}
		}
	}
	action := "allow"
	if masked != text {
		action = "mask"
	} else if len(findings) > 0 {
		action = "audit"
	}
	return M{"action": action, "findings": findings, "text": masked, "masked_text": masked, "original_length": len(text), "allowed": true}
}
func assessSQL(q string) (out M) {
	out = M{"action": "allow", "risk": "low", "findings": []M{}, "readonly": true}
	defer func() {
		if e := recover(); e != nil {
			out = M{"action": "block", "risk": "high", "readonly": false, "requires_approval": true, "reasons": []string{fmt.Sprint(e)}, "findings": []M{{"severity": "high", "label": fmt.Sprint(e)}}}
		}
	}()
	guard(q)
	return
}
func (a *App) scan(ws, ref string) M {
	t := a.resolve(ws, ref)
	cols := a.rows("SELECT * FROM columns WHERE table_id=?", t["id"])
	findings := []M{}
	for _, c := range cols {
		name := strings.ToLower(str(c["name"]))
		for _, spec := range []struct{ Pattern, Tag, Label string }{{"phone|mobile|手机号|电话", "phone", "手机号"}, {"email|邮箱", "email", "邮箱"}, {"id_card|身份证", "id_card", "身份证号"}, {"bank_card|银行卡", "bank_card", "银行卡号"}} {
			if regexp.MustCompile(spec.Pattern).MatchString(name) {
				findings = append(findings, M{"column_id": c["id"], "column": c["name"], "column_name": c["name"], "tag": spec.Tag, "label": spec.Label, "level": "L3", "confidence": 0.95, "evidence": "字段名匹配敏感信息规则"})
			}
		}
	}
	return M{"table_ref": t["ref"], "table": t, "findings": findings, "columns": findings, "count": len(findings)}
}
func (a *App) recordEdge(ws string, m M) M {
	up := str(def(m, "upstream", m["source"]))
	down := str(def(m, "downstream", m["target"]))
	if up == "" || down == "" {
		fail(422, "血缘需要上下游资产")
	}
	r := a.row("SELECT * FROM lineage WHERE workspace_id=? AND upstream=? AND downstream=? AND coalesce(src_column,'')=? AND coalesce(dst_column,'')=?", ws, up, down, str(m["src_column"]), str(m["dst_column"]))
	if r != nil {
		r["created"] = false
		return r
	}
	r = M{"id": id(), "workspace_id": ws, "upstream": up, "downstream": down, "kind": def(m, "kind", "sql"), "detail": def(m, "detail", ""), "src_column": str(m["src_column"]), "dst_column": str(m["dst_column"]), "transform": str(m["transform"]), "created_at": now()}
	a.insert("lineage", r)
	r["created"] = true
	return r
}
func (a *App) lineageGraph(ws, ref, direction string, depth int) M {
	if depth < 1 || depth > 10 {
		depth = 3
	}
	edges := a.list("lineage", ws)
	seen := map[string]bool{}
	visited := map[string]bool{}
	front := []string{ref}
	levels := map[string]int{ref: 0}
	if ref == "" {
		for _, e := range edges {
			front = append(front, str(e["upstream"]), str(e["downstream"]))
		}
	}
	nodes := []M{}
	out := []M{}
	for d := 0; d <= depth; d++ {
		next := []string{}
		for _, key := range front {
			if seen[key] || key == "" {
				continue
			}
			seen[key] = true
			nodes = append(nodes, M{"id": key, "ref": key, "name": key, "label": key, "depth": d, "level": levels[key]})
			for _, e := range edges {
				match := direction != "upstream" && e["upstream"] == key || direction != "downstream" && e["downstream"] == key
				if match {
					if !visited[str(e["id"])] {
						v := M{}
						for k, x := range e {
							v[k] = x
						}
						v["source"] = e["upstream"]
						v["target"] = e["downstream"]
						out = append(out, v)
						visited[str(e["id"])] = true
					}
					if e["upstream"] == key {
						target := str(e["downstream"])
						if _, ok := levels[target]; !ok {
							levels[target] = levels[key] + 1
						}
						next = append(next, target)
					} else {
						target := str(e["upstream"])
						if _, ok := levels[target]; !ok {
							levels[target] = levels[key] - 1
						}
						next = append(next, target)
					}
				}
			}
		}
		front = next
	}
	return M{"nodes": nodes, "edges": out, "root": ref, "table_ref": ref, "direction": direction, "depth": depth}
}
func (a *App) governanceRoutes() []route {
	r := []route{
		{"GET", "/api/security/policy", false, false, func(c *request) any {
			return M{"three_factors": []M{{"key": "untrusted_input", "label": "输入不可信"}, {"key": "state_change", "label": "会改变状态"}, {"key": "sensitive_data", "label": "触及敏感数据"}}, "owasp": metadata["owasp"], "tools": nativeTools()}
		}},
		{"GET", "/api/security/owasp", false, false, func(c *request) any { return metadata["owasp"] }},
		{"POST", "/api/security/check-input", false, false, func(c *request) any {
			out := checkInput(c.s("text"))
			a.audit(c.WS, str(c.User["id"]), "security_input", str(out["action"]))
			return out
		}},
		{"POST", "/api/security/check-output", false, false, func(c *request) any { return checkOutput(c.s("text")) }},
		{"POST", "/api/security/assess-sql", false, false, func(c *request) any { return assessSQL(c.s("sql")) }},
		{"POST", "/api/security/assess-tool", false, false, func(c *request) any {
			args := obj(c.Body["args"])
			write := strings.HasPrefix(c.s("tool"), "create_") || strings.HasPrefix(c.s("tool"), "apply_")
			sensitive := boolOf(obj(c.Body["context"])["sensitive_data"])
			required := write && sensitive
			return M{"risk": map[bool]string{true: "high", false: "low"}[required], "requires_approval": required, "allowed": !required, "action": map[bool]string{true: "block", false: "allow"}[required], "args": args}
		}},
		{"POST", "/api/governance/scan", false, false, func(c *request) any { return a.scan(c.WS, refOf(c.Body)) }},
		{"GET", "/api/governance/tags", false, false, func(c *request) any {
			return M{"tags": a.rows("SELECT ct.*,c.name AS column_name,t.name AS table_name FROM column_tags ct JOIN columns c ON c.id=ct.column_id JOIN tables t ON t.id=c.table_id JOIN schemas s ON s.id=t.schema_id JOIN catalogs ca ON ca.id=s.catalog_id WHERE ca.workspace_id=?", c.WS)}
		}},
		{"POST", "/api/governance/tags", false, false, func(c *request) any {
			items := arr(def(c.Body, "tags", c.Body["findings"]))
			applied := 0
			for _, v := range items {
				m := obj(v)
				col := a.row("SELECT c.id FROM columns c JOIN tables t ON t.id=c.table_id JOIN schemas s ON s.id=t.schema_id JOIN catalogs ca ON ca.id=s.catalog_id WHERE c.id=? AND ca.workspace_id=?", m["column_id"], c.WS)
				if col == nil {
					fail(403, "字段不属于当前工作空间")
				}
				a.exec("INSERT INTO column_tags(id,column_id,tag,level,source,confidence,evidence,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(column_id,tag) DO UPDATE SET level=excluded.level", id(), m["column_id"], m["tag"], def(m, "level", "L3"), "manual", def(m, "confidence", 1), m["evidence"], now())
				applied++
			}
			return M{"applied": applied}
		}},
		{"GET", "/api/governance/policies", false, false, func(c *request) any {
			policies := []M{}
			for _, t := range a.tables(c.WS) {
				findings := arr(a.scan(c.WS, str(t["ref"]))["findings"])
				if len(findings) > 0 {
					policies = append(policies, M{"table_ref": t["ref"], "suggestion": "查询和导出时默认脱敏 L3 字段", "fields": findings})
				}
			}
			return M{"policies": policies}
		}},
		{"GET", "/api/lineage/summary", false, false, func(c *request) any {
			ee := a.list("lineage", c.WS)
			nn := map[string]bool{}
			fields := 0
			for _, e := range ee {
				nn[str(e["upstream"])] = true
				nn[str(e["downstream"])] = true
				if str(e["src_column"]) != "" {
					fields++
				}
			}
			return M{"edges": len(ee), "nodes": len(nn), "table_edges": len(ee) - fields, "column_edges": fields}
		}},
		{"GET", "/api/lineage/edges", false, false, func(c *request) any { rr := a.list("lineage", c.WS); return M{"edges": rr, "count": len(rr)} }},
		{"POST", "/api/lineage/record", false, false, func(c *request) any { e := a.recordEdge(c.WS, c.Body); return M{"edge": e, "created": e["created"]} }},
		{"POST", "/api/lineage/batch", false, false, func(c *request) any {
			out := []M{}
			for _, v := range arr(c.Body["edges"]) {
				out = append(out, a.recordEdge(c.WS, obj(v)))
			}
			return M{"edges": out, "count": len(out)}
		}},
		{"GET", "/api/lineage/graph", false, false, func(c *request) any {
			return a.lineageView(c.WS, firstNonempty(c.q("table_ref"), c.q("table")), c.q("direction"), c.q("view"), int(num(c.q("depth"))))
		}},
		{"GET", "/api/lineage/impact", false, false, func(c *request) any {
			ref := c.q("table_ref")
			if ref == "" {
				ref = c.q("table")
			}
			g := a.lineageGraph(c.WS, ref, "downstream", int(num(c.q("max_depth"))))
			g["impacted"] = g["nodes"]
			g["affected_count"] = len(arr(g["nodes"])) - 1
			return g
		}},
		{"POST", "/api/lineage/discover", false, false, func(c *request) any { return a.discoverLineage(c.WS, c.Body) }},
		{"DELETE", "/api/lineage/{edge_id}", false, false, func(c *request) any {
			a.owned("lineage", c.p("edge_id"), c.WS)
			a.exec("DELETE FROM lineage WHERE id=?", c.p("edge_id"))
			return M{"ok": true}
		}},
		{"GET", "/api/catalog/models", false, false, func(c *request) any { return M{"models": a.list("registered_models", c.WS), "note": ""} }},
		{"GET", "/api/catalog/tree-ext", false, false, func(c *request) any {
			return a.catalogTree(c.WS)
		}},
	}
	for _, path := range []string{"/ops/levels", "/ops/overview", "/ops/tree-flat", "/ops/diagnose"} {
		path := path
		r = append(r, route{"GET", "/api" + path, false, false, func(c *request) any { return a.ops(c.WS, path) }})
	}
	return r
}
func health(rr []M) M {
	total := float64(len(rr))
	if total == 0 {
		return M{"score": 100, "label": "健康"}
	}
	success, failed, running, waiting := float64(0), float64(0), float64(0), float64(0)
	for _, r := range rr {
		switch r["status"] {
		case "success":
			success++
		case "failed":
			failed++
		case "running":
			running++
		case "waiting":
			waiting++
		}
	}
	score := 100 * (0.5*success/total + 0.25*(1-failed/total) + 0.15*(1-waiting/total) + 0.1*(1-running/total))
	label := "健康"
	if score < 90 {
		label = "关注"
	}
	if score < 50 {
		label = "异常"
	}
	return M{"score": score, "label": label}
}
func (a *App) ops(ws, path string) M {
	rr := a.rows("SELECT r.*,f.name AS flow_name FROM flow_runs r JOIN flows f ON f.id=r.flow_id WHERE f.workspace_id=? ORDER BY r.started_at DESC LIMIT 200", ws)
	flows := a.list("flows", ws)
	resources := []M{}
	flat := []M{}
	findings := []M{}
	for _, f := range flows {
		runs := []M{}
		tasks := []M{}
		for _, r := range rr {
			if r["flow_id"] != f["id"] {
				continue
			}
			runs = append(runs, r)
			task := M{"id": r["id"], "level": "task", "name": r["started_at"], "status": r["status"], "health": health([]M{r}), "metrics": r, "children": []M{}}
			tasks = append(tasks, task)
			flat = append(flat, task)
			if r["status"] == "failed" {
				findings = append(findings, M{"severity": "critical", "title": "工作流执行失败", "root_cause": f["name"], "suggestion": "检查节点错误日志并修正 SQL 或上游数据", "scope": f["id"]})
			}
		}
		node := M{"id": f["id"], "level": "resource", "name": f["name"], "health": health(runs), "metrics": M{"runs": len(runs)}, "children": tasks}
		resources = append(resources, node)
		flat = append(flat, node)
	}
	w := a.owned("workspaces", ws, ws)
	workspace := M{"id": ws, "level": "workspace", "name": w["name"], "health": health(rr), "metrics": M{"tables": len(a.tables(ws)), "flows": len(flows), "runs": len(rr)}, "children": resources}
	root := M{"id": "platform", "level": "platform", "name": "OpenDataBuddy", "health": health(rr), "metrics": workspace["metrics"], "children": []M{workspace}}
	flat = append(flat, workspace, root)
	out := M{"root": root, "tree": root, "nodes": flat, "levels": []M{{"key": "platform", "label": "平台层"}, {"key": "workspace", "label": "工作空间层"}, {"key": "resource", "label": "资源层"}, {"key": "task", "label": "任务层"}}, "health": health(rr), "summary": workspace["metrics"], "recent_runs": rr, "findings": findings, "issues": findings, "diagnosis": findings, "generated_at": time.Now().UTC().Format(time.RFC3339)}

	successes, failures, running, queued := 0, 0, 0, 0
	for _, r := range rr {
		switch r["status"] {
		case "success":
			successes++
		case "failed":
			failures++
		case "running":
			running++
		case "waiting", "queued":
			queued++
		}
	}
	metrics := obj(workspace["metrics"])
	metrics["total"] = len(rr)
	metrics["success"] = successes
	metrics["failed"] = failures
	metrics["running"] = running
	metrics["queued"] = queued
	if len(rr) > 0 {
		metrics["success_rate"] = float64(successes) / float64(len(rr))
	}
	causes, suggestions := []M{}, []M{}
	for _, f := range findings {
		causes = append(causes, M{"severity": f["severity"], "title": f["title"], "detail": f["root_cause"]})
		suggestions = append(suggestions, M{"title": f["title"], "action": f["suggestion"]})
	}
	out["root_causes"] = causes
	out["suggestions"] = suggestions
	out["engine"] = "rules"
	out["rules_applied"] = []M{{"id": "failure_rate", "label": "失败率告警线", "threshold": 0.2}, {"id": "consecutive_failures", "label": "连续失败次数", "threshold": 3}, {"id": "timeout", "label": "超时秒数", "threshold": 1800}}
	if path == "/ops/diagnose" {
		out["summary"] = fmt.Sprintf("检查 %d 次运行，成功 %d 次，失败 %d 次；识别 %d 条异常信号。", len(rr), successes, failures, len(causes))
	}
	return out
}

func firstNonempty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func (a *App) catalogTree(ws string) M {
	cc := a.list("catalogs", ws)
	tables := a.tables(ws)
	volumes := a.list("volumes", ws)
	functionCount, schemas := 0, 0
	for _, cat := range cc {
		ss := a.rows("SELECT * FROM schemas WHERE catalog_id=?", cat["id"])
		schemas += len(ss)
		for _, sch := range ss {
			tt := []M{}
			for _, t := range tables {
				if t["schema_id"] == sch["id"] {
					tt = append(tt, t)
				}
			}
			ff := a.rows("SELECT * FROM functions WHERE schema_id=?", sch["id"])
			functionCount += len(ff)
			vv := []M{}
			for _, v := range volumes {
				if v["catalog"] == cat["name"] && v["schema"] == sch["name"] {
					v["ref"] = str(cat["name"]) + "." + str(sch["name"]) + "." + str(v["name"])
					vv = append(vv, v)
				}
			}
			sch["tables"] = tt
			sch["functions"] = ff
			sch["volumes"] = vv
		}
		cat["schemas"] = ss
	}
	return M{"catalogs": cc, "counts": M{"catalogs": len(cc), "schemas": schemas, "tables": len(tables), "functions": functionCount, "volumes": len(volumes), "models": len(a.list("registered_models", ws))}}
}
