package mcpruntime

import (
	"bytes"
	"context"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"crane/persistence"
)

func controllerTestRoot(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatalf("Getwd() error = %v", err)
	}
	root := filepath.Dir(wd)
	if _, err := os.Stat(filepath.Join(root, "crane", "mcp_runtime", "test_server.py")); err != nil {
		t.Fatalf("test MCP server was not found under %s: %v", root, err)
	}
	return root
}

func controllerPython(t *testing.T) string {
	t.Helper()
	if configured := strings.TrimSpace(os.Getenv("CRANE_PYTHON")); configured != "" {
		return configured
	}
	return "python"
}

func newControllerStore(t *testing.T) *persistence.Store {
	t.Helper()
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatalf("NewStore() error = %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func startController(t *testing.T, store *persistence.Store, root string) *Controller {
	t.Helper()
	controller := New(store, Config{PythonHome: root})
	if err := controller.Start(); err != nil {
		t.Fatalf("Controller.Start() error = %v", err)
	}
	t.Cleanup(func() { _ = controller.Close() })
	return controller
}

func invocationText(t *testing.T, result InvokeResult) string {
	t.Helper()
	invocation := result.Invocation
	if status, _ := invocation["status"].(string); status != "succeeded" {
		t.Fatalf("invocation status = %v, detail = %v", status, invocation["error"])
	}
	payload, _ := invocation["result"].(map[string]any)
	text, _ := payload["text"].(string)
	if text == "" {
		t.Fatalf("invocation result had no text: %#v", invocation["result"])
	}
	return text
}

func TestControllerStdioServerPolicyAndInvocation(t *testing.T) {
	root := controllerTestRoot(t)
	python := controllerPython(t)
	t.Setenv("CRANE_MCP_ALLOWED_COMMANDS", "python,pythonw,node,"+python)
	store := newControllerStore(t)
	controller := startController(t, store, root)

	server := persistence.MCPServerRecord{
		ID:                       "stdio-test",
		Name:                     "Stdio Test",
		Transport:                "stdio",
		Command:                  python,
		Args:                     []string{filepath.Join(root, "crane", "mcp_runtime", "test_server.py")},
		Enabled:                  true,
		ConnectTimeoutSeconds:    10,
		InvocationTimeoutSeconds: 30,
	}
	if err := controller.AddServer(server); err != nil {
		t.Fatalf("AddServer() error = %v", err)
	}
	if err := controller.Connect(server.ID); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	tools, err := controller.ListTools(server.ID)
	if err != nil || len(tools) != 3 {
		t.Fatalf("ListTools() = %#v, %v", tools, err)
	}
	policies := map[string]string{}
	for _, tool := range tools {
		policies[tool.ToolName] = tool.PolicyClass
	}
	if policies["get_test_echo"] != "allowed" || policies["delete_test_record"] != "approval_required" || policies["get_invocation_counts"] != "allowed" {
		t.Fatalf("unexpected tool policies: %#v", policies)
	}

	readOnly, err := controller.Invoke(server.ID, "get_test_echo", map[string]any{"value": "hello"})
	if err != nil {
		t.Fatalf("read-only Invoke() error = %v", err)
	}
	if text := invocationText(t, readOnly); !strings.Contains(text, "hello") {
		t.Fatalf("read-only result = %q", text)
	}
	if readOnly.ApprovalRequestID != "" {
		t.Fatalf("read-only tool requested approval: %q", readOnly.ApprovalRequestID)
	}

	blocked, err := controller.Invoke(server.ID, "delete_test_record", map[string]any{"record_id": "rec-1"})
	if err != nil {
		t.Fatalf("consequential Invoke() error = %v", err)
	}
	if status, _ := blocked.Invocation["status"].(string); status != "blocked" {
		t.Fatalf("consequential tool status = %v, invocation = %#v", status, blocked.Invocation)
	}
	if blocked.ApprovalRequestID == "" {
		t.Fatal("consequential tool did not create an approval request")
	}
	approved, err := controller.DecideApproval(blocked.ApprovalRequestID, "granted", "test-user")
	if err != nil || approved.Status != "granted" {
		t.Fatalf("DecideApproval() = %#v, %v", approved, err)
	}
	allowed, err := controller.Invoke(server.ID, "delete_test_record", map[string]any{"record_id": "rec-1"})
	if err != nil {
		t.Fatalf("approved Invoke() error = %v", err)
	}
	if text := invocationText(t, allowed); !strings.Contains(text, "rec-1") {
		t.Fatalf("approved result = %q", text)
	}

	invocations, err := controller.ListInvocations(20)
	if err != nil || len(invocations) != 3 {
		t.Fatalf("ListInvocations() count = %d, error = %v", len(invocations), err)
	}
}

func TestControllerStreamableHTTPInvocation(t *testing.T) {
	root := controllerTestRoot(t)
	python := controllerPython(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("net.Listen() error = %v", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cmd := exec.CommandContext(ctx, python, "-m", "crane.mcp_runtime.test_server", "--http", "--port", strconv.Itoa(port))
	cmd.Dir = root
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start HTTP MCP server: %v", err)
	}
	defer func() {
		cancel()
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
	}()

	url := "http://127.0.0.1:" + strconv.Itoa(port) + "/mcp"
	deadline := time.Now().Add(15 * time.Second)
	ready := false
	for time.Now().Before(deadline) {
		connection, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), 500*time.Millisecond)
		if err == nil {
			_ = connection.Close()
			ready = true
			break
		}
		time.Sleep(200 * time.Millisecond)
	}
	if !ready {
		t.Fatalf("HTTP MCP server did not start: %s", stderr.String())
	}

	store := newControllerStore(t)
	controller := startController(t, store, root)
	server := persistence.MCPServerRecord{
		ID:                       "http-test",
		Name:                     "HTTP Test",
		Transport:                "streamable_http",
		URL:                      url,
		Enabled:                  true,
		ConnectTimeoutSeconds:    10,
		InvocationTimeoutSeconds: 30,
	}
	if err := controller.AddServer(server); err != nil {
		t.Fatalf("AddServer() error = %v", err)
	}
	if err := controller.Connect(server.ID); err != nil {
		t.Fatalf("Connect() error = %v", err)
	}
	result, err := controller.Invoke(server.ID, "get_test_echo", map[string]any{"value": "http-ok"})
	if err != nil {
		t.Fatalf("Invoke() error = %v", err)
	}
	if text := invocationText(t, result); !strings.Contains(text, "http-ok") {
		t.Fatalf("HTTP tool result = %q", text)
	}
}

func TestControllerStartIncludesStderrOnFailure(t *testing.T) {
	store := newControllerStore(t)
	python := controllerPython(t)
	// Force an immediate failure with identifiable stderr.
	controller := New(store, Config{
		PythonPath:   python,
		PythonArgs:   []string{"-c", "import sys; sys.stderr.write('mcp-start-fail'); sys.exit(7)"},
		StartTimeout: 15 * time.Second,
	})
	err := controller.Start()
	if err == nil {
		t.Fatal("Controller.Start() expected error")
	}
	if !strings.Contains(err.Error(), "mcp-start-fail") {
		t.Fatalf("Start() error = %q, want stderr detail", err)
	}
}

func TestDefaultStartTimeoutIsColdStartSafe(t *testing.T) {
	if defaultStartTimeout < 60*time.Second {
		t.Fatalf("defaultStartTimeout = %s, want >= 60s for packaged cold start", defaultStartTimeout)
	}
}
