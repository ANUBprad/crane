package persistence

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMCPSchemaMigrationAndRecords(t *testing.T) {
	store := newTestStore(t)
	version, err := store.MigrationVersion()
	if err != nil {
		t.Fatalf("MigrationVersion() error = %v", err)
	}
	if version != 2 {
		t.Fatalf("MigrationVersion() = %d, want 2", version)
	}

	server := MCPServerRecord{
		ID:                       "test-mcp",
		Name:                     "Test MCP",
		Transport:                "stdio",
		Command:                  "python",
		Args:                     []string{"server.py"},
		Env:                      map[string]string{"CRANE_MCP_REGION": "private-value"},
		Enabled:                  true,
		ConnectTimeoutSeconds:    5,
		InvocationTimeoutSeconds: 20,
		Status:                   "registered",
	}
	if err := store.UpsertMCPServer(server); err != nil {
		t.Fatalf("UpsertMCPServer() error = %v", err)
	}
	stored, err := store.GetMCPServer("test-mcp")
	if err != nil {
		t.Fatalf("GetMCPServer() error = %v", err)
	}
	if stored.Env["CRANE_MCP_REGION"] != "private-value" {
		t.Fatalf("private env was not persisted for the process boundary: %#v", stored.Env)
	}
	encoded, err := json.Marshal(stored)
	if err != nil {
		t.Fatalf("json.Marshal(server) error = %v", err)
	}
	if strings.Contains(string(encoded), "private-value") {
		t.Fatalf("API-safe server JSON leaked an environment value: %s", encoded)
	}

	tools := []MCPToolRecord{{
		ServerID:    "test-mcp",
		ToolName:    "get_test_echo",
		Description: "Echo text",
		InputSchema: json.RawMessage(`{"type":"object","properties":{"value":{"type":"string"}}}`),
		Annotations: json.RawMessage(`{"readOnlyHint":true}`),
		PolicyClass: "allowed",
	}}
	if err := store.ReplaceMCPTools("test-mcp", tools); err != nil {
		t.Fatalf("ReplaceMCPTools() error = %v", err)
	}
	discovered, err := store.ListMCPTools("test-mcp")
	if err != nil || len(discovered) != 1 || discovered[0].PolicyClass != "allowed" {
		t.Fatalf("ListMCPTools() = %#v, %v", discovered, err)
	}

	fingerprint := ApprovalFingerprint("mcp-daemon", "test-mcp", "delete_test_record", map[string]any{"id": "one"})
	if fingerprint == "" {
		t.Fatal("ApprovalFingerprint() returned empty digest")
	}
	request := ApprovalRequestRecord{
		ID:          "approval-1",
		TaskID:      "mcp-daemon",
		ServerID:    "test-mcp",
		ToolName:    "delete_test_record",
		Arguments:   json.RawMessage(`{"id":"one"}`),
		Fingerprint: fingerprint,
	}
	if err := store.CreateApprovalRequest(request); err != nil {
		t.Fatalf("CreateApprovalRequest() error = %v", err)
	}
	if err := store.CreateApprovalRequest(request); err != nil {
		t.Fatalf("duplicate exact action should be idempotent: %v", err)
	}
	decided, err := store.DecideApprovalRequest("approval-1", "granted", "local-user")
	if err != nil || decided.Status != "granted" {
		t.Fatalf("DecideApprovalRequest() = %#v, %v", decided, err)
	}
	grants, err := store.GrantedApprovalFingerprints("mcp-daemon")
	if err != nil || len(grants) != 1 || grants[0] != fingerprint {
		t.Fatalf("GrantedApprovalFingerprints() = %#v, %v", grants, err)
	}
	if _, err := store.DecideApprovalRequest("approval-1", "denied", "local-user"); err != ErrApprovalAlreadyDecided {
		t.Fatalf("second decision error = %v, want %v", err, ErrApprovalAlreadyDecided)
	}

	invocation := MCPInvocationRecord{
		ID:             "invocation-1",
		TaskID:         "task-1",
		ServerID:       "test-mcp",
		ServerName:     "Test MCP",
		ToolName:       "get_test_echo",
		Arguments:      json.RawMessage(`{"value":"token=abc123"}`),
		Status:         "succeeded",
		Result:         json.RawMessage(`{"text":"ok"}`),
		PolicyDecision: "allowed",
		DurationMS:     12,
	}
	if err := store.RecordMCPInvocation(invocation); err != nil {
		t.Fatalf("RecordMCPInvocation() error = %v", err)
	}
	invocations, err := store.ListMCPInvocations(10)
	if err != nil || len(invocations) != 1 {
		t.Fatalf("ListMCPInvocations() = %#v, %v", invocations, err)
	}
	if strings.Contains(string(invocations[0].Arguments), "abc123") {
		t.Fatalf("invocation arguments leaked a secret: %s", invocations[0].Arguments)
	}

	if err := store.DeleteMCPServer("test-mcp"); err != nil {
		t.Fatalf("DeleteMCPServer() error = %v", err)
	}
	tools, err = store.ListMCPTools("test-mcp")
	if err != nil || len(tools) != 0 {
		t.Fatalf("tools were not deleted with server: %#v, %v", tools, err)
	}
}

func TestValidateMCPServerRecordRejectsDangerousConfiguration(t *testing.T) {
	base := MCPServerRecord{
		ID:                       "safe",
		Name:                     "Safe",
		Transport:                "stdio",
		Command:                  "python",
		Args:                     []string{"server.py"},
		ConnectTimeoutSeconds:    10,
		InvocationTimeoutSeconds: 30,
	}
	if err := ValidateMCPServerRecord(base); err != nil {
		t.Fatalf("valid record rejected: %v", err)
	}
	dangerous := base
	dangerous.Args = []string{"-c", "print(1)"}
	if err := ValidateMCPServerRecord(dangerous); err == nil {
		t.Fatal("inline Python execution argument was accepted")
	}
	remote := MCPServerRecord{
		ID:                       "remote",
		Name:                     "Remote",
		Transport:                "streamable_http",
		URL:                      "https://example.invalid/mcp",
		ConnectTimeoutSeconds:    10,
		InvocationTimeoutSeconds: 30,
	}
	if err := ValidateMCPServerRecord(remote); err == nil {
		t.Fatal("remote MCP endpoint was accepted without authorization")
	}
	remote.AllowRemoteEndpoint = true
	if err := ValidateMCPServerRecord(remote); err != nil {
		t.Fatalf("explicit remote endpoint rejected: %v", err)
	}
}

func TestApprovalFingerprintBindsTaskAndDetectsArgTampering(t *testing.T) {
	args := map[string]any{"id": "one"}
	a := ApprovalFingerprint("task-a", "srv", "delete_test_record", args)
	b := ApprovalFingerprint("task-b", "srv", "delete_test_record", args)
	if a == "" || b == "" || a == b {
		t.Fatalf("fingerprints must be task-bound: %q %q", a, b)
	}
	tampered := ApprovalFingerprint("task-a", "srv", "delete_test_record", map[string]any{"id": "two"})
	if tampered == a {
		t.Fatal("altered arguments produced same approval fingerprint")
	}
}

func TestApprovalCannotBeReusedAcrossTasksOrReplayedAfterConsume(t *testing.T) {
	store, err := NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	server := MCPServerRecord{
		ID: "srv", Name: "Srv", Transport: "stdio", Command: "python",
		Args: []string{"server.py"}, Enabled: true,
		ConnectTimeoutSeconds: 10, InvocationTimeoutSeconds: 30,
	}
	if err := store.UpsertMCPServer(server); err != nil {
		t.Fatal(err)
	}
	args := json.RawMessage(`{"id":"one"}`)
	fpA := ApprovalFingerprint("task-a", "srv", "delete_test_record", map[string]any{"id": "one"})
	req := ApprovalRequestRecord{
		ID: "approval-a", TaskID: "task-a", ServerID: "srv", ToolName: "delete_test_record",
		Arguments: args, Fingerprint: fpA,
	}
	if err := store.CreateApprovalRequest(req); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DecideApprovalRequest("approval-a", "granted", "local-user"); err != nil {
		t.Fatal(err)
	}

	grantsA, err := store.GrantedApprovalFingerprints("task-a")
	if err != nil || len(grantsA) != 1 {
		t.Fatalf("task-a grants = %#v, %v", grantsA, err)
	}
	grantsB, err := store.GrantedApprovalFingerprints("task-b")
	if err != nil || len(grantsB) != 0 {
		t.Fatalf("task-b must not see task-a grant: %#v, %v", grantsB, err)
	}

	if err := store.ConsumeGrantedApproval("task-a", fpA); err != nil {
		t.Fatal(err)
	}
	grantsA, err = store.GrantedApprovalFingerprints("task-a")
	if err != nil || len(grantsA) != 0 {
		t.Fatalf("consumed grant must not be replayed: %#v, %v", grantsA, err)
	}

	req2 := req
	req2.ID = "approval-a2"
	if err := store.CreateApprovalRequest(req2); err != nil {
		t.Fatal(err)
	}
	got, err := store.GetApprovalRequest("approval-a2")
	if err != nil || got.Status != "pending" {
		t.Fatalf("re-request after consume = %#v, %v", got, err)
	}
}

func TestDeniedAndExpiredApprovalsCannotBeGranted(t *testing.T) {
	store, err := NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()

	expiredFP := ApprovalFingerprint("task-exp", "srv", "delete_test_record", map[string]any{"id": "e"})
	if err := store.CreateApprovalRequest(ApprovalRequestRecord{
		ID: "approval-exp", TaskID: "task-exp", ServerID: "srv", ToolName: "delete_test_record",
		Arguments: json.RawMessage(`{"id":"e"}`), Fingerprint: expiredFP,
		ExpiresAt: time.Now().UTC().Add(-time.Minute).Format(time.RFC3339Nano),
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DecideApprovalRequest("approval-exp", "granted", "local-user"); err != ErrApprovalExpired {
		t.Fatalf("expired grant err = %v, want %v", err, ErrApprovalExpired)
	}

	deniedFP := ApprovalFingerprint("task-denied", "srv", "delete_test_record", map[string]any{"id": "d"})
	if err := store.CreateApprovalRequest(ApprovalRequestRecord{
		ID: "approval-denied", TaskID: "task-denied", ServerID: "srv", ToolName: "delete_test_record",
		Arguments: json.RawMessage(`{"id":"d"}`), Fingerprint: deniedFP,
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DecideApprovalRequest("approval-denied", "denied", "local-user"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DecideApprovalRequest("approval-denied", "granted", "local-user"); err != ErrApprovalAlreadyDecided {
		t.Fatalf("denied redecision err = %v, want %v", err, ErrApprovalAlreadyDecided)
	}
}
