package platform

import (
	"strings"
	"time"
)

func (a *App) authRoutes() []route {
	return []route{
		{"GET", "/api/health", true, true, func(c *request) any {
			return M{"status": "ok", "version": "0.4.0", "framework": "GoFrame", "framework_version": "v2.10.4", "auth": "local", "billing": "disabled", "capabilities": extra}
		}},
		{"GET", "/api/capabilities", true, true, func(c *request) any { return M{"core": core, "extra": extra, "missing": []string{}} }},
		{"POST", "/api/auth/login", true, true, func(c *request) any {
			u := a.row("SELECT * FROM users WHERE lower(email)=lower(?)", strings.TrimSpace(c.s("email")))
			if u == nil || !verify(c.s("password"), str(u["password_hash"])) {
				fail(401, "邮箱或密码不正确")
			}
			token := id() + id()
			a.insert("sessions", M{"token": token, "user_id": u["id"], "created_at": now(), "expires_at": time.Now().UTC().Add(time.Duration(mathMax(1, num(env("DATABUDDY_SESSION_TTL_HOURS", "720")))) * time.Hour).Format(time.RFC3339)})
			a.audit("", str(u["id"]), "login", c.s("email"))
			return M{"token": token, "user": publicUser(u), "workspaces": a.workspaces(u)}
		}},
		{"GET", "/api/auth/me", false, true, func(c *request) any { return M{"user": c.User, "workspaces": a.workspaces(c.User)} }},
		{"POST", "/api/auth/logout", false, true, func(c *request) any {
			a.exec("DELETE FROM sessions WHERE token=?", bearer(c.R))
			return M{"ok": true}
		}},
		{"POST", "/api/auth/password", false, true, func(c *request) any {
			u := a.row("SELECT * FROM users WHERE id=?", c.User["id"])
			if !verify(c.s("old_password"), str(u["password_hash"])) {
				fail(400, "原密码不正确")
			}
			if len(c.s("new_password")) < 6 {
				fail(422, "密码至少 6 位")
			}
			a.update("users", str(u["id"]), M{"password_hash": hash(c.s("new_password"))})
			a.exec("DELETE FROM sessions WHERE user_id=?", u["id"])
			return M{"ok": true}
		}},
		{"GET", "/api/users", false, true, func(c *request) any {
			return M{"users": a.rows("SELECT id,email,name,role,created_at FROM users ORDER BY created_at")}
		}},
		{"POST", "/api/users", false, true, func(c *request) any {
			c.admin()
			if c.s("email") == "" || len(c.s("password")) < 6 {
				fail(422, "邮箱和至少 6 位密码为必填项")
			}
			if a.row("SELECT id FROM users WHERE lower(email)=lower(?)", c.s("email")) != nil {
				fail(400, "邮箱已存在")
			}
			role := c.s("role")
			if role != "admin" {
				role = "member"
			}
			u := a.insert("users", M{"id": id(), "email": c.s("email"), "name": def(c.Body, "name", c.s("email")), "password_hash": hash(c.s("password")), "role": role, "created_at": now()})
			return M{"user": publicUser(u)}
		}},
		{"DELETE", "/api/users/{user_id}", false, true, func(c *request) any {
			c.admin()
			if c.p("user_id") == c.User["id"] {
				fail(400, "不能删除当前账号")
			}
			a.exec("DELETE FROM sessions WHERE user_id=?", c.p("user_id"))
			a.exec("DELETE FROM workspace_members WHERE user_id=?", c.p("user_id"))
			a.exec("DELETE FROM users WHERE id=?", c.p("user_id"))
			return M{"ok": true}
		}},
		{"GET", "/api/workspaces", false, true, func(c *request) any { return M{"workspaces": a.workspaces(c.User)} }},
		{"POST", "/api/workspaces", false, true, func(c *request) any {
			if strings.TrimSpace(c.s("name")) == "" {
				fail(422, "请输入工作空间名称")
			}
			w := a.insert("workspaces", M{"id": id(), "name": c.s("name"), "description": def(c.Body, "description", ""), "owner_id": c.User["id"], "created_at": now()})
			a.insert("workspace_members", M{"workspace_id": w["id"], "user_id": c.User["id"], "role": "admin"})
			return M{"workspace": w}
		}},
		{"PATCH", "/api/workspaces/{ws_id}", false, true, func(c *request) any {
			a.workspace(c.User, c.p("ws_id"))
			if c.User["role"] != "admin" && a.row("SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=? AND role='admin'", c.p("ws_id"), c.User["id"]) == nil {
				fail(403, "需要工作空间管理员权限")
			}
			a.update("workspaces", c.p("ws_id"), M{"name": c.s("name"), "description": c.s("description")})
			return M{"ok": true}
		}},
		{"GET", "/api/workspaces/{ws_id}/members", false, true, func(c *request) any {
			a.workspace(c.User, c.p("ws_id"))
			return M{"members": a.rows("SELECT u.id,u.email,u.name,m.role,u.role AS global_role,m.role AS space_role FROM users u JOIN workspace_members m ON m.user_id=u.id WHERE m.workspace_id=?", c.p("ws_id")), "users": a.rows("SELECT id,name,email FROM users")}
		}},
		{"POST", "/api/workspaces/{ws_id}/members", false, true, func(c *request) any {
			c.admin()
			u := c.s("user_id")
			if u == "" {
				r := a.row("SELECT id FROM users WHERE email=?", c.s("email"))
				u = str(r["id"])
			}
			if a.row("SELECT id FROM users WHERE id=?", u) == nil {
				fail(404, "用户不存在")
			}
			role := c.s("role")
			if role != "admin" {
				role = "member"
			}
			a.exec("INSERT INTO workspace_members(workspace_id,user_id,role) VALUES (?,?,?) ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role", c.p("ws_id"), u, role)
			return M{"ok": true}
		}},
		{"DELETE", "/api/workspaces/{ws_id}/members/{user_id}", false, true, func(c *request) any {
			c.admin()
			a.exec("DELETE FROM workspace_members WHERE workspace_id=? AND user_id=?", c.p("ws_id"), c.p("user_id"))
			return M{"ok": true}
		}},
		{"GET", "/api/settings", false, true, func(c *request) any { return a.settings() }},
		{"PUT", "/api/settings", false, true, func(c *request) any {
			c.admin()
			for _, k := range []string{"provider", "base_url", "model", "temperature", "api_key"} {
				if v, ok := c.Body[k]; ok && v != "" {
					a.setSetting("llm."+k, v)
				}
			}
			if v, ok := c.Body["workspace_rule"]; ok {
				a.setSetting("workspace_rule", v)
			}
			return a.settings()
		}},
		{"GET", "/api/ops/audit", false, false, func(c *request) any {
			return M{"logs": a.rows("SELECT * FROM audit_logs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 100", c.WS)}
		}},
		{"GET", "/api/ops/traces", false, false, func(c *request) any { return a.traces(c.WS) }},
	}
}
func (a *App) settings() M {
	key := a.llm("api_key", "")
	masked := ""
	if key != "" {
		masked = "已配置"
	}
	return M{"llm": M{"provider": a.llm("provider", "deepseek"), "base_url": a.llm("base_url", "https://api.deepseek.com/v1"), "model": a.llm("model", "deepseek-chat"), "temperature": 0.3, "api_key_masked": masked, "configured": key != ""}, "workspace_rule": a.setting("workspace_rule"), "platform": M{"version": "0.4.0", "framework": "GoFrame v2.10.4", "data_dir": a.Dir, "auth": "local", "billing": "disabled"}, "providers": []M{{"id": "deepseek", "label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}, {"id": "ollama", "label": "Ollama 本地", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:7b"}}}
}
func (a *App) llm(k, d string) string {
	if v := a.setting("llm." + k); v != "" {
		return v
	}
	return env("LLM_"+strings.ToUpper(k), env("DEEPSEEK_"+strings.ToUpper(k), d))
}
func (a *App) traces(ws string) M {
	return M{"calls": len(a.list("traces", ws)), "tools": a.rows("SELECT tool,count(*) AS calls,avg(CASE WHEN ok=1 THEN 1.0 ELSE 0 END) AS success_rate,avg(duration_ms) AS avg_ms FROM traces WHERE workspace_id=? GROUP BY tool", ws), "recent": a.rows("SELECT * FROM traces WHERE workspace_id=? ORDER BY created_at DESC LIMIT 50", ws)}
}
