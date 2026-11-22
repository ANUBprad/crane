package localapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"crane/mcpruntime"
	"crane/persistence"
)

type mockMCPController struct {
	server  persistence.MCPServerRecord
	added   persistence.MCPServerRecord
	invoke  mcpInvokeRequest
	decided [3]string
}

func (m *mockMCPController) AddServer(record persistence.MCPServerRecord) error {
	m.added = record
	m.server = record
	return nil
}

func (m *mockMCPController) RemoveServer(string) error     { return nil }
func (m *mockMCPController) SetEnabled(string, bool) error { return nil }
func (m *mockMCPController) Connect(string) error          { return nil }
func (m *mockMCPController) Disconnect(string) error       { return nil }
func (m *mockMCPController) Discover(string) error         { return nil }
func (m *mockMCPController) ListTools(string) ([]persistence.MCPToolRecord, error) {
	return []persistence.MCPToolRecord{{ServerID: "demo", ToolName: "get_test_echo", InputSchema: json.RawMessage(`{}`), PolicyClass: "allowed"}}, nil
}

func (m *mockMCPController) ListServers() ([]mcpruntime.ServerStatus, error) {
	return []mcpruntime.ServerStatus{{MCPServerRecord: m.server, Connected: true, ToolCount: 1}}, nil
}

func (m *mockMCPController) ListInvocations(int) ([]persistence.MCPInvocationRecord, error) {
	return []persistence.MCPInvocationRecord{{ID: "inv-1", ServerID: "demo", ToolName: "get_test_echo", Status: "succeeded"}}, nil
}

func (m *mockMCPController) Invoke(serverID, toolName string, arguments map[string]any) (mcpruntime.InvokeResult, error) {
	m.invoke = mcpInvokeRequest{ServerID: serverID, ToolName: toolName, Arguments: arguments}
	return mcpruntime.InvokeResult{Invocation: map[string]any{
		"invocation_id": "inv-1", "status": "succeeded", "result": map[string]any{"text": "ok"},
	}}, nil
}

func (m *mockMCPController) ListApprovals() ([]persistence.ApprovalRequestRecord, error) {
	return []persistence.ApprovalRequestRecord{{ID: "approval-1", ServerID: "demo", ToolName: "delete_test_record", Status: "pending"}}, nil
}

func (m *mockMCPController) DecideApproval(id, decision, actor string) (persistence.ApprovalRequestRecord, error) {
	m.decided = [3]string{id, decision, actor}
	return persistence.ApprovalRequestRecord{ID: id, Status: decision}, nil
}

func TestMCPRoutesAreAuthenticatedAndRedacted(t *testing.T) {
	state := newTestState(t)
	mock := &mockMCPController{server: persistence.MCPServerRecord{
		ID: "demo", Name: "Demo", Transport: "stdio", Command: "python", Enabled: true,
		Env: map[string]string{"CRANE_MCP_REGION": "private"}, EnvKeys: []string{"CRANE_MCP_REGION"},
	}}
	server := state.newServer(t)
	server.SetMCPController(mock)
	httpServer := httptest.NewServer(server)
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	status, header, body := apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/mcp/servers", "", nil)
	envelope := decodeEnvelope(t, status, header, body)
	if status != http.StatusUnauthorized || envelope.Error.Code != "unauthorized" {
		t.Fatalf("unauthenticated MCP route: status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/mcp/servers", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("MCP servers status = %d, body = %s", status, body)
	}
	if bytes.Contains(body, []byte("private")) {
		t.Fatalf("MCP server listing leaked an environment value: %s", body)
	}
	var list struct {
		Servers []map[string]any `json:"servers"`
	}
	if err := json.Unmarshal(envelope.Data, &list); err != nil {
		t.Fatal(err)
	}
	if len(list.Servers) != 1 || list.Servers[0]["server_id"] != "demo" {
		t.Fatalf("unexpected MCP server list: %s", envelope.Data)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/mcp/servers", state.token,
		[]byte(`{"server_id":"new-demo","name":"New Demo","transport":"stdio","command":"python","args":["server.py"],"env":{"CRANE_MCP_REGION":"private"}}`))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusCreated {
		t.Fatalf("MCP server create status = %d, body = %s", status, body)
	}
	if bytes.Contains(body, []byte("private")) {
		t.Fatalf("MCP server create leaked an environment value: %s", body)
	}
	if mock.added.Env["CRANE_MCP_REGION"] != "private" {
		t.Fatalf("MCP server create did not pass the private value to the controller: %#v", mock.added.Env)
	}
	if !mock.added.Enabled {
		t.Fatal("MCP server create did not default enabled to true")
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/mcp/invoke", state.token,
		[]byte(`{"server_id":"demo","tool_name":"get_test_echo","arguments":{"value":"hello"}}`))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("MCP invoke status = %d, body = %s", status, body)
	}
	if mock.invoke.ToolName != "get_test_echo" || mock.invoke.Arguments["value"] != "hello" {
		t.Fatalf("unexpected invocation request: %#v", mock.invoke)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/mcp/approvals/approval-1/decide", state.token,
		[]byte(`{"decision":"granted"}`))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("MCP approval decision status = %d, body = %s", status, body)
	}
	if mock.decided != [3]string{"approval-1", "granted", "local-user"} {
		t.Fatalf("unexpected approval decision: %#v", mock.decided)
	}
}

func TestMCPRoutesRejectInvalidAndMissingCredentials(t *testing.T) {
	state := newTestState(t)
	server := state.newServer(t)
	server.SetMCPController(&mockMCPController{})
	httpServer := httptest.NewServer(server)
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	paths := []struct {
		method string
		path   string
		body   []byte
	}{
		{http.MethodGet, "/v1/mcp/status", nil},
		{http.MethodGet, "/v1/mcp/servers", nil},
		{http.MethodPost, "/v1/mcp/servers", []byte(`{"server_id":"x","name":"X","transport":"stdio","command":"python"}`)},
		{http.MethodGet, "/v1/mcp/approvals", nil},
		{http.MethodPost, "/v1/mcp/approvals/approval-1/decide", []byte(`{"decision":"granted"}`)},
		{http.MethodPost, "/v1/mcp/invoke", []byte(`{"server_id":"demo","tool_name":"get_test_echo","arguments":{}}`)},
	}
	for _, tc := range paths {
		status, header, body := apiRequest(t, client, httpServer.URL, tc.method, tc.path, "", tc.body)
		envelope := decodeEnvelope(t, status, header, body)
		if status != http.StatusUnauthorized || envelope.Error.Code != "unauthorized" {
			t.Fatalf("%s %s missing token: status=%d body=%s", tc.method, tc.path, status, body)
		}
		status, header, body = apiRequest(t, client, httpServer.URL, tc.method, tc.path, "bad-token", tc.body)
		envelope = decodeEnvelope(t, status, header, body)
		if status != http.StatusUnauthorized || envelope.Error.Code != "unauthorized" {
			t.Fatalf("%s %s bad token: status=%d body=%s", tc.method, tc.path, status, body)
		}
	}
}

func TestMCPInvokeRejectsMalformedAndTraversalIDs(t *testing.T) {
	state := newTestState(t)
	mock := &mockMCPController{}
	server := state.newServer(t)
	server.SetMCPController(mock)
	httpServer := httptest.NewServer(server)
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	badBodies := []string{
		`{"server_id":"../bad","tool_name":"get_test_echo","arguments":{}}`,
		`{"server_id":"demo","tool_name":"../../etc/passwd","arguments":{}}`,
		`{"server_id":"demo","tool_name":"get_test_echo"}`,
		`not-json`,
	}
	for _, payload := range badBodies {
		status, _, body := apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/mcp/invoke", state.token, []byte(payload))
		if status != http.StatusBadRequest {
			t.Fatalf("payload %s: status=%d body=%s", payload, status, body)
		}
	}
	if mock.invoke.ToolName != "" {
		t.Fatalf("controller invoke was called for bad input: %#v", mock.invoke)
	}
}
