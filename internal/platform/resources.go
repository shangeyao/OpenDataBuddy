package platform

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var metadata M

func init() {
	b, e := assets.ReadFile("assets/metadata.json")
	must(e)
	must(json.Unmarshal(b, &metadata))
}
func decoded(r M) M {
	if r == nil {
		return nil
	}
	for _, k := range []string{"dimensions", "joins", "sections", "channels", "condition", "snapshot", "config", "params", "spec", "stats", "metrics", "traffic", "meta", "issues", "nodes", "edges", "cells", "tools", "table_refs", "synonyms", "tables", "metric_ids", "resources", "log", "output"} {
		if s, ok := r[k].(string); ok && s != "" {
			var v any
			if json.Unmarshal([]byte(s), &v) == nil {
				r[k] = v
			}
		}
	}
	return r
}

type resource struct {
	Path, Table, Plural, Singular, Param string
	Defaults                             M
}

func (a *App) resourceRoutes() []route {
	defs := []resource{
		{"/sources", "sources", "sources", "source", "source_id", M{"type": "file", "config": M{}, "status": "unknown"}},
		{"/metrics", "metrics", "metrics", "metric", "metric_id", M{"expr": "count(*)", "table_ref": "", "dimensions": []any{}, "joins": []any{}}},
		{"/knowledge", "knowledge_docs", "docs", "doc", "doc_id", M{"title": "未命名文档", "content": ""}},
		{"/dashboards", "dashboards", "dashboards", "dashboard", "board_id", M{"spec": M{"cards": []any{}}}},
		{"/compute-resources", "compute_resources", "resources", "resource", "resource_id", M{"type": "interactive", "engine": "DuckDB", "cu": 64, "status": "running"}},
		{"/agents", "agents", "agents", "agent", "agent_id", M{"mode": "engineering", "tools": []any{}, "status": "published"}},
		{"/apps", "apps", "apps", "app", "app_id", M{}},
		{"/buddy-tasks", "buddy_tasks", "tasks", "task", "task_id", M{"enabled": false, "cron": "0 7 * * *"}},
		{"/analysis-spaces", "analysis_spaces", "spaces", "space", "space_id", M{"tables": []any{}, "metrics": []any{}}},
		{"/external-catalogs", "external_catalogs", "external_catalogs", "external_catalog", "catalog_id", M{"type": "sqlite", "config": M{}, "status": "unknown"}},
		{"/ontology/entities", "ontology_entities", "entities", "entity", "entity_id", M{"kind": "subject", "table_refs": []any{}}},
		{"/ontology/relations", "ontology_relations", "relations", "relation", "relation_id", M{"cardinality": "1:N", "verb": "关联"}},
		{"/glossary", "glossary_terms", "terms", "term", "term_id", M{"synonyms": []any{}, "status": "published"}},
		{"/features", "features", "features", "feature", "feature_id", M{"dtype": "float", "version": 1, "status": "published"}},
		{"/alerts/channels", "alert_channels", "channels", "channel", "channel_id", M{"type": "webhook", "config": M{}, "enabled": true}},
		{"/alerts/rules", "alert_rules", "rules", "rule", "rule_id", M{"enabled": true}},
		{"/reports", "reports", "reports", "report", "report_id", M{"sections": []any{}, "status": "draft"}},
		{"/report-schedules", "report_schedules", "schedules", "schedule", "schedule_id", M{"enabled": true, "cron": "0 7 * * *"}},
	}
	routes := []route{}
	for _, d := range defs {
		d := d
		routes = append(routes, route{"GET", "/api" + d.Path, false, false, func(c *request) any {
			rr := a.list(d.Table, c.WS)
			for _, r := range rr {
				decoded(r)
			}
			out := M{d.Plural: rr, "total": len(rr), "summary": M{"total": len(rr)}}
			if d.Table == "compute_resources" {
				running, cu := 0, float64(0)
				types := obj(metadata["cluster_types"])
				for _, r := range rr {
					meta := obj(types[str(r["type"])])
					r["type_label"] = meta["label"]
					r["usage"] = meta["use"]
					cu += num(r["cu"])
					if r["status"] == "running" {
						running++
					}
				}
				out["types"] = types
				out["summary"] = M{"count": len(rr), "running": running, "total_cu": cu}
			}
			if d.Table == "agents" {
				out["apps"] = a.list("apps", c.WS)
			}

			if d.Table == "alert_channels" {
				out["types"] = metadata["channel_types"]
			}
			if d.Table == "alert_rules" {
				out["events"] = metadata["alert_events"]
			}
			if d.Table == "dashboards" {
				for _, r := range rr {
					r["card_count"] = len(arr(obj(r["spec"])["cards"]))
				}
			}
			if d.Table == "features" {
				out["summary"] = featureSummary(rr)
			}
			return out
		}})
		routes = append(routes, route{"POST", "/api" + d.Path, false, false, func(c *request) any {
			m := M{"id": id(), "workspace_id": c.WS, "created_at": now(), "updated_at": now()}
			for k, v := range d.Defaults {
				m[k] = v
			}
			for k, v := range c.Body {
				if k != "id" && k != "workspace_id" && k != "created_at" && k != "user_id" {
					m[k] = v
				}
			}
			if d.Table == "dashboards" && c.Body["cards"] != nil {
				m["spec"] = M{"cards": c.Body["cards"]}
			}
			if d.Table == "buddy_tasks" {
				m["user_id"] = c.User["id"]
			}
			a.validateResource(d, m, c)
			a.insert(d.Table, m)
			if d.Table == "features" {
				a.featureRevision(c.WS, m)
			}
			a.audit(c.WS, str(c.User["id"]), "create_"+d.Singular, str(m["id"]))
			return M{d.Singular: decoded(a.owned(d.Table, str(m["id"]), c.WS))}
		}})
		path := "/api" + d.Path + "/{" + d.Param + "}"
		routes = append(routes, route{"GET", path, false, false, func(c *request) any {
			r := decoded(a.owned(d.Table, c.p(d.Param), c.WS))
			out := M{d.Singular: r}
			if d.Table == "dashboards" {
				cards := []M{}
				for _, v := range arr(obj(r["spec"])["cards"]) {
					card := obj(v)
					q := str(card["sql"])
					if q != "" {
						result := a.query(c.WS, q, 1000)
						for k, v := range result {
							card[k] = v
						}
						if card["chart"] == nil {
							card["chart"] = chart(result)
						}
					}
					cards = append(cards, card)
				}
				out["cards"] = cards
			}
			return out
		}})
		routes = append(routes, route{"PATCH", path, false, false, func(c *request) any {
			a.owned(d.Table, c.p(d.Param), c.WS)
			delete(c.Body, "workspace_id")
			delete(c.Body, "owner_id")
			delete(c.Body, "user_id")
			delete(c.Body, "created_at")
			c.Body["updated_at"] = now()
			if d.Table == "features" {
				old := a.owned(d.Table, c.p(d.Param), c.WS)
				merged := M{}
				for k, v := range old {
					merged[k] = v
				}
				for k, v := range c.Body {
					merged[k] = v
				}
				a.validateResource(d, merged, c)
				c.Body["version"] = int(num(old["version"])) + 1
			}
			a.update(d.Table, c.p(d.Param), c.Body)
			if d.Table == "features" {
				a.featureRevision(c.WS, a.owned(d.Table, c.p(d.Param), c.WS))
			}
			return M{d.Singular: decoded(a.owned(d.Table, c.p(d.Param), c.WS)), "ok": true}
		}})
		routes = append(routes, route{"DELETE", path, false, false, func(c *request) any {
			a.owned(d.Table, c.p(d.Param), c.WS)
			a.exec("DELETE FROM "+quote(d.Table)+" WHERE id=?", c.p(d.Param))
			return M{"ok": true}
		}})
	}
	for _, x := range []struct{ Path, Table, Param string }{{"/compute-resources", "compute_resources", "resource_id"}, {"/models/services", "model_services", "service_id"}} {
		x := x
		routes = append(routes, route{"POST", "/api" + x.Path + "/{" + x.Param + "}/action", false, false, func(c *request) any {
			r := a.owned(x.Table, c.p(x.Param), c.WS)
			status := "running"
			if c.s("action") == "stop" {
				status = "stopped"
			}
			a.update(x.Table, str(r["id"]), M{"status": status})
			return M{"ok": true, "resource": a.owned(x.Table, str(r["id"]), c.WS)}
		}})
	}
	routes = append(routes,
		route{"GET", "/api/console/workspaces", false, true, func(c *request) any {
			ww := a.workspaces(c.User)
			for _, w := range ww {
				w["stats"] = M{"tables": len(a.tables(str(w["id"]))), "members": len(a.rows("SELECT * FROM workspace_members WHERE workspace_id=?", w["id"]))}
				w["tables"] = obj(w["stats"])["tables"]
				w["members"] = obj(w["stats"])["members"]
			}
			return M{"workspaces": ww}
		}},
		route{"GET", "/api/usage", false, false, func(c *request) any {
			var size int64
			_ = filepath.Walk(a.Dir, func(_ string, i os.FileInfo, e error) error {
				if e == nil && !i.IsDir() {
					size += i.Size()
				}
				return nil
			})
			cu := float64(0)
			cc := a.list("compute_resources", c.WS)
			for _, r := range cc {
				cu += num(r["cu"])
			}
			tables := a.tables(c.WS)
			var rows float64
			for _, table := range tables {
				rows += num(table["row_count"])
			}
			return M{"seats": M{"used": len(a.rows("SELECT user_id FROM workspace_members WHERE workspace_id=?", c.WS))}, "data": M{"tables": len(tables), "rows": rows}, "compute": M{"clusters": len(cc), "cu_total": cu}, "storage": M{"bytes": size}, "billing": M{"enabled": false}, "agent": M{"calls": len(a.list("traces", c.WS)), "conversations": len(a.list("conversations", c.WS))}}
		}},
		route{"GET", "/api/models", false, false, func(c *request) any {
			experiments := a.list("model_experiments", c.WS)
			for _, e := range experiments {
				decoded(e)
			}
			return M{"experiments": experiments, "registry": a.list("registered_models", c.WS), "services": a.list("model_services", c.WS), "features": a.list("features", c.WS)}
		}},
		route{"POST", "/api/models/experiments", false, false, func(c *request) any {
			m := c.Body
			m["id"] = id()
			m["workspace_id"] = c.WS
			m["created_at"] = now()
			r := a.insert("model_experiments", m)
			return M{"experiment": r}
		}},
		route{"GET", "/api/knowledge-search", false, false, func(c *request) any {
			return M{"results": a.rows("SELECT * FROM knowledge_docs WHERE workspace_id=? AND (title LIKE ? OR content LIKE ?)", c.WS, "%"+c.q("q")+"%", "%"+c.q("q")+"%")}
		}},
		route{"POST", "/api/knowledge/rebuild-catalog", false, false, func(c *request) any {
			count := 0
			for _, t := range a.tables(c.WS) {
				title := str(t["ref"])
				if a.row("SELECT id FROM knowledge_docs WHERE workspace_id=? AND title=?", c.WS, title) == nil {
					a.insert("knowledge_docs", M{"id": id(), "workspace_id": c.WS, "title": title, "content": js(t), "source_type": "catalog", "created_at": now()})
					count++
				}
			}
			return M{"created": count}
		}},
		route{"GET", "/api/ontology/overview", false, false, func(c *request) any {
			entities := a.list("ontology_entities", c.WS)
			covered := map[string]bool{}
			for _, e := range entities {
				for _, ref := range arr(e["table_refs"]) {
					covered[str(ref)] = true
				}
			}
			total := len(a.tables(c.WS))
			coverage := float64(0)
			if total > 0 {
				coverage = float64(len(covered)) / float64(total)
			}
			return M{"entities": len(entities), "relations": len(a.list("ontology_relations", c.WS)), "terms": len(a.list("glossary_terms", c.WS)), "covered_tables": len(covered), "total_tables": total, "coverage": coverage, "kinds": metadata["ontology_kinds"], "verbs": metadata["ontology_verbs"], "cardinalities": metadata["ontology_cardinalities"]}
		}},
		route{"GET", "/api/ontology/graph", false, false, func(c *request) any {
			nodes := []M{}
			for _, e := range a.list("ontology_entities", c.WS) {
				e["label"] = firstNonempty(str(e["display_name"]), str(e["name"]))
				e["kind_label"] = obj(metadata["ontology_kinds"])[str(e["kind"])]
				nodes = append(nodes, e)
			}
			edges := []M{}
			for _, r := range a.list("ontology_relations", c.WS) {
				r["from"] = r["from_entity"]
				r["to"] = r["to_entity"]
				r["label"] = firstNonempty(str(obj(metadata["ontology_verbs"])[str(r["verb"])]), str(r["verb"]))
				edges = append(edges, r)
			}
			return M{"nodes": nodes, "edges": edges}
		}},
		route{"GET", "/api/glossary/resolve", false, false, func(c *request) any {
			q := strings.ToLower(c.q("q"))
			var best M
			for _, r := range a.list("glossary_terms", c.WS) {
				words := append(arr(r["synonyms"]), r["term"])
				for _, w := range words {
					if strings.Contains(q, strings.ToLower(str(w))) && (best == nil || len(str(r["term"])) > len(str(best["term"]))) {
						best = decoded(r)
					}
				}
			}
			return M{"match": best}
		}},
	)
	return routes
}
func (a *App) validateResource(d resource, m M, c *request) {
	if d.Table == "report_schedules" {
		a.owned("reports", str(m["report_id"]), c.WS)
	}
	if d.Table == "report_schedules" || d.Table == "buddy_tasks" {
		if str(m["cron"]) != "" {
			cronMatches(str(m["cron"]), time.Now())
		}
	}
	if a.columns[d.Table]["name"] && strings.TrimSpace(str(m["name"])) == "" {
		fail(422, "名称不能为空")
	}
	if d.Table == "metrics" || d.Table == "features" {
		t := a.resolve(c.WS, str(m["table_ref"]))
		m["table_ref"] = t["ref"]
		if str(m["expr"]) == "" {
			fail(422, "SQL 表达式不能为空")
		}
		a.query(c.WS, "SELECT "+str(m["expr"])+" FROM "+physical(t)+" f LIMIT 0", 1)
	}
	if d.Table == "ontology_relations" {
		a.owned("ontology_entities", str(m["from_entity"]), c.WS)
		a.owned("ontology_entities", str(m["to_entity"]), c.WS)
	}
}
func featureSummary(rr []M) M {
	entities := map[string]bool{}
	tables := map[string]bool{}
	for _, r := range rr {
		entities[str(r["entity"])] = true
		tables[str(r["table_ref"])] = true
	}
	return M{"total": len(rr), "published": len(rr), "entities": len(entities), "tables": len(tables)}
}
