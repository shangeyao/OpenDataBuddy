package platform

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ServeMCP exposes native Go tools using newline-delimited JSON-RPC over stdio.
// A console session token is required; calls requiring approval are denied.
func (a *App) ServeMCP(input io.Reader, output io.Writer) error {
	token := env("DATABUDDY_TOKEN", "")
	u := a.row("SELECT u.*,s.expires_at AS session_expires FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=?", token)
	if u == nil {
		return fmt.Errorf("请先设置有效的 DATABUDDY_TOKEN")
	}
	expires, e := time.Parse(time.RFC3339, str(u["session_expires"]))
	if e != nil || !expires.After(time.Now()) {
		return fmt.Errorf("DATABUDDY_TOKEN 已过期")
	}
	delete(u, "session_expires")
	c := &request{A: a, User: publicUser(u), WS: a.workspace(u, env("DATABUDDY_WORKSPACE_ID", "")), Body: M{}}
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	writer := json.NewEncoder(output)
	for scanner.Scan() {
		var msg M
		if e := json.Unmarshal(scanner.Bytes(), &msg); e != nil {
			if e := writer.Encode(M{"jsonrpc": "2.0", "id": nil, "error": M{"code": -32700, "message": "Parse error"}}); e != nil {
				return e
			}
			continue
		}
		if _, ok := msg["id"]; !ok {
			continue
		}
		response := a.mcpMessage(c, msg)
		if e := writer.Encode(response); e != nil {
			return e
		}
	}
	return scanner.Err()
}
func (a *App) mcpMessage(c *request, msg M) (response M) {
	response = M{"jsonrpc": "2.0", "id": msg["id"]}
	defer func() {
		if e := recover(); e != nil {
			delete(response, "result")
			response["error"] = M{"code": -32603, "message": fmt.Sprint(e)}
		}
	}()
	params := obj(msg["params"])
	switch str(msg["method"]) {
	case "initialize":
		response["result"] = M{"protocolVersion": "2024-11-05", "capabilities": M{"tools": M{"listChanged": false}}, "serverInfo": M{"name": "opendatabuddy", "version": "0.4.0"}}
	case "ping":
		response["result"] = M{}
	case "tools/list":
		tt := []M{}
		for _, v := range arr(metadata["tool_schemas"]) {
			f := obj(obj(v)["function"])
			tt = append(tt, M{"name": f["name"], "description": f["description"], "inputSchema": f["parameters"]})
		}
		response["result"] = M{"tools": tt}
	case "tools/call":
		name := str(params["name"])
		args := obj(params["arguments"])
		if a.toolApproval(c.WS, name, args) {
			response["result"] = M{"content": []M{{"type": "text", "text": "此操作需要人工审批，请到 Buddy 控制台执行。"}}, "isError": true}
		} else {
			out := a.invoke(c, "mcp", name, args)
			text := str(checkOutput(js(out))["text"])
			response["result"] = M{"content": []M{{"type": "text", "text": text}}, "isError": out["error"] != nil}
			a.audit(c.WS, str(c.User["id"]), "mcp_call", name)
		}
	default:
		response["error"] = M{"code": -32601, "message": "Method not found"}
	}
	return
}

// BridgeMCP keeps stdio clients independent while the HTTP process owns DuckDB.
// All authentication, tools, guardrails and business execution stay in GoFrame.
func BridgeMCP(input io.Reader, output io.Writer, base, token, workspace string) error {
	if token == "" {
		return fmt.Errorf("请设置 DATABUDDY_TOKEN")
	}
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	encoder := json.NewEncoder(output)
	client := &http.Client{Timeout: 120 * time.Second}
	for scanner.Scan() {
		var msg M
		if e := json.Unmarshal(scanner.Bytes(), &msg); e != nil {
			if e := encoder.Encode(M{"jsonrpc": "2.0", "id": nil, "error": M{"code": -32700, "message": "Parse error"}}); e != nil {
				return e
			}
			continue
		}
		if _, ok := msg["id"]; !ok {
			continue
		}
		req, e := http.NewRequest("POST", strings.TrimRight(base, "/")+"/api/mcp", strings.NewReader(js(msg)))
		if e != nil {
			return e
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+token)
		if workspace != "" {
			req.Header.Set("X-Workspace-Id", workspace)
		}
		response, e := client.Do(req)
		var result M
		if e != nil {
			result = M{"jsonrpc": "2.0", "id": msg["id"], "error": M{"code": -32603, "message": "无法连接 GoFrame 服务，请先启动控制台 HTTP 服务"}}
		} else {
			body, readErr := io.ReadAll(io.LimitReader(response.Body, 4<<20))
			response.Body.Close()
			if readErr != nil {
				return readErr
			}
			if response.StatusCode != 200 {
				result = M{"jsonrpc": "2.0", "id": msg["id"], "error": M{"code": -32603, "message": fmt.Sprintf("GoFrame 返回 HTTP %d，请检查身份与工作空间", response.StatusCode)}}
			} else if e = json.Unmarshal(body, &result); e != nil {
				return e
			}
		}
		if e := encoder.Encode(result); e != nil {
			return e
		}
	}
	return scanner.Err()
}
func (a *App) mcpRoutes() []route {
	return []route{{"POST", "/api/mcp", false, false, func(c *request) any { return a.mcpMessage(c, c.Body) }}}
}
