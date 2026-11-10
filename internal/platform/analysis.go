package platform

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
)

func nativeTools() any { return metadata["tool_catalog"] }
func periodWindow(period string) M {
	loc, e := time.LoadLocation(env("DATABUDDY_TIMEZONE", "Asia/Shanghai"))
	if e != nil {
		fail(422, "业务时区无效")
	}
	at := time.Now().In(loc)
	today := time.Date(at.Year(), at.Month(), at.Day(), 0, 0, 0, 0, loc)
	start, end := today, today
	key := period
	label := "上月"
	switch key {
	case "today":
		label = "今天"
	case "yesterday":
		start = today.AddDate(0, 0, -1)
		end = start
		label = "昨天"
	case "last_7d", "last_7_days", "last7":
		key = "last_7d"
		start = today.AddDate(0, 0, -6)
		label = "近7天"
	case "last_30d", "last_30_days":
		key = "last_30d"
		start = today.AddDate(0, 0, -29)
		label = "近30天"
	case "last_90d", "last_90_days":
		key = "last_90d"
		start = today.AddDate(0, 0, -89)
		label = "近90天"
	case "this_month":
		start = time.Date(today.Year(), today.Month(), 1, 0, 0, 0, 0, loc)
		label = "本月"
	case "this_year":
		start = time.Date(today.Year(), 1, 1, 0, 0, 0, 0, loc)
		label = "本年"
	default:
		key = "last_month"
		first := time.Date(today.Year(), today.Month(), 1, 0, 0, 0, 0, loc)
		start = first.AddDate(0, -1, 0)
		end = first.AddDate(0, 0, -1)
	}
	span := int(end.Sub(start).Hours()/24) + 1
	prevStart, prevEnd := start.AddDate(0, 0, -span), start.AddDate(0, 0, -1)
	if key == "last_month" || key == "this_month" {
		prevStart = start.AddDate(0, -1, 0)
		prevEnd = start.AddDate(0, 0, -1)
	}
	if key == "this_year" {
		prevStart = start.AddDate(-1, 0, 0)
		prevEnd = end.AddDate(-1, 0, 0)
	}
	return M{"key": key, "label": label, "start": start.Format("2006-01-02"), "end": end.Format("2006-01-02"), "prev_start": prevStart.Format("2006-01-02"), "prev_end": prevEnd.Format("2006-01-02")}
}
func (a *App) metric(ws, name string) M {
	for _, m := range a.list("metrics", ws) {
		if strings.EqualFold(str(m["name"]), name) || strings.EqualFold(str(m["display_name"]), name) {
			return decoded(m)
		}
	}
	fail(404, "指标不存在："+name)
	return nil
}
func (a *App) metricSQL(ws string, m M, args M) string {
	t := a.resolve(ws, str(m["table_ref"]))
	joins := arr(m["joins"])
	from := physical(t) + " f"
	dimExpr := map[string]string{}
	for _, c := range a.rows("SELECT name FROM columns WHERE table_id=?", t["id"]) {
		dimExpr[str(c["name"])] = "f." + quote(str(c["name"]))
	}
	for _, v := range joins {
		j := obj(v)
		alias := validName(str(j["alias"]))
		on := str(j["on"])
		guard("SELECT 1 WHERE " + on)
		from += " LEFT JOIN " + physical(a.resolve(ws, str(j["table"]))) + " " + alias + " ON " + on
		for _, v := range arr(j["columns"]) {
			dimExpr[str(v)] = alias + "." + quote(str(v))
		}
	}
	allowed := map[string]bool{}
	for _, d := range arr(m["dimensions"]) {
		allowed[str(d)] = true
	}
	dims := []string{}
	for _, d := range arr(args["dimensions"]) {
		name := str(d)
		if name == "__date__" {
			name = "order_date"
		}
		if !allowed[name] || dimExpr[name] == "" {
			fail(422, "指标不支持维度："+name)
		}
		expr := dimExpr[name]
		if name == "order_date" && args["granularity"] != nil {
			grain := str(args["granularity"])
			if grain != "day" && grain != "week" && grain != "month" && grain != "year" {
				fail(422, "无效时间粒度")
			}
			expr = "date_trunc('" + grain + "'," + expr + ")"
		}
		dims = append(dims, expr+" AS "+quote(name))
	}
	expr := str(m["expr"])
	guard("SELECT " + expr)
	selects := append(append([]string{}, dims...), expr+" AS "+quote(str(m["name"])))
	q := "SELECT " + strings.Join(selects, ",") + " FROM " + from
	where := []string{}
	if str(m["filters"]) != "" {
		guard("SELECT 1 WHERE " + str(m["filters"]))
		where = append(where, str(m["filters"]))
	}
	for _, k := range []string{"start", "end"} {
		if v := str(args[k]); v != "" {
			if _, e := time.Parse("2006-01-02", v); e != nil {
				fail(422, "日期格式应为 YYYY-MM-DD")
			}
			if dimExpr["order_date"] != "" {
				op := ">="
				if k == "end" {
					op = "<="
				}
				where = append(where, dimExpr["order_date"]+op+"DATE '"+v+"'")
			}
		}
	}
	if len(where) > 0 {
		q += " WHERE " + strings.Join(where, " AND ")
	}
	if len(dims) > 0 {
		positions := []string{}
		for i := range dims {
			positions = append(positions, fmt.Sprint(i+1))
		}
		q += " GROUP BY " + strings.Join(positions, ",") + " ORDER BY 1"
	}
	return q
}
func (a *App) queryMetric(ws string, args M) M {
	m := a.metric(ws, str(args["metric"]))
	out := a.query(ws, a.metricSQL(ws, m, args), 500)
	out["metric"] = m
	out["chart"] = chart(out)
	return out
}
func (a *App) ask(ws, question, period string) M {
	if question == "" {
		fail(422, "请输入问题")
	}
	picked := ""
	for _, m := range a.list("metrics", ws) {
		if strings.Contains(strings.ToLower(question), strings.ToLower(str(m["name"]))) || strings.Contains(question, str(m["display_name"])) {
			picked = str(m["name"])
			break
		}
	}
	if picked == "" {
		for _, pair := range [][2]string{{"销售", "gmv"}, {"金额", "gmv"}, {"成交额", "gmv"}, {"订单", "order_cnt"}, {"客户", "customer_cnt"}, {"客单价", "avg_order_value"}} {
			if strings.Contains(question, pair[0]) {
				picked = pair[1]
				break
			}
		}
	}
	if picked == "" {
		fail(422, "未匹配注册指标，请在问题中指定指标名")
	}
	dims := []string{}
	for _, p := range [][2]string{{"渠道", "channel_name"}, {"品类", "category"}, {"城市", "city"}, {"趋势", "__date__"}, {"每日", "__date__"}} {
		if strings.Contains(question, p[0]) {
			dims = append(dims, p[1])
			break
		}
	}
	if period == "" {
		for _, p := range [][2]string{{"近7", "last_7d"}, {"近 7", "last_7d"}, {"近30", "last_30d"}, {"近 30", "last_30d"}, {"本月", "this_month"}, {"今天", "today"}, {"昨天", "yesterday"}} {
			if strings.Contains(question, p[0]) {
				period = p[1]
				break
			}
		}
	}
	win := periodWindow(period)
	args := M{"metric": picked, "dimensions": dims, "start": win["start"], "end": win["end"]}
	out := a.queryMetric(ws, args)
	out["engine"] = "metric"
	out["question"] = question
	out["period"] = win
	out["answer"] = fmt.Sprintf("按注册口径 %s 查询，共返回 %v 行；时间范围为 %s 至 %s。", picked, out["row_count"], win["start"], win["end"])
	out["reasoning"] = "匹配注册指标与业务维度，通过确定性语义层编译 SQL 并执行只读校验。"
	out["chart_data"] = M{"spec": out["chart"], "columns": out["columns"], "rows": out["rows"]}
	return out
}
func (a *App) attribute(ws string, m M) M {
	metric := a.metric(ws, str(m["metric"]))
	dim := str(m["dimension"])
	if dim == "" {
		for _, v := range arr(metric["dimensions"]) {
			if v != "order_date" {
				dim = str(v)
				break
			}
		}
	}
	win := periodWindow(str(def(m, "period", "last_month")))
	cur := a.queryMetric(ws, M{"metric": metric["name"], "dimensions": []string{dim}, "start": win["start"], "end": win["end"]})
	prev := a.queryMetric(ws, M{"metric": metric["name"], "dimensions": []string{dim}, "start": win["prev_start"], "end": win["prev_end"]})
	current, previous := map[string]float64{}, map[string]float64{}
	keys := map[string]bool{}
	// Overall ratios and distinct counts cannot be summed across dimension groups.
	currentTotal := a.queryMetric(ws, M{"metric": metric["name"], "start": win["start"], "end": win["end"]})
	previousTotal := a.queryMetric(ws, M{"metric": metric["name"], "start": win["prev_start"], "end": win["prev_end"]})
	totalCur := num(currentTotal["rows"].([][]any)[0][0])
	totalPrev := num(previousTotal["rows"].([][]any)[0][0])
	for _, r := range cur["rows"].([][]any) {
		current[str(r[0])] = num(r[1])
		keys[str(r[0])] = true
	}
	for _, r := range prev["rows"].([][]any) {
		previous[str(r[0])] = num(r[1])
		keys[str(r[0])] = true
	}
	delta := totalCur - totalPrev
	cc := []M{}
	for k := range keys {
		diff := current[k] - previous[k]
		share := float64(0)
		if delta != 0 {
			share = diff / delta
		}
		var growth any
		if previous[k] != 0 {
			growth = diff / previous[k]
		}
		cc = append(cc, M{"value": k, "dimension_value": k, "dim_value": k, "label": k, "current": current[k], "previous": previous[k], "delta": diff, "share": share, "growth": growth, "contribution": share, "contribution_rate": share})
	}
	sort.Slice(cc, func(i, j int) bool { return math.Abs(num(cc[i]["delta"])) > math.Abs(num(cc[j]["delta"])) })
	for i, contribution := range cc {
		contribution["rank"] = i + 1
	}
	driver := ""
	if len(cc) > 0 {
		driver = str(cc[0]["value"])
	}
	rr := [][]any{}
	for _, c := range cc {
		rr = append(rr, []any{c["value"], c["current"], c["previous"], c["delta"], c["contribution"]})
	}
	cols := []string{dim, "本期", "上期", "差值", "贡献度"}
	var growth any
	if totalPrev != 0 {
		growth = delta / totalPrev
	}
	return M{"metric": metric, "dimension": dim, "dimension_label": dim, "period": win, "total_current": totalCur, "total_previous": totalPrev, "total_delta": delta, "growth": growth, "driver": driver, "contributions": cc, "all_contributions": cc, "conclusion": fmt.Sprintf("本期 %g，上期 %g，差值 %g；主要贡献维度为 %s。", totalCur, totalPrev, delta, driver), "table": M{"columns": cols, "rows": rr}, "chart_data": M{"columns": cols, "rows": rr, "spec": M{"type": "bar", "x": 0, "y": 3}}}
}
func (a *App) report(ws string, m M) M {
	names := arr(m["metrics"])
	if len(names) == 0 {
		for _, r := range a.list("metrics", ws) {
			names = append(names, r["name"])
			if len(names) == 3 {
				break
			}
		}
	}
	win := periodWindow(str(def(m, "period", "last_month")))
	table := [][]any{}
	picked := []M{}
	for _, n := range names {
		metric := a.metric(ws, str(n))
		r := a.queryMetric(ws, M{"metric": n, "start": win["start"], "end": win["end"]})
		value := any(nil)
		if rr := r["rows"].([][]any); len(rr) > 0 {
			value = rr[0][0]
		}
		table = append(table, []any{metric["display_name"], value, metric["unit"]})
		picked = append(picked, metric)
	}
	sections := []M{{"title": "经营概览", "body": "依据注册指标口径计算本期实际数据。", "table": M{"columns": []string{"指标", "本期值", "单位"}, "rows": table}}, {"title": "数据口径", "body": "所有结果来自当前工作空间的本地 DuckDB，指标口径以统一语义层为准。"}, {"title": "质量与风险", "body": fmt.Sprintf("当前共有 %v 条质量规则，最近失败 %v 条。", a.qualitySummary(ws)["rule_count"], a.qualitySummary(ws)["failing_count"])}, {"title": "经营建议", "body": "结合渠道、品类和城市维度检查贡献变化；对异常指标先进行数据质量校验。"}}
	if len(sections) > 0 {
		sections[0]["chart"] = M{"spec": M{"type": "table"}, "columns": []string{"指标", "本期值", "单位"}, "rows": table}
	}
	for _, s := range sections {
		s["heading"] = s["title"]
	}
	return M{"title": str(def(m, "topic", "经营分析")) + " · " + str(win["label"]), "topic": def(m, "topic", "经营分析"), "period": win, "metrics": picked, "sections": sections, "summary": fmt.Sprintf("已完成 %d 个指标的本期统计。", len(picked)), "generated_at": now()}
}
func (a *App) runReport(ws string, r M, trigger string) M {
	out := a.report(ws, M{"topic": def(r, "topic", r["name"]), "metrics": arr(r["metrics"]), "period": r["period"]})
	deliveries := []M{}
	for _, v := range arr(r["_channels"]) {
		channel := obj(v)
		deliveries = append(deliveries, M{"channel": firstNonempty(str(channel["name"]), str(channel["type"])), "ok": true, "simulated": true, "detail": "本地模拟投递", "target": channel["target"]})
	}
	run := M{"id": id(), "workspace_id": ws, "report_id": r["id"], "status": "success", "trigger": trigger, "title": out["title"], "summary": out["summary"], "sections": out["sections"], "snapshot": out, "channels": deliveries, "started_at": now(), "finished_at": now()}
	a.insert("report_runs", run)
	a.update("reports", str(r["id"]), M{"sections": out["sections"], "summary": out["summary"], "status": "ready", "updated_at": now()})
	return run
}
func (a *App) embed(ws, space string, rotate bool) M {
	a.owned("analysis_spaces", space, ws)
	if !rotate {
		if r := a.row("SELECT * FROM analysis_embeds WHERE workspace_id=? AND space_id=? AND revoked=0 ORDER BY created_at DESC LIMIT 1", ws, space); r != nil {
			r["iframe"] = `<iframe src="` + str(r["readonly_url"]) + `" width="100%" height="720"></iframe>`
			return r
		}
	}
	token := id() + id()
	r := a.insert("analysis_embeds", M{"id": id(), "workspace_id": ws, "space_id": space, "token": token, "readonly_url": "/api/embed/" + token, "scope": "readonly", "revoked": false, "created_at": now()})
	r["iframe"] = `<iframe src="/api/embed/` + token + `"></iframe>`
	return r
}
func (a *App) analysisRoutes() []route {
	r := []route{
		{"POST", "/api/metrics/query", false, false, func(c *request) any { return a.queryMetric(c.WS, c.Body) }},
		{"GET", "/api/analysis/meta", false, false, func(c *request) any {
			mm := a.list("metrics", c.WS)
			dims := []M{}
			seen := map[string]bool{}
			for _, m := range mm {
				decoded(m)
				for _, v := range arr(m["dimensions"]) {
					if !seen[str(v)] {
						dims = append(dims, M{"name": v, "label": v})
						seen[str(v)] = true
					}
				}
			}
			return M{"metrics": mm, "dimensions": dims, "periods": metadata["periods"], "summary": M{"reports": len(a.list("reports", c.WS)), "schedules": len(a.list("report_schedules", c.WS))}}
		}},
		{"POST", "/api/analysis/ask", false, false, func(c *request) any { return a.ask(c.WS, c.s("question"), c.s("period")) }},
		{"POST", "/api/analysis/attribute", false, false, func(c *request) any { return a.attribute(c.WS, c.Body) }},
		{"POST", "/api/analysis/report/generate", false, false, func(c *request) any { return a.report(c.WS, c.Body) }},
		{"POST", "/api/analysis/dashboard/generate", false, false, func(c *request) any {
			question := c.s("prompt")
			cards := []M{}
			for _, q := range []string{question, "每日GMV趋势", "品类GMV"} {
				out := a.ask(c.WS, q, c.s("period"))
				out["title"] = q
				cards = append(cards, out)
			}
			return M{"name": question, "description": "由语义层生成", "cards": cards, "metrics": []M{}, "dimensions": []M{}, "window": periodWindow(c.s("period"))["label"], "reasoning": "按指标口径生成查询与可视化卡片"}
		}},
		{"POST", "/api/reports/{report_id}/run", false, false, func(c *request) any {
			return M{"run": a.runReport(c.WS, a.owned("reports", c.p("report_id"), c.WS), "manual")}
		}},
		{"GET", "/api/reports/{report_id}/runs", false, false, func(c *request) any {
			a.owned("reports", c.p("report_id"), c.WS)
			rr := a.rows("SELECT * FROM report_runs WHERE workspace_id=? AND report_id=? ORDER BY started_at DESC", c.WS, c.p("report_id"))
			for _, r := range rr {
				decoded(r)
			}
			return M{"runs": rr}
		}},
		{"GET", "/api/analysis-spaces/{space_id}/embed", false, false, func(c *request) any { return M{"embed": a.embed(c.WS, c.p("space_id"), false)} }},
		{"POST", "/api/analysis-spaces/{space_id}/embed", false, false, func(c *request) any { return M{"embed": a.embed(c.WS, c.p("space_id"), true)} }},
		{"DELETE", "/api/analysis-spaces/{space_id}/embed", false, false, func(c *request) any {
			a.owned("analysis_spaces", c.p("space_id"), c.WS)
			a.exec("UPDATE analysis_embeds SET revoked=1 WHERE workspace_id=? AND space_id=?", c.WS, c.p("space_id"))
			return M{"ok": true}
		}},
		{"GET", "/api/embed/{token}", true, true, func(c *request) any {
			r := a.row("SELECT * FROM analysis_embeds WHERE token=? AND revoked=0 AND (expires_at IS NULL OR expires_at='' OR expires_at>?)", c.p("token"), now())
			if r == nil {
				fail(404, "嵌出链接无效或已撤销")
			}
			space := decoded(a.owned("analysis_spaces", str(r["space_id"]), str(r["workspace_id"])))
			delete(r, "token")
			r["space"] = space
			return M{"embed": r}
		}},
	}
	return r
}
