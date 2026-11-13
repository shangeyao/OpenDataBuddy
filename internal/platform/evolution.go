package platform

import (
	"fmt"
	"regexp"
	"strings"
	"unicode"
)

// evolutionIdentASCII 是列名的严格 ASCII 标识符正则；evolutionValidName 在它
// 不匹配时再放行 Unicode 字母（中文列名等）。DDL 无法参数化，故列名必须先过此校验。
var evolutionIdentASCII = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// evolutionValidName 校验列名：ASCII 严格正则优先，其次允许 Unicode 字母/数字，
// 首字符只能是字母或下划线（不能是数字）。
func evolutionValidName(s string) bool {
	if evolutionIdentASCII.MatchString(s) {
		return true
	}
	if s == "" {
		return false
	}
	for i, r := range s {
		switch {
		case r == '_' || unicode.IsLetter(r):
		case i > 0 && unicode.IsDigit(r):
		default:
			return false
		}
	}
	return true
}

// evolutionSafeType 把列类型收敛到 DuckDB 白名单；DDL 无法参数化，故只放行这些
// 字面量，返回统一的大写形式供 ALTER TABLE 直接拼接。
func evolutionSafeType(t string) string {
	// 去掉全部空白并大写，兼容 "decimal(18, 2)" / "double " 等写法
	key := strings.ToUpper(strings.Join(strings.Fields(t), ""))
	switch key {
	case "VARCHAR", "BIGINT", "INTEGER", "DOUBLE", "BOOLEAN",
		"DATE", "TIMESTAMP", "TIMESTAMPTZ", "DECIMAL(18,2)",
		"TEXT", "FLOAT", "BLOB", "JSON":
		return key
	}
	fail(422, "不合法的列类型："+t)
	return ""
}

// evolutionGuardPhysical 守卫来自 physical(t)/a.target() 的完全引用物理表名。
// 它本已是加引号的内部串，这里只挡住注释与多语句注入。
func evolutionGuardPhysical(p string) {
	if p == "" {
		fail(422, "物理表名不能为空")
	}
	for _, bad := range []string{";", "--", "/*", "*/"} {
		if strings.Contains(p, bad) {
			fail(422, "非法的物理表名："+p)
		}
	}
}

// evolutionDescribe 容错读取 DESCRIBE 结果（column_name / column_type 等），
// 任何错误（含物理表不存在）都返回空切片，绝不把错误抛给上层。
func (a *App) evolutionDescribe(physicalTable string) []M {
	out := []M{}
	evolutionGuardPhysical(physicalTable)
	r, e := a.Lake.Query("DESCRIBE " + physicalTable)
	if e != nil {
		return out
	}
	defer r.Close()
	cols, e := r.Columns()
	if e != nil {
		return out
	}
	for r.Next() {
		vals := make([]any, len(cols))
		ptr := make([]any, len(cols))
		for i := range vals {
			ptr[i] = &vals[i]
		}
		if e = r.Scan(ptr...); e != nil {
			return out
		}
		m := M{}
		for i, c := range cols {
			v := vals[i]
			if b, ok := v.([]byte); ok {
				v = string(b)
			}
			m[c] = v
		}
		out = append(out, m)
	}
	if r.Err() != nil {
		return []M{}
	}
	return out
}

// evolutionRef 由物理表名反查逻辑三段名，供演进记录 table_ref 使用；找不到则回退物理名。
func (a *App) evolutionRef(ws, physicalTable string) string {
	for _, t := range a.tables(ws) {
		if physical(t) == physicalTable {
			return str(t["ref"])
		}
	}
	return physicalTable
}

// recordEvolution 落一条 table_evolutions；added/removed 为 []M，由 a.insert 自动 JSON 化。
func (a *App) recordEvolution(ws, tableRef string, added, removed []M, before, after int, trigger, detail string) string {
	key := id()
	a.insert("table_evolutions", M{
		"id":              key,
		"workspace_id":    ws,
		"table_ref":       tableRef,
		"added_columns":   added,
		"removed_columns": removed,
		"before_columns":  before,
		"after_columns":   after,
		"trigger":         trigger,
		"detail":          detail,
		"created_at":      now(),
	})
	return key
}

// evolveSchema 是共享 diff 核心：
//
//	① DESCRIBE 读当前列，按列名小写化建 present 集合；
//	② desired 中不在 present 的为 added，present 中不在 desired 的为 removed（只上报、绝不删列）；
//	③ 仅对 added 列做 ALTER TABLE ... ADD COLUMN，列名/类型先过白名单；
//	④ 返回 (added, removed, len(current), len(current)+len(added))。
func (a *App) evolveSchema(ws, physicalTable string, desired []M) (added []M, removed []M, before, after int) {
	_ = ws // 预留给后续审计/鉴权；diff 本身不依赖工作空间
	evolutionGuardPhysical(physicalTable)

	current := rows(a.Lake, "DESCRIBE "+physicalTable)
	present := map[string]bool{}
	for _, r := range current {
		present[strings.ToLower(str(r["column_name"]))] = true
	}

	added, removed = []M{}, []M{}
	desiredKeys := map[string]bool{}
	for _, d := range desired {
		name := strings.TrimSpace(str(d["name"]))
		k := strings.ToLower(name)
		if desiredKeys[k] {
			continue
		}
		desiredKeys[k] = true
		if present[k] {
			// 已存在列：只参与 removed 差集计算，不进 DDL，无需严格校验标识符/类型
			continue
		}
		// 新增列会被拼进 ALTER TABLE DDL，必须先过标识符与类型白名单
		if !evolutionValidName(name) {
			fail(422, "不合法的列名："+name)
		}
		added = append(added, M{"name": name, "type": evolutionSafeType(str(d["type"]))})
	}
	for _, r := range current {
		n := str(r["column_name"])
		if !desiredKeys[strings.ToLower(n)] {
			removed = append(removed, M{"name": n, "type": str(r["column_type"])})
		}
	}

	// 只做加列，绝不 DROP COLUMN；removed 仅作为差异上报给调用方
	for _, c := range added {
		ddl := "ALTER TABLE " + physicalTable + " ADD COLUMN " + quote(str(c["name"])) + " " + str(c["type"])
		if _, e := a.Lake.Exec(ddl); e != nil {
			fail(400, e.Error())
		}
	}

	before = len(current)
	after = before + len(added)
	return
}

// ensureTableShape 供 ingest/sync/operations 复用：表不存在时不重建（交回调用方
// CREATE OR REPLACE）；表存在时做真实演进，并在有加列时登记演进记录。
func (a *App) ensureTableShape(ws, physicalTable string, desired []M) M {
	evolutionGuardPhysical(physicalTable)
	probe, e := a.Lake.Query("SELECT 1 FROM " + physicalTable + " LIMIT 0")
	if e != nil {
		// 查不到表（或任何查询错误）：交回调用方做 CREATE OR REPLACE
		return M{"exists": false, "created": false}
	}
	for probe.Next() {
	}
	e = probe.Err()
	probe.Close()
	if e != nil {
		return M{"exists": false, "created": false}
	}

	added, removed, before, after := a.evolveSchema(ws, physicalTable, desired)
	evolved := len(added) > 0
	evoID := ""
	if evolved {
		evoID = a.recordEvolution(ws, a.evolutionRef(ws, physicalTable), added, removed, before, after, "manual", "")
	}
	return M{
		"exists":       true,
		"added":        added,
		"removed":      removed,
		"before":       before,
		"after":        after,
		"evolved":      evolved,
		"evolution_id": evoID,
	}
}

// existingColumns 返回物理表当前的列名（DESCRIBE 顺序 = ordinal 顺序），供调用方
// 在真实演进之后只 INSERT 已存在的列；出错返回空切片。
func (a *App) existingColumns(physicalTable string) []string {
	out := []string{}
	for _, c := range a.evolutionDescribe(physicalTable) {
		out = append(out, str(c["column_name"]))
	}
	return out
}

// evolutionReregisterColumns 删除并重建 columns 元数据行，保持与 DESCRIBE 一致。
func (a *App) evolutionReregisterColumns(t M, physicalTable string) []M {
	key := str(t["id"])
	// 先清掉旧列与列标签，再按 DESCRIBE 重建，保证元数据与物理表一致
	a.exec("DELETE FROM column_tags WHERE column_id IN (SELECT id FROM columns WHERE table_id=?)", key)
	a.exec("DELETE FROM columns WHERE table_id=?", key)
	out := []M{}
	for i, c := range a.evolutionDescribe(physicalTable) {
		m := M{
			"id":          id(),
			"table_id":    key,
			"name":        str(c["column_name"]),
			"data_type":   str(c["column_type"]),
			"ordinal":     i,
			"is_nullable": true,
			"created_at":  now(),
		}
		a.insert("columns", m)
		out = append(out, m)
	}
	return out
}

// evolveTable 手动演进端点：解析三段 table_ref，收集期望列，调用共享演进并重登记元数据。
func (a *App) evolveTable(ws string, body M) M {
	ref := strings.TrimSpace(str(body["table_ref"]))
	t := a.resolve(ws, ref)
	pt := physical(t)
	evolutionGuardPhysical(pt)

	// ① 期望列：显式 columns 优先；为空则从 DESCRIBE 推导（no-op）
	desired := []M{}
	for _, v := range arr(body["columns"]) {
		m := obj(v)
		desired = append(desired, M{"name": str(m["name"]), "type": str(m["type"])})
	}
	if len(desired) == 0 {
		for _, c := range a.evolutionDescribe(pt) {
			desired = append(desired, M{"name": str(c["column_name"]), "type": str(c["column_type"])})
		}
	}

	// ② 共享演进：DESCRIBE diff + ALTER TABLE ADD COLUMN，并登记演进记录
	shape := a.ensureTableShape(ws, pt, desired)
	if shape["exists"] != true {
		// 物理表尚不存在，交回调用方用 CREATE OR REPLACE 建表
		return M{
			"table_ref": ref, "ref": t["ref"], "exists": false,
			"added": []M{}, "removed": []M{}, "before": 0, "after": 0,
			"evolved": false, "columns": []M{}, "evolution_id": "",
		}
	}

	// ③ 重登记 columns 元数据，保持与物理表一致
	metaCols := a.evolutionReregisterColumns(t, pt)

	return M{
		"table_ref":    ref,
		"ref":          t["ref"],
		"exists":       shape["exists"],
		"added":        shape["added"],
		"removed":      shape["removed"],
		"before":       shape["before"],
		"after":        shape["after"],
		"evolved":      shape["evolved"],
		"columns":      metaCols,
		"evolution_id": shape["evolution_id"],
	}
}

// listEvolutions 返回工作空间全部演进记录（时间倒序），解码 JSON 列并附中文摘要。
func (a *App) listEvolutions(ws string) []M {
	out := []M{}
	for _, r := range a.rows("SELECT * FROM table_evolutions WHERE workspace_id=? ORDER BY created_at DESC, id DESC", ws) {
		added := arr(r["added_columns"])
		removed := arr(r["removed_columns"])
		r["added_columns"] = added
		r["removed_columns"] = removed
		r["summary"] = fmt.Sprintf("新增 %d 列 / 移除 %d 列（%d → %d），触发：%s",
			len(added), len(removed), int(num(r["before_columns"])), int(num(r["after_columns"])), str(r["trigger"]))
		out = append(out, r)
	}
	return out
}

// triggerEvolutionFromDrift 由 drift.go 在发现 kind:"schema" 异常时调用：只归档
// 一条 trigger=drift 的演进事件，不自动加列（无期望列的可靠来源），诚实标注需显式登记。
func (a *App) triggerEvolutionFromDrift(ws, tableRef, detail string) M {
	t := a.resolve(ws, tableRef)
	pt := physical(t)
	evolutionGuardPhysical(pt)
	n := len(a.evolutionDescribe(pt))
	evoID := a.recordEvolution(ws, str(t["ref"]), []M{}, []M{}, n, n, "drift", detail)
	return M{
		"recorded":           true,
		"evolution_id":       evoID,
		"trigger":            "drift",
		"table_ref":          tableRef,
		"auto_columns_added": 0,
		"note":               "漂移记录已归档为结构演进事件；新增列需通过 /api/catalog/evolve 显式登记",
	}
}
