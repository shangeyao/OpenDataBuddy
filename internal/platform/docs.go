package platform

import (
	"encoding/json"
)

func (a *App) docsRoutes() []route {
	return []route{
		{"GET", "/api/openapi.json", true, true, func(c *request) any {
			b, e := assets.ReadFile("assets/openapi.json")
			must(e)
			var doc M
			must(json.Unmarshal(b, &doc))
			info := obj(doc["info"])
			info["version"] = "0.4.0"
			info["description"] = "OpenDataBuddy · GoFrame v2.10.4 原生后端。保留 v0.3 接口契约。"
			paths := obj(doc["paths"])
			for _, r := range a.routesWithoutDocs() {
				p := obj(paths[r.Path])
				if len(p) == 0 {
					p = M{}
				}
				method := r.Method
				switch method {
				case "GET":
					method = "get"
				case "POST":
					method = "post"
				case "PATCH":
					method = "patch"
				case "PUT":
					method = "put"
				case "DELETE":
					method = "delete"
				}
				if p[method] == nil {
					p[method] = M{"responses": M{"200": M{"description": "成功"}}}
				}
				paths[r.Path] = p
			}
			return doc
		}},
		{"GET", "/api/docs", true, true, func(c *request) any {
			c.R.Response.Header().Set("Content-Type", "text/html; charset=utf-8")
			c.R.Response.Write(docsHTML)
			return nil
		}},
	}
}

const docsHTML = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>OpenDataBuddy API</title><style>body{font:14px system-ui;margin:40px;color:#242424}a{color:#0052d9}table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}input{padding:10px;width:420px;margin:20px 0}code{color:#0052d9}</style><h1>OpenDataBuddy API</h1><p>GoFrame v2.10.4 · <a href="/api/openapi.json">OpenAPI JSON</a> · <a href="/">控制台</a></p><p>受保护接口使用 Authorization: Bearer TOKEN；工作空间通过 X-Workspace-Id 指定。</p><input id="filter" placeholder="筛选路径或方法"><table><thead><tr><th>方法</th><th>路径</th><th>说明</th></tr></thead><tbody id="rows"></tbody></table><script>fetch('/api/openapi.json').then(r=>r.json()).then(doc=>{const rows=[];for(const [path,methods] of Object.entries(doc.paths)){for(const [method,spec] of Object.entries(methods)){if(!['get','post','patch','put','delete'].includes(method))continue;rows.push([method.toUpperCase(),path,spec.summary||'']);}}const render=()=>{const q=document.querySelector('#filter').value.toLowerCase();const host=document.querySelector('#rows');host.replaceChildren();for(const row of rows){if(!row.join(' ').toLowerCase().includes(q))continue;const tr=document.createElement('tr');for(const value of row){const td=document.createElement('td');td.textContent=value;tr.append(td)}host.append(tr)}};document.querySelector('#filter').addEventListener('input',render);render()});</script></html>`
