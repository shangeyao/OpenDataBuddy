package platform

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"strings"
)

// serveguard.go 实现 P2-5：在线推理 /predict 的工作空间访问令牌鉴权与
// 令牌桶限流。明文令牌只在创建响应里出现一次，库中仅保存 SHA-256 摘要。

// predictTokenDigest 计算令牌的 SHA-256 十六进制摘要，创建与校验共用。
func predictTokenDigest(plain string) string {
	sum := sha256.Sum256([]byte(plain))
	return hex.EncodeToString(sum[:])
}

// createPredictToken 生成一个随机推理令牌并落库。明文只随本次响应返回，
// 后续任何接口都拿不到它；库中只存摘要与掩码。
func (a *App) createPredictToken(ws string, body M) M {
	// 关键步骤 1：校验名称
	name := strings.TrimSpace(str(body["name"]))
	if name == "" {
		fail(422, "令牌名称不能为空")
	}

	// 关键步骤 2：32 字节随机数 → hex → 加可读前缀
	b := make([]byte, 32)
	if _, e := rand.Read(b); e != nil {
		fail(500, "生成令牌失败")
	}
	plain := "odb_" + hex.EncodeToString(b)
	digest := predictTokenDigest(plain)
	masked := plain[:8] + "..." + plain[len(plain)-4:]

	// 关键步骤 3：service_id 为空表示该工作空间下全部服务
	serviceID := str(body["service_id"])

	// 关键步骤 4：scopes 默认 ["predict"]，只允许 predict / predict:read
	scopes := []any{"predict"}
	if raw, ok := body["scopes"]; ok && raw != nil && strings.TrimSpace(str(raw)) != "" {
		items := arr(raw)
		if len(items) == 0 {
			fail(422, "scopes 必须为非空数组")
		}
		scopes = []any{}
		for _, item := range items {
			s, ok := item.(string)
			if !ok {
				fail(422, "scopes 必须为字符串数组")
			}
			if s != "predict" && s != "predict:read" {
				fail(422, "不支持的 scope："+s)
			}
			scopes = append(scopes, s)
		}
	}

	// 关键步骤 5：只持久化摘要，明文绝不入库
	row := M{
		"id":           id(),
		"workspace_id": ws,
		"name":         name,
		"token_hash":   digest,
		"masked":       masked,
		"service_id":   serviceID,
		"scopes":       scopes,
		"enabled":      true,
		"created_by":   str(body["created_by"]),
		"created_at":   now(),
		"last_used_at": "",
	}
	a.insert("predict_tokens", row)

	// 关键步骤 6：明文仅此一次返回
	out := M{}
	for k, v := range row {
		out[k] = v
	}
	out["token"] = plain
	out["warning"] = "请立即保存，明文令牌仅显示一次"
	return out
}

// listPredictTokens 列出工作空间的令牌，隐藏摘要、解码 scopes、布尔化 enabled。
func (a *App) listPredictTokens(ws string) []M {
	rr := a.rows("SELECT * FROM predict_tokens WHERE workspace_id=? ORDER BY created_at DESC", ws)
	out := []M{}
	for _, r := range rr {
		// 摘要绝不外泄，明文本就不在库里
		delete(r, "token_hash")
		r["scopes"] = arr(r["scopes"])
		r["enabled"] = boolOf(r["enabled"])
		r["hint"] = str(r["masked"])
		out = append(out, r)
	}
	return out
}

// deletePredictToken 校验归属后吊销令牌。
func (a *App) deletePredictToken(ws, tokenID string) M {
	// 关键步骤：先确认令牌属于当前工作空间
	a.owned("predict_tokens", tokenID, ws)
	a.exec("DELETE FROM predict_tokens WHERE id=? AND workspace_id=?", tokenID, ws)
	return M{"ok": true, "id": tokenID, "revoked": true}
}

// predictLimits 读取工作空间的容量 / 速率 / 是否强制令牌鉴权等配置。
func (a *App) predictLimits(ws string) M {
	// 容量：整数，1..10000，缺省 20
	capacity := clampInt(a.setting("predict."+ws+".capacity"), 1, 10000, 20)

	// 速率：支持 0.1 精度的小数。放大 10 倍后用 clampInt 校验
	// （0.1~1000 对应 1~10000，缺省 5 → 50），再还原成每秒令牌数。
	rateSetting := a.setting("predict." + ws + ".rate")
	rate := float64(clampInt(math.Round(num(rateSetting)*10), 1, 10000, 50)) / 10

	// 令牌鉴权默认关闭，保持向后兼容（会话鉴权已在 a.route 完成）
	rt := strings.ToLower(strings.TrimSpace(a.setting("predict." + ws + ".require_token")))
	requireToken := rt == "1" || rt == "true"

	// 已启用的令牌数量
	tokensConfigured := 0
	if r := a.row("SELECT count(*) AS n FROM predict_tokens WHERE workspace_id=? AND enabled=1", ws); r != nil {
		tokensConfigured = int(num(r["n"]))
	}

	// 桶数量必须加锁读取，避免与 allowToken 并发写 map 造成数据竞争
	a.predictLimiterMu.Lock()
	bucketsActive := len(a.predictBuckets)
	a.predictLimiterMu.Unlock()

	return M{
		"capacity":          capacity,
		"rate":              rate,
		"require_token":     requireToken,
		"window":            "token_bucket",
		"scope":             "workspace+service",
		"tokens_configured": tokensConfigured,
		"buckets_active":    bucketsActive,
	}
}

// setPredictLimit 供管理员写入 predict.<ws>.<key> 配置（capacity / rate /
// require_token），后续由 predictLimits 读取。
func (a *App) setPredictLimit(ws, key string, v any) {
	a.setSetting("predict."+ws+"."+key, v)
}

// predictAuth 是鉴权核心：默认会话鉴权；开启后按令牌哈希、服务绑定、
// scopes 逐项校验，并刷新最后使用时间。
func (a *App) predictAuth(c *request, serviceID string) M {
	ws := c.WS

	// 步骤 a：未开启强制令牌鉴权时沿用会话鉴权
	rt := strings.ToLower(strings.TrimSpace(a.setting("predict." + ws + ".require_token")))
	if rt != "1" && rt != "true" {
		return M{"authenticated": true, "mode": "session", "reason": "令牌鉴权未启用，使用会话鉴权"}
	}

	// 步骤 b：优先 X-Predict-Token，回退 Authorization: Bearer
	token := strings.TrimSpace(c.R.Header.Get("X-Predict-Token"))
	if token == "" {
		token = strings.TrimSpace(bearer(c.R))
	}
	if token == "" {
		fail(401, "缺少推理访问令牌（X-Predict-Token）")
	}

	// 步骤 c：按摘要查找启用中的令牌
	digest := predictTokenDigest(token)
	r := a.row("SELECT * FROM predict_tokens WHERE workspace_id=? AND token_hash=? AND enabled=1", ws, digest)
	if r == nil {
		fail(401, "推理令牌无效或已吊销")
	}

	// 步骤 d：服务绑定校验（空表示允许该工作空间全部服务）
	allowedService := str(r["service_id"])
	if allowedService != "" && allowedService != serviceID {
		fail(403, "令牌无权访问该模型服务")
	}

	// 步骤 e：scopes 必须含 predict
	hasPredict := false
	for _, s := range arr(r["scopes"]) {
		if str(s) == "predict" {
			hasPredict = true
			break
		}
	}
	if !hasPredict {
		fail(403, "令牌缺少 predict 权限")
	}

	// 步骤 f：刷新最后使用时间
	a.update("predict_tokens", str(r["id"]), M{"last_used_at": now()})

	// 步骤 g：返回令牌身份信息
	return M{
		"authenticated": true,
		"mode":          "token",
		"token_id":      r["id"],
		"token_name":    r["name"],
		"service_id":    allowedService,
	}
}

// predictGuarded 是 /predict 的新入口：先鉴权、再限流、最后调用原有 predict，
// 并把鉴权与限流信息合并进结果，不破坏原有字段。
func (a *App) predictGuarded(c *request, serviceID string, body M) M {
	// 步骤 a：令牌 / 会话鉴权
	auth := a.predictAuth(c, serviceID)

	// 步骤 b：按 工作空间:服务 分桶的令牌桶限流
	lim := a.predictLimits(c.WS)
	key := c.WS + ":" + serviceID
	if !a.allowToken(key, float64(num(lim["capacity"])), num(lim["rate"])) {
		fail(429, "推理请求过于频繁，请稍后重试")
	}

	// 步骤 c：复用既有推理逻辑，不重复实现 predict
	out := a.predict(c.WS, serviceID, body)
	if out == nil {
		return auth
	}

	// 步骤 d：合并鉴权与限流信息，保留原有键
	out["auth"] = M{"mode": auth["mode"], "authenticated": true}
	out["rate_limit"] = M{"capacity": lim["capacity"], "rate": lim["rate"]}
	return out
}
