package platform

import (
	"strings"
	"time"
)

// clampInt constrains a numeric config value to a sane inclusive range.
func clampInt(v any, lo, hi int, fallback int) int {
	n := int(num(v))
	if n < lo || n > hi {
		return fallback
	}
	return n
}

// allowToken consumes one token from a per-key token bucket. capacity is the
// burst size and rate is the sustained refill in tokens per second. A disabled
// limiter (capacity <= 0) always allows the call.
func (a *App) allowToken(key string, capacity, rate float64) bool {
	if capacity <= 0 {
		return true
	}
	if rate <= 0 {
		rate = 1
	}
	a.predictLimiterMu.Lock()
	if a.predictBuckets == nil {
		a.predictBuckets = map[string]*tokenBucket{}
	}
	b := a.predictBuckets[key]
	if b == nil {
		b = &tokenBucket{tokens: capacity, capacity: capacity, rate: rate, last: time.Now()}
		a.predictBuckets[key] = b
	}
	a.predictLimiterMu.Unlock()

	b.mu.Lock()
	defer b.mu.Unlock()
	now := time.Now()
	if elapsed := now.Sub(b.last).Seconds(); elapsed > 0 {
		b.tokens += elapsed * b.rate
		if b.tokens > b.capacity {
			b.tokens = b.capacity
		}
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// wildcardGrantMatch matches a grant object/target against a concrete ref.
// "*" grants apply to everything; "catalog.*" style prefixes are supported by
// matching the leading segment.
func grantMatch(grant, ref string) bool {
	g := strings.ToLower(strings.TrimSpace(grant))
	r := strings.ToLower(strings.TrimSpace(ref))
	if g == "" || g == "*" {
		return true
	}
	if g == r {
		return true
	}
	if strings.HasSuffix(g, ".*") && strings.HasPrefix(r, strings.TrimSuffix(g, "*")) {
		return true
	}
	return false
}

// foundation.go defines the shared API contract for the six remaining items in
// docs/OSS_DIFF.md §6.9. Keeping every route in one place gives each feature
// module a fixed surface to implement and keeps the route inventory test
// (TestContractMigrationInventory) honest.
//
// The handlers delegate to methods implemented by the feature modules:
//
//	evolution.go   - P2-3  表结构演进
//	serveguard.go  - P2-5  推理服务鉴权与限流
//	semantic.go    - P2-7  语义检索
//	grants.go      - P2-8  行级 / 表级权限
//	quota.go       - P2-12 工作空间资源配额
//
// P2-6 (看板自由布局) is frontend-only and lives in web/js/pages-gov.js.

func (a *App) extensionRoutes() []route {
	return []route{
		// ---- P2-3 表结构演进：DESCRIBE diff + ALTER TABLE ADD COLUMN ----
		{"POST", "/api/catalog/evolve", false, false, func(c *request) any {
			return a.evolveTable(c.WS, c.Body)
		}},
		{"GET", "/api/catalog/evolutions", false, false, func(c *request) any {
			return M{"evolutions": a.listEvolutions(c.WS)}
		}},

		// ---- P2-5 在线推理鉴权与限流 ----
		{"GET", "/api/serving/tokens", false, false, func(c *request) any {
			return M{"tokens": a.listPredictTokens(c.WS)}
		}},
		{"POST", "/api/serving/tokens", false, false, func(c *request) any {
			return M{"token": a.createPredictToken(c.WS, c.Body)}
		}},
		{"DELETE", "/api/serving/tokens/{token_id}", false, false, func(c *request) any {
			return a.deletePredictToken(c.WS, c.p("token_id"))
		}},
		{"GET", "/api/serving/limits", false, false, func(c *request) any {
			return a.predictLimits(c.WS)
		}},

		// ---- P2-7 语义检索（本地哈希 embedding + 倒排索引 + 混合召回）----
		{"POST", "/api/knowledge/index", false, false, func(c *request) any {
			return a.rebuildSemanticIndex(c.WS)
		}},
		{"GET", "/api/knowledge/semantic", false, false, func(c *request) any {
			return M{"mode": "hybrid", "results": a.semanticSearch(c.WS, c.q("q"), int(num(def(M{"limit": c.q("limit")}, "limit", 8))))}
		}},

		// ---- P2-8 行级 / 表级权限 ----
		{"GET", "/api/grants/meta", false, false, func(c *request) any {
			return M{"actions": metadata["grant_actions"], "object_types": metadata["grant_object_types"], "subject_types": metadata["grant_subject_types"]}
		}},
		{"GET", "/api/grants", false, false, func(c *request) any {
			return M{"grants": a.listGrants(c.WS)}
		}},
		{"POST", "/api/grants", false, false, func(c *request) any {
			return M{"grant": a.createGrant(c.WS, c.Body)}
		}},
		{"POST", "/api/grants/check", false, false, func(c *request) any {
			return a.checkGrant(c.WS, c.User, c.Body)
		}},
		{"PATCH", "/api/grants/{grant_id}", false, false, func(c *request) any {
			return M{"grant": a.updateGrant(c.WS, c.p("grant_id"), c.Body)}
		}},
		{"DELETE", "/api/grants/{grant_id}", false, false, func(c *request) any {
			return a.deleteGrant(c.WS, c.p("grant_id"))
		}},

		// ---- P2-12 工作空间资源配额 ----
		{"GET", "/api/quota", false, false, func(c *request) any {
			return a.quotaOverview(c.WS)
		}},
		{"PUT", "/api/quota/{resource}", false, false, func(c *request) any {
			c.admin()
			return a.setQuota(c.WS, c.p("resource"), c.Body)
		}},
	}
}
