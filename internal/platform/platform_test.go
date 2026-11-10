package platform

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func testApp(t *testing.T) *App {
	t.Helper()
	root, e := filepath.Abs("../..")
	if e != nil {
		t.Fatal(e)
	}
	a, e := Open(root, t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(a.Close)
	return a
}
func catches(t *testing.T, status int, fn func()) {
	t.Helper()
	defer func() {
		x := recover()
		if x == nil {
			t.Errorf("expected rejection %d", status)
			return
		}
		e, ok := x.(apiError)
		if !ok || e.Status != status {
			t.Errorf("expected %d got %#v", status, x)
		}
	}()
	fn()
}

func TestAttributionUsesOverallRatio(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	win := periodWindow("last_month")
	expected := a.query(ws, fmt.Sprintf("SELECT sum(amount)/count(DISTINCT order_id) FROM fact_orders WHERE order_date >= DATE '%s' AND order_date <= DATE '%s'", win["start"], win["end"]), 10)
	result := a.attribute(ws, M{"metric": "avg_order_value", "dimension": "channel_name", "period": "last_month"})
	if math.Abs(num(result["total_current"])-num(expected["rows"].([][]any)[0][0])) > 1e-9 {
		t.Fatal("ratio must use the overall aggregate, not sum group ratios", result)
	}
	first := result["contributions"].([]M)[0]
	if num(first["rank"]) != 1 || str(first["dim_value"]) == "" || first["share"] == nil {
		t.Fatal("attribution renderer contract", first)
	}
}
func TestNativeBootstrapAndPersistence(t *testing.T) {
	a := testApp(t)
	w := a.row("SELECT * FROM workspaces LIMIT 1")
	if len(a.tables(str(w["id"]))) != 4 {
		t.Fatal("demo catalog missing")
	}
	q := a.query(str(w["id"]), "SELECT count(*) AS n FROM fact_orders", 10)
	if num(q["rows"].([][]any)[0][0]) != 5200 {
		t.Fatal(q)
	}
	if q["columns"].([]string)[0] != "n" {
		t.Fatal(q)
	}
	if verify("wrong", hash("correct")) {
		t.Fatal("password mismatch accepted")
	}
	a.setSetting("test", "kept")
	if a.setting("test") != "kept" {
		t.Fatal("setting not persisted")
	}
}
func TestSQLWorkspaceAndGuard(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	other := id()
	a.insert("workspaces", M{"id": other, "name": "isolated", "created_at": now()})
	cases := []string{"SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM fact_orders; DELETE FROM users", "WITH x AS (DELETE FROM fact_orders RETURNING *) SELECT * FROM x", "SELECT * FROM information_schema.tables", "SELECT * FROM fact_orders, secret", "SELECT * FROM fact_orders JOIN query('select 1') ON true"}
	for _, q := range cases {
		t.Run(q, func(t *testing.T) {
			defer func() {
				if recover() == nil {
					t.Error("unsafe query accepted")
				}
			}()
			a.query(ws, q, 10)
		})
	}
	catches(t, 403, func() { a.query(other, "SELECT * FROM demo.retail.fact_orders", 10) })
	q := a.query(ws, "WITH recent AS (SELECT order_id FROM fact_orders LIMIT 3) SELECT * FROM recent", 10)
	if q["row_count"] != 3 {
		t.Fatal(q)
	}
	q = a.query(ws, "SELECT 'from fact_orders' AS label, 1 AS n", 1)
	if q["rows"].([][]any)[0][0] != "from fact_orders" {
		t.Fatal(q)
	}
	q = a.query(ws, "SELECT order_id FROM fact_orders", 5)
	if q["row_count"] != 5 || q["truncated"] != true {
		t.Fatal(q)
	}
}
func TestQualityAndDAG(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	r := a.newRule(ws, M{"table_ref": "fact_orders", "column_name": "amount", "rule_type": "range", "config": M{"min": 0, "max": 1000000, "blocking": true}})
	result := a.runRule(ws, r)
	if result["status"] != "pass" {
		t.Fatal(result)
	}
	a.update("quality_rules", str(r["id"]), M{"config": M{"min": 1000000, "blocking": true}})
	if a.runRules(ws, "demo.retail.fact_orders", true)["blocked"] != true {
		t.Fatal("blocking check did not block")
	}
	catches(t, 422, func() { topo([]any{M{"id": "a"}, M{"id": "b"}}, []any{[]any{"a", "b"}, []any{"b", "a"}}) })
	f := a.newFlow(ws, M{"name": "native-dag", "nodes": []any{M{"id": "a", "type": "sql", "config": M{"sql": "SELECT count(*) FROM fact_orders"}}, M{"id": "b", "type": "sql", "config": M{"sql": "SELECT 2"}}}, "edges": []any{[]any{"a", "b"}}})
	run := a.runFlow(ws, f, M{}, "test", 0)
	if run["status"] != "success" || len(arr(run["nodes"])) != 2 {
		t.Fatal(run)
	}
	if len(a.rows("SELECT * FROM flow_node_runs WHERE run_id=?", run["id"])) != 2 {
		t.Fatal("node audit missing")
	}
}
func TestNativePythonKernel(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	key := ws + ":" + id()
	t.Cleanup(func() { stopKernel(key) })
	out := a.python(key, "x = 40\nx + 2", M{})
	if out["status"] != "success" || num(out["result"]) != 42 {
		t.Fatal(out)
	}
	out = a.python(key, "x + 3", M{})
	callback := a.python(key, `query("SELECT count(*) FROM fact_orders")["rows"][0][0]`, M{})
	if num(callback["result"]) != 5200 {
		t.Fatal("native SQL callback failed", callback)
	}
	if num(out["result"]) != 43 {
		t.Fatal("cross-cell state missing", out)
	}
}
func TestBundleIdempotencyAndDestroy(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	admin := a.row("SELECT * FROM users LIMIT 1")
	c := &request{A: a, WS: ws, User: publicUser(admin)}
	text := "bundle: e2e\ntargets:\n  dev: {}\nresources:\n  agents:\n    - name: bundle-agent\n      mode: analysis\n      tools: []\n"
	b := a.insert("bundles", M{"id": id(), "workspace_id": ws, "name": "e2e", "yaml": text, "created_at": now()})
	first := a.deployBundle(c, b, "dev")
	second := a.deployBundle(c, b, "dev")
	if num(obj(first["counts"])["created"]) != 1 || num(obj(second["counts"])["unchanged"]) != 1 {
		t.Fatal(first, second)
	}
}
func TestContractMigrationInventory(t *testing.T) {
	a := &App{}
	have := map[string]bool{}
	for _, r := range a.routes() {
		key := r.Method + " " + r.Path
		if have[key] {
			t.Errorf("duplicate route %s", key)
		}
		have[key] = true
	}
	b, e := assets.ReadFile("assets/openapi.json")
	if e != nil {
		t.Fatal(e)
	}
	var doc struct {
		Paths map[string]map[string]json.RawMessage
	}
	if e = json.Unmarshal(b, &doc); e != nil {
		t.Fatal(e)
	}
	missing := []string{}
	total := 0
	for path, methods := range doc.Paths {
		if path == "/" {
			continue
		}
		for method := range methods {
			total++
			if !have[strings.ToUpper(method)+" "+path] {
				missing = append(missing, strings.ToUpper(method)+" "+path)
			}
		}
	}
	t.Logf("legacy operations: %d, native matches: %d, pending: %d", total, total-len(missing), len(missing))
	if len(missing) > 0 {
		t.Errorf("%d legacy operations have no native route", len(missing))
	}
	for _, s := range missing {
		t.Log("pending", s)
	}
}

func TestReportScheduleMinuteDedup(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	a.exec("UPDATE flows SET enabled=0")
	a.exec("UPDATE buddy_tasks SET enabled=0")
	r := a.insert("reports", M{"id": id(), "workspace_id": ws, "name": "scheduled", "metrics": []string{"gmv"}, "period": "last_month", "created_at": now()})
	at := time.Now().UTC()
	loc, _ := time.LoadLocation("Asia/Shanghai")
	local := at.In(loc)
	cron := fmt.Sprintf("%d %d * * *", local.Minute(), local.Hour())
	a.insert("report_schedules", M{"id": id(), "workspace_id": ws, "report_id": r["id"], "cron": cron, "enabled": true, "created_at": now()})
	a.Tick(at)
	a.Tick(at)
	if count := num(a.row("SELECT count(*) AS n FROM report_runs WHERE report_id=?", r["id"])["n"]); count != 1 {
		t.Fatalf("same-minute duplicate: %v", count)
	}
	a.Tick(at.Add(time.Minute))
	if count := num(a.row("SELECT count(*) AS n FROM report_runs WHERE report_id=?", r["id"])["n"]); count != 1 {
		t.Fatalf("off-minute run: %v", count)
	}
}
func TestAdditiveMigrationAndReopen(t *testing.T) {
	root, _ := filepath.Abs("../..")
	dir := t.TempDir()
	a, e := Open(root, dir)
	if e != nil {
		t.Fatal(e)
	}
	original := a.row("SELECT id,password_hash FROM users LIMIT 1")
	a.exec("ALTER TABLE flows DROP COLUMN timezone")
	a.Close()
	a, e = Open(root, dir)
	if e != nil {
		t.Fatal(e)
	}
	defer a.Close()
	if !a.columns["flows"]["timezone"] {
		t.Fatal("missing additive migration")
	}
	u := a.row("SELECT id,password_hash FROM users LIMIT 1")
	if u["id"] != original["id"] || u["password_hash"] != original["password_hash"] {
		t.Fatal("existing identity was modified")
	}
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	r := a.query(ws, "SELECT count(*) FROM fact_orders", 1)
	if num(r["rows"].([][]any)[0][0]) != 5200 {
		t.Fatal("warehouse was reseeded or lost")
	}
}
func TestMCPNativeIdentityAndTools(t *testing.T) {
	a := testApp(t)
	u := a.row("SELECT * FROM users LIMIT 1")
	token := id()
	a.insert("sessions", M{"token": token, "user_id": u["id"], "created_at": now(), "expires_at": time.Now().Add(time.Hour).UTC().Format(time.RFC3339)})
	t.Setenv("DATABUDDY_TOKEN", token)
	input := strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize"}` + "\n" + `{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"run_query","arguments":{"sql":"SELECT count(*) FROM fact_orders"}}}` + "\n")
	var out bytes.Buffer
	if e := a.ServeMCP(input, &out); e != nil {
		t.Fatal(e)
	}
	if !strings.Contains(out.String(), "5200") || !strings.Contains(out.String(), "protocolVersion") {
		t.Fatal(out.String())
	}
	t.Setenv("DATABUDDY_TOKEN", "")
	if e := a.ServeMCP(strings.NewReader(""), &bytes.Buffer{}); e == nil {
		t.Fatal("MCP accepted missing identity")
	}
}

func TestWorkflowApprovalResumesWithoutReplay(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	f := a.newFlow(ws, M{"name": "approval", "nodes": []M{{"id": "a", "type": "sql", "config": M{"sql": "SELECT 1"}}, {"id": "b", "type": "approval", "config": M{"reason": "check"}}, {"id": "c", "type": "sql", "config": M{"sql": "SELECT 2"}}}, "edges": []any{[]any{"a", "b"}, []any{"b", "c"}}})
	run := a.runFlow(ws, f, M{}, "test", 0)
	if run["status"] != "waiting" {
		t.Fatal(run)
	}
	first := a.row("SELECT id FROM flow_node_runs WHERE run_id=? AND node_id='a'", run["id"])
	approval := a.row("SELECT * FROM approvals WHERE id=?", obj(run["approval"])["id"])
	u := a.row("SELECT * FROM users LIMIT 1")
	c := &request{A: a, WS: ws, User: publicUser(u)}
	resumed := obj(a.workflowDecision(c, approval, "approve")["run"])
	if resumed["status"] != "success" || resumed["id"] != run["id"] {
		t.Fatal(resumed)
	}
	if a.row("SELECT id FROM flow_node_runs WHERE run_id=? AND node_id='a'", run["id"])["id"] != first["id"] {
		t.Fatal("completed node was replayed")
	}
	if len(a.rows("SELECT * FROM flow_node_runs WHERE run_id=?", run["id"])) != 3 {
		t.Fatal("unexpected node history")
	}
}

func TestCatalogStorageIsolation(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	other := id()
	catID, schID := id(), id()
	a.insert("workspaces", M{"id": other, "name": "independent", "created_at": now()})
	cat := a.insert("catalogs", M{"id": catID, "workspace_id": other, "name": "demo", "physical_prefix": "odb_" + other + "_demo", "created_at": now()})
	a.insert("schemas", M{"id": schID, "catalog_id": cat["id"], "name": "retail", "created_at": now()})
	_, e := a.Lake.Exec("CREATE SCHEMA " + quote(lakeNamespace(str(cat["physical_prefix"]), "demo", "retail")))
	if e != nil {
		t.Fatal(e)
	}
	a.importSQL(other, "demo.retail", "fact_orders", "SELECT 1 AS n", "isolated")
	one := a.query(other, "SELECT count(*) FROM fact_orders", 1)
	original := a.query(ws, "SELECT count(*) FROM fact_orders", 1)
	if num(one["rows"].([][]any)[0][0]) != 1 || num(original["rows"].([][]any)[0][0]) != 5200 {
		t.Fatal("catalog namespaces overlap", one, original)
	}
}

func TestLineageColumnsAndGraphContract(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	out := a.discoverLineage(ws, M{"sql": "CREATE TABLE demo.retail.daily AS SELECT order_date, sum(amount) AS gmv FROM demo.retail.fact_orders GROUP BY 1", "save": true, "record_columns": true})
	if num(out["recorded_columns"]) != 2 {
		t.Fatal(out)
	}
	g := a.lineageView(ws, "demo.retail.fact_orders", "both", "table", 3)
	if num(obj(g["counts"])["edges"]) == 0 {
		t.Fatal(g)
	}
	for _, v := range arr(g["edges"]) {
		e := obj(v)
		if e["from"] == nil || e["to"] == nil {
			t.Fatal("SVG edge contract missing", e)
		}
	}
	columns := a.lineageView(ws, "demo.retail.fact_orders", "both", "column", 3)
	if num(obj(columns["counts"])["edges"]) != 2 {
		t.Fatal(columns)
	}
}
