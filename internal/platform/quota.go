package platform

import "strings"

// quotaResources 是 P2-12 支持的六种配额资源，顺序即概览中的展示顺序。
var quotaResources = []string{"tables", "flows", "dashboards", "agents", "sources", "conversations"}

// quotaTableResource 是 SQLite 表名 → 配额资源名的映射。
// 仅下列表参与配额；catalogs / schemas 等其它表不计数，未命中即不限制。
var quotaTableResource = map[string]string{
	"tables":        "tables",
	"flows":         "flows",
	"dashboards":    "dashboards",
	"agents":        "agents",
	"sources":       "sources",
	"conversations": "conversations",
}

// quotaLabels 是各资源的中文展示名。
var quotaLabels = map[string]string{
	"tables":        "数据表",
	"flows":         "工作流",
	"dashboards":    "看板",
	"agents":        "智能体",
	"sources":       "数据源",
	"conversations": "会话",
}

// quotaResourcesList 返回资源名列表的副本，避免调用方修改包级切片。
func (a *App) quotaResourcesList() []string {
	out := make([]string, len(quotaResources))
	copy(out, quotaResources)
	return out
}

// quotaMap 读取工作空间六种资源的限额，未配置的资源默认 -1（不限制）。
func (a *App) quotaMap(ws string) M {
	m := M{}
	for _, r := range quotaResources {
		m[r] = -1
	}
	for _, row := range a.rows("SELECT resource,max_items FROM workspace_quota WHERE workspace_id=?", ws) {
		res := str(row["resource"])
		if _, ok := m[res]; ok {
			m[res] = int(num(row["max_items"]))
		}
	}
	return m
}

// countQuotaResource 统计单个资源的当前用量：统一用一条 COUNT 查询。
// tables 表没有 workspace_id，需通过 schema / catalog 关联到工作空间。
func (a *App) countQuotaResource(ws, resource string) int {
	q := "SELECT count(*) AS n FROM " + quote(resource) + " WHERE workspace_id=?"
	if resource == "tables" {
		q = "SELECT count(*) AS n FROM tables t JOIN schemas s ON s.id=t.schema_id JOIN catalogs ca ON ca.id=s.catalog_id WHERE ca.workspace_id=?"
	}
	r := a.row(q, ws)
	if r == nil {
		return 0
	}
	return int(num(r["n"]))
}

// quotaOverview 汇总限额、用量与展示信息，供 GET /api/quota 使用。
func (a *App) quotaOverview(ws string) M {
	limits := a.quotaMap(ws)
	usage := M{}
	resources := []M{}
	limited := 0
	for _, r := range quotaResources {
		max := int(num(limits[r]))
		used := a.countQuotaResource(ws, r)
		usage[r] = used
		// 负限额表示不限制：remaining 固定为 -1，且不计入已限制数。
		remaining := -1
		if max >= 0 {
			remaining = max - used
			limited++
		}
		resources = append(resources, M{
			"resource":  r,
			"label":     quotaLabels[r],
			"limit":     max,
			"used":      used,
			"remaining": remaining,
			"unlimited": max < 0,
		})
	}
	return M{
		"limits":        limits,
		"usage":         usage,
		"resources":     resources,
		"enforced":      limited > 0,
		"workspace_id":  ws,
		"total_limited": limited,
	}
}

// setQuota 设置单个资源的限额；-1 表示不限制，超限不改动已有数据只给出告警。
func (a *App) setQuota(ws, resource string, body M) M {
	// 校验资源名合法性
	valid := false
	for _, r := range quotaResources {
		if r == resource {
			valid = true
			break
		}
	}
	if !valid {
		fail(422, "不支持的配额资源："+resource)
	}
	// 读取限额：优先 max_items，其次 limit / max
	raw := body["max_items"]
	if raw == nil {
		raw = body["limit"]
	}
	if raw == nil {
		raw = body["max"]
	}
	// -1 / null / "unlimited" / "无限制" 视为不限制，必须在 clampInt 之前处理：
	// clampInt 对越界值会返回 fallback(-1)，不能直接喂给它 -1 之外的语义值。
	n := -1
	if raw != nil {
		s := strings.TrimSpace(str(raw))
		switch {
		case s == "" || strings.EqualFold(s, "unlimited") || s == "无限制":
			n = -1
		case int(num(raw)) == -1:
			n = -1
		default:
			n = clampInt(raw, 0, 100000, -1)
		}
	}
	// 复合主键 (workspace_id, resource) 用 UPSERT 写入，全部参数化
	a.exec("INSERT INTO workspace_quota(workspace_id,resource,max_items,updated_at) VALUES (?,?,?,?) ON CONFLICT(workspace_id,resource) DO UPDATE SET max_items=excluded.max_items, updated_at=excluded.updated_at", ws, resource, n, now())
	// 合并概览键到返回顶层
	out := M{"ok": true, "resource": resource, "max_items": n}
	overview := a.quotaOverview(ws)
	for k, v := range overview {
		out[k] = v
	}
	// 新限额低于当前用量时仅告警，不拒绝、不回滚
	if n >= 0 {
		used := int(num(obj(overview["usage"])[resource]))
		if used > n {
			out["warning"] = "当前用量 " + str(used) + " 已超过新限额 " + str(n) + "，仅限制后续新建"
		}
	}
	return out
}

// checkQuota 在资源创建前校验配额；无表映射或未设置限额时直接放行。
func (a *App) checkQuota(ws, table string) {
	res, ok := quotaTableResource[table]
	if !ok {
		return
	}
	limit := int(num(a.quotaMap(ws)[res]))
	if limit < 0 {
		return
	}
	// 与概览共用同一套计数逻辑，单条 COUNT 查询，足够轻量
	if a.countQuotaResource(ws, res) >= limit {
		fail(409, "工作空间配额已达上限：资源 "+quotaLabels[res]+" 限额 "+str(limit)+" 项")
	}
}
