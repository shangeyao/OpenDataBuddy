package platform

import (
	"context"
	"fmt"
	"strings"
	"time"
	"unicode"
)

type sqlToken struct {
	Text string
	Kind byte
}

// Tokenize before rewriting: literals and quoted identifiers must never be rewritten as SQL.
func sqlTokens(q string) []sqlToken {
	out := []sqlToken{}
	r := []rune(q)
	for i := 0; i < len(r); {
		c := r[i]
		if unicode.IsSpace(c) {
			i++
			continue
		}

		if unicode.IsDigit(c) || (c == '.' && i+1 < len(r) && unicode.IsDigit(r[i+1])) {
			start := i
			i++
			for i < len(r) && (unicode.IsDigit(r[i]) || r[i] == '.') {
				i++
			}
			if i < len(r) && (r[i] == 'e' || r[i] == 'E') {
				i++
				if i < len(r) && (r[i] == '+' || r[i] == '-') {
					i++
				}
				for i < len(r) && unicode.IsDigit(r[i]) {
					i++
				}
			}
			out = append(out, sqlToken{string(r[start:i]), 'n'})
			continue
		}
		if i+2 < len(r) && string(r[i:i+3]) == "->>" {
			out = append(out, sqlToken{"->>", 'p'})
			i += 3
			continue
		}
		if i+1 < len(r) && strings.Contains("|>=|<=|<>|!=|==||| |::|->|**|", "|"+string(r[i:i+2])+"|") {
			out = append(out, sqlToken{string(r[i : i+2]), 'p'})
			i += 2
			continue
		}
		if c == '\'' || c == '"' {
			start := i
			i++
			closed := false
			for i < len(r) {
				if r[i] == c {
					if i+1 < len(r) && r[i+1] == c {
						i += 2
						continue
					}
					i++
					closed = true
					break
				}
				i++
			}
			if !closed {
				fail(400, "SQL 引号未闭合")
			}
			kind := byte('s')
			if c == '"' {
				kind = 'i'
			}
			out = append(out, sqlToken{string(r[start:i]), kind})
			continue
		}
		if unicode.IsLetter(c) || c == '_' || unicode.IsDigit(c) {
			start := i
			i++
			for i < len(r) && (unicode.IsLetter(r[i]) || unicode.IsDigit(r[i]) || r[i] == '_' || r[i] == '$') {
				i++
			}
			out = append(out, sqlToken{string(r[start:i]), 'i'})
			continue
		}
		if c == '\x00' || c == ';' || c == '-' && i+1 < len(r) && r[i+1] == '-' || c == '/' && i+1 < len(r) && r[i+1] == '*' {
			fail(400, "一次只允许单条、无注释的 SQL")
		}
		out = append(out, sqlToken{string(c), 'p'})
		i++
	}
	return out
}
func tokenName(t sqlToken) string {
	if strings.HasPrefix(t.Text, `"`) {
		return strings.ReplaceAll(strings.Trim(t.Text, `"`), `""`, `"`)
	}
	return t.Text
}
func guard(q string) string {
	q = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(q), ";"))
	tt := sqlTokens(q)
	if len(tt) == 0 {
		fail(400, "SQL 为空")
	}
	first := strings.ToLower(tt[0].Text)
	if !strings.Contains("|select|with|describe|desc|show|summarize|explain|values|pivot|", "|"+first+"|") {
		fail(400, "只允许只读查询")
	}
	for i, t := range tt {
		if t.Kind != 'i' || strings.HasPrefix(t.Text, `"`) {
			continue
		}
		s := strings.ToLower(t.Text)
		if strings.Contains("|attach|detach|install|load|copy|export|import|pragma|call|create|insert|update|delete|drop|alter|truncate|grant|revoke|vacuum|checkpoint|set|reset|use|begin|commit|rollback|", "|"+s+"|") {
			fail(400, "受限 SQL 关键字："+s)
		}
		if i+1 < len(tt) && tt[i+1].Text == "(" && (strings.HasPrefix(s, "read_") || strings.HasSuffix(s, "_scan") || strings.HasPrefix(s, "duckdb_") || strings.HasPrefix(s, "sqlite_") || strings.Contains("|glob|getenv|shell|system|query|query_table|httpfs|", "|"+s+"|")) {
			fail(400, "禁止读取外部文件或系统资源")
		}
	}
	return q
}
func (a *App) qualify(ws, q string) string {
	tt := sqlTokens(q)
	tables := a.tables(ws)
	allowed := map[string]string{}
	counts := map[string]int{}
	ctes := map[string]bool{}
	for _, t := range tables {
		counts[str(t["name"])]++
	}
	for _, t := range tables {
		target := physical(t)
		allowed[strings.ToLower(str(t["ref"]))] = target
		allowed[strings.ToLower(lakeNamespace(str(t["physical_prefix"]), str(t["catalog_name"]), str(t["schema_name"]))+"."+str(t["name"]))] = target
		if counts[str(t["name"])] == 1 {
			allowed[strings.ToLower(str(t["name"]))] = target
		}
	}
	for i := 0; i+2 < len(tt); i++ {
		if tt[i].Kind == 'i' && strings.EqualFold(tt[i+1].Text, "as") && tt[i+2].Text == "(" {
			ctes[strings.ToLower(tokenName(tt[i]))] = true
		}
	}
	depth := 0
	from := map[int]bool{}
	need := false
	for i := 0; i < len(tt); i++ {
		t := tt[i]
		lower := strings.ToLower(t.Text)
		if need {
			need = false
			if t.Text == "(" {
				depth++
				continue
			}
			if t.Kind != 'i' {
				fail(400, "只允许查询工作空间目录中的表")
			}
			parts := []string{tokenName(t)}
			j := i
			for j+2 < len(tt) && tt[j+1].Text == "." && tt[j+2].Kind == 'i' {
				parts = append(parts, tokenName(tt[j+2]))
				j += 2
			}
			name := strings.ToLower(strings.Join(parts, "."))
			if j+1 < len(tt) && tt[j+1].Text == "(" {
				fail(400, "请先通过数据接入创建表，SQL 不支持表函数")
			}
			target, ok := allowed[name]
			if !ok && !ctes[name] {
				fail(403, "表不属于当前工作空间或名称不唯一："+name)
			}
			if ok {
				tt[i].Text = target
				for k := i + 1; k <= j; k++ {
					tt[k].Text = ""
				}
			}
			i = j
			continue
		}
		switch lower {
		case "(":
			depth++
		case ")":
			delete(from, depth)
			depth--
			if depth < 0 {
				fail(400, "SQL 括号不匹配")
			}
		case "from", "join":
			from[depth] = true
			need = true
		case ",":
			if from[depth] {
				need = true
			}
		case "where", "group", "order", "having", "limit", "union", "except", "intersect", "qualify", "window":
			from[depth] = false
		case "describe", "desc", "summarize":
			if i == 0 {
				need = true
			}
		case "show":
			fail(400, "请使用数据目录查看工作空间表清单")
		}
	}
	if need || depth != 0 {
		fail(400, "SQL 结构不完整")
	}
	parts := []string{}
	for _, t := range tt {
		if t.Text != "" {
			parts = append(parts, t.Text)
		}
	}
	return strings.Join(parts, " ")
}
func (a *App) query(ws, q string, limit int) M {
	start := time.Now()
	q = a.qualify(ws, guard(q))
	if limit <= 0 || limit > 10000 {
		limit = 1000
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	r, e := a.Lake.QueryContext(ctx, q)
	if e != nil {
		fail(400, e.Error())
	}
	defer r.Close()
	cols, e := r.Columns()
	must(e)
	result := [][]any{}
	truncated := false
	for r.Next() {
		if len(result) == limit {
			truncated = true
			break
		}
		values := make([]any, len(cols))
		pointers := make([]any, len(cols))
		for i := range values {
			pointers[i] = &values[i]
		}
		must(r.Scan(pointers...))
		for i, v := range values {
			switch v := v.(type) {
			case []byte:
				values[i] = string(v)
			case time.Time:
				values[i] = v.Format(time.RFC3339)
			case fmt.Stringer:
				values[i] = v.String()
			}
		}
		result = append(result, values)
	}
	must(r.Err())
	return M{"columns": cols, "rows": result, "row_count": len(result), "truncated": truncated, "elapsed_ms": time.Since(start).Milliseconds(), "sql": q}
}
