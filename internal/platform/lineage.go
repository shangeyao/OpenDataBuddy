package platform

import (
	"fmt"
	"regexp"
	"strings"
)

func shortRef(ref string) string { parts := strings.Split(ref, "."); return parts[len(parts)-1] }
func (a *App) lineageView(ws, ref, direction, view string, depth int) M {
	g := a.lineageGraph(ws, ref, direction, depth)
	tableNodes := arr(g["nodes"])
	levels := map[string]float64{}
	for _, v := range tableNodes {
		n := obj(v)
		levels[str(n["ref"])] = num(n["level"])
		n["type"] = "table"
		n["type_label"] = "表"
		n["label"] = shortRef(str(n["ref"]))
		n["distance"] = mathAbs(num(n["level"]))
		n["is_center"] = n["ref"] == ref
	}
	edges := arr(g["edges"])
	if view == "column" {
		nodes := []M{}
		out := []M{}
		seen := map[string]bool{}
		tables := map[string]bool{}
		add := func(table, column string) string {
			key := table + "::" + column
			if !seen[key] {
				nodes = append(nodes, M{"id": key, "ref": key, "table": table, "column": column, "label": column, "type": "column", "type_label": "字段", "level": levels[table], "distance": mathAbs(levels[table]), "is_center": table == ref})
				seen[key] = true
				tables[table] = true
			}
			return key
		}
		for _, v := range edges {
			e := obj(v)
			src, dst := str(e["src_column"]), str(e["dst_column"])
			if src == "" || dst == "" {
				continue
			}
			e["from"] = add(str(e["upstream"]), src)
			e["to"] = add(str(e["downstream"]), dst)
			out = append(out, e)
		}
		if len(nodes) == 0 {
			for _, t := range a.tables(ws) {
				if t["ref"] == ref {
					for _, c := range a.rows("SELECT name FROM columns WHERE table_id=? ORDER BY ordinal", t["id"]) {
						add(ref, str(c["name"]))
					}
				}
			}
		}
		g["nodes"] = nodes
		g["edges"] = out
		g["counts"] = M{"nodes": len(nodes), "edges": len(out), "tables": len(tables)}
		g["view"] = "column"
		return g
	}
	grouped := map[string]M{}
	order := []string{}
	for _, v := range edges {
		e := obj(v)
		key := str(e["upstream"]) + "|" + str(e["downstream"]) + "|" + str(e["kind"])
		if grouped[key] == nil {
			m := M{}
			for k, v := range e {
				m[k] = v
			}
			m["from"] = e["upstream"]
			m["to"] = e["downstream"]
			m["column_map"] = []M{}
			m["kind_label"] = e["kind"]
			grouped[key] = m
			order = append(order, key)
		}
		m := grouped[key]
		if str(e["src_column"]) != "" {
			m["column_map"] = append(arr(m["column_map"]), M{"src_column": e["src_column"], "dst_column": e["dst_column"], "transform": e["transform"]})
		}
		m["column_count"] = len(arr(m["column_map"]))
	}
	out := []M{}
	for _, key := range order {
		out = append(out, grouped[key])
	}
	g["edges"] = out
	g["counts"] = M{"nodes": len(tableNodes), "edges": len(out), "tables": len(tableNodes)}
	g["view"] = "table"
	return g
}
func mathAbs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}
func (a *App) discoverLineage(ws string, m M) M {
	text := str(m["sql"])
	target := str(def(m, "downstream", m["target"]))
	if target == "" {
		match := regexp.MustCompile(`(?i)(?:create\s+(?:or\s+replace\s+)?table|insert\s+into)\s+([\w."]+)`).FindStringSubmatch(text)
		if len(match) > 1 {
			target = strings.ReplaceAll(match[1], `"`, ``)
		}
	}
	sources := []string{}
	aliases := map[string]string{}
	columnsByTable := map[string]map[string]bool{}
	seen := map[string]bool{}
	reserved := map[string]bool{"on": true, "where": true, "group": true, "order": true, "left": true, "right": true, "inner": true, "full": true, "join": true, "limit": true, "union": true}
	for _, hit := range regexp.MustCompile(`(?i)\b(?:from|join)\s+([\w."]+)(?:\s+(?:as\s+)?([A-Za-z_]\w*))?`).FindAllStringSubmatch(text, -1) {
		ref := strings.ReplaceAll(hit[1], `"`, ``)
		var table M
		for _, t := range a.tables(ws) {
			if t["ref"] == ref || t["name"] == ref {
				if table != nil {
					fail(422, "源表名不唯一")
				}
				table = t
			}
		}
		if table != nil {
			ref = str(table["ref"])
		}
		if !seen[ref] {
			sources = append(sources, ref)
			seen[ref] = true
		}
		alias := hit[2]
		if alias != "" && !reserved[strings.ToLower(alias)] {
			aliases[alias] = ref
		}
		aliases[shortRef(ref)] = ref
		cols := map[string]bool{}
		if table != nil {
			for _, c := range a.rows("SELECT name FROM columns WHERE table_id=?", table["id"]) {
				cols[str(c["name"])] = true
			}
		}
		columnsByTable[ref] = cols
	}
	tt := sqlTokens(strings.TrimSuffix(strings.TrimSpace(text), ";"))
	selectIndex, fromIndex := -1, -1
	depth := 0
	for i, t := range tt {
		if strings.EqualFold(t.Text, "select") && selectIndex < 0 {
			selectIndex = i
			continue
		}
		if selectIndex >= 0 {
			if t.Text == "(" {
				depth++
			}
			if t.Text == ")" {
				depth--
			}
			if strings.EqualFold(t.Text, "from") && depth == 0 {
				fromIndex = i
				break
			}
		}
	}
	expressions := [][]sqlToken{}
	if selectIndex >= 0 && fromIndex > selectIndex {
		part := []sqlToken{}
		depth = 0
		for _, t := range tt[selectIndex+1 : fromIndex] {
			if t.Text == "(" {
				depth++
			}
			if t.Text == ")" {
				depth--
			}
			if t.Text == "," && depth == 0 {
				expressions = append(expressions, part)
				part = nil
			} else {
				part = append(part, t)
			}
		}
		if len(part) > 0 {
			expressions = append(expressions, part)
		}
	}
	columns := []M{}
	for i, expr := range expressions {
		parts := []string{}
		for _, t := range expr {
			parts = append(parts, t.Text)
		}
		transform := strings.Join(parts, " ")
		dest := fmt.Sprintf("expr_%d", i+1)
		for j, t := range expr {
			if strings.EqualFold(t.Text, "as") && j+1 < len(expr) {
				dest = tokenName(expr[j+1])
				expr = expr[:j]
				break
			}
		}
		if len(expr) == 1 && expr[0].Kind == 'i' {
			dest = tokenName(expr[0])
		}
		if len(expr) == 3 && expr[1].Text == "." {
			dest = tokenName(expr[2])
		}
		for _, ref := range sources {
			cols := columnsByTable[ref]
			for col := range cols {
				used := false
				for j, t := range expr {
					if t.Text == "*" {
						if j >= 2 && expr[j-1].Text == "." {
							used = used || aliases[tokenName(expr[j-2])] == ref
						} else {
							used = true
						}
						continue
					}
					if tokenName(t) != col || t.Kind != 'i' {
						continue
					}
					if j >= 2 && expr[j-1].Text == "." {
						used = used || aliases[tokenName(expr[j-2])] == ref
					} else if j+1 >= len(expr) || expr[j+1].Text != "(" {
						used = true
					}
				}
				if used {
					dst := dest
					if len(expr) == 1 && expr[0].Text == "*" || len(expr) == 3 && expr[2].Text == "*" {
						dst = col
					}
					columns = append(columns, M{"upstream": ref, "downstream": target, "src_column": col, "dst_column": dst, "transform": transform})
				}
			}
		}
	}
	recorded, recordedColumns := 0, 0
	relations := []M{}
	if target != "" && boolOf(def(m, "save", true)) {
		for _, src := range sources {
			edge := a.recordEdge(ws, M{"upstream": src, "downstream": target, "kind": "sql", "detail": text})
			relations = append(relations, edge)
			if edge["created"] == true {
				recorded++
			}
		}
		if boolOf(def(m, "record_columns", true)) {
			for _, col := range columns {
				edge := a.recordEdge(ws, col)
				if edge["created"] == true {
					recordedColumns++
				}
			}
		}
	}
	return M{"sources": sources, "target": target, "kind": "sql", "kind_label": "SQL", "columns": columns, "notes": []string{"使用 SELECT 投影与目录字段推断，复杂子查询需人工核对。"}, "recorded": recorded, "recorded_columns": recordedColumns, "recorded_relations": relations}
}
