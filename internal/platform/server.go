package platform

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/gogf/gf/v2/net/ghttp"
	"golang.org/x/crypto/pbkdf2"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type apiError struct {
	Status int
	Detail string
}

func fail(status int, detail string) { panic(apiError{status, detail}) }

type request struct {
	R      *ghttp.Request
	A      *App
	User   M
	WS     string
	Body   M
	Params map[string]string
}

func (c *request) p(k string) string { return c.Params[k] }
func (c *request) q(k string) string { return c.R.URL.Query().Get(k) }
func (c *request) s(k string) string { return str(c.Body[k]) }
func (c *request) admin() {
	if c.User["role"] != "admin" {
		fail(403, "需要管理员权限")
	}
}
func hash(p string) string {
	salt := id()
	return "pbkdf2$" + salt + "$" + hex.EncodeToString(pbkdf2.Key([]byte(p), []byte(salt), 120000, 32, sha256.New))
}
func verify(p, stored string) bool {
	v := strings.Split(stored, "$")
	if len(v) != 3 || v[0] != "pbkdf2" {
		return false
	}
	calc := hex.EncodeToString(pbkdf2.Key([]byte(p), []byte(v[1]), 120000, 32, sha256.New))
	return hmac.Equal([]byte(calc), []byte(v[2]))
}
func publicUser(u M) M {
	o := M{}
	for k, v := range u {
		if k != "password_hash" {
			o[k] = v
		}
	}
	return o
}
func (a *App) workspaces(u M) []M {
	if u["role"] == "admin" {
		return a.rows("SELECT *, 'admin' AS role FROM workspaces ORDER BY created_at")
	}
	return a.rows("SELECT w.*,m.role FROM workspaces w JOIN workspace_members m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.created_at", u["id"])
}
func (a *App) authenticate(r *ghttp.Request) M {
	token := bearer(r)
	if token == "" {
		if c, e := r.Request.Cookie("db_token"); e == nil {
			token = c.Value
		}
	}
	u := a.row("SELECT u.*,s.expires_at AS session_expires FROM users u JOIN sessions s ON u.id=s.user_id WHERE s.token=?", token)
	if u != nil {
		expires, e := time.Parse(time.RFC3339, str(u["session_expires"]))
		if e != nil || !expires.After(time.Now()) {
			u = nil
		} else {
			delete(u, "session_expires")
		}
	}
	if u == nil {
		fail(401, "未登录或会话已过期")
	}
	return publicUser(u)
}
func (a *App) workspace(u M, w string) string {
	if w != "" {
		if a.row("SELECT id FROM workspaces WHERE id=?", w) == nil {
			fail(404, "工作空间不存在")
		}
		if u["role"] != "admin" && a.row("SELECT 1 FROM workspace_members WHERE workspace_id=? AND user_id=?", w, u["id"]) == nil {
			fail(403, "无权访问该工作空间")
		}
		return w
	}
	ww := a.workspaces(u)
	if len(ww) == 0 {
		fail(400, "尚未创建工作空间")
	}
	return str(ww[0]["id"])
}

type Handler func(*request) any

// auditRead marks the read endpoints that expose data or run statements, so
// data access leaves a trace next to the write operations.
func auditRead(path string) bool {
	for _, p := range []string{"/api/sql/run", "/api/catalog/preview", "/api/analysis/ask", "/api/analysis/attribute", "/api/analysis/dashboard/generate", "/api/quality/profile", "/api/governance/scan", "/api/serving/services/", "/api/knowledge-search"} {
		if strings.Contains(path, p) {
			return true
		}
	}
	return false
}

type route struct {
	Method, Path   string
	Public, Global bool
	Handler        Handler
}

func (a *App) route(s *ghttp.Server, rt route) {
	s.BindHandler(rt.Method+":"+rt.Path, func(r *ghttp.Request) {
		defer func() {
			if e := recover(); e != nil {
				status := 500
				detail := "内部服务错误"
				if x, ok := e.(apiError); ok {
					status = x.Status
					detail = x.Detail
				} else {
					fmt.Fprintf(os.Stderr, "%s %s: %v\n", r.Method, r.URL.Path, e)
				}
				r.Response.ClearBuffer()
				r.Response.WriteStatus(status, M{"detail": detail})
			}
		}()
		c := &request{R: r, A: a, Body: M{}, Params: map[string]string{}}
		for _, p := range strings.Split(rt.Path, "/") {
			if strings.HasPrefix(p, "{") {
				k := strings.Trim(p, "{}")
				c.Params[k] = r.Get(k).String()
			}
		}
		if !rt.Public {
			c.User = a.authenticate(r)
			if !rt.Global {
				c.WS = a.workspace(c.User, r.Header.Get("X-Workspace-Id"))
			}
		}
		if strings.Contains(r.Header.Get("Content-Type"), "application/json") {
			decoder := json.NewDecoder(http.MaxBytesReader(r.Response.Writer, r.Body, 16<<20))
			if e := decoder.Decode(&c.Body); e != nil {
				fail(422, "请求体必须为 JSON 对象")
			}
			if c.Body == nil {
				fail(422, "请求体必须为 JSON 对象")
			}
		}
		out := rt.Handler(c)
		if !rt.Public {
			if rt.Method != "GET" {
				a.audit(c.WS, str(c.User["id"]), strings.ToLower(rt.Method)+" "+rt.Path, r.URL.Path)
			} else if auditRead(rt.Path) {
				a.audit(c.WS, str(c.User["id"]), "read "+rt.Path, r.URL.Path)
			}
		}
		if out != nil {
			r.Response.WriteJson(out)
		}
	})
}
func (a *App) Server(addr string) *ghttp.Server {
	s := ghttp.GetServer("odb-" + id())
	s.SetAddr(addr)
	s.SetGraceful(false)
	s.SetDumpRouterMap(false)
	s.SetIndexFolder(false)
	s.SetClientMaxBodySize(128 << 20)
	s.AddStaticPath("/assets", filepath.Join(a.Root, "web"))
	s.BindHandler("GET:/", func(r *ghttp.Request) { r.Response.ServeFile(filepath.Join(a.Root, "web", "index.html")) })
	for _, rt := range a.routes() {
		a.route(s, rt)
	}
	return s
}
func (a *App) routes() []route { return append(a.routesWithoutDocs(), a.docsRoutes()...) }
func (a *App) routesWithoutDocs() []route {
	r := a.authRoutes()
	r = append(r, a.dataRoutes()...)
	r = append(r, a.resourceRoutes()...)
	r = append(r, a.qualityRoutes()...)
	r = append(r, a.notebookRoutes()...)
	r = append(r, a.workflowRoutes()...)
	r = append(r, a.mlopsRoutes()...)
	r = append(r, a.bundleRoutes()...)
	r = append(r, a.ingestRoutes()...)
	r = append(r, a.egressRoutes()...)
	r = append(r, a.syncRoutes()...)
	r = append(r, a.governanceRoutes()...)
	r = append(r, a.analysisRoutes()...)
	r = append(r, a.driftRoutes()...)
	r = append(r, a.agentRoutes()...)
	r = append(r, a.operationRoutes()...)
	r = append(r, a.mcpRoutes()...)
	r = append(r, a.extensionRoutes()...)
	return r
}

var core = []string{"auth", "data", "chat", "ops", "console"}
var extra = []string{"quality", "workflow", "cicd", "mlops", "catalog_ext", "ingest_ext", "notebook", "ops_ext", "analysis", "semantic_ext"}

func bearer(r *ghttp.Request) string {
	parts := strings.Fields(r.Header.Get("Authorization"))
	if len(parts) == 2 && strings.EqualFold(parts[0], "Bearer") {
		return parts[1]
	}
	return ""
}
