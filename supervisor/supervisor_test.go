package supervisor

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"crane/persistence"
	"crane/python_protocol"
)

// TestHelperRuntime runs this test binary as a stand-in Python child process
// when CRANE_TEST_RUNTIME_HELPER=1. It speaks the same versioned NDJSON
// protocol as crane.python_runtime without requiring Python in Go unit tests.
func TestHelperRuntime(t *testing.T) {
	if os.Getenv("CRANE_TEST_RUNTIME_HELPER") != "1" {
		return
	}
	write := func(message any) {
		encoded, err := json.Marshal(message)
		if err != nil {
			return
		}
		_, _ = fmt.Fprintln(os.Stdout, string(encoded))
	}
	write(map[string]any{
		"version": python_protocol.ProtocolVersion, "type": python_protocol.TypeRuntimeIdentity,
		"runtime_version": "0.1.0", "python_version": "helper", "pid": os.Getpid(),
	})

	mode := os.Getenv("CRANE_TEST_RUNTIME_MODE")
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 0, 64<<10), python_protocol.MaxMessageBytes)
	for scanner.Scan() {
		var message struct {
			Type   string `json:"type"`
			TaskID string `json:"task_id"`
		}
		if json.Unmarshal(scanner.Bytes(), &message) != nil {
			continue
		}
		switch message.Type {
		case python_protocol.TypeTaskSubmission:
			if mode == "wait" {
				write(taskStateMessage(message.TaskID, "running", "executing", false))
				continue
			}
			write(taskStateMessage(message.TaskID, "running", "executing", false))
			write(map[string]any{
				"version": python_protocol.ProtocolVersion, "type": python_protocol.TypeTaskEvent,
				"task_id": message.TaskID, "event_type": "workflow_started",
				"payload": map[string]any{"provider": "helper"},
			})
			write(taskResultMessage(message.TaskID, "succeeded", map[string]any{"response": "helper result"}, ""))
			return
		case python_protocol.TypeTaskCancellation:
			write(taskStateMessage(message.TaskID, "cancelled", "cancelled", true))
			write(map[string]any{
				"version": python_protocol.ProtocolVersion, "type": python_protocol.TypeTaskEvent,
				"task_id": message.TaskID, "event_type": "cancelled",
				"payload": map[string]any{"reason": "supervisor_request"},
			})
			write(taskResultMessage(message.TaskID, "cancelled", nil, "task cancelled by supervisor request"))
			return
		}
	}
}

func taskStateMessage(id, state, step string, cancelled bool) map[string]any {
	return map[string]any{
		"version": python_protocol.ProtocolVersion, "type": python_protocol.TypeTaskState,
		"task_id": id, "state": state, "current_step": step, "graph_steps": 1,
		"wall_clock_ms": 1, "result_summary": nil, "failure_reason": nil,
		"cancelled": cancelled,
		"checkpoint": map[string]any{
			"task_id": id, "state": state, "current_step": step,
			"cancelled": cancelled, "last_tool_side_effect": nil,
		},
	}
}

func taskResultMessage(id, status string, result any, failure string) map[string]any {
	return map[string]any{
		"version": python_protocol.ProtocolVersion, "type": python_protocol.TypeTaskResult,
		"task_id": id, "status": status, "result": result,
		"result_summary": status, "failure_reason": failure,
		"graph_steps": 2, "wall_clock_ms": 2, "current_step": status,
		"checkpoint": map[string]any{"task_id": id, "state": status, "current_step": status},
	}
}

// TestNewSupervisor creates a new Supervisor and verifies it is non-nil.
func TestNewSupervisor(t *testing.T) {
	s := New()
	if s == nil {
		t.Fatal("Expected non-nil Supervisor")
	}
}

// TestSupervisorStop verifies Stop() does not panic.
func TestSupervisorStop(t *testing.T) {
	s := New()
	s.Stop() // should not panic
}

func TestMCPSubmissionContextAndToolResultPersistence(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	sup := NewWithStore(store)

	server := persistence.MCPServerRecord{
		ID: "demo", Name: "Demo", Transport: "stdio", Command: "python",
		Args: []string{"server.py"}, Env: map[string]string{"CRANE_MCP_REGION": "private"},
		Enabled: true, ConnectTimeoutSeconds: 5, InvocationTimeoutSeconds: 20,
	}
	if err := store.UpsertMCPServer(server); err != nil {
		t.Fatal(err)
	}
	fingerprint := persistence.ApprovalFingerprint("demo", "delete_test_record", map[string]any{"id": "one"})
	request := persistence.ApprovalRequestRecord{
		ID: "approval-1", ServerID: "demo", ToolName: "delete_test_record",
		Arguments: json.RawMessage(`{"id":"one"}`), Fingerprint: fingerprint,
	}
	if err := store.CreateApprovalRequest(request); err != nil {
		t.Fatal(err)
	}
	if _, err := store.DecideApprovalRequest("approval-1", "granted", "local-user"); err != nil {
		t.Fatal(err)
	}

	servers, grants, err := sup.mcpSubmissionContext()
	if err != nil {
		t.Fatal(err)
	}
	if len(servers) != 1 || servers[0].Env["CRANE_MCP_REGION"] != "private" {
		t.Fatalf("MCP submission context = %#v", servers)
	}
	if len(grants) != 1 || grants[0] != fingerprint {
		t.Fatalf("approval grants = %#v", grants)
	}

	if _, err := store.CreateTask("mcp-task", "queued", "use a tool"); err != nil {
		t.Fatal(err)
	}
	frame := []byte(`{"version":"0.1.0","type":"tool_result","task_id":"mcp-task","invocation_id":"inv-mcp-1","server_id":"demo","server_name":"Demo","tool_name":"get_test_echo","arguments":{"value":"token=abc123"},"result":{"text":"ok"},"policy_decision":"allowed","duration_ms":4,"status":"succeeded"}`)
	if sup.handleProtocolFrame("mcp-task", &runningTask{id: "mcp-task", done: make(chan struct{})}, frame) {
		t.Fatal("tool_result frame ended the task")
	}
	invocations, err := store.ListMCPInvocations(10)
	if err != nil || len(invocations) != 1 {
		t.Fatalf("ListMCPInvocations() = %#v, %v", invocations, err)
	}
	if strings.Contains(string(invocations[0].Arguments), "abc123") {
		t.Fatalf("tool_result leaked a secret: %s", invocations[0].Arguments)
	}
}

// TestVersion verifies the Version() function returns the expected version string.
func TestVersion(t *testing.T) {
	ver := Version()
	if ver == "" {
		t.Fatal("Expected non-empty version string")
	}
	if ver != "0.1.0" {
		t.Fatalf("Expected version 0.1.0, got: %s", ver)
	}
}

// TestHealthCheck verifies HealthCheck() does not panic.
func TestHealthCheck(t *testing.T) {
	err := HealthCheck()
	// Should not panic; may return an error if the default store is unavailable.
	_ = err
}

// TestPersistentState_jsonMarshalUnmarshal verifies that TaskState can be
// marshaled and unmarshaled correctly via JSON.
func TestPersistentState_jsonMarshalUnmarshal(t *testing.T) {
	state := python_protocol.TaskState{
		Message: python_protocol.Message{
			Version: python_protocol.ProtocolVersion,
			Type:    python_protocol.TypeTaskState,
		},
		TaskId:      "test-struct-1",
		State:       "running",
		CurrentStep: "initialization",
		GraphSteps:  0,
		WallClockMs: 0,
		Cancelled:   false,
	}
	data, err := json.Marshal(state)
	if err != nil {
		t.Fatalf("Failed to marshal TaskState: %v", err)
	}
	if len(data) == 0 {
		t.Fatal("Expected non-empty marshalled data")
	}

	var decoded python_protocol.TaskState
	err = json.Unmarshal(data, &decoded)
	if err != nil {
		t.Fatalf("Failed to unmarshal TaskState: %v", err)
	}
	if decoded.TaskId != "test-struct-1" {
		t.Fatalf("Expected task_id test-struct-1, got: %s", decoded.TaskId)
	}
}

func TestSupervisorLifecycleAndRuntimeProbe(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	supervisor := NewWithStore(store)
	defer supervisor.Close()
	if supervisor.IsRunning() {
		t.Fatal("supervisor reported running before Start()")
	}
	if err := supervisor.Start(); err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if !supervisor.IsRunning() {
		t.Fatal("supervisor did not report running after Start()")
	}
	if err := supervisor.HealthCheck(); err != nil {
		t.Fatalf("HealthCheck() error = %v", err)
	}

	if _, err := exec.LookPath("python"); err != nil {
		t.Skip("python is not available on PATH")
	}
	root, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CRANE_PYTHON_HOME", root)
	t.Setenv("CRANE_PYTHON", "python")
	available, detail := supervisor.RuntimeAvailable()
	if !available || detail == "" {
		t.Fatalf("RuntimeAvailable() = %v, %q", available, detail)
	}

	supervisor.Stop()
	if supervisor.IsRunning() {
		t.Fatal("supervisor still reported running after Stop()")
	}
	if err := supervisor.HealthCheck(); err == nil {
		t.Fatal("HealthCheck() succeeded after Stop()")
	}
}

func newHelperSupervisor(t *testing.T, mode string) (*Supervisor, *persistence.Store) {
	t.Helper()
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	s := NewWithStore(store)
	s.pythonRuntimePath = os.Args[0]
	s.pythonArgs = []string{"-test.run=^TestHelperRuntime$"}
	t.Setenv("CRANE_TEST_RUNTIME_HELPER", "1")
	t.Setenv("CRANE_TEST_RUNTIME_MODE", mode)
	return s, store
}

func waitForStatus(t *testing.T, store *persistence.Store, id, status string) persistence.TaskRecord {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		record, err := store.GetTaskRecord(id)
		if err == nil && record.Status == status {
			return record
		}
		time.Sleep(20 * time.Millisecond)
	}
	record, err := store.GetTaskRecord(id)
	if err != nil {
		t.Fatalf("GetTaskRecord(%q): %v", id, err)
	}
	t.Fatalf("task %q status = %q, want %q", id, record.Status, status)
	return record
}

func hasEventType(events []persistence.TaskEvent, eventType string) bool {
	for _, event := range events {
		if event.Type == eventType {
			return true
		}
	}
	return false
}

func TestAdmitTaskExecutesAndPersistsResult(t *testing.T) {
	s, store := newHelperSupervisor(t, "success")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()

	if _, err := s.AdmitTask("exec-task", "do useful work"); err != nil {
		t.Fatalf("AdmitTask() error = %v", err)
	}
	record := waitForStatus(t, store, "exec-task", "succeeded")
	if record.Result == "" || record.Result != `{"response":"helper result"}` {
		t.Fatalf("unexpected persisted result: %q", record.Result)
	}
	if record.WorkflowRef == "" {
		t.Fatal("workflow reference was not persisted")
	}
	checkpoint, ok := store.GetLatestTaskState("exec-task")
	if !ok || !json.Valid(checkpoint) {
		t.Fatalf("checkpoint was not persisted: %s", checkpoint)
	}
	events, err := store.GetTaskEventsAfter("exec-task", 0, 100)
	if err != nil {
		t.Fatal(err)
	}
	for _, required := range []string{"task_admitted", "task_starting", "checkpoint", "workflow_started", "task_finished"} {
		if !hasEventType(events, required) {
			t.Fatalf("missing persisted event %q in %#v", required, events)
		}
	}
}

func TestAdmissionRejectsDuplicateTask(t *testing.T) {
	s, _ := newHelperSupervisor(t, "success")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	if _, err := s.AdmitTask("duplicate-task", "first"); err != nil {
		t.Fatal(err)
	}
	_, err := s.AdmitTask("duplicate-task", "second")
	if !errors.Is(err, persistence.ErrDuplicateTask) {
		t.Fatalf("duplicate admission error = %v, want ErrDuplicateTask", err)
	}
}

func TestCancelTaskConfirmsPythonOutcome(t *testing.T) {
	s, store := newHelperSupervisor(t, "wait")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	if _, err := s.AdmitTask("cancel-task", "long running work"); err != nil {
		t.Fatal(err)
	}
	waitForStatus(t, store, "cancel-task", "running")
	record, err := s.CancelTask("cancel-task")
	if err != nil {
		t.Fatalf("CancelTask() error = %v", err)
	}
	if record.Status != "cancelled" {
		t.Fatalf("status after cancel = %q (result=%q error=%q), want cancelled", record.Status, record.Result, record.Error)
	}
	events, err := store.GetTaskEventsAfter("cancel-task", 0, 100)
	if err != nil {
		t.Fatal(err)
	}
	if !hasEventType(events, "cancellation_requested") || !hasEventType(events, "cancelled") {
		t.Fatalf("cancellation events missing: %#v", events)
	}
}

func newRealPythonSupervisor(t *testing.T) (*Supervisor, *persistence.Store) {
	t.Helper()
	if _, err := exec.LookPath("python"); err != nil {
		t.Skip("python is not available on PATH")
	}
	root, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = store.Close() })
	s := NewWithStore(store)
	s.SetPythonRuntime("python")
	t.Setenv("CRANE_PYTHON_HOME", root)
	return s, store
}

func waitForStatusTimeout(t *testing.T, store *persistence.Store, id, status string, timeout time.Duration) persistence.TaskRecord {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		record, err := store.GetTaskRecord(id)
		if err == nil && record.Status == status {
			return record
		}
		time.Sleep(25 * time.Millisecond)
	}
	record, err := store.GetTaskRecord(id)
	if err != nil {
		t.Fatalf("GetTaskRecord(%q): %v", id, err)
	}
	t.Fatalf("task %q status = %q, want %q", id, record.Status, status)
	return record
}

func TestRealPythonRuntimeExecutesThroughLangGraph(t *testing.T) {
	s, store := newRealPythonSupervisor(t)
	t.Setenv("CRANE_MODEL_PROVIDER", "local")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	if _, err := s.AdmitTask("real-python-task", "Review this repository for execution facts"); err != nil {
		t.Fatal(err)
	}
	record := waitForStatusTimeout(t, store, "real-python-task", "succeeded", 30*time.Second)
	if !strings.Contains(record.Result, `"execution_mode":"deterministic_local"`) {
		t.Fatalf("unexpected real Python result: %q", record.Result)
	}
	if !strings.Contains(record.Result, `"model_output":null`) {
		t.Fatalf("deterministic mode invented model output: %q", record.Result)
	}
}

func TestRealPythonRuntimeProviderFailureIsExplicit(t *testing.T) {
	s, store := newRealPythonSupervisor(t)
	t.Setenv("CRANE_MODEL_PROVIDER", "ollama")
	t.Setenv("CRANE_MODEL", "")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	if _, err := s.AdmitTask("provider-failure-task", "This task must not silently fall back"); err != nil {
		t.Fatal(err)
	}
	record := waitForStatusTimeout(t, store, "provider-failure-task", "failed", 30*time.Second)
	if !strings.Contains(record.Result, `"code":"provider_not_configured"`) {
		t.Fatalf("unexpected provider failure result: %q", record.Result)
	}
	if strings.Contains(record.Result, `"execution_mode":"deterministic_local"`) {
		t.Fatalf("provider failure silently fell back: %q", record.Result)
	}
}

// useFakeLangChainOllama keeps the supervisor integration tests fast while
// preserving the real provider configuration, LangGraph and SQLite path. The
// real ChatOllama constructor is covered separately by the Python tests.
func useFakeLangChainOllama(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	stub := `import os


class _Response:
    def __init__(self, content):
        self.content = content


class ChatOllama:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def invoke(self, prompt):
        if os.environ.get("CRANE_TEST_FAKE_CHAT_FAILURE") == "1":
            raise RuntimeError("mocked invocation failure")
        return _Response("Mocked Ollama response")
`
	if err := os.WriteFile(filepath.Join(dir, "langchain_ollama.py"), []byte(stub), 0o600); err != nil {
		t.Fatal(err)
	}
	existing := os.Getenv("PYTHONPATH")
	if existing == "" {
		t.Setenv("PYTHONPATH", dir)
		return
	}
	t.Setenv("PYTHONPATH", dir+string(os.PathListSeparator)+existing)
}

func fakeOllamaServer() *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprint(w, `{"models":[{"name":"crane-test:latest"}]}`)
	}))
}

func TestRealPythonRuntimeOllamaProviderPersistsModelOutput(t *testing.T) {
	server := fakeOllamaServer()
	defer server.Close()
	s, store := newRealPythonSupervisor(t)
	t.Setenv("CRANE_MODEL_PROVIDER", "ollama")
	t.Setenv("CRANE_MODEL", "crane-test:latest")
	t.Setenv("OLLAMA_HOST", server.URL)
	useFakeLangChainOllama(t)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	if _, err := s.AdmitTask("ollama-provider-task", "Return a concise mocked provider response"); err != nil {
		t.Fatal(err)
	}
	record := waitForStatusTimeout(t, store, "ollama-provider-task", "succeeded", 55*time.Second)
	for _, expected := range []string{
		`"execution_mode":"langchain_ollama"`,
		`"provider":"ollama"`,
		`"model":"crane-test:latest"`,
		`"model_output":"Mocked Ollama response"`,
	} {
		if !strings.Contains(record.Result, expected) {
			t.Fatalf("persisted result missing %s: %q", expected, record.Result)
		}
	}
}

func TestRealPythonRuntimeOllamaFailurePersistsWithoutFallback(t *testing.T) {
	server := fakeOllamaServer()
	defer server.Close()
	s, store := newRealPythonSupervisor(t)
	t.Setenv("CRANE_MODEL_PROVIDER", "ollama")
	t.Setenv("CRANE_MODEL", "crane-test:latest")
	t.Setenv("OLLAMA_HOST", server.URL)
	t.Setenv("CRANE_TEST_FAKE_CHAT_FAILURE", "1")
	useFakeLangChainOllama(t)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	if _, err := s.AdmitTask("ollama-failure-task", "This task must not silently fall back"); err != nil {
		t.Fatal(err)
	}
	record := waitForStatusTimeout(t, store, "ollama-failure-task", "failed", 55*time.Second)
	if !strings.Contains(record.Result, `"code":"provider_invocation_failed"`) {
		t.Fatalf("unexpected Ollama failure result: %q", record.Result)
	}
	if strings.Contains(record.Result, `"execution_mode":"deterministic_local"`) ||
		strings.Contains(record.Result, `"model_output"`) {
		t.Fatalf("Ollama failure fell back or invented output: %q", record.Result)
	}
}

func TestCancelImmediatelyAfterAdmissionStaysCancelled(t *testing.T) {
	s, store := newHelperSupervisor(t, "wait")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	defer s.Stop()

	ids := make([]string, 0, 10)
	for i := 0; i < 10; i++ {
		id := fmt.Sprintf("race-cancel-%d", i)
		ids = append(ids, id)
		if _, err := s.AdmitTask(id, "cancel before registration"); err != nil {
			t.Fatal(err)
		}
		record, err := s.CancelTask(id)
		if err != nil {
			t.Fatalf("CancelTask(%q) error = %v", id, err)
		}
		if record.Status != "cancelled" {
			t.Fatalf("CancelTask(%q) status = %q, want cancelled", id, record.Status)
		}
	}
	// Give any already-started child time to report a late result; that result
	// must not overwrite the supervisor's confirmed cancellation.
	time.Sleep(500 * time.Millisecond)
	for _, id := range ids {
		record, err := store.GetTaskRecord(id)
		if err != nil {
			t.Fatal(err)
		}
		if record.Status != "cancelled" {
			t.Fatalf("task %q status drifted to %q after cancellation", id, record.Status)
		}
	}
}

func TestTaskExecutionTimeoutAllowsBoundedLocalInference(t *testing.T) {
	if taskExecutionTimeout < 180*time.Second || taskExecutionTimeout > 300*time.Second {
		t.Fatalf("taskExecutionTimeout = %s, want a bounded timeout that allows a cold local model", taskExecutionTimeout)
	}
}

func TestStartupRecoveryPolicies(t *testing.T) {
	t.Run("queued task resumes", func(t *testing.T) {
		s, store := newHelperSupervisor(t, "success")
		if _, err := store.CreateTask("recover-queued", "queued", "work"); err != nil {
			t.Fatal(err)
		}
		if err := s.Start(); err != nil {
			t.Fatal(err)
		}
		defer s.Stop()
		waitForStatus(t, store, "recover-queued", "succeeded")
	})

	t.Run("ambiguous running task fails safely", func(t *testing.T) {
		s, store := newHelperSupervisor(t, "success")
		if _, err := store.CreateTask("recover-ambiguous", "queued", "work"); err != nil {
			t.Fatal(err)
		}
		if err := store.UpdateTaskStatus("recover-ambiguous", "running"); err != nil {
			t.Fatal(err)
		}
		if err := store.SaveTaskState("recover-ambiguous", []byte(`{"state":"running","current_step":"executing","selected_tool":{"name":"mutation"}}`)); err != nil {
			t.Fatal(err)
		}
		if err := s.Start(); err != nil {
			t.Fatal(err)
		}
		defer s.Stop()
		record := waitForStatus(t, store, "recover-ambiguous", "failed")
		if record.Result != `{"code":"ambiguous_recovery"}` {
			t.Fatalf("unexpected recovery result: %q", record.Result)
		}
	})

	t.Run("unconfirmed cancellation fails", func(t *testing.T) {
		s, store := newHelperSupervisor(t, "success")
		if _, err := store.CreateTask("recover-cancelling", "queued", "work"); err != nil {
			t.Fatal(err)
		}
		if err := store.UpdateTaskStatus("recover-cancelling", "cancelling"); err != nil {
			t.Fatal(err)
		}
		if err := s.Start(); err != nil {
			t.Fatal(err)
		}
		defer s.Stop()
		record := waitForStatus(t, store, "recover-cancelling", "failed")
		if record.Result != `{"code":"cancellation_unconfirmed"}` {
			t.Fatalf("unexpected recovery result: %q", record.Result)
		}
	})

	t.Run("waiting approval is preserved", func(t *testing.T) {
		s, store := newHelperSupervisor(t, "success")
		if _, err := store.CreateTask("recover-approval", "queued", "work"); err != nil {
			t.Fatal(err)
		}
		if err := store.UpdateTaskStatus("recover-approval", "waiting_approval"); err != nil {
			t.Fatal(err)
		}
		if err := s.Start(); err != nil {
			t.Fatal(err)
		}
		defer s.Stop()
		waitForStatus(t, store, "recover-approval", "waiting_approval")
	})
}
