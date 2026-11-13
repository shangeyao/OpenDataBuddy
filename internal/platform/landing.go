package platform

import "strings"

// landing.go is the shared "write rows into a lake table" path for the import
// sites (sync task, external catalog sync, SQL/file ingest).
//
// P2-3: instead of blindly `CREATE OR REPLACE` (which drops every existing
// column and all metadata-tagged columns), it evolves the table shape with
// `ALTER TABLE ADD COLUMN` and then re-projects onto the columns that actually
// exist. Removed columns are reported but never dropped. A `replace` flag keeps
// full-import idempotent while incremental sync still appends.

func landingNames(desired []M) []string {
	out := []string{}
	for _, d := range desired {
		name := str(d["name"])
		if name != "" {
			out = append(out, name)
		}
	}
	return out
}

func landingDefs(desired []M) string {
	defs := []string{}
	for _, d := range desired {
		typ := str(d["type"])
		if typ == "" {
			typ = "VARCHAR"
		}
		defs = append(defs, quote(str(d["name"]))+" "+typ)
	}
	return strings.Join(defs, ",")
}

// intersection returns target columns that also exist in source, preserving
// target order. Case-insensitive match, original spelling kept.
func landingIntersection(source, target []string) []string {
	have := map[string]string{}
	for _, s := range source {
		have[strings.ToLower(s)] = s
	}
	out := []string{}
	for _, t := range target {
		if s, ok := have[strings.ToLower(t)]; ok {
			out = append(out, s)
		}
	}
	return out
}

func landingQuoted(cols []string) string {
	q := []string{}
	for _, c := range cols {
		q = append(q, quote(c))
	}
	return strings.Join(q, ",")
}

func landingPlaceholders(n int) string {
	q := []string{}
	for i := 0; i < n; i++ {
		q = append(q, "?")
	}
	return strings.Join(q, ",")
}

// landRows writes data into physicalTable. desired describes the source
// columns as []M{{"name":...,"type":...}}; data is source rows keyed by column
// name. replace=true empties the table first (full sync / file import),
// replace=false appends (incremental sync).
func (a *App) landRows(ws, physicalTable string, desired []M, data []M, replace bool) M {
	shape := a.ensureTableShape(ws, physicalTable, desired)
	if !boolOf(shape["exists"]) {
		_, e := a.Lake.Exec("CREATE OR REPLACE TABLE " + physicalTable + " (" + landingDefs(desired) + ")")
		must(e)
		shape = M{"exists": true, "evolved": false, "added": []M{}, "removed": []M{}}
	}
	target := a.existingColumns(physicalTable)
	if len(target) == 0 {
		target = landingNames(desired)
	}
	srcCols := landingIntersection(landingNames(desired), target)
	if len(srcCols) == 0 {
		shape["inserted"] = 0
		shape["columns"] = target
		return shape
	}
	// INSERT 列名用目标表拼写；取值按源列名从 row 里取
	insertCols := []string{}
	for _, s := range srcCols {
		for _, t := range target {
			if strings.EqualFold(t, s) {
				insertCols = append(insertCols, t)
				break
			}
		}
	}
	if replace {
		_, e := a.Lake.Exec("DELETE FROM " + physicalTable)
		must(e)
	}
	insertSQL := "INSERT INTO " + physicalTable + " (" + landingQuoted(insertCols) + ") VALUES (" + landingPlaceholders(len(insertCols)) + ")"
	inserted := 0
	if len(data) > 0 {
		tx, e := a.Lake.Begin()
		must(e)
		stmt, e := tx.Prepare(insertSQL)
		if e != nil {
			tx.Rollback()
			fail(400, e.Error())
		}
		for _, row := range data {
			values := []any{}
			for _, c := range srcCols {
				values = append(values, row[c])
			}
			if _, e = stmt.Exec(values...); e != nil {
				stmt.Close()
				tx.Rollback()
				fail(400, e.Error())
			}
			inserted++
		}
		stmt.Close()
		must(tx.Commit())
	}
	shape["inserted"] = inserted
	shape["columns"] = target
	shape["replaced"] = replace
	return shape
}

// landSQL creates or evolves a table from a read-only SELECT statement.
// P2-3: when the target already exists, new columns are added by ALTER TABLE
// and the SELECT is re-projected onto the intersection of source and target
// columns, so a source schema change no longer wipes the whole table.
func (a *App) landSQL(ws, physicalTable, selectSQL string) M {
	// 用 DESCRIBE <SELECT> 推导源列结构（不实际取数）
	desired := []M{}
	for _, d := range a.describeSelect(selectSQL) {
		desired = append(desired, M{"name": str(d["column_name"]), "type": str(d["column_type"])})
	}
	if len(desired) == 0 {
		fail(422, "查询没有返回任何列")
	}
	shape := a.ensureTableShape(ws, physicalTable, desired)
	target := a.existingColumns(physicalTable)
	if len(target) == 0 {
		target = landingNames(desired)
	}
	cols := landingIntersection(landingNames(desired), target)
	if len(cols) == 0 {
		shape["inserted"] = 0
		shape["columns"] = target
		return shape
	}
	if !boolOf(shape["exists"]) {
		_, e := a.Lake.Exec("CREATE OR REPLACE TABLE " + physicalTable + " AS " + selectSQL)
		must(e)
		shape = M{"exists": true, "evolved": false, "added": []M{}, "removed": []M{}, "inserted": 0, "columns": target}
		return shape
	}
	// 表已存在：先清空再按交集列写回，保持全量导入幂等
	_, e := a.Lake.Exec("DELETE FROM " + physicalTable)
	must(e)
	// INSERT 用目标列名，SELECT 用对应的源列名（大小写可能不同）
	insertCols := []string{}
	selectCols := []string{}
	for _, s := range cols {
		low := strings.ToLower(s)
		for _, t := range target {
			if strings.ToLower(t) == low {
				insertCols = append(insertCols, quote(t))
				selectCols = append(selectCols, quote(s))
				break
			}
		}
	}
	ins := "INSERT INTO " + physicalTable + " (" + strings.Join(insertCols, ",") + ") SELECT " + strings.Join(selectCols, ",") + " FROM (" + selectSQL + ") AS _src"
	r, e := a.Lake.Exec(ins)
	must(e)
	n, _ := r.RowsAffected()
	shape["inserted"] = n
	shape["columns"] = target
	shape["replaced"] = true
	return shape
}

// describeSelect returns DESCRIBE rows for a SELECT without scanning data.
func (a *App) describeSelect(selectSQL string) []M {
	r, e := a.Lake.Query("DESCRIBE " + selectSQL)
	if e != nil {
		fail(400, e.Error())
	}
	defer r.Close()
	cols, e := r.Columns()
	must(e)
	out := []M{}
	for r.Next() {
		vals := make([]any, len(cols))
		ptr := make([]any, len(cols))
		for i := range vals {
			ptr[i] = &vals[i]
		}
		must(r.Scan(ptr...))
		m := M{}
		for i, c := range cols {
			m[c] = vals[i]
		}
		out = append(out, m)
	}
	must(r.Err())
	return out
}
