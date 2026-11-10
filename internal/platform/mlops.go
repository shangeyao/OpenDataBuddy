package platform

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
)

func (a *App) model(ws string, m M) M {
	versions := a.rows("SELECT * FROM model_versions WHERE model_id=? AND workspace_id=? ORDER BY created_at", m["id"], ws)
	champ := ""
	for _, v := range versions {
		decoded(v)
		if v["stage"] == "production" {
			champ = str(v["version"])
		}
	}
	m["versions"] = versions
	m["version_count"] = len(versions)
	m["champion"] = champ
	m["production_version"] = champ
	m["latest_version"] = ""
	if len(versions) > 0 {
		m["latest_version"] = versions[len(versions)-1]["version"]
	}
	stages := M{"production": []any{}, "staging": []any{}, "archived": []any{}}
	for _, v := range versions {
		k := str(v["stage"])
		stages[k] = append(arr(stages[k]), v["version"])
	}
	m["stages"] = stages
	return m
}
func (a *App) addVersion(ws string, m M, body M) M {
	versions := a.rows("SELECT * FROM model_versions WHERE model_id=? AND workspace_id=?", m["id"], ws)
	if def(body, "stage", "staging") == "production" {
		a.exec("UPDATE model_versions SET stage='archived' WHERE model_id=? AND workspace_id=? AND stage='production'", m["id"], ws)
	}
	v := a.insert("model_versions", M{"id": id(), "workspace_id": ws, "model_id": m["id"], "version": fmt.Sprintf("v%d", len(versions)+1), "stage": def(body, "stage", "staging"), "metrics": obj(body["metrics"]), "artifact_uri": body["artifact_uri"], "description": body["description"], "run_id": body["run_id"], "created_at": now()})
	return decoded(v)
}
func normalizeTraffic(traffic M) M {
	sum := float64(0)
	keys := []string{}
	for k, v := range traffic {
		if num(v) < 0 || math.IsNaN(num(v)) {
			fail(422, "流量权重必须非负")
		}
		sum += num(v)
		keys = append(keys, k)
	}
	if sum <= 0 {
		fail(422, "至少为一个版本分配流量")
	}
	sort.Strings(keys)
	out := M{}
	remaining := 100
	for i, k := range keys {
		n := int(math.Round(num(traffic[k]) / sum * 100))
		if i == len(keys)-1 {
			n = remaining
		}
		if n > remaining {
			n = remaining
		}
		out[k] = n
		remaining -= n
	}
	return out
}
func (a *App) service(ws string, s M) M {
	decoded(s)
	if str(s["endpoint"]) == "" {
		s["endpoint"] = "/api/serving/services/" + str(s["id"]) + "/predict"
	}
	traffic := obj(s["traffic"])
	if len(traffic) == 0 {
		traffic = M{str(s["version"]): 100}
		s["traffic"] = traffic
	}
	detail := []M{}
	for v, w := range traffic {
		detail = append(detail, M{"version": v, "weight": w, "percent": w, "replicas": math.Ceil(num(s["replicas"]) * num(w) / 100)})
	}
	s["traffic_detail"] = detail
	s["autoscale"] = M{"capacity_qps": num(s["replicas"]) * num(s["target_qps"])}
	if m := a.row("SELECT * FROM registered_models WHERE workspace_id=? AND (id=? OR name=?)", ws, s["model_ref"], s["model_ref"]); m != nil {
		s["model"] = a.model(ws, m)
	}
	return s
}
func (a *App) mlopsRoutes() []route {
	r := []route{
		{"GET", "/api/registry/models", false, false, func(c *request) any {
			mm := a.list("registered_models", c.WS)
			for _, m := range mm {
				a.model(c.WS, m)
			}
			return M{"models": mm, "summary": a.mlopsSummary(c.WS), "stages": []string{"staging", "production", "archived"}}
		}},
		{"POST", "/api/registry/models", false, false, func(c *request) any {

			if c.s("source_experiment") != "" {
				exp := a.row("SELECT * FROM model_experiments WHERE workspace_id=? AND (name=? OR id=?)", c.WS, c.s("source_experiment"), c.s("source_experiment"))
				if exp == nil {
					fail(404, "实验不存在")
				}
				for _, k := range []string{"name", "framework", "owner", "metrics"} {
					if c.Body[k] == nil || c.Body[k] == "" || k == "metrics" && len(obj(c.Body[k])) == 0 {
						c.Body[k] = exp[k]
					}
				}
			}
			if c.s("name") == "" {
				fail(422, "模型名称不能为空")
			}

			m := M{"id": id(), "workspace_id": c.WS, "created_at": now()}
			for _, k := range []string{"name", "description", "source_experiment", "framework", "owner"} {
				m[k] = c.Body[k]
			}
			a.insert("registered_models", m)
			v := a.addVersion(c.WS, m, c.Body)
			return M{"model": a.model(c.WS, m), "version": v}
		}},
		{"GET", "/api/registry/summary", false, false, func(c *request) any {
			return a.mlopsSummary(c.WS)
		}},
		{"GET", "/api/registry/models/{model_id}", false, false, func(c *request) any {
			return M{"model": a.model(c.WS, a.owned("registered_models", c.p("model_id"), c.WS))}
		}},
		{"POST", "/api/registry/models/{model_id}/versions", false, false, func(c *request) any {
			m := a.owned("registered_models", c.p("model_id"), c.WS)
			v := a.addVersion(c.WS, m, c.Body)
			return M{"version": v, "versions": a.model(c.WS, m)["versions"]}
		}},
		{"POST", "/api/registry/models/{model_id}/versions/{version_id}/promote", false, false, func(c *request) any {
			m := a.owned("registered_models", c.p("model_id"), c.WS)
			v := a.row("SELECT * FROM model_versions WHERE model_id=? AND workspace_id=? AND (id=? OR version=?)", m["id"], c.WS, c.p("version_id"), c.p("version_id"))
			if v == nil {
				fail(404, "模型版本不存在")
			}
			stage := str(def(c.Body, "stage", "production"))
			if stage != "production" && stage != "staging" && stage != "archived" {
				fail(422, "未知版本阶段")
			}
			tx, e := a.DB.Begin()
			must(e)
			defer tx.Rollback()
			if stage == "production" {
				_, e = tx.Exec("UPDATE model_versions SET stage='archived' WHERE model_id=? AND stage='production'", m["id"])
				must(e)
			}
			_, e = tx.Exec("UPDATE model_versions SET stage=? WHERE id=?", stage, v["id"])
			must(e)
			must(tx.Commit())
			return M{"model": a.model(c.WS, m), "version": decoded(a.owned("model_versions", str(v["id"]), c.WS))}
		}},
		{"GET", "/api/registry/models/{model_id}/compare", false, false, func(c *request) any {
			m := a.owned("registered_models", c.p("model_id"), c.WS)
			return a.compareModels(c.WS, m, c.q("a"), c.q("b"))
		}},
		{"GET", "/api/serving/services", false, false, func(c *request) any {
			rr := a.list("model_services", c.WS)
			for _, s := range rr {
				a.service(c.WS, s)
			}
			return M{"services": rr, "summary": a.mlopsSummary(c.WS), "models": a.list("registered_models", c.WS)}
		}},
		{"GET", "/api/serving/services/{service_id}", false, false, func(c *request) any {
			return M{"service": a.service(c.WS, a.owned("model_services", c.p("service_id"), c.WS))}
		}},
		{"POST", "/api/serving/services/{service_id}/traffic", false, false, func(c *request) any {
			s := a.owned("model_services", c.p("service_id"), c.WS)
			traffic := normalizeTraffic(obj(c.Body["traffic"]))
			model := a.row("SELECT id FROM registered_models WHERE workspace_id=? AND (id=? OR name=?)", c.WS, s["model_ref"], s["model_ref"])
			if model == nil {
				fail(404, "模型不存在")
			}
			for v := range traffic {
				if a.row("SELECT id FROM model_versions WHERE model_id=? AND version=?", model["id"], v) == nil {
					fail(422, "流量引用不存在的模型版本")
				}
			}
			a.update("model_services", c.p("service_id"), M{"traffic": traffic, "updated_at": now()})
			return M{"service": a.service(c.WS, a.owned("model_services", c.p("service_id"), c.WS))}
		}},
		{"POST", "/api/serving/services/{service_id}/autoscale", false, false, func(c *request) any {
			s := a.owned("model_services", c.p("service_id"), c.WS)
			target := mathMax(1, num(s["target_qps"]))
			desired := math.Max(num(s["min_replicas"]), math.Min(num(s["max_replicas"]), math.Ceil(num(c.Body["qps"])/target)))
			return M{"current_replicas": s["replicas"], "desired_replicas": desired, "target_qps": target, "qps": num(c.Body["qps"])}
		}},
		{"POST", "/api/serving/auto-stop-check", false, false, func(c *request) any { return a.autoStop(c.WS) }},
		{"GET", "/api/features/summary", false, false, func(c *request) any { return featureSummary(a.list("features", c.WS)) }},
		{"POST", "/api/features/compute", false, false, func(c *request) any { return a.computeFeatures(c.WS, c.Body) }},
		{"POST", "/api/features/training-set", false, false, func(c *request) any { return a.trainingSet(c.WS, c.Body) }},
		{"GET", "/api/features/by-table", false, false, func(c *request) any {
			return M{"features": a.rows("SELECT * FROM features WHERE workspace_id=? AND table_ref=?", c.WS, c.q("table_ref"))}
		}},
		{"GET", "/api/features/{feature_id}/versions", false, false, func(c *request) any {
			f := a.owned("features", c.p("feature_id"), c.WS)
			return M{"feature": decoded(f), "versions": a.rows("SELECT * FROM feature_revisions WHERE feature_id=?", f["id"])}
		}},
		{"GET", "/api/features/{feature_id}/lineage", false, false, func(c *request) any {
			f := a.owned("features", c.p("feature_id"), c.WS)
			return M{"feature": f, "upstream": []string{str(f["table_ref"])}, "downstream": []M{}}
		}},
	}
	for _, base := range []string{"/serving/services", "/models/services"} {
		base := base
		r = append(r, route{"POST", "/api" + base, false, false, func(c *request) any {
			if c.s("name") == "" {
				fail(422, "服务名称不能为空")
			}
			s := M{"id": id(), "workspace_id": c.WS, "name": c.s("name"), "model_ref": def(c.Body, "model_ref", c.Body["model"]), "version": def(c.Body, "version", "v1"), "replicas": def(c.Body, "replicas", 1), "min_replicas": def(c.Body, "min_replicas", 1), "max_replicas": def(c.Body, "max_replicas", 4), "target_qps": def(c.Body, "target_qps", 50), "traffic": obj(c.Body["traffic"]), "status": "running", "created_at": now()}
			if num(s["replicas"]) < 1 || num(s["min_replicas"]) < 1 || num(s["max_replicas"]) < num(s["min_replicas"]) {
				fail(422, "副本数范围无效")
			}
			if c.Body["start"] == false {
				s["status"] = "stopped"
			}
			for _, k := range []string{"idle_timeout_seconds", "description", "endpoint"} {
				if v, ok := c.Body[k]; ok {
					s[k] = v
				}
			}
			a.validateService(c.WS, s)
			a.insert("model_services", s)
			return M{"service": a.service(c.WS, s)}
		}})
	}
	r = append(r, route{"PATCH", "/api/serving/services/{service_id}", false, false, func(c *request) any {
		old := a.owned("model_services", c.p("service_id"), c.WS)
		m := M{}
		for _, k := range []string{"version", "replicas", "min_replicas", "max_replicas", "target_qps", "idle_timeout_seconds", "description"} {
			if v, ok := c.Body[k]; ok {
				m[k] = v
			}
		}
		merged := M{}
		for k, v := range old {
			merged[k] = v
		}
		for k, v := range m {
			merged[k] = v
		}
		a.validateService(c.WS, merged)
		a.update("model_services", c.p("service_id"), merged)
		return M{"service": a.service(c.WS, a.owned("model_services", c.p("service_id"), c.WS))}
	}}, route{"POST", "/api/serving/services/{service_id}/action", false, false, func(c *request) any {

		s := a.owned("model_services", c.p("service_id"), c.WS)
		switch c.s("action") {
		case "start":
			s["status"] = "running"
		case "stop":
			s["status"] = "stopped"
		case "scale":
			for _, k := range []string{"replicas", "min_replicas", "max_replicas", "target_qps"} {
				if v, ok := c.Body[k]; ok {
					s[k] = v
				}
			}
		default:
			fail(422, "action 仅支持 start / stop / scale")
		}
		a.validateService(c.WS, s)
		s["last_active_at"] = now()
		a.update("model_services", c.p("service_id"), s)

		return M{"service": a.service(c.WS, a.owned("model_services", c.p("service_id"), c.WS))}
	}})
	return r
}

func (a *App) mlopsSummary(ws string) M {
	versions := a.list("model_versions", ws)
	stages := M{"staging": 0, "production": 0, "archived": 0}
	champions := map[string]bool{}
	for _, v := range versions {
		k := str(v["stage"])
		stages[k] = num(stages[k]) + 1
		if k == "production" {
			champions[str(v["model_id"])] = true
		}
	}
	services := a.list("model_services", ws)
	running, replicas, ab := 0, 0, 0
	for _, s := range services {
		if s["status"] == "running" {
			running++
			replicas += int(num(s["replicas"]))
		}
		if len(obj(s["traffic"])) > 1 {
			ab++
		}
	}
	return M{"models": len(a.list("registered_models", ws)), "versions": len(versions), "champions": len(champions), "stages": stages, "services": len(services), "running_services": running, "replicas": replicas, "ab_services": ab}
}
func (a *App) autoStop(ws string) M {
	ss := a.list("model_services", ws)
	stopped := []string{}
	for _, s := range ss {
		timeout := num(s["idle_timeout_seconds"])
		last := firstNonempty(str(s["last_active_at"]), str(s["created_at"]))
		at, e := time.Parse(time.RFC3339, last)
		if e == nil && timeout > 0 && s["status"] == "running" && time.Since(at) > time.Duration(timeout)*time.Second {
			a.update("model_services", str(s["id"]), M{"status": "stopped", "updated_at": now()})
			stopped = append(stopped, str(s["id"]))
		}
	}
	return M{"checked": len(ss), "stopped": stopped}
}

func (a *App) compareModels(ws string, m M, left, right string) M {
	one := func(key string) M {
		r := a.row("SELECT * FROM model_versions WHERE model_id=? AND workspace_id=? AND (id=? OR version=?)", m["id"], ws, key, key)
		if r == nil {
			fail(404, "版本不存在")
		}
		return decoded(r)
	}
	x, y := one(left), one(right)
	xm, ym := obj(x["metrics"]), obj(y["metrics"])
	keys := map[string]bool{}
	for k := range xm {
		keys[k] = true
	}
	for k := range ym {
		keys[k] = true
	}
	metrics := []M{}
	votes := 0
	for k := range keys {
		delta := num(ym[k]) - num(xm[k])
		better := "tie"
		low := false
		for _, hint := range []string{"loss", "mae", "mse", "rmse", "mape", "error", "latency", "cost", "size"} {
			low = low || strings.Contains(strings.ToLower(k), hint)
		}
		if delta != 0 {
			bBetter := delta > 0
			if low {
				bBetter = !bBetter
			}
			if bBetter {
				better = "b"
				votes++
			} else {
				better = "a"
				votes--
			}
		}
		var pct any
		if num(xm[k]) != 0 {
			pct = delta / num(xm[k])
		}
		metrics = append(metrics, M{"metric": k, "a": xm[k], "b": ym[k], "delta": delta, "delta_pct": pct, "better": better})
	}
	recommendation := "tie"
	if votes > 0 {
		recommendation = "b"
	}
	if votes < 0 {
		recommendation = "a"
	}
	return M{"model": m, "a": x, "b": y, "metrics": metrics, "recommendation": recommendation, "recommendation_text": "按各指标改进方向比较，推荐 " + recommendation}
}

func (a *App) validateService(ws string, s M) {
	model := a.row("SELECT * FROM registered_models WHERE workspace_id=? AND (name=? OR id=?)", ws, s["model_ref"], s["model_ref"])
	if model == nil {
		if a.row("SELECT id FROM model_services WHERE id=? AND workspace_id=?", s["id"], ws) != nil {
			return
		}
		fail(404, "模型未注册")
	}
	if num(s["min_replicas"]) < 1 || num(s["max_replicas"]) < num(s["min_replicas"]) || num(s["target_qps"]) <= 0 {
		fail(422, "副本范围或目标 QPS 无效")
	}
	if num(s["replicas"]) < num(s["min_replicas"]) || num(s["replicas"]) > num(s["max_replicas"]) {
		fail(422, "副本数超出 min/max 范围")
	}
	version := str(s["version"])
	if version == "" {
		mm := a.model(ws, model)
		version = firstNonempty(str(mm["production_version"]), str(mm["latest_version"]))
	}
	if a.row("SELECT id FROM model_versions WHERE model_id=? AND version=?", model["id"], version) == nil {
		fail(404, "模型版本不存在")
	}
	s["version"] = version
	traffic := obj(s["traffic"])
	if len(traffic) == 0 {
		traffic = M{version: 100}
	}
	traffic = normalizeTraffic(traffic)
	for v := range traffic {
		if a.row("SELECT id FROM model_versions WHERE model_id=? AND version=?", model["id"], v) == nil {
			fail(422, "流量版本不存在")
		}
	}
	s["traffic"] = traffic
	s["updated_at"] = now()
}
