package platform

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Python is an execution kernel only: no HTTP, SQL, metadata or business imports.
type pythonKernel struct {
	mu  sync.Mutex
	cmd *exec.Cmd
	in  io.WriteCloser
	out *bufio.Scanner
}

var lastQueries sync.Map

var kernels = struct {
	sync.Mutex
	m map[string]*pythonKernel
}{m: map[string]*pythonKernel{}}

func stopKernel(key string) {
	kernels.Lock()
	k := kernels.m[key]
	delete(kernels.m, key)
	kernels.Unlock()
	if k != nil {
		k.mu.Lock()
		defer k.mu.Unlock()
		if k.cmd != nil {
			k.cmd.Process.Kill()
			k.cmd.Wait()
			k.cmd = nil
		}
	}
}
func (a *App) python(key, source string, params M) M {
	a.ownedKernels.Store(key, true)
	kernels.Lock()
	k := kernels.m[key]
	if k == nil {
		k = &pythonKernel{}
		kernels.m[key] = k
	}
	kernels.Unlock()
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.cmd == nil {
		cmd := exec.Command(env("DATABUDDY_PYTHON", "python3"), "-I", "-u", filepath.Join(a.Root, "scripts", "python_kernel.py"))
		in, e := cmd.StdinPipe()
		if e != nil {
			fail(500, e.Error())
		}
		out, e := cmd.StdoutPipe()
		must(e)
		cmd.Stderr = io.Discard
		cmd.Dir = a.notebookDir(key)
		must(os.MkdirAll(cmd.Dir, 0700))
		if e = cmd.Start(); e != nil {
			fail(503, "Python 执行器不可用："+e.Error())
		}
		k.cmd = cmd
		k.in = in
		k.out = bufio.NewScanner(out)
		k.out.Buffer(make([]byte, 4096), 2<<20)
	}
	_, e := fmt.Fprintln(k.in, js(M{"source": source, "params": params}))
	must(e)
	type result struct {
		line string
		err  error
	}
	ch := make(chan result, 1)
	go func() {

		for k.out.Scan() {
			line := k.out.Text()
			var message M
			if json.Unmarshal([]byte(line), &message) == nil && message["rpc"] != nil {
				answer := a.pythonRPC(strings.Split(key, ":")[0], message)
				if _, e := fmt.Fprintln(k.in, js(answer)); e != nil {
					ch <- result{"", e}
					return
				}
				continue
			}
			ch <- result{line, nil}
			return
		}
		ch <- result{"", k.out.Err()}

	}()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	select {
	case r := <-ch:
		if r.err != nil || r.line == "" {
			k.cmd.Process.Kill()
			k.cmd.Wait()
			k.cmd = nil
			fail(500, "Python 执行进程已退出")
		}
		var out M
		must(json.Unmarshal([]byte(r.line), &out))
		return out
	case <-ctx.Done():
		k.cmd.Process.Kill()
		k.cmd.Wait()
		k.cmd = nil
		return M{"status": "error", "error": "执行超过 30 秒，内核已重置", "stdout": ""}
	}
}
func (a *App) runCell(ws, nbid string, cell M, params M, depth int) (result M) {
	defer func() {
		if e := recover(); e != nil {
			result = M{"status": "error", "error": fmt.Sprint(e), "output": ""}
		}
	}()
	if depth > 8 {
		fail(400, "Notebook 引用超过 8 层")
	}
	source := str(cell["source"])
	typ := str(cell["type"])
	if strings.HasPrefix(strings.TrimSpace(source), "%") {
		line, rest, _ := strings.Cut(strings.TrimSpace(source), "\n")
		command, args, _ := strings.Cut(line, " ")
		source = strings.TrimSpace(args + "\n" + rest)
		switch command {
		case "%sql":
			typ = "sql"
		case "%python":
			typ = "python"
		case "%md":
			typ = "markdown"
		default:
			typ = "magic"
		}
		_ = command
	}
	start := time.Now()
	var out M
	if typ == "magic" {
		first := strings.Fields(strings.TrimSpace(str(cell["source"])))[0]
		switch first {
		case "%help":
			out = M{"magics": metadata["magics"]}
		case "%tables":
			out = M{"tables": a.tables(ws)}
		case "%desc":
			out = M{"columns": a.rows("SELECT * FROM columns WHERE table_id=? ORDER BY ordinal", a.resolve(ws, source)["id"])}
		case "%clear":
			stopKernel(ws + ":" + nbid)
			out = M{"stdout": "内核已重置"}
		case "%vars":
			out = a.python(ws+":"+nbid, "", params)
		case "%param":
			for _, token := range regexp.MustCompile(`[A-Za-z_][\w.]*=(?:'[^']*'|"[^"]*"|\S+)`).FindAllString(source, -1) {
				name, value, _ := strings.Cut(token, "=")
				var parsed any
				if json.Unmarshal([]byte(value), &parsed) != nil {
					parsed = strings.Trim(value, "\"'")
				}
				params[name] = parsed
			}
			if n := a.row("SELECT id FROM notebooks WHERE id=? AND workspace_id=?", nbid, ws); n != nil {
				a.update("notebooks", nbid, M{"params": params})
			}
			out = M{"params": params}
		case "%time":
			return a.runCell(ws, nbid, M{"source": source, "type": cell["type"]}, params, depth+1)
		case "%chart":
			if value, ok := lastQueries.Load(ws + ":" + nbid); ok {
				out = M{}
				for k, v := range obj(value) {
					out[k] = v
				}
				spec := chart(out)
				if strings.Contains("|line|bar|pie|kpi|table|", "|"+strings.TrimSpace(source)+"|") {
					spec["type"] = strings.TrimSpace(source)
				}
				out["chart"] = spec
			} else {
				fail(400, "请先运行一次 SQL 查询")
			}
		case "%run":
			n := a.row("SELECT * FROM notebooks WHERE workspace_id=? AND (id=? OR name=?)", ws, strings.TrimSpace(source), strings.TrimSpace(source))
			if n == nil {
				fail(404, "引用 Notebook 不存在")
			}
			out = a.runNotebook(ws, n, params, depth+1)
		case "%load":
			dir := a.notebookDir(ws + ":" + nbid)
			path := filepath.Join(dir, strings.Trim(strings.TrimSpace(source), "\"'"))
			rel, e := filepath.Rel(dir, path)
			if e != nil || strings.HasPrefix(rel, "..") {
				fail(400, "文件必须位于 Notebook 工作目录内")
			}
			f, e := os.Open(path)
			if e != nil {
				fail(404, "文件不存在")
			}
			defer f.Close()
			b, e := io.ReadAll(io.LimitReader(f, 2<<20))
			must(e)
			out = M{"text": string(b), "stdout": string(b)}
		default:
			fail(400, "未知魔法命令")
		}
	} else {
		switch typ {
		case "python":
			out = a.python(ws+":"+nbid, source, params)
		case "markdown", "md":
			out = M{"text": source, "markdown": source}
		default:
			for k, v := range params {
				source = strings.ReplaceAll(source, "{{"+k+"}}", "'"+strings.ReplaceAll(str(v), "'", "''")+"'")
			}
			out = a.query(ws, source, 1000)
			out["chart"] = chart(out)
			snapshot := M{}
			for k, v := range out {
				snapshot[k] = v
			}
			lastQueries.Store(ws+":"+nbid, snapshot)
		}
	}
	if out["status"] == nil {
		out["status"] = "success"
	}
	out["elapsed_ms"] = time.Since(start).Milliseconds()
	out["type"] = typ
	text := str(out["stdout"])
	if out["result"] != nil {
		text += str(out["result"])
	}
	if typ == "markdown" {
		text = source
	}
	if typ == "magic" && text == "" {
		text = js(out)
	}
	out["output"] = text
	return out
}
func (a *App) runNotebook(ws string, n M, params M, depth int) M {
	cells := arr(n["cells"])
	defaults := obj(n["params"])
	for k, v := range params {
		defaults[k] = v
	}
	outputs := []M{}
	status := "success"
	for _, v := range cells {
		cell := obj(v)
		out := a.runCell(ws, str(n["id"]), cell, defaults, depth)
		for _, k := range []string{"output", "status", "error", "elapsed_ms", "columns", "rows", "chart"} {
			cell[k] = out[k]
		}
		outputs = append(outputs, out)
		a.update("notebooks", str(n["id"]), M{"cells": cells, "updated_at": now()})
		if out["status"] == "error" {
			status = "failed"
			break
		}
	}
	a.update("notebooks", str(n["id"]), M{"cells": cells, "updated_at": now()})
	failed := 0
	for _, out := range outputs {
		if out["status"] == "error" {
			failed++
		}
	}
	return M{"ok": status == "success", "failed": failed, "elapsed_ms": 0, "status": status, "outputs": outputs, "results": outputs, "notebook": decoded(a.owned("notebooks", str(n["id"]), ws))}
}
func (a *App) notebookRoutes() []route {
	r := []route{
		{"GET", "/api/notebooks", false, false, func(c *request) any {
			rr := a.list("notebooks", c.WS)
			count := 0
			for _, n := range rr {
				decoded(n)
				n["cell_count"] = len(arr(n["cells"]))
				count += len(arr(n["cells"]))
			}
			return M{"notebooks": rr, "summary": M{"total": len(rr), "cells": count}}
		}},
		{"POST", "/api/notebooks", false, false, func(c *request) any {
			cells := arr(c.Body["cells"])
			for _, v := range cells {
				cell := obj(v)
				if str(cell["id"]) == "" {
					cell["id"] = id()
				}
				if cell["type"] == nil {
					cell["type"] = "sql"
				}
			}
			n := a.insert("notebooks", M{"id": id(), "workspace_id": c.WS, "name": def(c.Body, "name", "未命名 Notebook"), "description": c.s("description"), "cells": cells, "params": obj(c.Body["params"]), "workdir": c.s("workdir"), "created_at": now(), "updated_at": now()})
			return M{"notebook": decoded(n)}
		}},
		{"GET", "/api/notebooks/magic-help", true, true, func(c *request) any {
			return M{"magics": metadata["magics"], "count": len(arr(metadata["magics"])), "categories": []string{"query", "python", "document", "utility"}, "shortcuts": []M{}}
		}},
		{"GET", "/api/notebooks/{notebook_id}", false, false, func(c *request) any {
			n := decoded(a.owned("notebooks", c.p("notebook_id"), c.WS))
			return M{"notebook": n, "params": n["params"], "workdir": a.notebookDir(c.WS + ":" + c.p("notebook_id")), "magics": metadata["magics"]}
		}},
		{"PATCH", "/api/notebooks/{notebook_id}", false, false, func(c *request) any {
			a.owned("notebooks", c.p("notebook_id"), c.WS)
			m := M{}
			for _, k := range []string{"name", "description", "cells", "params", "workdir"} {
				if v, ok := c.Body[k]; ok {
					m[k] = v
				}
			}
			m["updated_at"] = now()
			a.update("notebooks", c.p("notebook_id"), m)
			return M{"notebook": decoded(a.owned("notebooks", c.p("notebook_id"), c.WS))}
		}},
		{"DELETE", "/api/notebooks/{notebook_id}", false, false, func(c *request) any {
			a.owned("notebooks", c.p("notebook_id"), c.WS)
			stopKernel(c.WS + ":" + c.p("notebook_id"))
			a.exec("DELETE FROM notebooks WHERE id=?", c.p("notebook_id"))
			return M{"ok": true}
		}},
		{"POST", "/api/notebooks/{notebook_id}/cells", false, false, func(c *request) any {
			n := a.owned("notebooks", c.p("notebook_id"), c.WS)
			cc := arr(n["cells"])
			cell := M{"id": id(), "type": def(c.Body, "type", "sql"), "source": c.s("source"), "status": "idle", "output": nil}
			index := len(cc)
			if v, ok := c.Body["index"]; ok {
				index = int(num(v))
			}
			if index < 0 || index > len(cc) {
				fail(422, "Cell 索引超出范围")
			}
			cc = append(cc, nil)
			copy(cc[index+1:], cc[index:])
			cc[index] = cell
			a.update("notebooks", str(n["id"]), M{"cells": cc, "updated_at": now()})
			return M{"cell": cell, "notebook": decoded(a.owned("notebooks", str(n["id"]), c.WS))}
		}},
		{"POST", "/api/notebooks/{notebook_id}/run-all", false, false, func(c *request) any {
			return a.runNotebook(c.WS, a.owned("notebooks", c.p("notebook_id"), c.WS), obj(c.Body["params"]), 0)
		}},
		{"POST", "/api/notebooks/{notebook_id}/reset", false, false, func(c *request) any {
			a.owned("notebooks", c.p("notebook_id"), c.WS)
			stopKernel(c.WS + ":" + c.p("notebook_id"))
			return M{"ok": true}
		}},
		{"POST", "/api/sql/explain", false, false, func(c *request) any { return a.query(c.WS, "EXPLAIN "+guard(c.s("sql")), 1000) }},
	}
	for _, method := range []string{"PATCH", "DELETE", "POST"} {
		method := method
		path := "/api/notebooks/{notebook_id}/cells/{cell_id}"
		if method == "POST" {
			path += "/run"
		}
		r = append(r, route{method, path, false, false, func(c *request) any {
			n := a.owned("notebooks", c.p("notebook_id"), c.WS)
			cc := arr(n["cells"])
			idx := -1
			for i, v := range cc {
				if obj(v)["id"] == c.p("cell_id") {
					idx = i
					break
				}
			}
			if idx < 0 {
				fail(404, "Cell 不存在")
			}
			cell := obj(cc[idx])
			var output any
			if method == "DELETE" {
				cc = append(cc[:idx], cc[idx+1:]...)
			} else {
				for _, k := range []string{"type", "source"} {
					if v, ok := c.Body[k]; ok {
						cell[k] = v
					}
				}
				if method == "POST" {
					params := obj(n["params"])
					for k, v := range obj(c.Body["params"]) {
						params[k] = v
					}
					out := a.runCell(c.WS, str(n["id"]), cell, params, 0)
					for _, k := range []string{"output", "status", "error", "elapsed_ms", "columns", "rows", "chart"} {
						cell[k] = out[k]
					}
					output = out
				}
			}
			a.update("notebooks", str(n["id"]), M{"cells": cc, "updated_at": now()})
			return M{"ok": true, "cell": cell, "result": output, "output": output, "notebook": decoded(a.owned("notebooks", str(n["id"]), c.WS))}
		}})
	}
	return r
}

func (a *App) notebookDir(key string) string {
	ws, nbid, _ := strings.Cut(key, ":")
	if n := a.row("SELECT workdir FROM notebooks WHERE id=? AND workspace_id=?", nbid, ws); n != nil && str(n["workdir"]) != "" {
		path := str(n["workdir"])
		if !filepath.IsAbs(path) {
			path = filepath.Join(a.Root, path)
		}
		return path
	}
	safe := strings.NewReplacer(":", "_", "/", "_", "\\", "_").Replace(key)
	return filepath.Join(a.Dir, "notebooks", safe)
}
func (a *App) pythonRPC(ws string, m M) (out M) {
	defer func() {
		if e := recover(); e != nil {
			out = M{"error": fmt.Sprint(e)}
		}
	}()
	switch m["rpc"] {
	case "query":
		return a.query(ws, str(m["sql"]), int(num(def(m, "limit", 200))))
	case "tables":
		return M{"tables": a.tables(ws)}
	case "describe":
		t := a.resolve(ws, str(m["table"]))
		return M{"columns": a.rows("SELECT * FROM columns WHERE table_id=? ORDER BY ordinal", t["id"])}
	default:
		return M{"error": "未知内核回调"}
	}
}
