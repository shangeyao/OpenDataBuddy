package platform

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/smtp"
	"net/url"
	"os/exec"
	"strings"
	"time"
)

// notify.go delivers alerts to real channels. HTTP channels post to their
// endpoint and email uses SMTP; when a channel has no usable configuration the
// delivery is recorded as failed with the reason, never as a silent success.

// alertSimulated forces local-only delivery, for offline development.
func alertSimulated() bool {
	return strings.EqualFold(env("DATABUDDY_ALERT_SIMULATE", ""), "1") || strings.EqualFold(env("DATABUDDY_ALERT_SIMULATE", ""), "true")
}

// smtpConfig reads the SMTP settings from the platform settings and falls back
// to environment variables.
func (a *App) smtpConfig() M {
	cfg := M{}
	if raw := a.setting("smtp"); raw != "" {
		json.Unmarshal([]byte(raw), &cfg)
	}
	if str(cfg["host"]) == "" {
		cfg["host"] = env("DATABUDDY_SMTP_HOST", "")
	}
	if str(cfg["port"]) == "" {
		cfg["port"] = env("DATABUDDY_SMTP_PORT", "587")
	}
	if str(cfg["username"]) == "" {
		cfg["username"] = env("DATABUDDY_SMTP_USER", "")
	}
	if str(cfg["password"]) == "" {
		cfg["password"] = env("DATABUDDY_SMTP_PASSWORD", "")
	}
	if str(cfg["from"]) == "" {
		cfg["from"] = env("DATABUDDY_SMTP_FROM", "alert@databuddy.local")
	}
	return cfg
}

// webhookPayload builds the endpoint, body and headers of a webhook style channel.
func webhookPayload(kind string, cfg M, subject, message, severity string) (string, any, map[string]string, string) {
	headers := map[string]string{"Content-Type": "application/json"}
	switch kind {
	case "webhook":
		target := str(cfg["url"])
		if target == "" {
			return "", nil, nil, "未配置回调地址"
		}
		method := str(def(cfg, "method", "POST"))
		switch method {
		case "POST", "PUT", "PATCH":
		default:
			method = "POST"
		}
		for k, v := range obj(cfg["headers"]) {
			headers[k] = str(v)
		}
		return target, M{"event": subject, "title": subject, "message": message, "severity": severity, "timestamp": now()}, headers, ""
	case "teams":
		target := str(cfg["webhook_url"])
		if target == "" {
			return "", nil, nil, "未配置 Teams 连接器地址"
		}
		color := str(def(cfg, "theme_color", "D14343"))
		body := M{"@type": "MessageCard", "@context": "https://schema.org/extensions", "summary": subject, "themeColor": color, "sections": []any{M{"activityTitle": subject, "activitySubtitle": severity, "text": message}}}
		return target, body, headers, ""
	case "wecom":
		target := str(def(cfg, "webhook_url", cfg["url"]))
		if target == "" {
			key := str(cfg["key"])
			if key == "" {
				return "", nil, nil, "未配置企业微信机器人 Key 或地址"
			}
			target = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + url.QueryEscape(key)
		}
		if str(def(cfg, "msgtype", "markdown")) == "markdown" {
			return target, M{"msgtype": "markdown", "markdown": M{"content": "**" + subject + "**\n" + message}}, headers, ""
		}
		return target, M{"msgtype": "text", "text": M{"content": subject + "\n" + message}}, headers, ""
	case "slack":
		target := str(cfg["webhook_url"])
		if target == "" {
			return "", nil, nil, "未配置 Slack Webhook 地址"
		}
		body := M{"text": subject + "\n" + message}
		if ch := str(cfg["channel"]); ch != "" {
			body["channel"] = ch
		}
		return target, body, headers, ""
	case "feishu":
		target := str(def(cfg, "webhook_url", cfg["url"]))
		if target == "" {
			return "", nil, nil, "未配置飞书机器人地址"
		}
		body := M{"msg_type": "text", "content": M{"text": subject + "\n" + message}}
		return target, body, headers, ""
	}
	return "", nil, nil, "未知渠道类型：" + kind
}

// deliverAlert sends one event to one channel and reports the outcome.
func (a *App) deliverAlert(channel M, event M) M {
	kind := str(channel["type"])
	cfg := obj(channel["config"])
	subject := str(def(event, "subject", "告警"))
	message := str(def(event, "message", subject))
	severity := str(def(event, "severity", "info"))
	if alertSimulated() {
		return M{"ok": true, "simulated": true, "channel": kind, "message": "已按配置生成投递内容（当前为本地模拟模式）", "subject": subject}
	}
	switch kind {
	case "email":
		return a.deliverEmail(cfg, subject, message)
	}
	target, body, headers, reason := webhookPayload(kind, cfg, subject, message, severity)
	if reason != "" {
		return M{"ok": false, "simulated": false, "channel": kind, "message": reason}
	}
	payload, e := json.Marshal(body)
	if e != nil {
		return M{"ok": false, "simulated": false, "channel": kind, "message": "构造投递内容失败：" + e.Error()}
	}
	method := "POST"
	if kind == "webhook" {
		method = str(def(cfg, "method", "POST"))
		switch method {
		case "POST", "PUT", "PATCH":
		default:
			method = "POST"
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	req, e := http.NewRequestWithContext(ctx, method, target, bytes.NewReader(payload))
	if e != nil {
		return M{"ok": false, "simulated": false, "channel": kind, "message": "请求构造失败：" + e.Error()}
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, e := http.DefaultClient.Do(req)
	if e != nil {
		return M{"ok": false, "simulated": false, "channel": kind, "message": "投递失败：" + e.Error()}
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	ok := resp.StatusCode < 400
	return M{"ok": ok, "simulated": false, "channel": kind, "status": resp.StatusCode, "message": fmt.Sprintf("HTTP %d", resp.StatusCode), "subject": subject}
}

// deliverWorkspaceNotify delivers an event to the workspace level notification
// settings. It is used when no alert rule claims the event, so the two
// configuration surfaces do not produce duplicate deliveries.
func (a *App) deliverWorkspaceNotify(ws string, event M) []M {
	notify := obj(a.setting("notify." + ws))
	if len(notify) == 0 {
		return nil
	}
	if enabled, ok := notify["enabled"]; ok && !boolOf(enabled) {
		return nil
	}
	out := []M{}
	for _, item := range []struct{ kind, key string }{{"webhook", "webhook"}, {"wecom", "wecom"}, {"email", "email"}} {
		value := str(notify[item.key])
		if value == "" {
			continue
		}
		cfg := M{}
		switch item.kind {
		case "webhook":
			cfg["url"] = value
		case "wecom":
			cfg["webhook_url"] = value
		case "email":
			cfg["to"] = value
		}
		result := a.deliverAlert(M{"type": item.kind, "name": "工作空间通知"}, event)
		out = append(out, result)
		status := "failed"
		if boolOf(result["ok"]) {
			status = "sent"
		}
		a.insert("alert_deliveries", M{"id": id(), "workspace_id": ws, "channel_name": "工作空间通知", "channel_type": item.kind, "event": str(event["event"]), "subject": str(event["subject"]), "severity": str(event["severity"]), "status": status, "simulated": boolOf(result["simulated"]), "payload": event, "delivery": result, "created_at": now()})
	}
	return out
}

// gitRemoteRefs lists remote refs with git ls-remote, so a connection test
// proves the repository is reachable for a real Git client.
func gitRemoteRefs(url, branch string) ([]string, string) {
	git, err := exec.LookPath("git")
	if err != nil {
		return nil, "本机未安装 git"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	args := []string{"ls-remote", "--heads", url}
	if branch != "" {
		args = append(args, branch)
	}
	out, e := exec.CommandContext(ctx, git, args...).Output()
	if e != nil {
		return nil, strings.TrimSpace(e.Error())
	}
	refs := []string{}
	for _, line := range strings.Split(string(out), "\n") {
		fields := strings.Fields(strings.TrimSpace(line))
		if len(fields) == 2 {
			refs = append(refs, fields[1])
		}
	}
	if len(refs) == 0 {
		return nil, "仓库没有可读的分支"
	}
	return refs, ""
}

// deliverEmail sends a plain text mail over SMTP.
func (a *App) deliverEmail(cfg M, subject, message string) M {
	smtpCfg := a.smtpConfig()
	host := str(smtpCfg["host"])
	if host == "" {
		return M{"ok": false, "simulated": false, "channel": "email", "message": "未配置 SMTP 服务器，可在平台设置中填写或设置 DATABUDDY_SMTP_HOST"}
	}
	to := str(cfg["to"])
	if to == "" {
		return M{"ok": false, "simulated": false, "channel": "email", "message": "未配置收件人"}
	}
	from := str(def(cfg, "from", smtpCfg["from"]))
	recipients := []string{}
	for _, t := range strings.Split(to, ",") {
		if s := strings.TrimSpace(t); s != "" {
			recipients = append(recipients, s)
		}
	}
	if len(recipients) == 0 {
		return M{"ok": false, "simulated": false, "channel": "email", "message": "收件人为空"}
	}
	port := str(def(smtpCfg, "port", "587"))
	msg := strings.Join([]string{
		"From: " + from,
		"To: " + strings.Join(recipients, ", "),
		"Subject: " + subject,
		"Date: " + time.Now().Format(time.RFC1123Z),
		"MIME-Version: 1.0",
		"Content-Type: text/plain; charset=UTF-8",
		"",
		message,
	}, "\r\n")
	var auth smtp.Auth
	if user := str(smtpCfg["username"]); user != "" {
		auth = smtp.PlainAuth("", user, str(smtpCfg["password"]), host)
	}
	if e := smtp.SendMail(net.JoinHostPort(host, port), auth, from, recipients, []byte(msg)); e != nil {
		return M{"ok": false, "simulated": false, "channel": "email", "message": "投递失败：" + e.Error()}
	}
	return M{"ok": true, "simulated": false, "channel": "email", "message": "邮件已发送", "subject": subject}
}
