package platform

import (
	"database/sql"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// sync.go implements scheduled ingestion: a sync task reads a registered source
// and lands it in the workspace lake, with optional incremental watermarks.

// syncRoutes exposes the sync task lifecycle.
func (a *App) syncRoutes() []route {
	return []route{
		{"GET", "/api/sync/tasks", false, false, func(c *request) any {
			rr := a.list("sync_tasks", c.WS)
			for _, t := range rr {
				decoded(t)
			}
			return M{"tasks": rr, "summary": a.syncSummary(c.WS)}
		}},
		{"POST", "/api/sync/tasks", false, false, func(c *request) any {
			return M{"task": a.newSyncTask(c.WS, c.Body)}
		}},
		{"GET", "/api/sync/tasks/{task_id}", false, false, func(c *request) any {
			t := a.owned("sync_tasks", c.p("task_id"), c.WS)
			decoded(t)
			return M{"task": t}
		}},
		{"PATCH", "/api/sync/tasks/{task_id}", false, false, func(c *request) any {
			t := decoded(a.owned("sync_tasks", c.p("task_id"), c.WS))
			for k, v := range c.Body {
				if k != "id" && k != "workspace_id" {
					t[k] = v
				}
			}
			if str(t["cron"]) != "" {
				cronMatches(str(t["cron"]), time.Now())
			}
			a.update("sync_tasks", c.p("task_id"), t)
			return M{"task": decoded(a.owned("sync_tasks", c.p("task_id"), c.WS))}
		}},
		{"DELETE", "/api/sync/tasks/{task_id}", false, false, func(c *request) any {
			a.owned("sync_tasks", c.p("task_id"), c.WS)
			a.exec("DELETE FROM sync_tasks WHERE id=?", c.p("task_id"))
			return M{"ok": true}
		}},
		{"POST", "/api/sync/tasks/{task_id}/run", false, false, func(c *request) any {
			t := a.owned("sync_tasks", c.p("task_id"), c.WS)
			return M{"run": a.runSyncTask(c.WS, t)}
		}},
	}
}

// syncSummary counts the sync tasks by state.
func (a *App) syncSummary(ws string) M {
	out := M{"total": 0, "enabled": 0, "failed": 0}
	for _, t := range a.list("sync_tasks", ws) {
		out["total"] = num(out["total"]) + 1
		if boolOf(t["enabled"]) {
			out["enabled"] = num(out["enabled"]) + 1
		}
		if str(t["last_status"]) == "failed" {
			out["failed"] = num(out["failed"]) + 1
		}
	}
	return out
}

// newSyncTask creates a sync task for a registered source.
func (a *App) newSyncTask(ws string, body M) M {
	if str(body["name"]) == "" {
		fail(422, "同步任务名称不能为空")
	}
	sourceID := str(def(body, "source_id", body["source"]))
	source := a.row("SELECT * FROM sources WHERE id=? AND workspace_id=?", sourceID, ws)
	if source == nil {
		source = a.row("SELECT * FROM sources WHERE workspace_id=? AND name=?", ws, sourceID)
	}
	if source == nil {
		fail(404, "数据源不存在")
	}
	if str(source["type"]) != "sqlite" {
		fail(422, "当前仅支持 SQLite 数据源的离线同步")
	}
	target := str(body["target"])
	if target != "" {
		parts := strings.Split(target, ".")
		if len(parts) != 2 {
			fail(422, "目标格式应为 catalog.schema")
		}
		if a.row("SELECT s.id FROM schemas s JOIN catalogs c ON c.id=s.catalog_id WHERE c.workspace_id=? AND c.name=? AND s.name=?", ws, parts[0], parts[1]) == nil {
			fail(404, "目标 Schema 不存在")
		}
	}
	mode := str(def(body, "mode", "full"))
	if mode != "full" && mode != "incremental" {
		fail(422, "同步方式仅支持 full 或 incremental")
	}
	t := M{
		"id": id(), "workspace_id": ws, "name": str(body["name"]), "source_id": str(source["id"]),
		"target": target, "tables": arr(def(body, "tables", body["table_list"])), "mode": mode,
		"watermark_column": str(body["watermark_column"]), "watermark_value": str(body["watermark_value"]),
		"cron": str(def(body, "cron", "")), "enabled": def(body, "enabled", false),
		"last_run_at": "", "last_status": "", "last_message": "", "created_at": now(),
	}
	a.insert("sync_tasks", t)
	return decoded(a.owned("sync_tasks", str(t["id"]), ws))
}

// runSyncTask copies the selected source tables into the workspace lake.
func (a *App) runSyncTask(ws string, t M) (out M) {
	out = M{"status": "success", "tables": []any{}, "total_rows": 0}
	defer func() {
		if e := recover(); e != nil {
			out = M{"status": "failed", "tables": []any{}, "total_rows": 0, "error": fmt.Sprint(e)}
		}
	}()
	source := a.owned("sources", str(t["source_id"]), ws)
	cfg := obj(source["config"])
	path := str(def(cfg, "path", cfg["database"]))
	if path == "" {
		fail(422, "数据源未配置本地路径")
	}
	db, e := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if e != nil {
		fail(400, e.Error())
	}
	defer db.Close()
	names := []string{}
	for _, n := range arr(t["tables"]) {
		names = append(names, str(n))
	}
	if len(names) == 0 {
		for _, r := range rows(db, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name") {
			names = append(names, str(r["name"]))
		}
	}
	target := str(t["target"])
	cat, sch := "", ""
	if target != "" {
		parts := strings.Split(target, ".")
		cat, sch = parts[0], parts[1]
	} else {
		cat, sch = "synced", "main"
	}
	catRow := a.row("SELECT * FROM catalogs WHERE workspace_id=? AND name=?", ws, cat)
	if catRow == nil {
		catRow = a.insert("catalogs", M{"id": id(), "workspace_id": ws, "name": cat, "physical_prefix": "odb_" + ws + "_" + cat, "created_at": now()})
	}
	schRow := a.row("SELECT * FROM schemas WHERE catalog_id=? AND name=?", catRow["id"], sch)
	if schRow == nil {
		schRow = a.insert("schemas", M{"id": id(), "catalog_id": catRow["id"], "name": sch, "created_at": now()})
	}
	schRow["catalog_name"] = cat
	schRow["physical_prefix"] = catRow["physical_prefix"]
	_, e = a.Lake.Exec("CREATE SCHEMA IF NOT EXISTS " + quote(lakeNamespace(str(catRow["physical_prefix"]), cat, sch)))
	must(e)
	results := []any{}
	for _, name := range names {
		if !identifier.MatchString(name) {
			continue
		}
		cols := rows(db, "PRAGMA table_info("+quote(name)+")")
		if len(cols) == 0 {
			continue
		}
		where := ""
		if str(t["mode"]) == "incremental" && str(t["watermark_column"]) != "" {
			where = " WHERE " + quote(str(t["watermark_column"])) + " > " + sqlLiteral(str(t["watermark_value"]))
		}
		data := rows(db, "SELECT * FROM "+quote(name)+where)
		defs := []string{}
		qs := []string{}
		for _, col := range cols {
			defs = append(defs, quote(str(col["name"]))+" "+columnType(str(col["type"])))
			qs = append(qs, "?")
		}
		targetTable := quote(lakeNamespace(str(catRow["physical_prefix"]), cat, sch)) + "." + quote(name)
		_, e = a.Lake.Exec("CREATE OR REPLACE TABLE " + targetTable + " (" + strings.Join(defs, ",") + ")")
		must(e)
		if len(data) > 0 {
			tx, e := a.Lake.Begin()
			must(e)
			stmt, e := tx.Prepare("INSERT INTO " + targetTable + " VALUES (" + strings.Join(qs, ",") + ")")
			must(e)
			for _, row := range data {
				values := []any{}
				for _, col := range cols {
					values = append(values, row[str(col["name"])])
				}
				if _, e = stmt.Exec(values...); e != nil {
					stmt.Close()
					tx.Rollback()
					fail(400, e.Error())
				}
			}
			stmt.Close()
			must(tx.Commit())
		}
		a.registerTable(ws, schRow, name, "同步任务 "+str(t["name"]))
		results = append(results, M{"table": name, "rows": len(data), "mode": str(t["mode"])})
		out["total_rows"] = num(out["total_rows"]) + float64(len(data))
		if str(t["mode"]) == "incremental" && str(t["watermark_column"]) != "" {
			max := ""
			for _, row := range data {
				v := str(row[str(t["watermark_column"])])
				if v > max {
					max = v
				}
			}
			if max != "" {
				a.update("sync_tasks", str(t["id"]), M{"watermark_value": max})
			}
		}
	}
	out["tables"] = results
	out["target"] = cat + "." + sch
	a.update("sync_tasks", str(t["id"]), M{"last_run_at": now(), "last_status": "success", "last_message": fmt.Sprintf("%d 张表、%d 行", len(results), int(num(out["total_rows"])))})
	return
}

// columnType maps a SQLite column type to a DuckDB type.
func columnType(t string) string {
	u := strings.ToUpper(t)
	switch {
	case strings.Contains(u, "INT"):
		return "BIGINT"
	case strings.Contains(u, "REAL"), strings.Contains(u, "FLOAT"), strings.Contains(u, "DOUBLE"), strings.Contains(u, "DECIMAL"), strings.Contains(u, "NUMERIC"):
		return "DOUBLE"
	case strings.Contains(u, "BOOL"):
		return "BOOLEAN"
	case strings.Contains(u, "DATE"), strings.Contains(u, "TIME"):
		return "TIMESTAMP"
	case strings.Contains(u, "BLOB"):
		return "BLOB"
	}
	return "VARCHAR"
}

// createSource registers a data source from a template.
func (a *App) createSource(ws string, args M) M {
	name := strings.TrimSpace(str(args["name"]))
	if name == "" {
		fail(422, "数据源名称不能为空")
	}
	template := str(args["template"])
	kind := str(args["type"])
	if kind == "" && template != "" {
		for _, v := range arr(metadata["source_templates"]) {
			tpl := obj(v)
			if str(tpl["id"]) == template || str(tpl["type"]) == template {
				kind = str(tpl["type"])
				break
			}
		}
	}
	if kind == "" {
		kind = "file"
	}
	cfg := obj(args["config"])
	if len(cfg) == 0 && template != "" {
		for _, v := range arr(metadata["source_templates"]) {
			tpl := obj(v)
			if str(tpl["id"]) != template && str(tpl["type"]) != template {
				continue
			}
			for _, f := range arr(tpl["fields"]) {
				field := obj(f)
				cfg[str(field["key"])] = ""
			}
			break
		}
	}
	s := M{"id": id(), "workspace_id": ws, "name": name, "type": kind, "config": cfg, "status": "unknown", "description": str(args["description"]), "created_at": now()}
	a.insert("sources", s)
	return M{"source": decoded(a.owned("sources", str(s["id"]), ws)), "next_step": "为该数据源创建同步任务，或直接在数据接入页测试连通性"}
}

// createSyncTask registers a sync task for a source.
func (a *App) createSyncTask(ws string, args M) M {
	body := M{"name": str(args["name"]), "source_id": str(def(args, "source_id", args["source"])), "target": str(args["target"]), "cron": str(args["cron"]), "mode": str(def(args, "mode", "full")), "tables": arr(args["tables"]), "enabled": def(args, "enabled", true), "watermark_column": str(args["watermark_column"])}
	t := a.newSyncTask(ws, body)
	return M{"task": t, "next_step": "同步任务已创建，可立即运行一次或在数据接入页查看调度状态"}
}

// extraTools are tools defined in code so the catalog can evolve with the
// implementation; they are merged into the metadata catalog at startup.
var extraTools = []M{
	{
		"name": "create_source", "group": "ingest", "risk": "write", "description": "注册数据源",
		"schema": M{"type": "function", "function": M{
			"name":        "create_source",
			"description": "注册一个数据源并生成连接配置。template 取模板 id（如 sqlite、mysql、postgres），config 填连接参数。",
			"parameters": M{"type": "object", "properties": M{
				"name":        M{"type": "string", "description": "数据源名称"},
				"template":   M{"type": "string", "description": "数据源模板 id"},
				"config":     M{"type": "object", "description": "连接配置，如 path、host、port、database、user、password"},
				"description": M{"type": "string", "description": "用途说明"},
			}, "required": []any{"name"}}}},
	},
	{
		"name": "create_sync_task", "group": "ingest", "risk": "write", "description": "创建同步任务",
		"schema": M{"type": "function", "function": M{
			"name":        "create_sync_task",
			"description": "为一个已注册的数据源创建离线同步任务：按 cron 调度把源表同步进湖仓。",
			"parameters": M{"type": "object", "properties": M{
				"name":            M{"type": "string", "description": "任务名称"},
				"source_id":       M{"type": "string", "description": "数据源 id 或名称"},
				"target":          M{"type": "string", "description": "目标 catalog.schema"},
				"cron":            M{"type": "string", "description": "五段 cron 表达式"},
				"mode":            M{"type": "string", "description": "full 或 incremental"},
				"tables":          M{"type": "array", "items": M{"type": "string"}, "description": "要同步的源表，留空表示全部"},
				"watermark_column": M{"type": "string", "description": "增量同步的水位列"},
			}, "required": []any{"name", "source_id"}}}},
	},
}

// applyExtraTools merges the code defined tools into the metadata catalog.
func applyExtraTools() {
	for _, t := range extraTools {
		metadata["tool_catalog"] = append(arr(metadata["tool_catalog"]), M{"name": t["name"], "group": t["group"], "risk": t["risk"], "description": t["description"]})
		metadata["tool_schemas"] = append(arr(metadata["tool_schemas"]), t["schema"])
	}
}

// itoa is a small helper for building messages.
func itoa(v int) string {
	return strconv.Itoa(v)
}
