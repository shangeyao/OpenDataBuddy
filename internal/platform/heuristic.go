package platform

import (
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
)

func (a *App) searchKnowledge(ws, q string, limit int) []M {
	docs := a.list("knowledge_docs", ws)
	terms := map[string]bool{}
	runes := []rune(strings.ToLower(q))
	for i := 0; i+1 < len(runes); i++ {
		terms[string(runes[i:i+2])] = true
	}
	for _, word := range strings.Fields(strings.ToLower(q)) {
		terms[word] = true
	}
	out := []M{}
	for _, d := range docs {
		text := strings.ToLower(str(d["title"]) + " " + str(d["content"]))
		score := float64(0)
		for term := range terms {
			if strings.Contains(text, term) {
				freq := 0
				for _, doc := range docs {
					if strings.Contains(strings.ToLower(str(doc["title"])+str(doc["content"])), term) {
						freq++
					}
				}
				score += math.Log(1 + float64(len(docs))/float64(freq+1))
			}
		}
		if score == 0 {
			continue
		}
		d["score"] = score
		snippet := []rune(str(d["content"]))
		if len(snippet) > 600 {
			snippet = snippet[:600]
		}
		d["snippet"] = string(snippet)
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return num(out[i]["score"]) > num(out[j]["score"]) })
	if limit < 1 || limit > 20 {
		limit = 4
	}
	if len(out) > limit {
		out = out[:limit]
	}
	return out
}
func (a *App) heuristic(c *request, conv, question string) (content string, out M) {
	defer func() {
		if e := recover(); e != nil {
			content = "当前未接入大模型。可以查询已注册指标、查看表结构、扫描敏感字段和运行质量检查；开放式任务请先配置模型。"
			out = M{"notice": content}
		}
	}()
	tables := a.tables(c.WS)
	pick := func(fallback string) string {
		for _, t := range tables {
			if strings.Contains(question, str(t["name"])) || strings.Contains(question, str(t["ref"])) {
				return str(t["ref"])
			}
		}
		for _, t := range tables {
			if t["name"] == fallback {
				return str(t["ref"])
			}
		}
		if len(tables) > 0 {
			return str(tables[0]["ref"])
		}
		fail(404, "当前空间没有数据表")
		return ""
	}
	call := func(name string, args M) M { return a.invoke(c, conv, name, args) }
	switch {
	case regexp.MustCompile(`有哪些表|表清单|哪些数据|有什么数据|数据资产|列出.*表`).MatchString(question):
		out = call("list_tables", M{})
		content = fmt.Sprintf("当前工作空间有 %d 张表。", len(tables))
	case regexp.MustCompile(`表结构|字段结构|有哪些列|(?i:schema)`).MatchString(question):
		out = call("describe_table", M{"table": pick("fact_orders")})
		content = fmt.Sprintf("%s 共 %d 个字段。", obj(out["table"])["ref"], len(arr(out["columns"])))
	case regexp.MustCompile(`敏感|脱敏|隐私|(?i:pii)|个人信息`).MatchString(question):
		out = call("scan_sensitive", M{"table": pick("dim_customer")})
		content = fmt.Sprintf("扫描完成，识别到 %v 个敏感字段。", out["count"])
	case regexp.MustCompile(`质量|校验|空值|重复|异常值`).MatchString(question):
		out = call("run_quality_rules", M{"table": pick("fact_orders")})
		content = fmt.Sprintf("质量检查：%v/%v 条规则通过。", out["passed"], out["total"])
	default:
		if regexp.MustCompile(`口径|定义|怎么算|如何计算|含义|是什么意思`).MatchString(question) {
			for _, m := range a.list("metrics", c.WS) {
				if strings.Contains(strings.ToLower(question), strings.ToLower(str(m["name"]))) || strings.Contains(question, str(m["display_name"])) {
					out = M{"metric": decoded(m)}
					content = fmt.Sprintf("**%s** 的口径：`%s`，取自 `%s`，单位为 %s。", m["display_name"], m["expr"], m["table_ref"], m["unit"])
					return
				}
			}
			docs := a.searchKnowledge(c.WS, question, 3)
			if len(docs) > 0 {
				out = M{"results": docs}
				content = "根据本地知识库：\n"
				for _, d := range docs {
					content += "\n**" + str(d["title"]) + "**\n" + str(d["snippet"]) + "\n"
				}
				return
			}
		}
		out = a.ask(c.WS, question, "")
		a.insert("traces", M{"id": id(), "workspace_id": c.WS, "conversation_id": conv, "tool": "query_metric", "args": M{"question": question}, "result": M{"row_count": out["row_count"]}, "ok": true, "created_at": now()})
		content = str(out["answer"])
	}
	return
}
