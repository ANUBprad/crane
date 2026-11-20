package localapi

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"crane/localauth"
	"crane/persistence"
)

type testState struct {
	auth       *localauth.Manager
	store      *persistence.Store
	token      string
	dbPath     string
	runtime    RuntimeStatus
	supervisor atomic.Bool
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
		auth:    auth,
		store:   store,
		token:   token,
		dbPath:  dbPath,
		runtime: RuntimeStatus{Available: true, Detail: "probe ok"},
	}
	state.supervisor.Store(true)
	t.Cleanup(func() { _ = store.Close() })
	return state
}

func (s *testState) newServer(t *testing.T) *Server {
	t.Helper()
	server, err := NewServer(s.auth, s.store, func() RuntimeStatus { return s.runtime }, s.supervisor.Load)
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
	restartedState := &testState{auth: auth, store: store, token: restoredToken, runtime: state.runtime}
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
	if _, err := NewServer(nil, nil, nil, nil); err == nil {
		t.Fatal("expected missing auth manager to fail")
	}
	state := newTestState(t)
	if _, err := NewServer(state.auth, nil, nil, nil); err == nil {
		t.Fatal("expected missing store to fail")
	}
	if _, err := NewServer(state.auth, state.store, nil, nil); err != nil {
		t.Fatalf("optional probes should default to unavailable: %v", err)
	}
	server, err := NewServer(state.auth, state.store, nil, nil)
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
