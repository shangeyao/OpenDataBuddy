package platform

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"os"
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
	if !pythonAvailable() {
		t.Skip("未安装可用的 Python 解释器，跳过内核测试")
	}
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

func TestSyncTaskLandsInLake(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	path := filepath.Join(t.TempDir(), "source.db")
	db, e := sql.Open("sqlite3", path)
	if e != nil {
		t.Fatal(e)
	}
	if _, e = db.Exec("CREATE TABLE sync_demo (id INTEGER, name TEXT, amount DOUBLE)"); e != nil {
		t.Fatal(e)
	}
	if _, e = db.Exec("INSERT INTO sync_demo VALUES (1,'a',10),(2,'b',20),(3,'c',30)"); e != nil {
		t.Fatal(e)
	}
	db.Close()
	src := a.insert("sources", M{"id": id(), "workspace_id": ws, "name": "本地源", "type": "sqlite", "config": M{"path": path}, "status": "ok", "created_at": now()})
	task := a.newSyncTask(ws, M{"name": "测试同步", "source_id": str(src["id"]), "target": "demo.retail", "mode": "full", "enabled": true})
	out := a.runSyncTask(ws, task)
	if str(out["status"]) != "success" || num(out["total_rows"]) != 3 {
		t.Fatal(out)
	}
	q := a.query(ws, "SELECT count(*) AS n, sum(amount) AS s FROM sync_demo", 10)
	if num(q["rows"].([][]any)[0][0]) != 3 || num(q["rows"].([][]any)[0][1]) != 60 {
		t.Fatal(q)
	}
	if a.row("SELECT id FROM tables WHERE name='sync_demo'") == nil {
		t.Fatal("synced table not registered")
	}
	exported := a.exportTable(ws, M{"table": "demo.retail.sync_demo", "format": "csv"})
	if !boolOf(exported["ok"]) {
		t.Fatal(exported)
	}
	if _, e = os.Stat(str(exported["path"])); e != nil {
		t.Fatal("export file missing", exported)
	}
}

func TestPredictServesRegisteredModel(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	model := a.insert("registered_models", M{"id": id(), "workspace_id": ws, "name": "复购率模型", "description": "", "source_experiment": "", "framework": "SQL", "owner": "", "created_at": now()})
	a.addVersion(ws, model, M{"artifact_uri": "sql:SELECT count(*) AS n FROM demo.retail.fact_orders", "stage": "production"})
	service := a.insert("model_services", M{"id": id(), "workspace_id": ws, "name": "复购率服务", "model_ref": str(model["id"]), "version": "v1", "replicas": 1, "min_replicas": 1, "max_replicas": 4, "target_qps": 50, "traffic": M{"v1": 100}, "status": "running", "created_at": now()})
	out := a.predict(ws, str(service["id"]), M{})
	rows := out["rows"].([][]any)
	if len(rows) != 1 || num(rows[0][0]) != 5200 {
		t.Fatal(out)
	}
	inst := a.predict(ws, str(service["id"]), M{"instances": []any{M{"label": "x"}}})
	if len(inst["instances"].([]any)) != 1 {
		t.Fatal(inst)
	}
	catches(t, 422, func() {
		plain := a.insert("registered_models", M{"id": id(), "workspace_id": ws, "name": "无推理逻辑模型", "description": "", "source_experiment": "", "framework": "sklearn", "owner": "", "created_at": now()})
		a.addVersion(ws, plain, M{"artifact_uri": "s3://bucket/model.pkl", "stage": "production"})
		bad := a.insert("model_services", M{"id": id(), "workspace_id": ws, "name": "无推理", "model_ref": str(plain["id"]), "version": "v1", "replicas": 1, "min_replicas": 1, "max_replicas": 4, "target_qps": 50, "traffic": M{"v1": 100}, "status": "running", "created_at": now()})
		a.predict(ws, str(bad["id"]), M{})
	})
}

func TestScenarioToolsAndPrincipalCheck(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	names := func(list []any) map[string]bool {
		out := map[string]bool{}
		for _, s := range list {
			out[str(obj(obj(s)["function"])["name"])] = true
		}
		return out
	}
	analysis := names(a.toolsForMode("analysis", nil))
	if analysis["create_flow"] || analysis["apply_tags"] || !analysis["query_metric"] {
		t.Fatal("analysis tools leaked", analysis)
	}
	governance := names(a.toolsForMode("governance", nil))
	if governance["create_flow"] || !governance["scan_sensitive"] {
		t.Fatal("governance tools leaked", governance)
	}
	engineering := names(a.toolsForMode("engineering", nil))
	if !engineering["create_flow"] || !engineering["create_source"] {
		t.Fatal("engineering tools missing", engineering)
	}
	whitelist := names(a.toolsForMode("engineering", []any{"list_tables"}))
	if len(whitelist) != 1 || !whitelist["list_tables"] {
		t.Fatal("whitelist ignored", whitelist)
	}
	delegated := a.assessPrincipal(ws, "owner@local", "guest@local")
	if !boolOf(delegated["delegated"]) || boolOf(obj(delegated["check"])["allowed"]) {
		t.Fatal("delegation must be denied by default", delegated)
	}
	a.setSetting("obo_policy."+ws, js(M{"allowed": []any{"guest@local"}, "default": "deny"}))
	allowed := a.assessPrincipal(ws, "owner@local", "guest@local")
	if !boolOf(obj(allowed["check"])["allowed"]) {
		t.Fatal("authorized delegation rejected", allowed)
	}
	assessment := a.assessTool(ws, "create_flow", M{"name": "x"}, M{"principal": "owner@local"})
	if !boolOf(assessment["requires_approval"]) || len(arr(assessment["three_factors"])) != 3 {
		t.Fatal(assessment)
	}
}

func TestEventTriggerWorkflow(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	flow := a.newFlow(ws, M{"name": "事件触发流", "nodes": []any{M{"id": "a", "type": "sql", "config": M{"sql": "SELECT 1"}}}, "edges": []any{}, "run_mode": "event", "enabled": true})
	if str(flow["trigger_key"]) == "" || str(flow["trigger_url"]) == "" {
		t.Fatal("event flow needs a trigger key", flow)
	}
	found := false
	for _, r := range a.workflowRoutes() {
		if r.Path == "/api/workflows/trigger/{key}" && r.Public {
			found = true
		}
	}
	if !found {
		t.Fatal("event trigger route missing")
	}
	catches(t, 422, func() {
		a.newFlow(ws, M{"name": "坏模式", "nodes": []any{}, "edges": []any{}, "run_mode": "whatever"})
	})
	catches(t, 422, func() {
		a.newFlow(ws, M{"name": "缺时间", "nodes": []any{}, "edges": []any{}, "run_mode": "once"})
	})
}

func TestReportDeliveryUsesScheduledChannel(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	t.Setenv("DATABUDDY_ALERT_SIMULATE", "1")
	report := a.insert("reports", M{"id": id(), "workspace_id": ws, "name": "投递报告", "sections": []any{}, "status": "draft", "created_at": now()})
	a.insert("report_schedules", M{"id": id(), "workspace_id": ws, "report_id": report["id"], "cron": "0 9 * * *", "channels": []any{M{"type": "webhook", "target": "https://example.invalid/hook"}}, "enabled": true, "created_at": now()})
	run := a.runReport(ws, a.owned("reports", str(report["id"]), ws), "manual")
	deliveries := arr(run["channels"])
	if len(deliveries) != 1 {
		t.Fatal("report must push to the scheduled channel", run)
	}
	d := obj(deliveries[0])
	if str(d["channel"]) != "webhook" || str(d["target"]) != "https://example.invalid/hook" {
		t.Fatal(d)
	}
	if !boolOf(d["ok"]) || !boolOf(d["simulated"]) {
		t.Fatal(d)
	}
}

func TestSuggestQualityRulesUseProfile(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	out := a.suggestQualityRules(ws, "demo.retail.dim_channel")
	rules := arr(out["rules"])
	if len(rules) == 0 {
		t.Fatal("no quality rule suggested", out)
	}
	kinds := map[string]bool{}
	for _, raw := range rules {
		r := obj(raw)
		kind := str(r["rule_type"])
		kinds[kind] = true
		if str(r["column_name"]) == "" || str(r["reason"]) == "" {
			t.Fatal("suggestion must name a column and a reason", r)
		}
		if len(obj(r["config"])) == 0 {
			t.Fatal("suggestion must carry a threshold", r)
		}
	}
	if !kinds["enum"] {
		t.Fatal("low cardinality column should yield an enum rule", rules)
	}
	if !kinds["unique"] {
		t.Fatal("key column should yield a unique rule", rules)
	}
	// A categorical column of a small table must not be reported as unique.
	for _, raw := range rules {
		r := obj(raw)
		if str(r["rule_type"]) == "unique" && str(r["column_name"]) == "channel_name" {
			t.Fatal("noise suggestion on a small categorical column", r)
		}
	}
	// The suggestion has to reach the frontend artifact contract.
	art := artifact(out, "质量规则建议")
	if str(art["type"]) != "quality_suggestions" || len(arr(art["items"])) != len(rules) {
		t.Fatal(art)
	}
	// A large table is sampled, so exact thresholds are not offered.
	big := a.suggestQualityRules(ws, "demo.retail.fact_orders")
	if boolOf(big["complete"]) {
		t.Fatal("a 5200 row table must be profiled as a sample", big)
	}
	for _, raw := range arr(big["rules"]) {
		r := obj(raw)
		if str(r["rule_type"]) == "enum" || str(r["rule_type"]) == "range" {
			t.Fatal("sampled table must not produce an exact threshold", r)
		}
	}
}
