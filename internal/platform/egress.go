package platform

import (
	"database/sql"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// reconcileSampleLimit caps how many source rows are compared row by row.
const reconcileSampleLimit = 200

// fetchRows reads rows without panicking, so a broken source never crashes the API.
func fetchRows(db *sql.DB, q string, args ...any) ([]M, bool) {
	r, e := db.Query(q, args...)
	if e != nil {
		return nil, false
	}
	defer r.Close()
	cols, e := r.Columns()
	if e != nil {
		return nil, false
	}
	out := []M{}
	for r.Next() {
		vals := make([]any, len(cols))
		ptr := make([]any, len(cols))
		for i := range vals {
			ptr[i] = &vals[i]
		}
		if e := r.Scan(ptr...); e != nil {
			return nil, false
		}
		m := M{}
		for i, c := range cols {
			if b, ok := vals[i].([]byte); ok {
				vals[i] = string(b)
			}
			m[c] = vals[i]
		}
		out = append(out, m)
	}
	if e := r.Err(); e != nil {
		return nil, false
	}
	return out, true
}

// scalarCount runs a single value query and reports whether the statement succeeded.
func scalarCount(db *sql.DB, q string, args ...any) (float64, bool) {
	r, e := db.Query(q, args...)
	if e != nil {
		return 0, false
	}
	defer r.Close()
	if !r.Next() {
		return 0, false
	}
	var v any
	if e := r.Scan(&v); e != nil {
		return 0, false
	}
	if e := r.Err(); e != nil {
		return 0, false
	}
	if b, ok := v.([]byte); ok {
		v = string(b)
	}
	return num(v), true
}

// findTableIn looks up a workspace table by full reference and returns nil when it is missing.
func findTableIn(list []M, ref string) M {
	for _, t := range list {
		if strings.EqualFold(str(t["ref"]), ref) {
			return t
		}
	}
	return nil
}

// findSyncedIn looks up the lake table a source table was synchronized into.
func findSyncedIn(list []M, catalog, name, sourceID string) M {
	for _, t := range list {
		if str(t["name"]) != name {
			continue
		}
		if sourceID != "" && str(t["source_id"]) == sourceID {
			return t
		}
		if str(t["catalog_name"]) == catalog && str(t["schema_name"]) == "main" {
			return t
		}
	}
	return nil
}

// egressSegment keeps only safe characters in a file name part.
func egressSegment(s string) string {
	out := []rune{}
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '_', r == '-':
			out = append(out, r)
		default:
			out = append(out, '_')
		}
	}
	name := string(out)
	if name == "" || name == "." || name == ".." {
		return "_"
	}
	return name
}

// egressDir returns the default export directory of a workspace.
func (a *App) egressDir(ws string) string {
	return filepath.Join(a.Dir, "egress", ws)
}

// egressFiles lists the exported files of a directory.
func (a *App) egressFiles(dir string) []M {
	files := []M{}
	entries, e := os.ReadDir(dir)
	if e != nil {
		return files
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		info, e := entry.Info()
		if e != nil {
			continue
		}
		files = append(files, M{"name": entry.Name(), "path": filepath.Join(dir, entry.Name()), "size": info.Size(), "size_bytes": info.Size(), "modified_at": info.ModTime().UTC().Format(time.RFC3339)})
	}
	sort.Slice(files, func(i, j int) bool { return str(files[i]["name"]) < str(files[j]["name"]) })
	return files
}

// exportTable copies a lake table to a local CSV or Parquet file.
// The SQL guard blocks COPY statements, so the statement runs on the lake connection directly.
func (a *App) exportTable(ws string, m M) M {
	ref := strings.TrimSpace(str(def(m, "table", m["table_ref"])))
	if ref == "" {
		fail(422, "请指定要导出的表")
	}
	t := a.resolve(ws, ref)
	format := strings.ToLower(strings.TrimSpace(str(def(m, "format", "csv"))))
	if format == "" {
		format = "csv"
	}
	if format != "csv" && format != "parquet" {
		fail(422, "导出格式仅支持 csv 或 parquet")
	}
	dir := a.egressDir(ws)
	if p := strings.TrimSpace(str(m["path"])); p != "" {
		for _, seg := range strings.Split(filepath.ToSlash(p), "/") {
			if seg == ".." {
				fail(422, "导出目录不允许包含 ..")
			}
		}
		dir = p
		if !filepath.IsAbs(dir) {
			dir = filepath.Join(a.Dir, dir)
		}
		dir = filepath.Clean(dir)
	}
	if !filepath.IsAbs(dir) {
		fail(422, "导出目录必须是绝对路径")
	}
	if e := os.MkdirAll(dir, 0700); e != nil {
		fail(500, "无法创建导出目录："+e.Error())
	}
	name := egressSegment(str(t["catalog_name"])) + "_" + egressSegment(str(t["schema_name"])) + "_" + egressSegment(str(t["name"])) + "." + format
	file := filepath.Join(dir, name)
	options := "(FORMAT CSV, HEADER)"
	if format == "parquet" {
		options = "(FORMAT PARQUET)"
	}
	if e := os.Remove(file); e != nil && !os.IsNotExist(e) {
		fail(500, "无法覆盖已存在的导出文件："+e.Error())
	}
	target := strings.ReplaceAll(filepath.ToSlash(file), "'", "''")
	if _, e := a.Lake.Exec("COPY (SELECT * FROM " + physical(t) + ") TO '" + target + "' " + options); e != nil {
		fail(400, e.Error())
	}
	info, e := os.Stat(file)
	if e != nil {
		fail(500, "导出文件不可访问："+e.Error())
	}
	count, _ := scalarCount(a.Lake, "SELECT count(*) AS n FROM "+physical(t))
	return M{"ok": true, "table": t["ref"], "format": format, "path": file, "name": name, "bytes": info.Size(), "size_bytes": info.Size(), "rows": count, "exported_at": now()}
}

// matchRows compares source rows against the synchronized lake table.
// Shared columns are matched row by row, otherwise the comparison degrades to row counts.
func (a *App) matchRows(src *sql.DB, name string, t M, srcRows float64) (float64, string, float64) {
	if srcRows <= 0 {
		targetRows, _ := scalarCount(a.Lake, "SELECT count(*) AS n FROM "+physical(t))
		if targetRows == 0 {
			return 0, "exact", 0
		}
		return 0, "row_count", 0
	}
	srcCols, ok := fetchRows(src, "PRAGMA table_info("+quote(name)+")")
	if !ok {
		return 0, "row_count", 0
	}
	targetCols := a.rows("SELECT name FROM columns WHERE table_id=? ORDER BY ordinal", t["id"])
	shared := []string{}
	for _, c := range srcCols {
		for _, d := range targetCols {
			if str(c["name"]) == str(d["name"]) {
				shared = append(shared, str(c["name"]))
				break
			}
		}
	}
	if len(shared) == 0 {
		targetRows, _ := scalarCount(a.Lake, "SELECT count(*) AS n FROM "+physical(t))
		return math.Min(srcRows, targetRows), "row_count", 0
	}
	limit := reconcileSampleLimit
	if srcRows < float64(limit) {
		limit = int(srcRows)
	}
	sample, ok := fetchRows(src, "SELECT * FROM "+quote(name)+" LIMIT "+strconv.Itoa(limit))
	if !ok || len(sample) == 0 {
		return 0, "row_count", 0
	}
	matched := 0
	for _, r := range sample {
		parts := []string{}
		args := []any{}
		for _, c := range shared {
			v := r[c]
			if b, ok := v.([]byte); ok {
				v = string(b)
			}
			parts = append(parts, quote(c)+" IS NOT DISTINCT FROM ?")
			args = append(args, v)
		}
		n, ok := scalarCount(a.Lake, "SELECT count(*) AS n FROM "+physical(t)+" WHERE "+strings.Join(parts, " AND "), args...)
		if ok && n > 0 {
			matched++
		}
	}
	if float64(len(sample)) >= srcRows {
		return float64(matched), "exact", float64(len(sample))
	}
	return srcRows * float64(matched) / float64(len(sample)), "sampled", float64(len(sample))
}

// reconcileTable compares one source table with its lake counterpart.
func (a *App) reconcileTable(src *sql.DB, name string, t M) M {
	res := M{"table": name, "source_table": name, "source_rows": float64(0), "target_rows": float64(0), "diff": float64(0), "matched_rows": float64(0), "consistency": float64(0)}
	if t == nil {
		if rows, ok := scalarCount(src, "SELECT count(*) AS n FROM "+quote(name)); ok {
			res["source_rows"] = rows
			res["diff"] = rows
		}
		res["status"] = "目标缺失"
		res["message"] = "湖仓中不存在对应表"
		return res
	}
	res["target"] = str(t["ref"])
	targetRows, ok := scalarCount(a.Lake, "SELECT count(*) AS n FROM "+physical(t))
	if !ok {
		if rows, ok := scalarCount(src, "SELECT count(*) AS n FROM "+quote(name)); ok {
			res["source_rows"] = rows
			res["diff"] = rows
		}
		res["status"] = "目标缺失"
		res["message"] = "湖仓物理表不存在"
		return res
	}
	res["target_rows"] = targetRows
	srcRows, readable := scalarCount(src, "SELECT count(*) AS n FROM "+quote(name))
	if !readable {
		res["message"] = "源表无法读取"
	}
	res["source_rows"] = srcRows
	res["diff"] = math.Abs(srcRows - targetRows)
	matched, mode, samples := a.matchRows(src, name, t, srcRows)
	consistency := float64(0)
	if srcRows > 0 {
		consistency = matched / srcRows
	} else if targetRows == 0 {
		consistency = 1
	}
	res["matched_rows"] = matched
	res["consistency"] = consistency
	res["match_mode"] = mode
	res["sample_size"] = samples
	if num(res["diff"]) == 0 && consistency >= 1-1e-9 {
		res["status"] = "一致"
	} else {
		res["status"] = "不一致"
	}
	return res
}

// reconcile compares an external SQLite catalog with the tables synchronized into the lake.
func (a *App) reconcile(ws string, m M) M {
	ref := strings.TrimSpace(str(def(m, "source", m["source_id"])))
	if ref == "" {
		fail(422, "请指定数据源")
	}
	src := a.row("SELECT * FROM external_catalogs WHERE workspace_id=? AND (id=? OR name=?)", ws, ref, ref)
	if src == nil {
		fail(404, "数据源不存在："+ref)
	}
	if str(src["type"]) != "sqlite" {
		fail(422, "对账目前仅支持 SQLite 数据源，其他类型请先导出文件再接入")
	}
	cfg := obj(src["config"])
	path := strings.TrimSpace(str(def(cfg, "path", cfg["database"])))
	if path == "" {
		fail(422, "数据源未配置 SQLite 文件路径")
	}
	db, e := sql.Open("sqlite3", "file:"+path+"?mode=ro")
	if e != nil {
		fail(400, "数据源无法打开："+e.Error())
	}
	defer db.Close()
	if e := db.Ping(); e != nil {
		fail(400, "数据源无法打开："+e.Error())
	}
	list := a.tables(ws)
	results := []M{}
	consistent, inconsistent, missing := 0, 0, 0
	target := strings.TrimSpace(str(m["target"]))
	if target != "" {
		name := target
		if i := strings.LastIndex(target, "."); i >= 0 {
			name = target[i+1:]
		}
		results = append(results, a.reconcileTable(db, name, findTableIn(list, target)))
	} else {
		names, ok := fetchRows(db, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
		if !ok {
			fail(400, "无法读取数据源表清单")
		}
		for _, r := range names {
			name := str(r["name"])
			results = append(results, a.reconcileTable(db, name, findSyncedIn(list, lakeName(str(src["name"])), name, str(src["id"]))))
		}
	}
	for _, r := range results {
		switch str(r["status"]) {
		case "一致":
			consistent++
		case "目标缺失":
			missing++
		default:
			inconsistent++
		}
	}
	message := ""
	if len(results) == 0 {
		message = "没有可对账的表"
	}
	return M{"ok": len(results) > 0 && inconsistent == 0 && missing == 0, "source": M{"id": src["id"], "name": src["name"], "type": src["type"], "path": path}, "results": results, "summary": M{"tables": len(results), "consistent": consistent, "inconsistent": inconsistent, "missing": missing}, "message": message, "checked_at": now()}
}

func (a *App) egressRoutes() []route {
	return []route{
		{"POST", "/api/ingest/reconcile", false, false, func(c *request) any {
			return a.reconcile(c.WS, c.Body)
		}},
		{"POST", "/api/ingest/egress", false, false, func(c *request) any {
			return a.exportTable(c.WS, c.Body)
		}},
		{"GET", "/api/ingest/egress", false, false, func(c *request) any {
			dir := a.egressDir(c.WS)
			files := a.egressFiles(dir)
			total := float64(0)
			for _, f := range files {
				total += num(f["size"])
			}
			return M{"files": files, "count": len(files), "directory": dir, "total_bytes": total}
		}},
	}
}
