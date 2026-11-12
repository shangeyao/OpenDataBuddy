package platform

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

func (a *App) emit(ws string, m M) M {
	event := str(def(m, "event", "flow_run_failed"))
	subject := str(def(m, "subject", "手动测试告警"))
	deliveries := []M{}
	for _, r := range a.list("alert_rules", ws) {
		if !boolOf(r["enabled"]) || r["event"] != event {
			continue
		}
		for _, key := range arr(r["channels"]) {
			channel := a.owned("alert_channels", str(key), ws)
			if !boolOf(channel["enabled"]) {
				continue
			}
			hash := fmt.Sprintf("%x", sha256.Sum256([]byte(event+":"+subject+":"+str(r["id"]))))
			severity := def(m, "severity", r["severity"])
			if !boolOf(m["force"]) && a.row("SELECT id FROM alert_deliveries WHERE workspace_id=? AND channel_id=? AND dedup_key=? AND created_at>? AND status='sent' LIMIT 1", ws, key, hash, time.Now().UTC().Add(-5*time.Minute).Format(time.RFC3339)) != nil {
				deliveries = append(deliveries, a.insert("alert_deliveries", M{"id": id(), "workspace_id": ws, "rule_id": r["id"], "rule_name": r["name"], "channel_id": key, "channel_name": channel["name"], "channel_type": channel["type"], "event": event, "subject": subject, "severity": severity, "dedup_key": hash, "status": "suppressed", "simulated": false, "payload": m, "created_at": now()}))
				continue
			}
			result := a.deliverAlert(channel, M{"event": event, "subject": subject, "severity": severity, "message": str(def(m, "message", subject))})
			status := "sent"
			if !boolOf(result["ok"]) {
				status = "failed"
			}
			d := a.insert("alert_deliveries", M{"id": id(), "workspace_id": ws, "rule_id": r["id"], "rule_name": r["name"], "channel_id": key, "channel_name": channel["name"], "channel_type": channel["type"], "event": event, "subject": subject, "severity": severity, "dedup_key": hash, "status": status, "simulated": boolOf(result["simulated"]), "payload": m, "delivery": result, "created_at": now()})
			deliveries = append(deliveries, d)
		}
	}
	if len(deliveries) == 0 {
		for _, r := range a.deliverWorkspaceNotify(ws, M{"event": event, "subject": subject, "severity": str(def(m, "severity", "info")), "message": str(def(m, "message", subject))}) {
			deliveries = append(deliveries, r)
		}
	}
	return M{"event": event, "deliveries": deliveries, "total": len(deliveries), "simulated": alertSimulated()}
}
func (a *App) buddyTask(ws string, t M) M {
	u := a.row("SELECT * FROM users WHERE id=?", t["user_id"])
	if u == nil {
		u = a.row("SELECT u.* FROM users u JOIN workspace_members m ON m.user_id=u.id WHERE m.workspace_id=? ORDER BY u.created_at LIMIT 1", ws)
	}
	if u == nil {
		fail(400, "任务没有有效执行身份")
	}
	a.workspace(u, ws)
	c := &request{A: a, User: publicUser(u), WS: ws, Body: M{}}
	conv := a.insert("conversations", M{"id": id(), "workspace_id": ws, "user_id": u["id"], "title": t["name"], "mode": def(t, "mode", "analysis"), "chat_mode": "agent", "created_at": now(), "updated_at": now()})
	out := a.agent(c, conv, str(def(t, "prompt", t["question"])))
	r := a.insert("buddy_task_runs", M{"id": id(), "task_id": t["id"], "workspace_id": ws, "conversation_id": conv["id"], "status": "success", "summary": out["content"], "started_at": now(), "finished_at": now()})
	a.update("buddy_tasks", str(t["id"]), M{"last_run": now()})
	return M{"run": r, "conversation": conv, "result": out}
}
func (a *App) operationRoutes() []route {
	return []route{
		{"GET", "/api/alerts/meta", false, false, func(c *request) any {
			return M{"channels": metadata["channel_types"], "channel_types": metadata["channel_types"], "events": metadata["alert_events"], "severities": []string{"info", "warning", "critical"}, "suppress_window_seconds": 300}
		}},
		{"GET", "/api/alerts/deliveries", false, false, func(c *request) any {
			rr := a.list("alert_deliveries", c.WS)
			stats := M{"sent": 0, "suppressed": 0, "failed": 0}
			for _, r := range rr {
				stats[str(r["status"])] = num(stats[str(r["status"])]) + 1
				decoded(r)
			}
			return M{"deliveries": rr, "total": len(rr), "stats": stats}
		}},
		{"POST", "/api/alerts/emit", false, false, func(c *request) any { return a.emit(c.WS, c.Body) }},
		{"POST", "/api/alerts/channels/{channel_id}/test", false, false, func(c *request) any {
			channel := a.owned("alert_channels", c.p("channel_id"), c.WS)
			result := a.deliverAlert(channel, M{"event": "test", "subject": "渠道测试", "severity": "info", "message": "这是一条来自 OpenDataBuddy 的渠道连通性测试。"})
			status := "sent"
			if !boolOf(result["ok"]) {
				status = "failed"
			}
			a.update("alert_channels", str(channel["id"]), M{"status": map[bool]string{true: "ok", false: "error"}[boolOf(result["ok"])], "last_test_at": now()})
			r := a.insert("alert_deliveries", M{"id": id(), "workspace_id": c.WS, "channel_id": channel["id"], "channel_type": channel["type"], "channel_name": channel["name"], "event": "test", "subject": "渠道测试", "status": status, "simulated": boolOf(result["simulated"]), "payload": M{"message": "渠道连通性测试"}, "delivery": result, "created_at": now()})
			return M{"ok": boolOf(result["ok"]), "simulated": boolOf(result["simulated"]), "delivery": r, "message": str(result["message"])}
		}},
		{"GET", "/api/settings/integrations", false, false, func(c *request) any {
			notify := obj(a.setting("notify." + c.WS))
			git := obj(a.setting("git." + c.WS))
			return M{"notify": notify, "git": git, "channels": a.list("alert_channels", c.WS)}
		}},
		{"PUT", "/api/settings/notify", false, false, func(c *request) any { a.setSetting("notify."+c.WS, js(c.Body)); return M{"ok": true, "notify": c.Body} }},
		{"POST", "/api/settings/notify/test", false, false, func(c *request) any {
			notify := obj(c.Body)
			if len(notify) == 0 {
				notify = obj(a.setting("notify." + c.WS))
			}
			payload := M{"event": "test", "subject": "通知配置测试", "severity": "info", "message": "这是一条来自 OpenDataBuddy 的通知配置测试。"}
			results := []any{}
			ok := true
			message := ""
			for _, item := range []struct{ kind, key string }{{"webhook", "webhook"}, {"wecom", "wecom"}, {"email", "email"}} {
				value := str(notify[item.key])
				if value == "" {
					continue
				}
				cfg := M{}
				switch item.kind {
				case "webhook":
					cfg["url"] = value
				case "wecom":
					cfg["webhook_url"] = value
				case "email":
					cfg["to"] = value
				}
				result := a.deliverAlert(M{"type": item.kind, "name": "通知配置"}, payload)
				results = append(results, result)
				if !boolOf(result["ok"]) {
					ok = false
					message = str(result["message"])
				}
			}
			if len(results) == 0 {
				return M{"ok": false, "message": "请先填写企业微信、Webhook 或邮件地址", "results": results}
			}
			if ok {
				message = "测试消息已发送"
			}
			return M{"ok": ok, "message": message, "results": results, "simulated": alertSimulated()}
		}},
		{"PUT", "/api/settings/git", false, false, func(c *request) any { a.setSetting("git."+c.WS, js(c.Body)); return M{"ok": true, "git": c.Body} }},
		{"POST", "/api/settings/git/test", false, false, func(c *request) any {
			raw := str(def(c.Body, "url", c.Body["repo_url"]))
			parsed, e := url.Parse(raw)
			if e != nil || parsed.Scheme != "https" && parsed.Scheme != "http" {
				fail(422, "仓库地址必须为 HTTP 或 HTTPS URL")
			}
			if refs, reason := gitRemoteRefs(raw, str(c.Body["branch"])); reason == "" {
				return M{"ok": true, "method": "git ls-remote", "message": "仓库可访问", "refs": refs, "ref_count": len(refs)}
			} else {
				ctx, cancel := context.WithTimeout(c.R.Context(), 10*time.Second)
				defer cancel()
				req, e2 := http.NewRequestWithContext(ctx, "HEAD", raw, nil)
				if e2 != nil {
					fail(400, "仓库无法访问："+reason)
				}
				resp, e3 := http.DefaultClient.Do(req)
				if e3 != nil {
					fail(400, "仓库无法访问："+reason)
				}
				defer resp.Body.Close()
				io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
				return M{"ok": resp.StatusCode < 400, "method": "http head", "status": resp.StatusCode, "message": fmt.Sprintf("HTTP %d（git 探测不可用：%s）", resp.StatusCode, reason)}
			}
		}},
		{"POST", "/api/buddy-tasks/{task_id}/run", false, false, func(c *request) any {
			t := a.owned("buddy_tasks", c.p("task_id"), c.WS)
			t["user_id"] = c.User["id"]
			return a.buddyTask(c.WS, t)
		}},
		{"GET", "/api/buddy-tasks/{task_id}/runs", false, false, func(c *request) any {
			a.owned("buddy_tasks", c.p("task_id"), c.WS)
			return M{"runs": a.rows("SELECT * FROM buddy_task_runs WHERE task_id=? AND workspace_id=? ORDER BY started_at DESC", c.p("task_id"), c.WS)}
		}},
		{"GET", "/api/buddy-runs", false, false, func(c *request) any {
			return M{"runs": a.rows("SELECT * FROM buddy_task_runs WHERE workspace_id=? ORDER BY started_at DESC LIMIT 50", c.WS)}
		}},
		{"POST", "/api/external-catalogs/{catalog_id}/sync", false, false, func(c *request) any {
			r := a.owned("external_catalogs", c.p("catalog_id"), c.WS)
			if r["type"] != "sqlite" {
				fail(422, "本地同步目前仅支持 SQLite，其他数据源请导出文件后接入")
			}
			cfg := obj(r["config"])
			source, e := sql.Open("sqlite3", "file:"+str(def(cfg, "path", cfg["database"]))+"?mode=ro")
			if e != nil {
				fail(400, e.Error())
			}
			defer source.Close()
			tt := rows(source, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
			name := lakeName(str(r["name"]))
			cat := a.row("SELECT * FROM catalogs WHERE workspace_id=? AND name=?", c.WS, name)
			if cat == nil {
				cat = a.insert("catalogs", M{"id": id(), "workspace_id": c.WS, "name": name, "physical_prefix": "odb_" + c.WS + "_" + name, "created_at": now()})
			}
			sch := a.row("SELECT * FROM schemas WHERE catalog_id=? AND name='main'", cat["id"])
			if sch == nil {
				sch = a.insert("schemas", M{"id": id(), "catalog_id": cat["id"], "name": "main", "created_at": now()})
			}
			sch["catalog_name"] = name
			sch["physical_prefix"] = cat["physical_prefix"]
			_, e = a.Lake.Exec("CREATE SCHEMA IF NOT EXISTS " + quote(lakeNamespace(str(cat["physical_prefix"]), name, "main")))
			must(e)
			imported := []M{}
			created := 0
			updated := 0
			for _, t := range tt {
				tableName := validName(str(t["name"]))
				existed := a.row("SELECT id FROM tables WHERE schema_id=? AND name=?", sch["id"], tableName) != nil
				data := rows(source, "SELECT * FROM "+quote(tableName)+" LIMIT 100000")
				cols := rows(source, "PRAGMA table_info("+quote(tableName)+")")
				defs := []string{}
				qs := []string{}
				for _, col := range cols {
					typ := "VARCHAR"
					u := strings.ToUpper(str(col["type"]))
					if strings.Contains(u, "INT") {
						typ = "BIGINT"
					}
					if strings.Contains(u, "REAL") || strings.Contains(u, "FLOAT") || strings.Contains(u, "DOUBLE") {
						typ = "DOUBLE"
					}
					defs = append(defs, quote(str(col["name"]))+" "+typ)
					qs = append(qs, "?")
				}
				target := quote(lakeNamespace(str(cat["physical_prefix"]), name, "main")) + "." + quote(tableName)
				_, e = a.Lake.Exec("CREATE OR REPLACE TABLE " + target + " (" + strings.Join(defs, ",") + ")")
				must(e)
				tx, e := a.Lake.Begin()
				must(e)
				stmt, e := tx.Prepare("INSERT INTO " + target + " VALUES (" + strings.Join(qs, ",") + ")")
				must(e)
				for _, row := range data {
					values := []any{}
					for _, col := range cols {
						values = append(values, row[str(col["name"])])
					}
					_, e = stmt.Exec(values...)
					if e != nil {
						stmt.Close()
						tx.Rollback()
						fail(400, e.Error())
					}
				}
				stmt.Close()
				must(tx.Commit())
				imported = append(imported, a.registerTable(c.WS, sch, tableName, "外部 SQLite 同步"))
				if existed {
					updated++
				} else {
					created++
				}
			}
			a.update("external_catalogs", str(r["id"]), M{"status": "ok", "last_sync_at": now()})
			return M{"ok": true, "tables": imported, "count": len(imported), "created": created, "updated": updated, "target": str(cat["name"]) + "." + str(sch["name"])}
		}},
	}
}

// Five-field cron with lists, ranges and steps. Scheduler timestamps provide restart-safe minute deduplication.
func cronField(field string, n, min, max int) bool {
	matched := false
	for _, part := range strings.Split(field, ",") {
		base, stepText, hasStep := strings.Cut(part, "/")
		step := 1
		if hasStep {
			v, e := strconv.Atoi(stepText)
			if e != nil || v <= 0 {
				fail(422, "cron 步长无效")
			}
			step = v
		}
		lo, hi := min, max
		if base != "*" {
			left, right, rangeOK := strings.Cut(base, "-")
			v, e := strconv.Atoi(left)
			if e != nil {
				fail(422, "cron 仅支持数值、*、列表、范围和步长")
			}
			lo = v
			hi = v
			if rangeOK {
				hi, e = strconv.Atoi(right)
				if e != nil {
					fail(422, "cron 范围无效")
				}
			} else if hasStep {
				hi = max
			}
		}
		if lo < min || hi > max || lo > hi {
			fail(422, "cron 超出有效范围")
		}
		if n >= lo && n <= hi && (n-lo)%step == 0 {
			matched = true
		}
	}
	return matched
}
func cronMatches(expr string, t time.Time) bool {
	fields := strings.Fields(expr)
	if len(fields) != 5 {
		fail(422, "cron 必须为 5 段")
	}
	minute := cronField(fields[0], t.Minute(), 0, 59)
	hour := cronField(fields[1], t.Hour(), 0, 23)
	day := cronField(fields[2], t.Day(), 1, 31)
	month := cronField(fields[3], int(t.Month()), 1, 12)
	week := cronField(fields[4], int(t.Weekday()), 0, 7)
	if t.Weekday() == time.Sunday {
		week = week || cronField(fields[4], 7, 0, 7)
	}
	if fields[2] != "*" && fields[4] != "*" {
		day = day || week
	} else {
		day = day && week
	}
	return minute && hour && day && month
}
func (a *App) Tick(at time.Time) {
	for _, table := range []string{"flows", "report_schedules", "buddy_tasks", "sync_tasks"} {
		for _, r := range a.rows("SELECT * FROM " + quote(table) + " WHERE enabled=1") {
			func() {
				defer func() {
					if e := recover(); e != nil {
						fmt.Printf("scheduler %s %v: %v\n", table, r["id"], e)
					}
				}()
				loc, e := time.LoadLocation(firstNonempty(str(r["timezone"]), "Asia/Shanghai"))
				if e != nil || loc == nil {
					loc = time.UTC
				}
				local := at.In(loc)
				last := firstNonempty(str(r["last_run_at"]), str(r["last_run"]))
				if table == "flows" {
					if a.row("SELECT id FROM flow_runs WHERE flow_id=? AND status IN ('running','waiting') LIMIT 1", r["id"]) != nil {
						return
					}
					if recent := a.row("SELECT started_at FROM flow_runs WHERE flow_id=? ORDER BY started_at DESC LIMIT 1", r["id"]); recent != nil {
						last = str(recent["started_at"])
					}
				}
				previous, e := time.Parse(time.RFC3339, last)
				if e == nil && previous.Unix()/60 == at.Unix()/60 {
					return
				}
				mode := str(def(r, "run_mode", "cron"))
				if mode == "manual" {
					return
				}
				due := false
				if mode == "continuous" {
					due = e != nil || at.Sub(previous) >= time.Duration(mathMax(60, num(r["interval_seconds"])))*time.Second
				} else if mode == "once" {
					if moment, e2 := parseRunAt(str(r["run_at"]), str(r["timezone"])); e2 == nil && !moment.After(local) {
						due = true
					}
				} else if str(r["cron"]) != "" {
					due = cronMatches(str(r["cron"]), local)
				}
				if !due {
					return
				}
				ws := str(r["workspace_id"])
				switch table {
				case "flows":
					a.runFlow(ws, r, M{}, "schedule", 0)
					if mode == "once" {
						a.update("flows", str(r["id"]), M{"enabled": false, "last_run_at": at.UTC().Format(time.RFC3339), "updated_at": at.UTC().Format(time.RFC3339)})
					}
				case "report_schedules":
					report := a.owned("reports", str(r["report_id"]), ws)
					report["_channels"] = arr(r["channels"])
					a.runReport(ws, report, "schedule")
					a.update(table, str(r["id"]), M{"last_run_at": at.UTC().Format(time.RFC3339), "last_status": "success"})
				case "buddy_tasks":
					a.buddyTask(ws, r)
				case "sync_tasks":
					a.runSyncTask(ws, r)
				}
			}()
		}
	}
}
func (a *App) StartScheduler() func() {
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		tick := time.NewTicker(20 * time.Second)
		defer tick.Stop()
		for {
			select {
			case at := <-tick.C:
				a.Tick(at)
			case <-ctx.Done():
				return
			}
		}
	}()
	return func() { cancel(); wg.Wait() }
}
