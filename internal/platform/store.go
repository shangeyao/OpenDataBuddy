package platform

import (
	"crypto/rand"
	"database/sql"
	"embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	_ "github.com/duckdb/duckdb-go/v2"
	_ "github.com/mattn/go-sqlite3"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
)

type M = map[string]any

//go:embed assets/*
var assets embed.FS

type App struct {
	DB, Lake     *sql.DB
	Dir, Root    string
	columns      map[string]map[string]bool
	ownedKernels sync.Map

	// predictLimiterMu guards per-(workspace, service) token buckets for the
	// online inference endpoint (P2-5). Buckets are created lazily.
	predictLimiterMu sync.Mutex
	predictBuckets   map[string]*tokenBucket
}

type tokenBucket struct {
	mu       sync.Mutex
	tokens   float64
	capacity float64
	rate     float64 // tokens per second
	last     time.Time
}

func now() string { return time.Now().UTC().Format(time.RFC3339) }
func id() string {
	b := make([]byte, 16)
	if _, e := rand.Read(b); e != nil {
		panic(e)
	}
	return hex.EncodeToString(b)
}
func str(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func obj(v any) M {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	var m M
	_ = json.Unmarshal([]byte(str(v)), &m)
	if m == nil {
		m = M{}
	}
	return m
}
func arr(v any) []any {
	if x, ok := v.([]M); ok {
		out := []any{}
		for _, m := range x {
			out = append(out, m)
		}
		return out
	}
	if x, ok := v.([]string); ok {
		out := []any{}
		for _, s := range x {
			out = append(out, s)
		}
		return out
	}
	if a, ok := v.([]any); ok {
		return a
	}
	var a []any
	_ = json.Unmarshal([]byte(str(v)), &a)
	if a == nil {
		a = []any{}
	}
	return a
}
func num(v any) float64 { var n float64; fmt.Sscan(str(v), &n); return n }
func def(m M, k string, v any) any {
	if x, ok := m[k]; ok && x != nil {
		return x
	}
	return v
}
func js(v any) string       { b, e := json.Marshal(v); must(e); return string(b) }
func quote(s string) string { return `"` + strings.ReplaceAll(s, `"`, `""`) + `"` }

// lakeName turns a display name into a safe SQL identifier. Unicode letters are
// kept, so a catalog named with Chinese text still lands in the lake.
func lakeName(name string) string {
	out := []rune{}
	for _, r := range name {
		switch {
		case r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r):
			out = append(out, r)
		default:
			out = append(out, '_')
		}
	}
	s := string(out)
	if s == "" {
		s = "catalog"
	}
	if unicode.IsDigit(rune(s[0])) {
		s = "c_" + s
	}
	if len([]rune(s)) > 64 {
		s = string([]rune(s)[:64])
	}
	return s
}
func must(e error) {
	if e != nil {
		panic(e)
	}
}
func rows(db *sql.DB, q string, args ...any) []M {
	r, e := db.Query(q, args...)
	must(e)
	defer r.Close()
	cols, e := r.Columns()
	must(e)
	out := []M{}
	for r.Next() {
		vals := make([]any, len(cols))
		ptr := make([]any, len(cols))
		for i := range vals {
			ptr[i] = &vals[i]
		}
		must(r.Scan(ptr...))
		m := M{}
		for i, c := range cols {
			if b, ok := vals[i].([]byte); ok {
				vals[i] = string(b)
			}
			if t, ok := vals[i].(time.Time); ok {
				vals[i] = t.Format(time.RFC3339)
			}
			m[c] = vals[i]
		}
		out = append(out, m)
	}
	must(r.Err())
	return out
}
func (a *App) rows(q string, args ...any) []M { return rows(a.DB, q, args...) }
func (a *App) row(q string, args ...any) M {
	r := a.rows(q, args...)
	if len(r) > 0 {
		return r[0]
	}
	return nil
}
func (a *App) exec(q string, args ...any) { _, e := a.DB.Exec(q, args...); must(e) }
func (a *App) insert(table string, m M) M {
	q, args, clean := a.insertStatement(table, m)
	a.exec(q, args...)
	return clean
}
func (a *App) insertStatement(table string, m M) (string, []any, M) {
	clean := M{}
	for k, v := range m {
		if a.columns[table][k] {
			switch v.(type) {
			case map[string]any, []any, []M, []string:
				v = js(v)
			}
			clean[k] = v
		}
	}
	keys := []string{}
	for k := range clean {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	qs := []string{}
	names := []string{}
	vals := []any{}
	for _, k := range keys {
		names = append(names, quote(k))
		qs = append(qs, "?")
		vals = append(vals, clean[k])
	}
	return "INSERT INTO " + quote(table) + " (" + strings.Join(names, ",") + ") VALUES (" + strings.Join(qs, ",") + ")", vals, clean
}
func (a *App) update(table, key string, m M) {
	keys := []string{}
	args := []any{}
	for k, v := range m {
		if k == "id" || !a.columns[table][k] {
			continue
		}
		switch v.(type) {
		case map[string]any, []any, []M, []string:
			v = js(v)
		}
		keys = append(keys, quote(k)+"=?")
		args = append(args, v)
	}
	if len(keys) == 0 {
		return
	}
	args = append(args, key)
	a.exec("UPDATE "+quote(table)+" SET "+strings.Join(keys, ",")+" WHERE id=?", args...)
}
func (a *App) list(table, ws string) []M {
	if a.columns[table]["workspace_id"] {
		return a.rows("SELECT * FROM "+quote(table)+" WHERE workspace_id=?", ws)
	}
	return a.rows("SELECT * FROM " + quote(table))
}
func (a *App) owned(table, key, ws string) M {
	q := "SELECT * FROM " + quote(table) + " WHERE id=?"
	args := []any{key}
	if a.columns[table]["workspace_id"] {
		q += " AND workspace_id=?"
		args = append(args, ws)
	}
	r := a.row(q, args...)
	if r == nil {
		fail(404, "记录不存在")
	}
	return r
}
func (a *App) audit(ws, user, action, target string) {
	a.insert("audit_logs", M{"id": id(), "workspace_id": ws, "user_id": user, "action": action, "target": target, "created_at": now()})
}
func (a *App) setting(k string) string {
	r := a.row("SELECT value FROM settings WHERE key=?", k)
	return str(r["value"])
}
func (a *App) setSetting(k string, v any) {
	a.exec("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, str(v))
}
func Open(root, dir string) (app *App, err error) {
	defer func() {
		if e := recover(); e != nil {
			if app != nil {
				app.Close()
			}
			app = nil
			err = fmt.Errorf("初始化数据失败: %v", e)
		}
	}()
	if e := os.MkdirAll(dir, 0700); e != nil {
		return nil, e
	}
	for _, d := range []string{"uploads", "volumes"} {
		if e := os.MkdirAll(filepath.Join(dir, d), 0700); e != nil {
			return nil, e
		}
	}
	db, e := sql.Open("sqlite3", filepath.Join(dir, "metadata.db")+"?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=on")
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(1)
	lake, e := sql.Open("duckdb", filepath.Join(dir, "warehouse.duckdb"))
	if e != nil {
		db.Close()
		return nil, e
	}
	lake.SetMaxOpenConns(1)
	if e = lake.Ping(); e != nil {
		lake.Close()
		db.Close()
		return nil, e
	}
	a := &App{DB: db, Lake: lake, Dir: dir, Root: root, columns: map[string]map[string]bool{}}
	app = a
	applyExtraTools()
	schema, e := assets.ReadFile("assets/schema.sql")
	if e != nil {
		return nil, e
	}
	tableSQL, indexSQL := []string{}, []string{}
	for _, stmt := range strings.Split(string(schema), ";") {
		if strings.Contains(strings.ToUpper(stmt), "CREATE INDEX") || strings.Contains(strings.ToUpper(stmt), "CREATE UNIQUE INDEX") {
			indexSQL = append(indexSQL, stmt)
		} else {
			tableSQL = append(tableSQL, stmt)
		}
	}
	if _, e = db.Exec(strings.Join(tableSQL, ";")); e != nil {
		a.Close()
		return nil, e
	}
	for _, t := range a.rows("SELECT name FROM sqlite_master WHERE type='table'") {
		name := str(t["name"])
		a.columns[name] = map[string]bool{}
		for _, c := range a.rows("PRAGMA table_info(" + quote(name) + ")") {
			a.columns[name][str(c["name"])] = true
		}
	}
	// Additive migrations preserve all existing SQLite data.
	for table, cols := range migrations {
		for name, decl := range cols {
			if !a.columns[table][name] {
				a.exec("ALTER TABLE " + quote(table) + " ADD COLUMN " + quote(name) + " " + decl)
				a.columns[table][name] = true
			}
		}
	}
	if _, e = db.Exec(strings.Join(indexSQL, ";")); e != nil {
		a.Close()
		return nil, e
	}
	a.bootstrap()
	return a, nil
}
func (a *App) Close() {
	a.ownedKernels.Range(func(key, value any) bool { stopKernel(str(key)); return true })
	if a.Lake != nil {
		a.Lake.Close()
	}
	if a.DB != nil {
		a.DB.Close()
	}
}

var migrations = func() map[string]map[string]string {
	b, e := assets.ReadFile("assets/migrations.json")
	must(e)
	var m map[string]map[string]string
	must(json.Unmarshal(b, &m))
	return m
}()

func (a *App) bootstrap() {
	if a.row("SELECT id FROM users WHERE role='admin' LIMIT 1") == nil {
		a.insert("users", M{"id": id(), "email": env("DATABUDDY_ADMIN_EMAIL", "admin@local"), "name": "管理员", "password_hash": hash(env("DATABUDDY_ADMIN_PASSWORD", "admin123")), "role": "admin", "created_at": now()})
	}
	if len(a.rows("SELECT id FROM workspaces LIMIT 1")) > 0 {
		return
	}
	var fixtures map[string][]M
	b, e := assets.ReadFile("assets/seed.json")
	must(e)
	must(json.Unmarshal(b, &fixtures))
	admin := a.row("SELECT id FROM users WHERE role='admin' LIMIT 1")
	var data []struct {
		Schema, Name string
		Columns      []struct{ Name, Type string }
		Rows         [][]any
	}
	b, e = assets.ReadFile("assets/warehouse.json")
	must(e)
	must(json.Unmarshal(b, &data))
	for _, t := range data {
		_, e = a.Lake.Exec("CREATE SCHEMA IF NOT EXISTS " + quote(t.Schema))
		must(e)
		cols := []string{}
		qs := []string{}
		for _, c := range t.Columns {
			cols = append(cols, quote(c.Name)+" "+c.Type)
			qs = append(qs, "?")
		}
		table := quote(t.Schema) + "." + quote(t.Name)
		_, e = a.Lake.Exec("CREATE TABLE IF NOT EXISTS " + table + " (" + strings.Join(cols, ",") + ")")
		must(e)
		if existing := rows(a.Lake, "SELECT count(*) AS n FROM "+table); len(existing) > 0 && num(existing[0]["n"]) > 0 {
			continue
		}
		tx, e := a.Lake.Begin()
		must(e)
		defer tx.Rollback()
		stmt, e := tx.Prepare("INSERT INTO " + table + " VALUES (" + strings.Join(qs, ",") + ")")
		must(e)
		for _, r := range t.Rows {
			if t.Name == "fact_orders" {
				anchor, _ := time.Parse("2006-01-02", "2026-11-10")
				loc, _ := time.LoadLocation("Asia/Shanghai")
				nowLocal := time.Now().In(loc)
				today, _ := time.Parse("2006-01-02", nowLocal.Format("2006-01-02"))
				days := int(today.Sub(anchor).Hours() / 24)
				for i, c := range t.Columns {
					if c.Name == "order_date" {
						if d, err := time.Parse("2006-01-02", str(r[i])); err == nil {
							r[i] = d.AddDate(0, 0, days).Format("2006-01-02")
						}
					}
				}
			}
			_, e = stmt.Exec(r...)
			must(e)
		}
		must(stmt.Close())
		must(tx.Commit())
	}
	metadataTx, e := a.DB.Begin()
	must(e)
	defer metadataTx.Rollback()
	for table, rr := range fixtures {
		for _, r := range rr {
			if table == "workspaces" {
				r["owner_id"] = admin["id"]
			}
			if table == "workspace_members" || table == "buddy_tasks" {
				r["user_id"] = admin["id"]
			}
			q, args, _ := a.insertStatement(table, r)
			_, e = metadataTx.Exec(q, args...)
			must(e)
		}
	}

	must(metadataTx.Commit())
	a.setSetting("seeded_at", now())
}
func env(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
