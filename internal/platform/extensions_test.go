package platform

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gogf/gf/v2/net/ghttp"
)

// extensions_test.go covers the six remaining §6.9 items landed in this round:
// P2-3 table evolution, P2-5 predict auth/rate limit, P2-7 semantic search,
// P2-8 row/table grants, P2-12 workspace quota, and the shared landing path.

// newTestGRequest 构造一个最小的 ghttp.Request，供 predictAuth 读请求头。
func newTestGRequest() *ghttp.Request {
	raw := httptest.NewRequest("POST", "/api/serving/services/x/predict", strings.NewReader("{}"))
	raw.Header.Set("Content-Type", "application/json")
	return &ghttp.Request{Request: raw}
}

// ---------------------------------------------------------------- P2-3 表结构演进

func TestTableEvolutionAddsColumnsWithoutDropping(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	cat := a.row("SELECT * FROM catalogs LIMIT 1")
	sch := a.row("SELECT * FROM schemas WHERE catalog_id=? LIMIT 1", cat["id"])
	physicalSchema := quote(lakeNamespace(str(cat["physical_prefix"]), str(cat["name"]), str(sch["name"])))
	_, e := a.Lake.Exec("CREATE SCHEMA IF NOT EXISTS " + physicalSchema)
	must(e)
	physicalTable := physicalSchema + "." + quote("evo_demo")

	desired := []M{{"name": "id", "type": "BIGINT"}, {"name": "amount", "type": "DOUBLE"}}
	first := a.landRows(ws, physicalTable, desired, []M{{"id": 1, "amount": 10.5}, {"id": 2, "amount": 20.5}}, true)
	if num(first["inserted"]) != 2 || boolOf(first["evolved"]) {
		t.Fatal("first landing should insert 2 rows without evolving", first)
	}
	if got := a.existingColumns(physicalTable); len(got) != 2 {
		t.Fatal("initial columns", got)
	}

	// 源端新增一列：应 ALTER TABLE ADD COLUMN，且保留已有 2 行数据
	desired = append(desired, M{"name": "channel", "type": "VARCHAR"})
	second := a.landRows(ws, physicalTable, desired, []M{{"id": 3, "amount": 30.5, "channel": "app"}}, false)
	if !boolOf(second["evolved"]) {
		t.Fatal("expected an evolution", second)
	}
	added := arr(second["added"])
	if len(added) != 1 || str(obj(added[0])["name"]) != "channel" {
		t.Fatal("wrong added columns", second)
	}
	cols := a.existingColumns(physicalTable)
	if len(cols) != 3 || cols[2] != "channel" {
		t.Fatal("column not added", cols)
	}
	if n := num(rows(a.Lake, "SELECT count(*) AS n FROM "+physicalTable)[0]["n"]); n != 3 {
		t.Fatal("existing rows were lost", n)
	}
	// 新增列在旧行上为 NULL，新行上有值
	nulls := num(rows(a.Lake, "SELECT count(*) AS n FROM "+physicalTable+" WHERE channel IS NULL")[0]["n"])
	if nulls != 2 {
		t.Fatal("old rows should have NULL in the new column", nulls)
	}
	if n := num(rows(a.Lake, "SELECT count(*) AS n FROM "+physicalTable+" WHERE channel='app'")[0]["n"]); n != 1 {
		t.Fatal("new row lost its value", n)
	}
	if evolutions := a.listEvolutions(ws); len(evolutions) == 0 {
		t.Fatal("evolution was not recorded")
	}
}

func TestTableEvolutionRejectsUnsafeType(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	pt := quote("odb_test_schema") + "." + quote("evo_guard")
	_, e := a.Lake.Exec("CREATE SCHEMA IF NOT EXISTS " + quote("odb_test_schema"))
	must(e)
	_, e = a.Lake.Exec("CREATE OR REPLACE TABLE " + pt + " (id BIGINT)")
	must(e)
	catches(t, 422, func() {
		a.evolveSchema(ws, pt, []M{{"name": "id", "type": "BIGINT"}, {"name": "bad", "type": "VARCHAR); DROP TABLE x; --"}})
	})
	if got := a.existingColumns(pt); len(got) != 1 {
		t.Fatal("unsafe DDL must not change the table", got)
	}
}

func TestDriftRecordsEvolutionEvent(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	out := a.triggerEvolutionFromDrift(ws, "demo.retail.fact_orders", "字段数量由 3 变为 4")
	if !boolOf(out["recorded"]) || str(out["trigger"]) != "drift" || num(out["auto_columns_added"]) != 0 {
		t.Fatal(out)
	}
	found := false
	for _, e := range a.listEvolutions(ws) {
		if str(e["trigger"]) == "drift" {
			found = true
		}
	}
	if !found {
		t.Fatal("drift evolution was not archived")
	}
}

// ---------------------------------------------------------------- P2-5 推理鉴权与限流

func TestPredictTokenAuthAndRateLimit(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	admin := publicUser(a.row("SELECT * FROM users LIMIT 1"))

	created := a.createPredictToken(ws, M{"name": "验收令牌", "scopes": []any{"predict"}})
	plain := str(created["token"])
	if !strings.HasPrefix(plain, "odb_") || len(plain) < 40 {
		t.Fatal("token not generated", created)
	}
	if created["token_hash"] != nil && str(created["token_hash"]) == plain {
		t.Fatal("plaintext leaked into stored hash")
	}
	listing := a.listPredictTokens(ws)
	if len(listing) != 1 || listing[0]["token_hash"] != nil {
		t.Fatal("list must not leak the hash", listing)
	}
	if str(listing[0]["hint"]) == plain {
		t.Fatal("list must not leak plaintext")
	}

	// 默认关闭令牌鉴权：会话模式直接放行
	a.setPredictLimit(ws, "require_token", "0")
	r := &request{A: a, WS: ws, User: admin, R: newTestGRequest()}
	session := a.predictAuth(r, "svc-1")
	if str(session["mode"]) != "session" {
		t.Fatal(session)
	}

	// 打开令牌鉴权：无令牌 401
	a.setPredictLimit(ws, "require_token", "1")
	r = &request{A: a, WS: ws, User: admin, R: newTestGRequest()}
	catches(t, 401, func() { a.predictAuth(r, "svc-1") })

	// 错误令牌 401
	bad := newTestGRequest()
	bad.Request.Header.Set("X-Predict-Token", "odb_wrong_token")
	catches(t, 401, func() { a.predictAuth(&request{A: a, WS: ws, User: admin, R: bad}, "svc-1") })

	// 正确令牌：mode=token，且刷新 last_used_at
	good := newTestGRequest()
	good.Request.Header.Set("X-Predict-Token", plain)
	tokenAuth := a.predictAuth(&request{A: a, WS: ws, User: admin, R: good}, "svc-1")
	if str(tokenAuth["mode"]) != "token" {
		t.Fatal(tokenAuth)
	}
	digest := predictTokenDigest(plain)
	row := a.row("SELECT * FROM predict_tokens WHERE workspace_id=? AND token_hash=?", ws, digest)
	if row == nil {
		t.Fatal("token hash not persisted correctly")
	}
	if str(row["last_used_at"]) == "" {
		t.Fatal("last_used_at was not refreshed")
	}

	// 令牌桶：capacity=1 时第二次调用应被 429 拒绝
	a.setPredictLimit(ws, "capacity", "1")
	a.setPredictLimit(ws, "rate", "0.1")
	a.setPredictLimit(ws, "require_token", "0")
	if !a.allowToken(ws+":bucket-test", 1, 0.1) {
		t.Fatal("first token should be allowed")
	}
	if a.allowToken(ws+":bucket-test", 1, 0.1) {
		t.Fatal("second token should be rate limited")
	}
	if !a.allowToken(ws+":other-bucket", 1, 0.1) {
		t.Fatal("rate limit must be scoped per key")
	}
	if lim := a.predictLimits(ws); num(lim["capacity"]) != 1 {
		t.Fatal("limit config not applied", lim)
	}

	if out := a.deletePredictToken(ws, str(row["id"])); !boolOf(out["revoked"]) {
		t.Fatal(out)
	}
	if len(a.listPredictTokens(ws)) != 0 {
		t.Fatal("token not revoked")
	}
}

// ---------------------------------------------------------------- P2-7 语义检索

func TestSemanticIndexAndHybridRecall(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	a.insert("knowledge_docs", M{"id": id(), "workspace_id": ws, "title": "GMV 口径说明", "content": "GMV 是未税成交总额，按渠道和日期汇总", "source_type": "manual", "created_at": now()})
	a.insert("knowledge_docs", M{"id": id(), "workspace_id": ws, "title": "用户隐私规范", "content": "手机号与身份证号属于敏感个人信息，需要脱敏存储", "source_type": "manual", "created_at": now()})

	built := a.rebuildSemanticIndex(ws)
	// 种子数据里已有知识文档，因此总数 >= 2；关键是索引确实建起来了
	if num(built["docs"]) < 2 || num(built["terms"]) == 0 || num(built["dim"]) != 256 {
		t.Fatal(built)
	}
	// 中文近义/分词查询：应命中「GMV 口径说明」
	hits := a.semanticSearch(ws, "成交总额 口径", 8)
	if len(hits) == 0 {
		t.Fatal("semantic search returned nothing")
	}
	if str(hits[0]["engine"]) != "hybrid" {
		t.Fatal("wrong engine", hits[0])
	}
	if num(hits[0]["score"]) <= 0 {
		t.Fatal("score should be positive", hits[0])
	}
	if str(hits[0]["snippet"]) == "" {
		t.Fatal("snippet missing")
	}
	// 纯词面查询也能通过 hybrid 兜底召回
	if len(a.hybridKnowledge(ws, "手机号", 8)) == 0 {
		t.Fatal("hybrid fallback lost keyword recall")
	}
	if len(a.semanticSearch(ws, "", 8)) != 0 {
		t.Fatal("empty query should return nothing")
	}
	// 更新文档后索引增量刷新
	doc := a.row("SELECT id FROM knowledge_docs WHERE workspace_id=? AND title='GMV 口径说明'", ws)
	a.update("knowledge_docs", str(doc["id"]), M{"content": "GMV 口径已更新为含税成交总额，新增退款扣减规则"})
	a.indexKnowledgeDoc(ws, str(doc["id"]))
	if len(a.semanticSearch(ws, "退款扣减", 8)) == 0 {
		t.Fatal("incremental index did not pick up new content")
	}
}

// ---------------------------------------------------------------- P2-8 行级 / 表级权限

func TestGrantPolicyEvaluationAndEnforcement(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	user := M{"id": "u-member", "role": "member"}

	// 无策略：兼容模式，一律放行
	if a.hasAnyGrant(ws) {
		t.Fatal("fresh install must have no grants")
	}
	if got := a.enforceReadGrant(ws, user, "SELECT * FROM fact_orders"); got != "SELECT * FROM fact_orders" {
		t.Fatal("compat mode changed the query", got)
	}
	if !boolOf(a.checkGrant(ws, user, M{"object_ref": "demo.retail.fact_orders", "action": "select"})["allowed"]) {
		t.Fatal("compat mode should allow")
	}

	// 显式 deny 覆盖 allow
	a.createGrant(ws, M{"subject_type": "role", "subject": "member", "object_ref": "demo.retail.fact_orders", "actions": []any{"select"}, "effect": "allow"})
	a.createGrant(ws, M{"subject_type": "user", "subject": "u-member", "object_ref": "demo.retail.fact_orders", "actions": []any{"select"}, "effect": "deny"})
	res := a.checkGrant(ws, user, M{"object_ref": "demo.retail.fact_orders", "action": "select"})
	if boolOf(res["allowed"]) || !strings.Contains(str(res["reason"]), "拒绝") {
		t.Fatal("deny must win", res)
	}
	catches(t, 403, func() { a.enforceReadGrant(ws, user, "SELECT * FROM demo.retail.fact_orders") })

	// 行级谓词：无行数据时返回待注入谓词，并改写查询
	a.exec("DELETE FROM grants WHERE workspace_id=?", ws)
	gr := a.createGrant(ws, M{"subject_type": "user", "subject": "u-member", "object_ref": "demo.retail.fact_orders", "actions": []any{"select"}, "row_predicate": "channel_name = 'app'", "effect": "allow"})
	if str(gr["row_predicate"]) != "channel_name = 'app'" {
		t.Fatal(gr)
	}
	filtered := a.checkGrant(ws, user, M{"object_ref": "demo.retail.fact_orders", "action": "select"})
	if !boolOf(filtered["row_filter_required"]) || len(arr(filtered["row_predicates"])) != 1 {
		t.Fatal("row predicate not surfaced", filtered)
	}
	rewritten := a.enforceReadGrant(ws, user, "SELECT * FROM demo.retail.fact_orders")
	if !strings.Contains(rewritten, "_grant_src") || !strings.Contains(rewritten, "channel_name = 'app'") {
		t.Fatal("query not rewritten with row filter", rewritten)
	}
	// 带行数据的求值：命中/不命中谓词
	if !boolOf(a.checkGrant(ws, user, M{"object_ref": "demo.retail.fact_orders", "action": "select", "row": M{"channel_name": "app"}})["allowed"]) {
		t.Fatal("matching row should be allowed")
	}

	// 谓词注入白名单：分号 / 子查询 / 裸标识符一律拒绝
	for _, bad := range []string{"1=1; DROP TABLE x", "channel = (SELECT 1)", "channel = secret_col", "channel = 'a' OR 1=1"} {
		catches(t, 422, func() {
			a.createGrant(ws, M{"subject_type": "user", "subject": "u-member", "object_ref": "*", "actions": []any{"select"}, "row_predicate": bad})
		})
	}
	if s := a.grantSummary(ws); num(s["row_level"]) < 1 || num(s["total"]) < 1 {
		t.Fatal(s)
	}
}

// ---------------------------------------------------------------- P2-12 工作空间配额

func TestWorkspaceQuotaEnforcement(t *testing.T) {
	a := testApp(t)
	ws := str(a.row("SELECT id FROM workspaces LIMIT 1")["id"])
	if num(a.quotaMap(ws)["dashboards"]) != -1 {
		t.Fatal("default should be unlimited")
	}
	before := num(obj(a.quotaOverview(ws)["usage"])["dashboards"])
	set := a.setQuota(ws, "dashboards", M{"max_items": before})
	if num(set["max_items"]) != before {
		t.Fatal(set)
	}
	if !boolOf(a.quotaOverview(ws)["enforced"]) {
		t.Fatal("quota should be enforced")
	}
	catches(t, 409, func() { a.checkQuota(ws, "dashboards") })
	// 未限制的资源不受影响
	a.checkQuota(ws, "metrics")
	// 改回不限制
	a.setQuota(ws, "dashboards", M{"max_items": -1})
	a.checkQuota(ws, "dashboards")
	// 非法资源
	catches(t, 422, func() { a.setQuota(ws, "not_a_resource", M{"max_items": 1}) })
}

// ---------------------------------------------------------------- 契约与资产

func TestExtensionRouteInventory(t *testing.T) {
	a := &App{}
	want := []string{
		"POST /api/catalog/evolve", "GET /api/catalog/evolutions",
		"GET /api/serving/tokens", "POST /api/serving/tokens", "DELETE /api/serving/tokens/{token_id}", "GET /api/serving/limits",
		"POST /api/knowledge/index", "GET /api/knowledge/semantic",
		"GET /api/grants/meta", "GET /api/grants", "POST /api/grants", "POST /api/grants/check", "PATCH /api/grants/{grant_id}", "DELETE /api/grants/{grant_id}",
		"GET /api/quota", "PUT /api/quota/{resource}",
	}
	have := map[string]bool{}
	for _, r := range a.routes() {
		have[r.Method+" "+r.Path] = true
	}
	for _, w := range want {
		if !have[w] {
			t.Errorf("missing route %s", w)
		}
	}
	// openapi.json 必须覆盖所有新路由（TestContractMigrationInventory 也会校验）
	b, e := assets.ReadFile("assets/openapi.json")
	must(e)
	var doc struct {
		Paths map[string]map[string]json.RawMessage
	}
	must(json.Unmarshal(b, &doc))
	for _, w := range want {
		parts := strings.SplitN(w, " ", 2)
		if _, ok := doc.Paths[parts[1]][strings.ToLower(parts[0])]; !ok {
			t.Errorf("openapi missing %s", w)
		}
	}
	// migrations.json 必须包含全部新表
	bm, e := assets.ReadFile("assets/migrations.json")
	must(e)
	var mig map[string]map[string]string
	must(json.Unmarshal(bm, &mig))
	for _, tbl := range []string{"table_evolutions", "predict_tokens", "doc_embeddings", "doc_terms", "grants", "workspace_quota"} {
		if _, ok := mig[tbl]; !ok {
			t.Errorf("migrations missing table %s", tbl)
		}
	}
	// 前端规则目录补齐 freshness（§6.4 复核项）
	bm2, e := assets.ReadFile("assets/metadata.json")
	must(e)
	var meta M
	must(json.Unmarshal(bm2, &meta))
	foundFreshness := false
	for _, v := range arr(meta["rule_types"]) {
		if str(obj(v)["type"]) == "freshness" {
			foundFreshness = true
		}
	}
	if !foundFreshness {
		t.Error("metadata.json rule_types still missing freshness")
	}
}
