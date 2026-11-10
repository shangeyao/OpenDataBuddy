package platform

import (
	"archive/zip"
	"encoding/csv"
	"encoding/xml"
	"io"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

var identifier = regexp.MustCompile(`^[\p{L}_][\p{L}\p{N}_]*$`)

func validName(s string) string {
	if len([]rune(s)) > 128 || !identifier.MatchString(s) {
		fail(422, "名称只能包含字母、数字和下划线，且不能以数字开头")
	}
	return s
}
func (a *App) target(ws, target, name string) (M, string) {
	validName(name)
	parts := strings.Split(target, ".")
	if len(parts) != 2 {
		fail(422, "目标格式应为 catalog.schema")
	}
	sch := a.row("SELECT s.*,c.name AS catalog_name,c.physical_prefix FROM schemas s JOIN catalogs c ON c.id=s.catalog_id WHERE c.workspace_id=? AND c.name=? AND s.name=?", ws, parts[0], parts[1])
	if sch == nil {
		fail(404, "目标 Schema 不存在")
	}
	return sch, quote(lakeNamespace(str(sch["physical_prefix"]), parts[0], parts[1])) + "." + quote(name)
}
func (a *App) registerTable(ws string, sch M, name, description string) M {
	old := a.row("SELECT * FROM tables WHERE schema_id=? AND name=?", sch["id"], name)
	key := id()
	if old != nil {
		key = str(old["id"])
		a.exec("DELETE FROM column_tags WHERE column_id IN (SELECT id FROM columns WHERE table_id=?)", key)
		a.exec("DELETE FROM columns WHERE table_id=?", key)
	}
	table := quote(lakeNamespace(str(sch["physical_prefix"]), str(sch["catalog_name"]), str(sch["name"]))) + "." + quote(name)
	count := rows(a.Lake, "SELECT count(*) AS n FROM "+table)[0]["n"]
	m := M{"id": key, "schema_id": sch["id"], "name": name, "kind": "table", "description": description, "row_count": count, "created_at": now(), "updated_at": now()}
	if old != nil {
		a.update("tables", key, m)
	} else {
		a.insert("tables", m)
	}
	for i, col := range rows(a.Lake, "DESCRIBE "+table) {
		a.insert("columns", M{"id": id(), "table_id": key, "name": col["column_name"], "data_type": col["column_type"], "ordinal": i, "is_nullable": true, "created_at": now()})
	}
	return a.resolve(ws, str(sch["catalog_name"])+"."+str(sch["name"])+"."+name)
}
func (a *App) importSQL(ws, target, name, q, desc string) M {
	sch, table := a.target(ws, target, name)
	q = a.qualify(ws, guard(q))
	_, e := a.Lake.Exec("CREATE OR REPLACE TABLE " + table + " AS " + q)
	if e != nil {
		fail(400, e.Error())
	}
	return a.registerTable(ws, sch, name, desc)
}
func xlsxCSV(path string) string {
	z, e := zip.OpenReader(path)
	must(e)
	defer z.Close()
	shared := []string{}
	sheet := []byte{}
	for _, f := range z.File {
		if f.Name != "xl/sharedStrings.xml" && f.Name != "xl/worksheets/sheet1.xml" {
			continue
		}
		r, e := f.Open()
		must(e)
		b, e := io.ReadAll(io.LimitReader(r, 32<<20))
		r.Close()
		must(e)
		if f.Name == "xl/sharedStrings.xml" {
			var doc struct {
				Items []struct {
					Text string `xml:"t"`
					Runs []struct {
						Text string `xml:"t"`
					} `xml:"r"`
				} `xml:"si"`
			}
			must(xml.Unmarshal(b, &doc))
			for _, v := range doc.Items {
				vtext := v.Text
				for _, r := range v.Runs {
					vtext += r.Text
				}
				shared = append(shared, vtext)
			}
		} else {
			sheet = b
		}
	}
	if len(sheet) == 0 {
		fail(422, "XLSX 没有第一个工作表")
	}
	var doc struct {
		Rows []struct {
			Cells []struct {
				Ref    string `xml:"r,attr"`
				Type   string `xml:"t,attr"`
				Value  string `xml:"v"`
				Inline string `xml:"is>t"`
			} `xml:"c"`
		} `xml:"sheetData>row"`
	}
	must(xml.Unmarshal(sheet, &doc))
	out, e := os.CreateTemp("", "odb-xlsx-*.csv")
	must(e)
	w := csv.NewWriter(out)
	for _, row := range doc.Rows {
		values := []string{}
		for _, c := range row.Cells {
			idx := 0
			for _, r := range c.Ref {
				if r < 'A' || r > 'Z' {
					break
				}
				idx = idx*26 + int(r-'A'+1)
			}
			if idx < 1 || idx > 16384 {
				fail(422, "XLSX 列号超出范围")
			}
			for len(values) < idx {
				values = append(values, "")
			}
			v := c.Value
			if c.Type == "s" {
				j := int(num(v))
				if j >= 0 && j < len(shared) {
					v = shared[j]
				}
			}
			if c.Type == "inlineStr" {
				v = c.Inline
			}
			values[idx-1] = v
		}
		must(w.Write(values))
	}
	w.Flush()
	must(w.Error())
	must(out.Close())
	return out.Name()
}
func (a *App) volumeFiles(v M) []M {
	root := str(v["path"])
	files := []M{}
	_ = filepath.Walk(root, func(path string, i os.FileInfo, e error) error {
		if e != nil {
			return nil
		}
		if !i.IsDir() {
			rel, e := filepath.Rel(root, path)
			must(e)
			files = append(files, M{"name": i.Name(), "path": rel, "size": i.Size(), "size_bytes": i.Size(), "modified_at": i.ModTime().UTC().Format(time.RFC3339)})
		}
		return nil
	})
	return files
}
func (a *App) probe(kind string, cfg M) M {
	ok := false
	message := ""
	if kind == "file" || kind == "sqlite" {
		path := str(def(cfg, "path", cfg["database"]))
		_, e := os.Stat(path)
		ok = e == nil
		if e != nil {
			message = e.Error()
		} else {
			message = "本地路径可访问"
		}
	} else {
		host := str(cfg["host"])
		port := str(def(cfg, "port", 5432))
		if kind == "mysql" {
			port = str(def(cfg, "port", 3306))
		}
		conn, e := net.DialTimeout("tcp", net.JoinHostPort(host, port), 3*time.Second)
		if e == nil {
			conn.Close()
			ok = true
			message = "TCP 连接成功（未验证数据库认证）"
		} else {
			message = e.Error()
		}
	}
	return M{"ok": ok, "message": message, "score": map[bool]int{true: 100, false: 0}[ok], "checks": []M{{"name": "连通性", "ok": ok, "message": message}}, "problems": []M{}, "suggestions": []string{}}
}
func (a *App) ingestRoutes() []route {
	return []route{
		{"POST", "/api/ingest/from-sql", false, false, func(c *request) any {
			return M{"table": a.importSQL(c.WS, c.s("target"), c.s("table"), c.s("sql"), c.s("description"))}
		}},
		{"POST", "/api/ingest/upload", false, false, func(c *request) any {
			r := c.R.Request
			r.Body = io.NopCloser(io.LimitReader(r.Body, 128<<20))
			if e := r.ParseMultipartForm(8 << 20); e != nil {
				fail(422, "上传表单无效")
			}
			if r.MultipartForm != nil {
				defer r.MultipartForm.RemoveAll()
			}
			f, h, e := r.FormFile("file")
			if e != nil {
				fail(422, "缺少上传文件")
			}
			defer f.Close()
			suffix := strings.ToLower(filepath.Ext(h.Filename))
			if !strings.Contains("|.csv|.json|.jsonl|.ndjson|.parquet|.pq|.xlsx|", "|"+suffix+"|") {
				fail(422, "不支持的文件类型")
			}
			temp, e := os.CreateTemp(filepath.Join(a.Dir, "uploads"), "upload-*"+suffix)
			must(e)
			defer os.Remove(temp.Name())
			_, e = io.Copy(temp, io.LimitReader(f, 128<<20))
			must(e)
			must(temp.Close())
			path := temp.Name()
			if suffix == ".xlsx" {
				path = xlsxCSV(path)
				defer os.Remove(path)
				suffix = ".csv"
			}
			name := r.FormValue("table")
			if name == "" {
				name = strings.TrimSuffix(filepath.Base(h.Filename), filepath.Ext(h.Filename))
			}
			sch, table := a.target(c.WS, r.FormValue("target"), name)
			reader := "read_csv_auto"
			if suffix == ".json" || suffix == ".jsonl" || suffix == ".ndjson" {
				reader = "read_json_auto"
			}
			if suffix == ".parquet" || suffix == ".pq" {
				reader = "read_parquet"
			}
			_, e = a.Lake.Exec("CREATE OR REPLACE TABLE " + table + " AS SELECT * FROM " + reader + "('" + strings.ReplaceAll(path, "'", "''") + "')")
			if e != nil {
				fail(400, e.Error())
			}
			t := a.registerTable(c.WS, sch, name, "文件导入："+filepath.Base(h.Filename))
			return M{"table": t, "row_count": t["row_count"], "ref": t["ref"]}
		}},
		{"GET", "/api/sources/templates", true, true, func(c *request) any {
			items := []M{}
			groups := M{}
			for _, v := range arr(metadata["source_templates"]) {
				m := obj(v)
				if c.q("category") != "" && m["category"] != c.q("category") {
					continue
				}
				if c.q("q") != "" && !strings.Contains(strings.ToLower(js(m)), strings.ToLower(c.q("q"))) {
					continue
				}
				items = append(items, m)
				k := str(m["category"])
				groups[k] = append(arr(groups[k]), m)
			}
			out := []M{}
			for k, v := range groups {
				out = append(out, M{"key": k, "label": k, "templates": v, "count": len(arr(v))})
			}
			return M{"templates": items, "groups": out, "categories": metadata["source_categories"], "count": len(items), "total": len(arr(metadata["source_templates"]))}
		}},
		{"GET", "/api/sources/templates/{template_id}", true, true, func(c *request) any {
			for _, v := range arr(metadata["source_templates"]) {
				m := obj(v)
				if m["id"] == c.p("template_id") {
					return M{"template": m, "related": []M{}}
				}
			}
			fail(404, "模板不存在")
			return nil
		}},
		{"GET", "/api/ingest/plans", true, true, func(c *request) any { return metadata["ingest_plans"] }},
		{"POST", "/api/ingest/validate", false, false, func(c *request) any { return a.probe(c.s("type"), obj(c.Body["config"])) }},
		{"POST", "/api/sources/{source_id}/test", false, false, func(c *request) any {
			s := a.owned("sources", c.p("source_id"), c.WS)
			out := a.probe(str(s["type"]), obj(s["config"]))
			status := "failed"
			if out["ok"] == true {
				status = "ok"
			}
			a.update("sources", str(s["id"]), M{"status": status, "last_test_at": now()})
			return out
		}},
		{"POST", "/api/sources/{source_id}/diagnose", false, false, func(c *request) any {
			s := a.owned("sources", c.p("source_id"), c.WS)
			return a.probe(str(s["type"]), obj(s["config"]))
		}},
		{"POST", "/api/sources/diagnose-all", false, false, func(c *request) any {
			rr := []M{}
			sum := float64(0)
			for _, s := range a.list("sources", c.WS) {
				out := a.probe(str(s["type"]), obj(s["config"]))
				out["source_id"] = s["id"]
				sum += num(out["score"])
				rr = append(rr, out)
			}
			if len(rr) > 0 {
				sum /= float64(len(rr))
			}
			return M{"sources": rr, "count": len(rr), "average_score": sum}
		}},
		{"GET", "/api/volumes", false, false, func(c *request) any {
			rr := a.list("volumes", c.WS)
			files, bytes := 0, float64(0)
			for _, v := range rr {
				ff := a.volumeFiles(v)
				size := float64(0)
				for _, f := range ff {
					size += num(f["size"])
				}
				v["file_count"] = len(ff)
				v["size_bytes"] = size
				v["ref"] = str(v["catalog"]) + "." + str(v["schema"]) + "." + str(v["name"])
				files += len(ff)
				bytes += size
			}
			return M{"volumes": rr, "summary": M{"count": len(rr), "files": files, "size_bytes": bytes}}
		}},
		{"POST", "/api/volumes", false, false, func(c *request) any {
			key := id()
			name := validName(c.s("name"))
			path := filepath.Join(a.Dir, "volumes", c.WS, key)
			must(os.MkdirAll(path, 0700))
			v := a.insert("volumes", M{"id": key, "workspace_id": c.WS, "name": name, "catalog": def(c.Body, "catalog", "demo"), "schema": def(c.Body, "schema", "retail"), "path": path, "created_at": now()})
			return M{"volume": v}
		}},
		{"GET", "/api/volumes/{volume_id}/files", false, false, func(c *request) any {
			v := a.owned("volumes", c.p("volume_id"), c.WS)
			return M{"volume": v, "files": a.volumeFiles(v)}
		}},
		{"DELETE", "/api/volumes/{volume_id}", false, false, func(c *request) any {
			v := a.owned("volumes", c.p("volume_id"), c.WS)
			root := filepath.Join(a.Dir, "volumes")
			rel, e := filepath.Rel(root, str(v["path"]))
			if e != nil || strings.HasPrefix(rel, "..") {
				fail(400, "卷目录不在数据目录内")
			}
			must(os.RemoveAll(str(v["path"])))
			a.exec("DELETE FROM volumes WHERE id=?", v["id"])
			return M{"ok": true, "deleted_files": true}
		}},
		{"POST", "/api/volumes/upload", false, false, func(c *request) any {
			r := c.R.Request
			if e := r.ParseMultipartForm(8 << 20); e != nil {
				fail(422, "上传表单无效")
			}
			if r.MultipartForm != nil {
				defer r.MultipartForm.RemoveAll()
			}
			v := a.owned("volumes", r.FormValue("volume_id"), c.WS)
			f, h, e := r.FormFile("file")
			if e != nil {
				fail(422, "缺少文件")
			}
			defer f.Close()
			name := filepath.Base(h.Filename)
			if name == "." || name == ".." {
				fail(422, "文件名无效")
			}
			dest, e := os.OpenFile(filepath.Join(str(v["path"]), name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
			if e != nil {
				fail(409, "文件已存在或不可写")
			}
			defer dest.Close()
			n, e := io.Copy(dest, io.LimitReader(f, 128<<20))
			must(e)
			return M{"ok": true, "name": name, "size": n}
		}},
		{"GET", "/api/functions", false, false, func(c *request) any {
			return M{"functions": a.rows("SELECT f.* FROM functions f JOIN schemas s ON s.id=f.schema_id JOIN catalogs c ON c.id=s.catalog_id WHERE c.workspace_id=?", c.WS), "kinds": []string{"udf", "view", "macro"}}
		}},
		{"POST", "/api/functions", false, false, func(c *request) any {
			sch, _ := a.target(c.WS, str(def(c.Body, "catalog", "demo"))+"."+str(def(c.Body, "schema", "retail")), c.s("name"))
			r := a.insert("functions", M{"id": id(), "schema_id": sch["id"], "name": c.s("name"), "kind": def(c.Body, "kind", "udf"), "body": c.s("body"), "description": c.s("description"), "created_at": now()})
			return M{"function": r}
		}},
		{"DELETE", "/api/functions/{function_id}", false, false, func(c *request) any {
			r := a.row("SELECT f.id FROM functions f JOIN schemas s ON s.id=f.schema_id JOIN catalogs c ON c.id=s.catalog_id WHERE c.workspace_id=? AND f.id=?", c.WS, c.p("function_id"))
			if r == nil {
				fail(404, "函数不存在")
			}
			a.exec("DELETE FROM functions WHERE id=?", r["id"])
			return M{"ok": true}
		}},
		{"POST", "/api/external-catalogs/{catalog_id}/test", false, false, func(c *request) any {
			r := a.owned("external_catalogs", c.p("catalog_id"), c.WS)
			return a.probe(str(r["type"]), obj(r["config"]))
		}},
	}
}
