package main

import (
	"flag"
	"fmt"
	"github.com/shangeyao/OpenDataBuddy/internal/platform"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
)

func main() {
	configRoot := "."
	for i, arg := range os.Args[1:] {
		if arg == "--root" && i+2 < len(os.Args) {
			configRoot = os.Args[i+2]
		}
		if strings.HasPrefix(arg, "--root=") {
			configRoot = strings.TrimPrefix(arg, "--root=")
		}
	}
	check(platform.LoadEnv(filepath.Join(configRoot, ".env")))
	mcp := flag.Bool("mcp", false, "启动 stdio MCP 服务")
	host := flag.String("host", getenv("DATABUDDY_HOST", "127.0.0.1"), "监听地址")
	port := flag.String("port", getenv("DATABUDDY_PORT", "8000"), "监听端口")
	root := flag.String("root", ".", "项目根目录")
	data := flag.String("data-dir", getenv("DATABUDDY_DATA_DIR", "data"), "数据目录")
	flag.Parse()
	r, e := filepath.Abs(*root)
	check(e)
	dataPath := *data
	if !filepath.IsAbs(dataPath) {
		dataPath = filepath.Join(r, dataPath)
	}
	d, e := filepath.Abs(dataPath)
	check(e)
	if *mcp {
		check(platform.BridgeMCP(os.Stdin, os.Stdout, getenv("DATABUDDY_API_URL", "http://127.0.0.1:8000"), getenv("DATABUDDY_TOKEN", ""), getenv("DATABUDDY_WORKSPACE_ID", "")))
		return
	}
	a, e := platform.Open(r, d)
	check(e)
	defer a.Close()

	stopScheduler := a.StartScheduler()
	defer stopScheduler()
	s := a.Server(net.JoinHostPort(*host, *port))
	check(s.Start())
	fmt.Printf("OpenDataBuddy · GoFrame v2.10.4\nhttp://%s\n数据目录 %s\n", net.JoinHostPort(*host, *port), d)
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	<-ch
	check(s.Shutdown())
}
func getenv(k, d string) string {
	if s := os.Getenv(k); s != "" {
		return s
	}
	return d
}
func check(e error) {
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}
