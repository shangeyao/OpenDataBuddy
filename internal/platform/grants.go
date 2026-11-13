package platform

import (
	"regexp"
	"strconv"
	"strings"
)

// grants.go 实现表级 / 行级授权实体与策略求值（docs/OSS_DIFF.md §6.9 - P2-8）。
//
// 策略模型：工作空间内一旦存在启用的授权策略即进入白名单模式（未命中允许策略默认拒绝）；
// 显式 deny 永远优先于 allow；带 row_predicate 的策略只放行满足谓词的行，
// 没有可用的行数据时则把谓词交给调用方注入到查询里（行级过滤）。
// 完全没有配置策略时保持兼容：hasAnyGrant 为 false，一律放行且零额外开销。

// grantFields 返回允许写入 / 更新的字段白名单，避免 id、workspace_id 等被外部改写。
func grantFields() []string {
	return []string{"subject_type", "subject", "object_type", "object_ref", "actions", "row_predicate", "effect", "enabled"}
}

func grantOneOf(v string, set ...string) bool {
	for _, s := range set {
		if v == s {
			return true
		}
	}
	return false
}

// grantMerge 把请求体叠加到已有行上，只保留白名单字段（更新时基于已有行做合并校验）。
func grantMerge(base, body M) M {
	out := M{}
	for _, k := range grantFields() {
		if v, ok := base[k]; ok {
			out[k] = v
		}
	}
	for _, k := range grantFields() {
		if v, ok := body[k]; ok {
			out[k] = v
		}
	}
	return out
}

// grantPredicateRE 是行级谓词的唯一文法：column op value。
// 右侧只允许单引号字面量（内部单引号写成两个单引号转义）、普通数字，或 IN 用的括号字面量列表；
// 任何裸标识符、函数、子查询、注释都无法通过整串匹配。
func grantPredicateRE() *regexp.Regexp {
	lit := `'(?:''|[^'])*'`
	number := `-?[0-9]+(?:\.[0-9]+)?`
	scalar := `(?:` + lit + `|` + number + `)`
	list := `\(\s*` + scalar + `(?:\s*,\s*` + scalar + `)*\s*\)`
	value := `(?:` + scalar + `|` + list + `)`
	return regexp.MustCompile(`(?i)^([A-Za-z_][A-Za-z0-9_]*)\s*(!=|>=|<=|=|>|<|in|like)\s*(` + value + `)$`)
}

func grantScalarRE() *regexp.Regexp {
	lit := `'(?:''|[^'])*'`
	number := `-?[0-9]+(?:\.[0-9]+)?`
	return regexp.MustCompile(lit + `|` + number)
}

// grantPredicate 用严格白名单校验行级谓词，返回规范化文本；第二个返回值为错误原因。
// 逐项拦截分号、注释、双引号标识符、不平衡引号、控制字符、SQL 关键字与超长输入。
func grantPredicate(raw string) (string, string) {
	p := strings.TrimSpace(raw)
	if p == "" {
		return "", ""
	}
	if len(p) > 200 {
		return "", "长度不能超过 200 字符"
	}
	for _, r := range p {
		if r < 0x20 || r == 0x7f {
			return "", "不能包含控制字符"
		}
	}
	for _, bad := range []string{";", "--", "/*", "*/", `"`, "`"} {
		if strings.Contains(p, bad) {
			return "", "不能包含注释、分号或引号标识符（" + bad + "）"
		}
	}
	if strings.Count(p, "'")%2 != 0 {
		return "", "单引号不平衡"
	}
	// 关键字黑名单只看结构骨架：把字面量与数字替换掉，避免误伤 'select' 这类取值；
	// 大小写不敏感、按词边界匹配，杜绝拼装式注入（select/from/where/union/drop...）。
	skeleton := grantScalarRE().ReplaceAllString(p, "_")
	kw := regexp.MustCompile(`(?i)\b(?:select|insert|update|delete|drop|create|alter|truncate|union|where|from|having|join|distinct|limit|offset|exec|execute|declare|cast|substring|substr|concat|replace|benchmark|pg_sleep|information_schema|sqlite_master|pragma|attach|detach|load_extension|read_csv|read_parquet|httpfs|export|import|copy|call|install|returning|recursive|vacuum|checkpoint|grant|revoke|exists|between|glob|getenv|shell|query_table|outfile|dumpfile|and|or|not|null|is|case|when|then|else|end|as|into|values|sleep|waitfor|char|chr|ascii|version|current_user|session_user|rand|random|hex|unhex|length|lower|upper|trim|abs|round|floor|ceil|coalesce|ifnull|nullif|extract)\b`)
	if kw.MatchString(skeleton) {
		return "", "不能包含 SQL 关键字"
	}
	m := grantPredicateRE().FindStringSubmatch(p)
	if m == nil {
		return "", "只允许 column op value 形式，右侧必须是单引号字面量、数字或 IN 列表"
	}
	col, op, val := m[1], strings.ToUpper(m[2]), m[3]
	isList := strings.HasPrefix(val, "(")
	if op == "IN" && !isList {
		return "", "IN 右侧必须是括号包裹的字面量列表"
	}
	if op != "IN" && isList {
		return "", "只有 IN 允许括号列表"
	}
	return col + " " + op + " " + val, ""
}

// grantValidate 校验并规范化授权字段，创建与更新共用同一套规则。
func grantValidate(in M) M {
	st := strings.ToLower(strings.TrimSpace(str(in["subject_type"])))
	if st == "" {
		st = "user"
	}
	if !grantOneOf(st, "user", "role", "workspace") {
		fail(422, "授权主体类型不合法："+st)
	}
	subj := strings.TrimSpace(str(in["subject"]))
	if subj == "" {
		fail(422, "授权主体不能为空（workspace 主体请用 '*'）")
	}
	ot := strings.ToLower(strings.TrimSpace(str(in["object_type"])))
	if ot == "" {
		ot = "table"
	}
	if !grantOneOf(ot, "table", "schema", "catalog", "column") {
		fail(422, "授权对象类型不合法："+ot)
	}
	ref := strings.TrimSpace(str(in["object_ref"]))
	if ref == "" {
		fail(422, "授权对象不能为空（'*' 表示全部）")
	}
	if strings.ContainsAny(ref, ";'\"()") || strings.Contains(ref, "--") || strings.Contains(ref, "/*") || strings.Contains(ref, "*/") {
		fail(422, "授权对象引用不合法")
	}
	acts := arr(in["actions"])
	if len(acts) == 0 {
		fail(422, "授权动作不能为空，且必须是数组")
	}
	canon := []any{}
	seen := map[string]bool{}
	for _, x := range acts {
		act := strings.ToLower(strings.TrimSpace(str(x)))
		if !grantOneOf(act, "select", "insert", "update", "delete", "all") {
			fail(422, "不支持的授权动作："+act)
		}
		if !seen[act] {
			seen[act] = true
			canon = append(canon, act)
		}
	}
	effect := strings.ToLower(strings.TrimSpace(str(in["effect"])))
	if effect == "" {
		effect = "allow"
	}
	if !grantOneOf(effect, "allow", "deny") {
		fail(422, "策略效果仅支持 allow / deny")
	}
	pred := strings.TrimSpace(str(in["row_predicate"]))
	if pred != "" {
		norm, why := grantPredicate(pred)
		if why != "" {
			fail(422, "行级谓词不合法："+why)
		}
		pred = norm
	}
	enabled := true
	if v, ok := in["enabled"]; ok {
		enabled = boolOf(v)
	}
	return M{
		"subject_type":  st,
		"subject":       subj,
		"object_type":   ot,
		"object_ref":    ref,
		"actions":       canon,
		"row_predicate": pred,
		"effect":        effect,
		"enabled":       enabled,
	}
}

// grantText 生成一条人类可读的策略摘要。
func grantText(g M) string {
	effect := "允许"
	if str(g["effect"]) == "deny" {
		effect = "拒绝"
	}
	acts := []string{}
	for _, x := range arr(g["actions"]) {
		acts = append(acts, str(x))
	}
	s := effect + " [" + str(g["subject_type"]) + ":" + str(g["subject"]) + "] 对 [" +
		str(g["object_type"]) + ":" + str(g["object_ref"]) + "] 的 [" + strings.Join(acts, ",") + "] 操作"
	if str(g["row_predicate"]) != "" {
		s += "（行级：" + str(g["row_predicate"]) + "）"
	}
	if !boolOf(g["enabled"]) {
		s += "（已停用）"
	}
	return s
}

// grantView 把数据库行整理成 API 输出：解码 JSON 字段、规范化 actions / enabled 并补摘要。
func grantView(r M) M {
	if r == nil {
		return nil
	}
	decoded(r)
	r["actions"] = arr(r["actions"])
	r["enabled"] = boolOf(r["enabled"])
	r["summary"] = grantText(r)
	return r
}

// listGrants 列出工作空间的全部策略，按创建时间排序。
func (a *App) listGrants(ws string) []M {
	out := []M{}
	for _, r := range a.rows("SELECT * FROM grants WHERE workspace_id=? ORDER BY created_at, id", ws) {
		out = append(out, grantView(r))
	}
	return out
}

// createGrant 新建一条授权策略。
func (a *App) createGrant(ws string, body M) M {
	m := grantValidate(grantMerge(M{}, body))
	m["id"] = id()
	m["workspace_id"] = ws
	m["created_at"] = now()
	a.insert("grants", m)
	return grantView(a.owned("grants", str(m["id"]), ws))
}

// updateGrant 只允许改白名单字段，并对合并后的最终值重新跑完整校验。
func (a *App) updateGrant(ws, grantID string, body M) M {
	cur := a.owned("grants", grantID, ws)
	patch := grantValidate(grantMerge(cur, body))
	patch["updated_at"] = now()
	a.update("grants", grantID, patch)
	return grantView(a.owned("grants", grantID, ws))
}

// deleteGrant 校验归属后删除策略。
func (a *App) deleteGrant(ws, grantID string) M {
	a.owned("grants", grantID, ws)
	a.exec("DELETE FROM grants WHERE id=? AND workspace_id=?", grantID, ws)
	return M{"ok": true, "id": grantID}
}

// hasAnyGrant 供 a.query() 使用：没有配置任何策略时完全跳过鉴权，保持向后兼容。
func (a *App) hasAnyGrant(ws string) bool {
	return a.row("SELECT id FROM grants WHERE workspace_id=? LIMIT 1", ws) != nil
}

// grantRoles 收集用户在本工作空间内的全部角色：全局角色 + workspace_members 角色。
func (a *App) grantRoles(ws string, user M) map[string]bool {
	roles := map[string]bool{}
	if r := strings.ToLower(str(user["role"])); r != "" {
		roles[r] = true
	}
	for _, m := range a.rows("SELECT role FROM workspace_members WHERE workspace_id=? AND user_id=?", ws, str(user["id"])) {
		if r := strings.ToLower(str(m["role"])); r != "" {
			roles[r] = true
		}
	}
	return roles
}

// grantApplies 判断策略主体是否命中当前调用者。
func grantApplies(g M, ws string, user M, roles map[string]bool) bool {
	st := strings.ToLower(str(g["subject_type"]))
	if st == "" {
		st = "user"
	}
	subj := str(g["subject"])
	switch st {
	case "user":
		return strings.EqualFold(subj, str(user["id"]))
	case "role":
		return subj == "*" || roles[strings.ToLower(subj)]
	case "workspace":
		return subj == "*" || subj == ws
	}
	return false
}

// grantActionMatch 判断请求的动作是否落在策略动作里（all 通配）。
func grantActionMatch(g M, action string) bool {
	for _, x := range arr(g["actions"]) {
		act := strings.ToLower(strings.TrimSpace(str(x)))
		if act == "all" || act == action {
			return true
		}
	}
	return false
}

// grantCellText 把行数据里的单元格转成可比较的文本。
func grantCellText(v any) string { return str(v) }

// grantParseRHS 解析谓词右侧，解码单引号字面量并保留数字文本。
func grantParseRHS(val string) []string {
	out := []string{}
	for _, s := range grantScalarRE().FindAllString(val, -1) {
		if len(s) >= 2 && s[0] == '\'' {
			s = strings.ReplaceAll(s[1:len(s)-1], "''", "'")
		}
		out = append(out, s)
	}
	return out
}

// grantEqual 优先按数值比较，回退到精确字符串比较。
func grantEqual(a, b string) bool {
	if a == b {
		return true
	}
	fa, ea := strconv.ParseFloat(strings.TrimSpace(a), 64)
	fb, eb := strconv.ParseFloat(strings.TrimSpace(b), 64)
	return ea == nil && eb == nil && fa == fb
}

// grantCompare 支持 > < >= <=：数值可比时按数值，否则按字典序。
func grantCompare(a, b, op string) bool {
	cmp := 0
	fa, ea := strconv.ParseFloat(strings.TrimSpace(a), 64)
	fb, eb := strconv.ParseFloat(strings.TrimSpace(b), 64)
	if ea == nil && eb == nil {
		switch {
		case fa < fb:
			cmp = -1
		case fa > fb:
			cmp = 1
		}
	} else {
		cmp = strings.Compare(a, b)
	}
	switch op {
	case ">":
		return cmp > 0
	case "<":
		return cmp < 0
	case ">=":
		return cmp >= 0
	case "<=":
		return cmp <= 0
	}
	return false
}

// grantLike 实现简单的 % 通配符匹配（其余字符一律按字面量处理）。
func grantLike(s, pattern string) bool {
	parts := strings.Split(pattern, "%")
	for i := range parts {
		parts[i] = regexp.QuoteMeta(parts[i])
	}
	re, e := regexp.Compile("(?s)^" + strings.Join(parts, ".*") + "$")
	if e != nil {
		return false
	}
	return re.MatchString(s)
}

// grantEvalPredicate 把已校验的谓词套用到一条具体的数据行上。
func grantEvalPredicate(pred string, row M) bool {
	m := grantPredicateRE().FindStringSubmatch(strings.TrimSpace(pred))
	if m == nil {
		return false
	}
	col, op := m[1], strings.ToUpper(m[2])
	var cell any
	found := false
	for k, v := range row {
		if strings.EqualFold(k, col) {
			cell, found = v, true
			break
		}
	}
	if !found {
		return false
	}
	left := grantCellText(cell)
	vals := grantParseRHS(m[3])
	switch op {
	case "IN":
		for _, v := range vals {
			if grantEqual(left, v) {
				return true
			}
		}
		return false
	case "LIKE":
		return len(vals) == 1 && grantLike(left, vals[0])
	case "=":
		return len(vals) == 1 && grantEqual(left, vals[0])
	case "!=":
		return len(vals) == 1 && !grantEqual(left, vals[0])
	case ">", "<", ">=", "<=":
		return len(vals) == 1 && grantCompare(left, vals[0], op)
	}
	return false
}

// checkGrant 是策略求值入口：先过滤出适用于调用者的策略，再按「拒绝优先」定结果。
func (a *App) checkGrant(ws string, user M, body M) M {
	ref := strings.TrimSpace(str(def(body, "object_ref", "*")))
	if ref == "" {
		ref = "*"
	}
	action := strings.ToLower(strings.TrimSpace(str(def(body, "action", "select"))))
	if action == "" {
		action = "select"
	}
	rowRaw, hasRow := body["row"]
	hasRow = hasRow && rowRaw != nil
	rowMap := obj(rowRaw)

	grants := a.rows("SELECT * FROM grants WHERE workspace_id=? ORDER BY created_at, id", ws)
	roles := a.grantRoles(ws, user)

	// 第一步：按主体 / 对象 / 动作 / 启用状态筛出候选策略；带谓词且给了行数据时先行试算。
	views := []M{}
	enabledCount := 0
	for _, g := range grants {
		if boolOf(g["enabled"]) {
			enabledCount++
		} else {
			continue
		}
		if !grantApplies(g, ws, user, roles) {
			continue
		}
		if !grantMatch(str(g["object_ref"]), ref) {
			continue
		}
		if !grantActionMatch(g, action) {
			continue
		}
		pred := strings.TrimSpace(str(g["row_predicate"]))
		if pred != "" && hasRow && !grantEvalPredicate(pred, rowMap) {
			continue
		}
		views = append(views, grantView(g))
	}

	// 第二步：硬拒绝（无谓词或谓词命中该行）优先；没给行数据的谓词转成待注入的 SQL 过滤。
	hardDeny, allowFull := false, false
	filteredAllow, filteredDeny := false, false
	rowPredicates := []string{}
	seenPred := map[string]bool{}
	for _, v := range views {
		pred := strings.TrimSpace(str(v["row_predicate"]))
		effect := strings.ToLower(str(v["effect"]))
		if effect == "" {
			effect = "allow"
		}
		if pred != "" && !hasRow {
			frag := pred
			if effect == "deny" {
				frag = "NOT (" + pred + ")"
				filteredDeny = true
			} else {
				filteredAllow = true
			}
			if !seenPred[frag] {
				seenPred[frag] = true
				rowPredicates = append(rowPredicates, frag)
			}
			continue
		}
		if effect == "deny" {
			hardDeny = true
		} else {
			allowFull = true
		}
	}

	out := M{
		"allowed":             false,
		"action":              action,
		"object_ref":          ref,
		"matched":             views,
		"row_predicates":      []string{},
		"row_filter_required": false,
		"reason":              "",
		"user":                M{"id": str(user["id"]), "role": str(user["role"])},
	}
	allowed, reason := false, ""
	rowFilterRequired := filteredAllow || filteredDeny
	switch {
	case len(grants) == 0 || enabledCount == 0:
		// 没有任何生效策略：兼容模式，不改动既有安装的行为。
		allowed, reason = true, "未配置生效的授权策略，兼容模式下默认允许"
	case hardDeny:
		// 显式拒绝覆盖一切，清空行级过滤交由上层直接拒绝。
		reason, rowFilterRequired, rowPredicates = "命中显式拒绝策略，拒绝优先于允许", false, []string{}
	case allowFull:
		allowed, reason = true, "命中允许策略"
		if rowFilterRequired {
			reason = "命中允许策略，需同时应用行级过滤"
		}
	case rowFilterRequired:
		// 只有带谓词的策略：不授予完整访问，仅返回需要注入的谓词。
		reason = "仅允许满足行级谓词的数据，需注入行级过滤"
	default:
		reason = "未命中任何允许策略，默认拒绝"
	}
	out["allowed"] = allowed
	out["reason"] = reason
	out["row_filter_required"] = rowFilterRequired
	out["row_predicates"] = rowPredicates
	return out
}

// grantTableRefs 从原始 SQL 中最小化地抽取 from / join / describe 后的表引用。
// 词法分析失败（畸形 SQL）时安静地返回空集合，交给后续 guard() 报错，绝不在此 panic。
func grantTableRefs(q string) (refs []string) {
	defer func() {
		if recover() != nil {
			refs = nil
		}
	}()
	tt := sqlTokens(q)
	ctes := map[string]bool{}
	for i := 0; i+2 < len(tt); i++ {
		if tt[i].Kind == 'i' && strings.EqualFold(tt[i+1].Text, "as") && tt[i+2].Text == "(" {
			ctes[strings.ToLower(tokenName(tt[i]))] = true
		}
	}
	out := []string{}
	seen := map[string]bool{}
	add := func(ref string) {
		if ref == "" || strings.HasPrefix(strings.ToLower(ref), "_grant_src") {
			return
		}
		key := strings.ToLower(ref)
		if len(strings.Split(ref, ".")) == 1 && ctes[key] {
			return
		}
		if !seen[key] {
			seen[key] = true
			out = append(out, ref)
		}
	}
	inFrom, need := false, false
	for i := 0; i < len(tt); i++ {
		t := tt[i]
		if need {
			need = false
			if t.Text == "(" {
				// 子查询：继续线性扫描，由内部的 from 自己触发。
				continue
			}
			if t.Kind != 'i' {
				continue
			}
			parts := []string{tokenName(t)}
			j := i
			for j+2 < len(tt) && tt[j+1].Text == "." && tt[j+2].Kind == 'i' {
				parts = append(parts, tokenName(tt[j+2]))
				j += 2
			}
			add(strings.Join(parts, "."))
			i = j
			continue
		}
		switch strings.ToLower(t.Text) {
		case "from", "join":
			inFrom, need = true, true
		case ",":
			if inFrom {
				need = true
			}
		case "describe", "desc", "summarize":
			if i == 0 {
				need = true
			}
		case "where", "group", "order", "having", "limit", "union", "except", "intersect", "qualify", "window", "on", "set":
			inFrom, need = false, false
		}
	}
	return out
}

// enforceReadGrant 在 guard() 之前执行：逐表校验 select 权限，并把行级谓词包成子查询过滤。
// 改写后的 SQL 仍由后续 qualify() 重写内部表名；谓词只含白名单里的顶层列名，因此是安全的。
func (a *App) enforceReadGrant(ws string, user M, q string) string {
	if !a.hasAnyGrant(ws) {
		return q
	}
	base := strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(q), ";"))
	if base == "" {
		return q
	}
	refs := grantTableRefs(base)
	if len(refs) == 0 {
		return q
	}
	predicates := []string{}
	seen := map[string]bool{}
	for _, ref := range refs {
		res := a.checkGrant(ws, user, M{"object_ref": ref, "action": "select"})
		// 仅「非行级过滤」场景下的拒绝才直接 403；行级过滤由下面的改写接管。
		if !boolOf(res["allowed"]) && !boolOf(res["row_filter_required"]) {
			fail(403, "无权访问表："+ref)
		}
		// 需要行级过滤却拿不到谓词时保守拒绝，避免「声称要过滤却没过滤」的绕过。
		rowPreds := arr(res["row_predicates"])
		if boolOf(res["row_filter_required"]) && len(rowPreds) == 0 {
			fail(403, "无权访问表："+ref)
		}
		for _, x := range rowPreds {
			p := strings.TrimSpace(str(x))
			if p != "" && !seen[p] {
				seen[p] = true
				predicates = append(predicates, p)
			}
		}
	}
	if len(predicates) == 0 {
		return q
	}
	// 只有返回结果集的 SELECT / WITH 才能包成子查询；DESCRIBE / SUMMARIZE 等只做表级校验。
	fields := strings.Fields(strings.ToLower(base))
	if len(fields) == 0 || (fields[0] != "select" && fields[0] != "with") {
		return q
	}
	return "SELECT * FROM (" + base + ") AS _grant_src WHERE " + strings.Join(predicates, " AND ")
}

// grantSummary 汇总工作空间的授权策略数量，供运维总览使用。
func (a *App) grantSummary(ws string) M {
	total, allow, deny, rowLevel, enabled := 0, 0, 0, 0, 0
	for _, g := range a.rows("SELECT * FROM grants WHERE workspace_id=?", ws) {
		total++
		effect := strings.ToLower(str(g["effect"]))
		if effect == "" {
			effect = "allow"
		}
		if effect == "deny" {
			deny++
		} else {
			allow++
		}
		if strings.TrimSpace(str(g["row_predicate"])) != "" {
			rowLevel++
		}
		if boolOf(g["enabled"]) {
			enabled++
		}
	}
	return M{"total": total, "allow": allow, "deny": deny, "row_level": rowLevel, "enabled": enabled}
}
