package platform

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os/exec"
	"regexp"
	"runtime"
	"strings"
	"time"
)

func topo(nodes, edges []any) []M {
	by := map[string]M{}
	indeg := map[string]int{}
	children := map[string][]string{}
	for _, v := range nodes {
		n := obj(v)
		key := str(n["id"])
		if key == "" || by[key] != nil {
			fail(422, "节点 ID 为空或重复")
		}
		by[key] = n
		indeg[key] = 0
	}
	for _, e := range edges {
		pair := arr(e)
		if len(pair) != 2 {
			fail(422, "边必须有两个节点")
		}
		from, to := str(pair[0]), str(pair[1])
		if by[from] == nil || by[to] == nil {
			fail(422, "边引用不存在的节点")
		}
		indeg[to]++
		children[from] = append(children[from], to)
	}
	queue := []string{}
	for _, n := range nodes {
		key := str(obj(n)["id"])
		if indeg[key] == 0 {
			queue = append(queue, key)
		}
	}
	out := []M{}
	for len(queue) > 0 {
		key := queue[0]
		queue = queue[1:]
		out = append(out, by[key])
		for _, child := range children[key] {
			indeg[child]--
			if indeg[child] == 0 {
				queue = append(queue, child)
			}
		}
	}
	if len(out) != len(nodes) {
		fail(422, "工作流包含环")
	}
	return out
}
func (a *App) flowShape(f M) M {
	decoded(f)
	f["node_count"] = len(arr(f["nodes"]))
	f["enabled"] = boolOf(f["enabled"])
	f["schedule_text"] = str(f["cron"])
	if f["schedule_text"] == "" {
		f["schedule_text"] = "仅手动触发"
	}
	switch str(f["run_mode"]) {
	case "once":
		f["schedule_text"] = "单次运行：" + str(f["run_at"])
	case "event":
		f["schedule_text"] = "事件触发"
		f["trigger_url"] = "/api/workflows/trigger/" + str(f["trigger_key"])
	}
	f["last_run"] = a.row("SELECT * FROM flow_runs WHERE flow_id=? ORDER BY started_at DESC LIMIT 1", f["id"])
	return f
}

// parseRunAt accepts RFC3339 and "YYYY-MM-DD HH:MM" in the flow timezone.
func parseRunAt(value, tz string) (time.Time, error) {
	if t, e := time.Parse(time.RFC3339, value); e == nil {
		return t, nil
	}
	loc, e := time.LoadLocation(firstNonempty(tz, "Asia/Shanghai"))
	if e != nil {
		loc = time.UTC
	}
	return time.ParseInLocation("2006-01-02 15:04", value, loc)
}
func normalizeNodes(m M) {
	cc := arr(m["nodes"])
	for _, v := range cc {
		n := obj(v)
		if str(n["id"]) == "" {
			n["id"] = id()
		}
		if n["type"] == nil {
			n["type"] = "sql"
		}
		if n["condition"] == nil {
			n["condition"] = "all_success"
		}
		if n["config"] == nil {
			n["config"] = M{}
		}
	}
	m["nodes"] = cc
	topo(cc, arr(m["edges"]))
}
func (a *App) newFlow(ws string, m M) M {
	normalizeNodes(m)
	if str(m["name"]) == "" {
		fail(422, "工作流名称不能为空")
	}
	mode := str(def(m, "run_mode", "cron"))
	switch mode {
	case "cron", "continuous", "manual", "once", "event":
	default:
		fail(422, "未知的运行方式")
	}
	f := M{"id": id(), "workspace_id": ws, "name": m["name"], "description": def(m, "description", ""), "nodes": m["nodes"], "edges": arr(m["edges"]), "params": obj(m["params"]), "cron": def(m, "cron", ""), "enabled": def(m, "enabled", false), "run_mode": mode, "run_at": def(m, "run_at", ""), "trigger_key": def(m, "trigger_key", ""), "interval_seconds": mathMax(60, num(def(m, "interval_seconds", 60))), "timezone": firstNonempty(str(m["timezone"]), "Asia/Shanghai"), "retries": def(m, "retries", 0), "created_at": now(), "updated_at": now()}
	if str(f["cron"]) != "" {
		cronMatches(str(f["cron"]), time.Now())
	}
	if _, e := time.LoadLocation(str(f["timezone"])); e != nil {
		fail(422, "时区无效")
	}
	if mode == "once" {
		if str(f["run_at"]) == "" {
			fail(422, "单次运行必须指定触发时间 run_at")
		}
		if _, e := parseRunAt(str(f["run_at"]), str(f["timezone"])); e != nil {
			fail(422, "触发时间格式无效，应为 RFC3339 或 YYYY-MM-DD HH:MM")
		}
	}
	if mode == "event" && str(f["trigger_key"]) == "" {
		f["trigger_key"] = "evt_" + id()
	}
	a.checkQuota(ws, "flows") // P2-12
	a.insert("flows", f)
	return a.flowShape(a.owned("flows", str(f["id"]), ws))
}
func mathMax(a, b float64) float64 {
	if a > b {
		return a
	}
	return b
}

var placeholder = regexp.MustCompile(`\{\{\s*([\w.]+)\s*\}\}`)

func renderValue(v any, scope M) any {
	switch x := v.(type) {
	case map[string]any:
		m := M{}
		for k, v := range x {
			m[k] = renderValue(v, scope)
		}
		return m
	case []any:
		out := []any{}
		for _, v := range x {
			out = append(out, renderValue(v, scope))
		}
		return out
	case string:
		return placeholder.ReplaceAllStringFunc(x, func(s string) string {
			parts := strings.Split(strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(s, "{{"), "}}")), ".")
			var cur any = scope
			for _, p := range parts {
				cur = obj(cur)[p]
			}
			return str(cur)
		})
	default:
		return v
	}
}
func condition(n M, up []M, params M) bool {
	kind := str(def(n, "condition", "all_success"))
	cfg := obj(n["condition_config"])
	success, failed, skipped := 0, 0, 0
	rows := float64(0)
	has := false
	for _, r := range up {
		switch r["status"] {
		case "success":
			success++
		case "failed":
			failed++
		case "skipped":
			skipped++
		}
		out := obj(r["output"])
		rows += num(out["row_count"])
		has = has || len(out) > 0
	}
	switch kind {
	case "always", "all_done":
		return true
	case "all_success":
		return success == len(up)
	case "any_success":
		return success > 0 || len(up) == 0
	case "any_failed":
		return failed > 0
	case "all_failed":
		return len(up) > 0 && failed == len(up)
	case "has_output":
		return has
	case "row_threshold":
		return rows >= num(cfg["min_rows"]) && (cfg["max_rows"] == nil || rows <= num(cfg["max_rows"]))
	case "quality_passed":
		for _, r := range up {
			if boolOf(obj(r["output"])["blocked"]) {
				return false
			}
		}
		return failed == 0
	case "manual_confirm":
		return boolOf(params[str(def(cfg, "param", "confirm_"+str(n["id"])))])
	case "time_window":
		loc, e := time.LoadLocation(str(def(cfg, "timezone", "Asia/Shanghai")))
		if e != nil {
			fail(422, "时区无效")
		}
		t := time.Now().In(loc)
		if days := str(cfg["days"]); days != "" {
			day := int(t.Weekday())
			if day == 0 {
				day = 7
			}
			found := false
			for _, v := range strings.Split(days, ",") {
				found = found || int(num(strings.TrimSpace(v))) == day
			}
			if !found {
				return false
			}
		}
		hh := t.Format("15:04")
		start, end := str(def(cfg, "start", "00:00")), str(def(cfg, "end", "23:59"))
		if start <= end {
			return hh >= start && hh <= end
		}
		return hh >= start || hh <= end
	case "expression":
		vars := M{"success": float64(success), "failed": float64(failed), "skipped": float64(skipped), "total": float64(len(up)), "rows_total": rows}
		for _, r := range up {
			vars["n_"+str(r["node_id"])] = num(obj(r["output"])["row_count"])
		}
		return evalCondition(str(cfg["expression"]), vars)
	default:
		fail(422, "未知运行条件")
	}
	return false
}
func (a *App) executeNode(ws string, n M, params, outputs M, depth int) (out M) {
	defer func() {
		if e := recover(); e != nil {
			out = M{"status": "failed", "error": fmt.Sprint(e)}
		}
	}()
	globalParams := params
	local := M{}
	for k, v := range params {
		local[k] = v
	}
	for k, v := range obj(n["params"]) {
		local[k] = v
	}
	params = local
	cfg := obj(renderValue(obj(n["config"]), M{"workflow": globalParams, "params": params, "nodes": outputs}))
	switch str(n["type"]) {
	case "sql":
		out = a.query(ws, str(def(cfg, "sql", n["sql"])), int(num(def(cfg, "limit", 200))))
	case "python":
		key := ws + ":workflow:" + id()
		defer stopKernel(key)
		out = a.python(key, str(def(cfg, "code", cfg["script"])), params)
	case "shell":
		seconds := num(def(cfg, "timeout", 60))
		if seconds < 1 || seconds > 300 {
			fail(422, "Shell 超时需在 1~300 秒")
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(seconds)*time.Second)
		defer cancel()
		shell, arg := "/bin/sh", "-c"
		if runtime.GOOS == "windows" {
			shell, arg = "cmd", "/C"
		}
		cmd := exec.CommandContext(ctx, shell, arg, str(cfg["command"]))
		if cwd := str(cfg["cwd"]); cwd != "" {
			cmd.Dir = cwd
		}
		b, e := cmd.CombinedOutput()
		out = M{"stdout": string(b)}
		if e != nil {
			out["status"] = "failed"
			out["error"] = e.Error()
		}
	case "quality":
		out = a.runRules(ws, str(a.resolve(ws, refOf(cfg))["ref"]), boolOf(def(cfg, "blocking", true)))
		if num(out["failed"]) > 0 {
			out["status"] = "failed"
		}
	case "notebook":
		if key := str(cfg["notebook_id"]); key != "" {
			out = a.runNotebook(ws, a.owned("notebooks", key, ws), params, 0)
		} else {

			cells := arr(cfg["cells"])
			if raw, ok := cfg["cells"].(string); ok && len(cells) == 0 {
				for _, line := range strings.Split(raw, "\n") {
					if strings.TrimSpace(line) != "" {
						cells = append(cells, M{"source": line, "type": "python"})
					}
				}
			}
			key := "workflow-" + id()
			defer stopKernel(ws + ":" + key)
			results := []M{}
			status := "success"
			for _, v := range cells {
				cell := obj(v)
				if text, ok := v.(string); ok {
					cell = M{"source": text, "type": "python"}
				}
				r := a.runCell(ws, key, cell, params, 0)
				results = append(results, r)
				if r["status"] == "error" {
					status = "failed"
					break
				}
			}
			out = M{"outputs": results, "status": status}

		}
	case "subflow":
		if depth >= 5 {
			fail(400, "子流程最多 5 层")
		}
		key := str(cfg["flow_id"])
		f := a.row("SELECT * FROM flows WHERE workspace_id=? AND (id=? OR name=?)", ws, key, key)
		if f == nil {
			fail(404, "子流程不存在")
		}
		out = a.runFlow(ws, f, params, "subflow", depth+1)
	case "notify":
		a.audit(ws, "", "workflow_notify", js(cfg))
		out = M{"simulated": true, "title": cfg["title"], "message": cfg["message"]}
	case "approval":
		if !boolOf(params["confirm_"+str(n["id"])]) {
			out = M{"status": "waiting", "reason": cfg["reason"]}
		} else {
			out = M{"approved": true}
		}
	case "http":
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		method := str(def(cfg, "method", "GET"))
		req, e := http.NewRequestWithContext(ctx, method, str(cfg["url"]), strings.NewReader(str(cfg["body"])))
		must(e)
		for k, v := range obj(cfg["headers"]) {
			req.Header.Set(k, str(v))
		}
		resp, e := http.DefaultClient.Do(req)
		must(e)
		defer resp.Body.Close()
		b, e := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		must(e)
		out = M{"status_code": resp.StatusCode, "body": string(b)}
		if resp.StatusCode >= 400 {
			out["status"] = "failed"
		}
	default:
		fail(422, "未知节点类型")
	}
	if out == nil {
		out = M{}
	}
	if out["status"] == nil {
		out["status"] = "success"
	}
	if out["status"] == "error" {
		out["status"] = "failed"
	}
	return out
}
func (a *App) runFlow(ws string, f M, overrides M, trigger string, depth int) M {
	return a.runFlowState(ws, f, overrides, trigger, depth, nil)
}
func (a *App) runFlowState(ws string, f M, overrides M, trigger string, depth int, resume M) M {
	nodes, edges := arr(f["nodes"]), arr(f["edges"])
	order := topo(nodes, edges)
	params := obj(f["params"])
	for k, v := range overrides {
		params[k] = v
	}
	var run M
	outputs := M{}
	results := []M{}
	status := "success"
	if resume == nil {
		run = a.insert("flow_runs", M{"id": id(), "flow_id": f["id"], "status": "running", "trigger": trigger, "started_at": now(), "log": []M{}})
	} else {
		run = a.row("SELECT * FROM flow_runs WHERE id=? AND flow_id=?", resume["run_id"], f["id"])
		if run == nil {
			fail(404, "运行记录不存在")
		}
		outputs = obj(resume["outputs"])
		for _, v := range arr(resume["results"]) {
			r := obj(v)
			if r["status"] != "waiting" {
				results = append(results, r)
				if r["status"] == "failed" {
					status = "failed"
				}
			}
		}
		a.update("flow_runs", str(run["id"]), M{"status": "running"})
	}
	for _, n := range order {
		key := str(n["id"])
		if done := obj(outputs[key]); len(done) > 0 && done["status"] != "waiting" {
			continue
		}
		if len(obj(outputs[key])) > 0 {
			a.exec("DELETE FROM flow_node_runs WHERE run_id=? AND node_id=? AND status='waiting'", run["id"], key)
		}
		up := []M{}
		for _, e := range edges {
			pair := arr(e)
			if str(pair[1]) == key {
				up = append(up, obj(outputs[str(pair[0])]))
			}
		}
		nodeRun := M{"id": id(), "run_id": run["id"], "flow_id": f["id"], "node_id": key, "name": str(n["name"]), "type": n["type"], "started_at": now()}
		if !condition(n, up, params) {
			nodeRun["status"] = "skipped"
			nodeRun["output"] = M{}
		} else {
			retries := int(num(def(n, "retries", def(f, "retries", 0))))
			if retries < 0 || retries > 5 {
				fail(422, "重试次数必须为 0~5")
			}
			var out M
			for attempt := 0; attempt <= retries; attempt++ {
				out = a.executeNode(ws, n, params, outputs, depth)
				out["attempts"] = attempt + 1
				if out["status"] != "failed" {
					break
				}
			}
			nodeRun["status"] = out["status"]
			nodeRun["output"] = out
			nodeRun["error"] = out["error"]
			if out["status"] == "failed" {
				status = "failed"
			}
			if out["status"] == "waiting" {
				status = "waiting"
			}
		}
		nodeRun["finished_at"] = now()
		a.insert("flow_node_runs", nodeRun)
		outputs[key] = nodeRun
		results = append(results, nodeRun)
		if status == "waiting" {
			state := M{"run_id": run["id"], "flow": f, "params": params, "outputs": outputs, "results": results, "node_id": key}
			approval := a.insert("approvals", M{"id": id(), "conversation_id": "workflow:" + str(run["id"]), "tool": "workflow_approval", "args": M{"kind": "workflow", "state": state}, "reason": obj(nodeRun["output"])["reason"], "status": "pending", "created_at": now()})
			run["approval"] = M{"id": approval["id"], "reason": approval["reason"], "tool": "workflow_approval"}
			break
		}
	}
	finished := now()
	if status == "waiting" {
		finished = ""
	}
	a.update("flow_runs", str(run["id"]), M{"status": status, "finished_at": finished, "log": results})
	run["status"] = status
	run["finished_at"] = finished
	run["log"] = results
	run["nodes"] = results
	if status == "failed" {
		a.emit(ws, M{"event": "flow_run_failed", "subject": "工作流失败：" + str(f["name"]), "detail": M{"flow_id": f["id"], "run_id": run["id"]}})
	}
	return run
}
func (a *App) workflowDecision(c *request, p M, action string) M {
	payload := obj(p["args"])
	state := obj(payload["state"])
	snapshot := obj(state["flow"])
	a.owned("flows", str(snapshot["id"]), c.WS)
	run := a.row("SELECT * FROM flow_runs WHERE id=? AND flow_id=? AND status='waiting'", state["run_id"], snapshot["id"])
	if run == nil {
		fail(409, "该工作流不再等待审批")
	}
	status := "approved"
	if action == "reject" {
		status = "rejected"
	}
	result, e := a.DB.Exec("UPDATE approvals SET status=?,decided_at=?,decided_by=? WHERE id=? AND status='pending'", status, now(), c.User["id"], p["id"])
	must(e)
	n, e := result.RowsAffected()
	must(e)
	if n != 1 {
		fail(409, "审批已处理")
	}
	if action == "reject" {
		a.update("flow_runs", str(run["id"]), M{"status": "cancelled", "finished_at": now()})
		return M{"status": "cancelled", "run_id": run["id"]}
	}
	params := obj(state["params"])
	params["confirm_"+str(state["node_id"])] = true
	return M{"run": a.runFlowState(c.WS, snapshot, params, "approval", 0, state)}
}
func (a *App) workflowRoutes() []route {
	r := []route{
		route{"POST", "/api/workflows/trigger/{key}", true, false, func(c *request) any {
			key := c.p("key")
			f := a.row("SELECT * FROM flows WHERE trigger_key=? AND enabled=1", key)
			if f == nil {
				fail(404, "触发密钥无效")
			}
			if str(f["run_mode"]) != "event" {
				fail(409, "该工作流不是事件触发模式")
			}
			params := obj(c.Body["params"])
			if len(params) == 0 {
				params = obj(c.Body)
			}
			ws := str(f["workspace_id"])
			a.audit(ws, "", "workflow_event_trigger", str(f["name"]))
			run := a.runFlow(ws, f, params, "event", 0)
			return M{"run": run, "flow_id": f["id"], "flow": str(f["name"]), "params": params}
		}},
	}
	for _, base := range []string{"/workflows", "/flows"} {
		base := base
		plural, singular := "workflows", "workflow"
		if base == "/flows" {
			plural, singular = "flows", "flow"
		}
		r = append(r,
			route{"GET", "/api" + base, false, false, func(c *request) any {
				rr := a.list("flows", c.WS)
				for _, f := range rr {
					a.flowShape(f)
				}
				return M{plural: rr}
			}},
			route{"POST", "/api" + base, false, false, func(c *request) any { return M{singular: a.newFlow(c.WS, c.Body)} }},
			route{"GET", "/api" + base + "/{flow_id}", false, false, func(c *request) any {
				f := a.flowShape(a.owned("flows", c.p("flow_id"), c.WS))
				runs := a.rows("SELECT * FROM flow_runs WHERE flow_id=? ORDER BY started_at DESC LIMIT 20", f["id"])
				for _, run := range runs {
					decoded(run)
				}
				f["runs"] = runs
				return M{singular: f, "runs": runs}
			}},
			route{"PATCH", "/api" + base + "/{flow_id}", false, false, func(c *request) any {
				f := decoded(a.owned("flows", c.p("flow_id"), c.WS))
				for k, v := range c.Body {
					if k != "workspace_id" && k != "id" {
						f[k] = v
					}
				}
				normalizeNodes(f)
				a.update("flows", c.p("flow_id"), f)
				return M{singular: a.flowShape(a.owned("flows", c.p("flow_id"), c.WS))}
			}},
			route{"DELETE", "/api" + base + "/{flow_id}", false, false, func(c *request) any {
				a.owned("flows", c.p("flow_id"), c.WS)
				a.exec("DELETE FROM flows WHERE id=?", c.p("flow_id"))
				return M{"ok": true}
			}},
			route{"POST", "/api" + base + "/{flow_id}/run", false, false, func(c *request) any {
				return M{"run": a.runFlow(c.WS, a.owned("flows", c.p("flow_id"), c.WS), obj(c.Body["params"]), "manual", 0)}
			}},
			route{"GET", "/api" + base + "/{flow_id}/runs", false, false, func(c *request) any {
				a.owned("flows", c.p("flow_id"), c.WS)
				return M{"runs": a.rows("SELECT * FROM flow_runs WHERE flow_id=? ORDER BY started_at DESC LIMIT 20", c.p("flow_id"))}
			}},
		)
	}
	r = append(r, route{"GET", "/api/workflow-meta", false, false, func(c *request) any {
		m := M{}
		for k, v := range obj(metadata["workflow"]) {
			m[k] = v
		}
		m["flows"] = a.list("flows", c.WS)
		return m
	}}, route{"GET", "/api/workflow-runs/{run_id}", false, false, func(c *request) any {
		run := a.row("SELECT r.* FROM flow_runs r JOIN flows f ON f.id=r.flow_id WHERE r.id=? AND f.workspace_id=?", c.p("run_id"), c.WS)
		if run == nil {
			fail(404, "运行记录不存在")
		}
		nn := a.rows("SELECT * FROM flow_node_runs WHERE run_id=? ORDER BY started_at", run["id"])
		for _, n := range nn {
			decoded(n)
		}
		return M{"run": decoded(run), "nodes": nn}
	}}, route{"POST", "/api/workflows/{flow_id}/validate", false, false, func(c *request) any {
		f := a.owned("flows", c.p("flow_id"), c.WS)
		order := topo(arr(f["nodes"]), arr(f["edges"]))
		return M{"ok": true, "issues": []M{}, "order": order}
	}}, route{"GET", "/api/workflow-schedule", false, false, func(c *request) any { return M{"workflows": a.list("flows", c.WS)} }})
	for _, action := range []string{"pause", "resume"} {
		action := action
		r = append(r, route{"POST", "/api/workflows/{flow_id}/" + action, false, false, func(c *request) any {
			a.owned("flows", c.p("flow_id"), c.WS)
			a.update("flows", c.p("flow_id"), M{"enabled": action == "resume"})
			return M{"ok": true, "workflow": a.flowShape(a.owned("flows", c.p("flow_id"), c.WS))}
		}})
	}
	return r
}
