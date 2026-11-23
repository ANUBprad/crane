package localapi

import (
	"bufio"
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"crane/localauth"
	"crane/persistence"
	"crane/python_protocol"
)

type testState struct {
	auth       *localauth.Manager
	store      *persistence.Store
	token      string
	dbPath     string
	runtime    RuntimeStatus
	supervisor atomic.Bool
	controller *mockController
}

type mockController struct {
	store     *persistence.Store
	mu        sync.Mutex
	subs      []chan persistence.TaskEvent
	admitted  chan string
	cancelled chan string
}

func newMockController(store *persistence.Store) *mockController {
	return &mockController{
		store:     store,
		admitted:  make(chan string, 8),
		cancelled: make(chan string, 8),
	}
}

func (m *mockController) AdmitTask(id, input string) (persistence.TaskRecord, error) {
	return m.AdmitTaskWithContext(id, input, "", nil, "")
}

func (m *mockController) AdmitTaskWithContext(id, input string, conversationID string, conversationMessages []python_protocol.ConversationMessage, workspace string) (persistence.TaskRecord, error) {
	if _, err := m.store.CreateTask(id, "queued", input); err != nil {
		return persistence.TaskRecord{}, err
	}
	m.admitted <- id
	return m.store.GetTaskRecord(id)
}

func (m *mockController) CancelTask(id string) (persistence.TaskRecord, error) {
	if _, err := m.store.GetTaskRecord(id); err != nil {
		return persistence.TaskRecord{}, err
	}
	if err := m.store.UpdateTaskStatus(id, "cancelled"); err != nil {
		return persistence.TaskRecord{}, err
	}
	m.cancelled <- id
	return m.store.GetTaskRecord(id)
}

func (m *mockController) ApproveTask(id string, approved bool, scope string) (persistence.TaskRecord, error) {
	if _, err := m.store.GetTaskRecord(id); err != nil {
		return persistence.TaskRecord{}, err
	}
	if approved {
		_ = m.store.UpdateTaskStatus(id, "queued")
	} else {
		_ = m.store.CompleteTask(id, "failed", "", "approval rejected")
	}
	return m.store.GetTaskRecord(id)
}

func (m *mockController) SubscribeTaskEvents(id string, after int64) (<-chan persistence.TaskEvent, func(), error) {
	replay, err := m.store.GetTaskEventsAfter(id, after, 128)
	if err != nil {
		return nil, nil, err
	}
	events := make(chan persistence.TaskEvent, 128)
	for _, event := range replay {
		events <- event
	}
	m.mu.Lock()
	m.subs = append(m.subs, events)
	m.mu.Unlock()
	return events, func() {
		m.mu.Lock()
		defer m.mu.Unlock()
		for i, sub := range m.subs {
			if sub == events {
				m.subs = append(m.subs[:i], m.subs[i+1:]...)
				close(sub)
				return
			}
		}
	}, nil
}

func (m *mockController) publish(event persistence.TaskEvent) {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, sub := range m.subs {
		select {
		case sub <- event:
		default:
		}
	}
}

type apiEnvelope struct {
	Data  json.RawMessage `json:"data"`
	Error *struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
}

func newTestState(t *testing.T) *testState {
	t.Helper()
	if runtime.GOOS != "windows" {
		t.Skip("protected installation tokens are validated on Windows")
	}
	root := t.TempDir()
	auth, err := localauth.NewManager(filepath.Join(root, "auth", "token.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := auth.LoadOrCreate(); err != nil {
		t.Fatal(err)
	}
	token, err := auth.CurrentToken()
	if err != nil {
		t.Fatal(err)
	}
	dbPath := filepath.Join(root, "data", "crane.db")
	store, err := persistence.NewStore(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.CreateTask("api-running", "queued", "private input"); err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateTaskStatus("api-running", "running"); err != nil {
		t.Fatal(err)
	}
	if err := store.SaveTaskState("api-running", []byte(`{"state":"running"}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CreateTask("api-done", "queued", "another private input"); err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateTaskStatus("api-done", "succeeded"); err != nil {
		t.Fatal(err)
	}

	state := &testState{
		auth:       auth,
		store:      store,
		token:      token,
		dbPath:     dbPath,
		runtime:    RuntimeStatus{Available: true, Detail: "probe ok"},
		controller: newMockController(store),
	}
	state.supervisor.Store(true)
	t.Cleanup(func() { _ = store.Close() })
	return state
}

func (s *testState) newServer(t *testing.T) *Server {
	t.Helper()
	server, err := NewServer(s.auth, s.store, func() RuntimeStatus { return s.runtime }, s.supervisor.Load, s.controller)
	if err != nil {
		t.Fatal(err)
	}
	return server
}

func apiRequest(t *testing.T, client *http.Client, baseURL, method, path, token string, body []byte) (int, http.Header, []byte) {
	t.Helper()
	req, err := http.NewRequest(method, baseURL+path, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := client.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, response.Header, data
}

func decodeEnvelope(t *testing.T, status int, header http.Header, body []byte) apiEnvelope {
	t.Helper()
	if got := header.Get("Content-Type"); got != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json", got)
	}
	if got := header.Get("X-Content-Type-Options"); got != "nosniff" {
		t.Fatalf("X-Content-Type-Options = %q, want nosniff", got)
	}
	var envelope apiEnvelope
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatalf("decode response %d %q: %v", status, body, err)
	}
	if status >= 400 {
		if envelope.Error == nil || envelope.Error.Code == "" {
			t.Fatalf("error response %d missing error code: %s", status, body)
		}
	} else if envelope.Error != nil {
		t.Fatalf("success response contains error: %s", body)
	}
	return envelope
}

func TestValidateAddressAndLoopbackBinding(t *testing.T) {
	for _, addr := range []string{"127.0.0.1:8787", "127.0.0.1:0"} {
		if err := ValidateAddress(addr); err != nil {
			t.Fatalf("ValidateAddress(%q) error = %v", addr, err)
		}
	}
	for _, addr := range []string{"0.0.0.0:8787", "localhost:8787", ":8787", "127.0.0.1", "127.0.0.1:bad"} {
		if err := ValidateAddress(addr); err == nil {
			t.Fatalf("ValidateAddress(%q) unexpectedly succeeded", addr)
		}
		if _, err := Listen(addr); err == nil {
			t.Fatalf("Listen(%q) unexpectedly succeeded", addr)
		}
	}
	listener, err := Listen("127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	host, _, err := net.SplitHostPort(listener.Addr().String())
	if err != nil || host != "127.0.0.1" {
		t.Fatalf("bound host = %q, error = %v", host, err)
	}
}

func TestAuthenticationAndEndpoints(t *testing.T) {
	state := newTestState(t)
	httpServer := httptest.NewServer(state.newServer(t))
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	status, header, body := apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", "", nil)
	envelope := decodeEnvelope(t, status, header, body)
	if status != http.StatusUnauthorized || envelope.Error.Code != "unauthorized" {
		t.Fatalf("missing token: status = %d, body = %s", status, body)
	}
	if bytes.Contains(body, []byte(state.token)) {
		t.Fatal("unauthorized response leaked the active token")
	}

	status, _, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", "invalid-token", nil)
	if status != http.StatusUnauthorized {
		t.Fatalf("invalid token status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/health", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusMethodNotAllowed || envelope.Error.Code != "method_not_allowed" || header.Get("Allow") != http.MethodGet {
		t.Fatalf("unsupported method: status = %d, allow = %q, body = %s", status, header.Get("Allow"), body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("health status = %d, body = %s", status, body)
	}
	var health HealthResponse
	if err := json.Unmarshal(envelope.Data, &health); err != nil {
		t.Fatal(err)
	}
	if health.Version != APIVersion || !health.Supervisor || !health.Persistence || !health.Runtime.Available || !health.Ready {
		t.Fatalf("unexpected health: %#v", health)
	}
	if bytes.Contains(body, []byte(state.token)) {
		t.Fatal("health response leaked the active token")
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("tasks status = %d, body = %s", status, body)
	}
	var taskList struct {
		Tasks []persistence.TaskRecord `json:"tasks"`
		Count int                      `json:"count"`
	}
	if err := json.Unmarshal(envelope.Data, &taskList); err != nil {
		t.Fatal(err)
	}
	if taskList.Count != 2 || len(taskList.Tasks) != 2 {
		t.Fatalf("unexpected task list: %#v", taskList)
	}
	byID := map[string]persistence.TaskRecord{}
	for _, task := range taskList.Tasks {
		byID[task.ID] = task
	}
	if byID["api-running"].Status != "running" || !byID["api-running"].HasCheckpoint || byID["api-done"].Status != "succeeded" {
		t.Fatalf("task list did not read persisted state: %#v", byID)
	}
	if bytes.Contains(body, []byte("private input")) {
		t.Fatal("task listing exposed raw task input")
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks/api-running", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("task detail status = %d, body = %s", status, body)
	}
	var detail struct {
		Task map[string]any `json:"task"`
	}
	if err := json.Unmarshal(envelope.Data, &detail); err != nil {
		t.Fatal(err)
	}
	if detail.Task["id"] != "api-running" || detail.Task["status"] != "running" {
		t.Fatalf("unexpected task detail: %#v", detail.Task)
	}
	if _, exists := detail.Task["input"]; exists {
		t.Fatal("task detail exposed raw task input")
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks/missing", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusNotFound || envelope.Error.Code != "not_found" {
		t.Fatalf("unknown task: status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks/bad%20id", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusNotFound {
		t.Fatalf("malformed task id: status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks?limit=1", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusBadRequest || envelope.Error.Code != "bad_request" {
		t.Fatalf("query string: status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks", state.token, []byte(`{"unexpected":true}`))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusBadRequest || envelope.Error.Code != "bad_request" {
		t.Fatalf("request body: status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/tasks", state.token, bytes.Repeat([]byte("x"), 1<<20+1))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusRequestEntityTooLarge || envelope.Error.Code != "request_too_large" {
		t.Fatalf("oversized body: status = %d, body = %s", status, body)
	}
}

func TestRotationAndRevocationTakeEffectInRunningServer(t *testing.T) {
	state := newTestState(t)
	httpServer := httptest.NewServer(state.newServer(t))
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	status, _, body := apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", state.token, nil)
	if status != http.StatusOK {
		t.Fatalf("initial token failed: status = %d, body = %s", status, body)
	}

	rotator, err := localauth.NewManager(state.auth.Path())
	if err != nil {
		t.Fatal(err)
	}
	if err := rotator.Load(); err != nil {
		t.Fatal(err)
	}
	if err := rotator.Rotate(); err != nil {
		t.Fatal(err)
	}
	rotated, err := rotator.CurrentToken()
	if err != nil {
		t.Fatal(err)
	}

	status, header, body := apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", state.token, nil)
	envelope := decodeEnvelope(t, status, header, body)
	if status != http.StatusUnauthorized || envelope.Error.Code != "unauthorized" {
		t.Fatalf("rotated-out token accepted: status = %d, body = %s", status, body)
	}
	if bytes.Contains(body, []byte(rotated)) || bytes.Contains(body, []byte(state.token)) {
		t.Fatal("auth error leaked a token")
	}
	status, _, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", rotated, nil)
	if status != http.StatusOK {
		t.Fatalf("rotated token rejected: status = %d, body = %s", status, body)
	}

	revoker, err := localauth.NewManager(state.auth.Path())
	if err != nil {
		t.Fatal(err)
	}
	if err := revoker.Load(); err != nil {
		t.Fatal(err)
	}
	if err := revoker.Revoke(); err != nil {
		t.Fatal(err)
	}
	status, _, body = apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", rotated, nil)
	if status != http.StatusUnauthorized {
		t.Fatalf("revoked token accepted: status = %d, body = %s", status, body)
	}
}

func TestHealthReportsRealComponentState(t *testing.T) {
	state := newTestState(t)
	httpServer := httptest.NewServer(state.newServer(t))
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	fetchHealth := func() HealthResponse {
		t.Helper()
		status, header, body := apiRequest(t, client, httpServer.URL, http.MethodGet, "/v1/health", state.token, nil)
		envelope := decodeEnvelope(t, status, header, body)
		if status != http.StatusOK {
			t.Fatalf("health status = %d, body = %s", status, body)
		}
		var health HealthResponse
		if err := json.Unmarshal(envelope.Data, &health); err != nil {
			t.Fatal(err)
		}
		return health
	}

	state.runtime = RuntimeStatus{Available: false, Detail: "probe failed"}
	health := fetchHealth()
	if health.Ready || health.Runtime.Available || health.Runtime.Detail != "probe failed" {
		t.Fatalf("runtime failure not reflected: %#v", health)
	}

	state.supervisor.Store(false)
	health = fetchHealth()
	if health.Supervisor || health.Ready {
		t.Fatalf("supervisor state not reflected: %#v", health)
	}
	state.supervisor.Store(true)

	if err := state.store.Close(); err != nil {
		t.Fatal(err)
	}
	health = fetchHealth()
	if health.Persistence || health.Ready {
		t.Fatalf("persistence failure not reflected: %#v", health)
	}
}

func TestLifecycleShutdownAndAuthenticationAfterRestart(t *testing.T) {
	state := newTestState(t)
	server := state.newServer(t)
	if err := server.Start(0); err != nil {
		t.Fatal(err)
	}
	addr := server.Addr()
	if !strings.HasPrefix(addr, "127.0.0.1:") {
		t.Fatalf("server bound to %q", addr)
	}
	baseURL := "http://" + addr
	client := &http.Client{Timeout: 5 * time.Second}
	status, _, body := apiRequest(t, client, baseURL, http.MethodGet, "/v1/health", state.token, nil)
	if status != http.StatusOK {
		t.Fatalf("health before shutdown: status = %d, body = %s", status, body)
	}
	if err := server.Shutdown(); err != nil {
		t.Fatalf("Shutdown() error = %v", err)
	}
	if err := server.ServeErr(); err != nil {
		t.Fatalf("ServeErr() after clean shutdown = %v", err)
	}
	if err := <-server.Done(); err != nil {
		t.Fatalf("Done() after clean shutdown = %v", err)
	}

	// Simulate a supervisor restart against the same protected token and DB.
	auth, err := localauth.NewManager(state.auth.Path())
	if err != nil {
		t.Fatal(err)
	}
	if err := auth.Load(); err != nil {
		t.Fatal(err)
	}
	restoredToken, err := auth.CurrentToken()
	if err != nil || restoredToken != state.token {
		t.Fatalf("token after restart = %q, error = %v", restoredToken, err)
	}
	store, err := persistence.NewStore(state.dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	restartedState := &testState{
		auth: auth, store: store, token: restoredToken, runtime: state.runtime,
		controller: newMockController(store),
	}
	restartedState.supervisor.Store(true)
	restarted := restartedState.newServer(t)
	if err := restarted.Start(0); err != nil {
		t.Fatal(err)
	}
	status, _, body = apiRequest(t, client, "http://"+restarted.Addr(), http.MethodGet, "/v1/tasks", restoredToken, nil)
	if status != http.StatusOK {
		t.Fatalf("health after restart: status = %d, body = %s", status, body)
	}
	if !strings.Contains(string(body), "api-running") {
		t.Fatalf("persisted task missing after restart: %s", body)
	}
	if err := restarted.Shutdown(); err != nil {
		t.Fatalf("second Shutdown() error = %v", err)
	}
	if err := restarted.Shutdown(); err != nil {
		t.Fatalf("repeated Shutdown() error = %v", err)
	}
}

func TestNewServerRejectsMissingDependencies(t *testing.T) {
	if _, err := NewServer(nil, nil, nil, nil, nil); err == nil {
		t.Fatal("expected missing auth manager to fail")
	}
	state := newTestState(t)
	if _, err := NewServer(state.auth, nil, nil, nil, nil); err == nil {
		t.Fatal("expected missing store to fail")
	}
	if _, err := NewServer(state.auth, state.store, nil, nil, nil); err != nil {
		t.Fatalf("optional probes should default to unavailable: %v", err)
	}
	server, err := NewServer(state.auth, state.store, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := server.Start(-1); err == nil {
		t.Fatal("expected invalid port to fail")
	}
	if err := server.Shutdown(); err != nil {
		t.Fatalf("Shutdown() before Start() error = %v", err)
	}
	_ = server.Addr()
}

type sseEvent struct {
	id   string
	name string
	data string
}

func readSSEEvent(t *testing.T, reader *bufio.Reader) sseEvent {
	t.Helper()
	event := sseEvent{}
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("read SSE event: %v", err)
		}
		line = strings.TrimRight(line, "\r\n")
		if line == "" {
			return event
		}
		switch {
		case strings.HasPrefix(line, "id: "):
			event.id = strings.TrimPrefix(line, "id: ")
		case strings.HasPrefix(line, "event: "):
			event.name = strings.TrimPrefix(line, "event: ")
		case strings.HasPrefix(line, "data: "):
			event.data = strings.TrimPrefix(line, "data: ")
		case strings.HasPrefix(line, ":"):
			// Keep-alive comment.
		default:
			t.Fatalf("unexpected SSE line %q", line)
		}
	}
}

func openEventStream(t *testing.T, client *http.Client, baseURL, id, token string) (*http.Response, *bufio.Reader) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, baseURL+"/v1/tasks/"+id+"/events?cursor=0", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	response, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		defer response.Body.Close()
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("event stream status = %d, body = %s", response.StatusCode, body)
	}
	if got := response.Header.Get("Content-Type"); got != "text/event-stream" {
		defer response.Body.Close()
		t.Fatalf("event stream Content-Type = %q", got)
	}
	return response, bufio.NewReader(response.Body)
}

func TestTaskAdmissionCancellationAndEventStream(t *testing.T) {
	state := newTestState(t)
	httpServer := httptest.NewServer(state.newServer(t))
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	status, header, body := apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/tasks", state.token,
		[]byte(`{"id":"api-submitted","input":"do useful work"}`))
	envelope := decodeEnvelope(t, status, header, body)
	if status != http.StatusCreated {
		t.Fatalf("task admission status = %d, body = %s", status, body)
	}
	var created struct {
		Task persistence.TaskRecord `json:"task"`
	}
	if err := json.Unmarshal(envelope.Data, &created); err != nil {
		t.Fatal(err)
	}
	if created.Task.ID != "api-submitted" || created.Task.Status != "queued" {
		t.Fatalf("unexpected admitted task: %#v", created.Task)
	}
	select {
	case id := <-state.controller.admitted:
		if id != "api-submitted" {
			t.Fatalf("controller admitted %q", id)
		}
	default:
		t.Fatal("controller did not receive the admitted task")
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/tasks", state.token,
		[]byte(`{"id":"api-submitted","input":"duplicate"}`))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusConflict || envelope.Error.Code != "task_already_exists" {
		t.Fatalf("duplicate admission: status = %d, body = %s", status, body)
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/tasks", state.token,
		[]byte(`{"id":"bad id","input":"work"}`))
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusBadRequest {
		t.Fatalf("invalid admission: status = %d, body = %s", status, body)
	}

	response, reader := openEventStream(t, client, httpServer.URL, "api-submitted", state.token)
	snapshot := readSSEEvent(t, reader)
	if snapshot.name != "snapshot" || !strings.Contains(snapshot.data, `"id":"api-submitted"`) {
		t.Fatalf("unexpected SSE snapshot: %#v", snapshot)
	}
	_ = response.Body.Close()

	select {
	case id := <-state.controller.cancelled:
		t.Fatalf("stream disconnect cancelled task %q", id)
	default:
	}

	status, header, body = apiRequest(t, client, httpServer.URL, http.MethodPost, "/v1/tasks/api-submitted/cancel", state.token, nil)
	envelope = decodeEnvelope(t, status, header, body)
	if status != http.StatusOK {
		t.Fatalf("cancel status = %d, body = %s", status, body)
	}
	select {
	case id := <-state.controller.cancelled:
		if id != "api-submitted" {
			t.Fatalf("controller cancelled %q", id)
		}
	default:
		t.Fatal("controller did not receive the cancellation")
	}
}

func TestEventStreamReplaysPersistedEventsWithCursor(t *testing.T) {
	state := newTestState(t)
	eventID, err := state.store.AppendTaskEvent("api-running", "task_finished", map[string]any{"status": "running"})
	if err != nil {
		t.Fatal(err)
	}
	httpServer := httptest.NewServer(state.newServer(t))
	defer httpServer.Close()
	client := &http.Client{Timeout: 5 * time.Second}

	response, reader := openEventStream(t, client, httpServer.URL, "api-running", state.token)
	snapshot := readSSEEvent(t, reader)
	if snapshot.name != "snapshot" {
		t.Fatalf("unexpected snapshot event: %#v", snapshot)
	}
	event := readSSEEvent(t, reader)
	if event.id != strconv.FormatInt(eventID, 10) || event.name != "task_finished" {
		t.Fatalf("unexpected replayed event: %#v", event)
	}
	_ = response.Body.Close()

	req, err := http.NewRequest(http.MethodGet,
		httpServer.URL+"/v1/tasks/api-running/events?cursor="+strconv.FormatInt(eventID, 10), nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+state.token)
	response, err = client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("cursor stream status = %d", response.StatusCode)
	}
	reader = bufio.NewReader(response.Body)
	_ = readSSEEvent(t, reader)
	state.controller.publish(persistence.TaskEvent{
		ID: eventID + 1, TaskID: "api-running", Type: "live_event",
		Payload: json.RawMessage(`{"ok":true}`),
	})
	live := readSSEEvent(t, reader)
	if live.id != strconv.FormatInt(eventID+1, 10) || live.name != "live_event" {
		t.Fatalf("unexpected live event: %#v", live)
	}
}
