package platform

import (
	"fmt"
	"gopkg.in/yaml.v3"
	"strings"
)

var bundleKinds = map[string]string{"flows": "flows", "metrics": "metrics", "dashboards": "dashboards", "quality_rules": "quality_rules", "knowledge": "knowledge_docs", "agents": "agents"}

func bundleTemplate(name string) string {
	b, e := yaml.Marshal(M{"bundle": name, "targets": M{"dev": M{}, "staging": M{}, "prod": M{}}, "resources": M{"flows": []any{}, "metrics": []any{}, "dashboards": []any{}, "quality_rules": []any{}, "agents": []any{}}})
	must(e)
	return string(b)
}
func (a *App) validateBundle(ws, text string) M {
	doc := M{}
	issues := []M{}
	if len(text) > 1<<20 {
		fail(413, "YAML 过大")
	}
	if e := yaml.Unmarshal([]byte(text), &doc); e != nil {
		return M{"ok": false, "issues": []M{{"level": "error", "message": e.Error()}}, "parsed": M{}, "counts": M{}}
	}
	if str(doc["bundle"]) == "" {
		issues = append(issues, M{"level": "error", "message": "缺少 bundle 名称"})
	}
	if len(obj(doc["targets"])) == 0 {
		issues = append(issues, M{"level": "error", "message": "至少声明一个 targets 环境"})
	}
	counts := M{}
	for kind, v := range obj(doc["resources"]) {
		if bundleKinds[kind] == "" {
			issues = append(issues, M{"level": "error", "message": "未知资源类型：" + kind})
			continue
		}
		items := arr(v)
		counts[kind] = len(items)
		seen := map[string]bool{}
		for _, v := range items {
			m := obj(v)
			key := "name"
			if kind == "knowledge" {
				key = "title"
			}
			name := str(m[key])
			if name == "" || seen[name] {
				issues = append(issues, M{"level": "error", "message": kind + " 资源名称为空或重复"})
			}
			seen[name] = true
			if kind == "flows" {
				normalizeNodes(m)
			}
			if kind == "metrics" || kind == "quality_rules" {
				a.resolve(ws, str(m["table_ref"]))
			}
		}
	}
	return M{"ok": len(issues) == 0, "issues": issues, "parsed": doc, "counts": counts, "envs": obj(doc["targets"])}
}
func sameValue(a, b any) bool {
	if a == nil && b == nil {
		return true
	}
	switch x := a.(type) {
	case string:
		var p any
		if strings.HasPrefix(x, "[") || strings.HasPrefix(x, "{") {
			p = obj(x)
			if strings.HasPrefix(x, "[") {
				p = arr(x)
			}
			return js(p) == js(b)
		}
	case int64:
		if y, ok := b.(bool); ok {
			return x != 0 == y
		}
		return num(a) == num(b)
	}
	return js(a) == js(b)
}
func (a *App) bundleRecord(b M, env, action string, log []M, summary string) {
	a.insert("bundle_deployments", M{"id": id(), "bundle_id": b["id"], "workspace_id": b["workspace_id"], "env": env, "action": action, "status": "success", "summary": summary, "log": log, "created_at": now()})
}
func (a *App) deployBundle(c *request, b M, env string) M {
	valid := a.validateBundle(c.WS, str(b["yaml"]))
	if valid["ok"] != true {
		return valid
	}
	doc := obj(valid["parsed"])
	target, ok := obj(doc["targets"])[env]
	if !ok {
		fail(422, "未声明目标环境")
	}
	ws := c.WS
	if name := str(obj(target)["workspace"]); name != "" {
		w := a.row("SELECT id FROM workspaces WHERE name=?", name)
		if w == nil {
			fail(404, "目标工作空间不存在")
		}
		ws = a.workspace(c.User, str(w["id"]))
	}
	counts := M{"created": 0, "updated": 0, "unchanged": 0}
	detail := []M{}
	for kind, v := range obj(doc["resources"]) {
		table := bundleKinds[kind]
		for _, v := range arr(v) {
			m := obj(v)
			key := "name"
			if kind == "knowledge" {
				key = "title"
			}
			name := str(m[key])
			current := a.row("SELECT * FROM "+quote(table)+" WHERE workspace_id=? AND "+quote(key)+"=?", ws, name)
			if kind == "quality_rules" {
				current = a.row("SELECT * FROM quality_rules WHERE workspace_id=? AND table_ref=? AND column_name=? AND rule_type=?", ws, m["table_ref"], m["column_name"], m["rule_type"])
			}
			action := "created"
			if current != nil {
				changes := M{}
				for k, v := range m {
					if a.columns[table][k] && k != "workspace_id" && k != "id" && !sameValue(current[k], v) {
						changes[k] = v
					}
				}
				if len(changes) == 0 {
					action = "unchanged"
				} else {
					action = "updated"
					a.update(table, str(current["id"]), changes)
				}
			} else {
				m["id"] = id()
				m["workspace_id"] = ws
				m["created_at"] = now()
				if kind == "flows" {
					normalizeNodes(m)
				}
				if kind == "metrics" {
					m["dimensions"] = []any{}
					m["joins"] = []any{}
				}
				a.insert(table, m)
				current = m
			}
			counts[action] = int(num(counts[action])) + 1
			detail = append(detail, M{"kind": kind, "kind_label": kind, "name": name, "action": action, "id": current["id"], "workspace_id": ws})
		}
	}
	summary := fmt.Sprintf("%s：创建 %v，更新 %v，未变 %v", env, counts["created"], counts["updated"], counts["unchanged"])
	a.bundleRecord(b, env, "deploy", detail, summary)
	a.update("bundles", str(b["id"]), M{"status": "valid", "issues": []M{}})
	return M{"ok": true, "env": env, "counts": counts, "detail": detail, "summary": summary}
}
func (a *App) bundleSummary(c *request, b M) M {
	valid := a.validateBundle(c.WS, str(b["yaml"]))
	doc := obj(valid["parsed"])
	envs := M{}
	for env := range obj(doc["targets"]) {
		present, declared := 0, 0
		missing := []M{}
		for kind, v := range obj(doc["resources"]) {
			key := "name"
			if kind == "knowledge" {
				key = "title"
			}
			table := bundleKinds[kind]
			if table == "" {
				continue
			}
			for _, v := range arr(v) {
				m := obj(v)
				declared++
				if a.columns[table][key] && a.row("SELECT id FROM "+quote(table)+" WHERE workspace_id=? AND "+quote(key)+"=?", c.WS, m[key]) != nil {
					present++
				} else {
					missing = append(missing, M{"kind": kind, "name": m[key]})
				}
			}
		}
		envs[env] = M{"env": env, "declared": declared, "present": present, "missing": missing, "drift": declared - present, "last_deploy": a.row("SELECT * FROM bundle_deployments WHERE bundle_id=? AND env=? ORDER BY created_at DESC LIMIT 1", b["id"], env)}
	}
	history := a.rows("SELECT * FROM bundle_deployments WHERE bundle_id=? ORDER BY created_at DESC LIMIT 30", b["id"])
	for _, h := range history {
		decoded(h)
	}
	return M{"bundle": b, "envs": envs, "history": history, "issues": valid["issues"]}
}
func (a *App) bundleRoutes() []route {
	r := []route{
		{"GET", "/api/bundles", false, false, func(c *request) any {
			rr := a.list("bundles", c.WS)
			for _, b := range rr {
				decoded(b)
				b["last_deployment"] = a.row("SELECT * FROM bundle_deployments WHERE bundle_id=? ORDER BY created_at DESC LIMIT 1", b["id"])
			}
			return M{"bundles": rr, "envs": []string{"dev", "staging", "prod"}, "kinds": bundleKinds}
		}},
		{"POST", "/api/bundles", false, false, func(c *request) any {
			if c.s("name") == "" {
				fail(422, "Bundle 名称不能为空")
			}
			b := a.insert("bundles", M{"id": id(), "workspace_id": c.WS, "name": c.s("name"), "yaml": bundleTemplate(c.s("name")), "status": "draft", "issues": []M{}, "created_at": now(), "updated_at": now()})
			return M{"bundle": decoded(b)}
		}},
		{"GET", "/api/bundles/template", true, true, func(c *request) any {
			return M{"yaml": bundleTemplate("my-bundle"), "envs": []string{"dev", "staging", "prod"}}
		}},
		{"GET", "/api/bundles/{bundle_id}", false, false, func(c *request) any { return M{"bundle": decoded(a.owned("bundles", c.p("bundle_id"), c.WS))} }},
		{"PATCH", "/api/bundles/{bundle_id}", false, false, func(c *request) any {
			a.owned("bundles", c.p("bundle_id"), c.WS)
			a.update("bundles", c.p("bundle_id"), M{"yaml": c.s("yaml"), "status": "draft", "updated_at": now()})
			return M{"bundle": decoded(a.owned("bundles", c.p("bundle_id"), c.WS))}
		}},
		{"POST", "/api/bundles/{bundle_id}/validate", false, false, func(c *request) any {
			b := a.owned("bundles", c.p("bundle_id"), c.WS)
			out := a.validateBundle(c.WS, str(b["yaml"]))
			status := "invalid"
			if out["ok"] == true {
				status = "valid"
			}
			a.update("bundles", str(b["id"]), M{"status": status, "issues": out["issues"]})
			return out
		}},
		{"POST", "/api/bundles/{bundle_id}/deploy", false, false, func(c *request) any {
			return a.deployBundle(c, a.owned("bundles", c.p("bundle_id"), c.WS), str(def(c.Body, "env", "dev")))
		}},
		{"GET", "/api/bundles/{bundle_id}/summary", false, false, func(c *request) any { return a.bundleSummary(c, a.owned("bundles", c.p("bundle_id"), c.WS)) }},
		{"GET", "/api/bundles/{bundle_id}/deployments", false, false, func(c *request) any {
			a.owned("bundles", c.p("bundle_id"), c.WS)
			rr := a.rows("SELECT * FROM bundle_deployments WHERE bundle_id=? ORDER BY created_at DESC", c.p("bundle_id"))
			for _, r := range rr {
				decoded(r)
			}
			return M{"deployments": rr}
		}},
		{"POST", "/api/bundles/{bundle_id}/run", false, false, func(c *request) any {
			b := a.owned("bundles", c.p("bundle_id"), c.WS)
			out := a.validateBundle(c.WS, str(b["yaml"]))
			if out["ok"] != true {
				return out
			}
			runs := []M{}
			for _, v := range arr(obj(obj(out["parsed"])["resources"])["flows"]) {
				name := str(obj(v)["name"])
				if c.s("flow") != "" && c.s("flow") != name {
					continue
				}
				f := a.row("SELECT * FROM flows WHERE workspace_id=? AND name=?", c.WS, name)
				if f == nil {
					fail(400, "请先发布工作流")
				}
				runs = append(runs, a.runFlow(c.WS, f, M{"env": def(c.Body, "env", "dev")}, "cicd", 0))
			}
			return M{"ok": true, "runs": runs}
		}},
		{"POST", "/api/bundles/{bundle_id}/destroy", false, false, func(c *request) any {
			b := a.owned("bundles", c.p("bundle_id"), c.WS)
			env := str(def(c.Body, "env", "dev"))
			rr := a.rows("SELECT * FROM bundle_deployments WHERE bundle_id=? AND env=? AND action='deploy' ORDER BY created_at", b["id"], env)
			removed := []M{}
			seen := map[string]bool{}
			for _, record := range rr {
				for _, v := range arr(record["log"]) {
					m := obj(v)
					key := str(m["id"])
					if m["action"] != "created" || seen[key] {
						continue
					}
					seen[key] = true
					table := bundleKinds[str(m["kind"])]
					if table == "" {
						continue
					}
					ws := c.WS
					if str(m["workspace_id"]) != "" {
						ws = a.workspace(c.User, str(m["workspace_id"]))
					}
					a.exec("DELETE FROM "+quote(table)+" WHERE id=? AND workspace_id=?", key, ws)
					removed = append(removed, m)
				}
			}
			a.bundleRecord(b, env, "destroy", removed, fmt.Sprintf("删除 %d 个自建资源", len(removed)))
			return M{"ok": true, "removed": removed, "count": len(removed)}
		}},
	}
	return r
}
