// Package localapi implements the authenticated loopback API owned by the Go
// supervisor. It serves only real supervisor, runtime and persistence state.
package localapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"crane/localauth"
	"crane/persistence"
)

const (
	APIVersion      = "1"
	maxRequestBody  = 1 << 20
	maxTaskInput    = 64 << 10
	defaultAPIPort  = 8787
	shutdownTimeout = 5 * time.Second
	sseKeepAlive    = 15 * time.Second
)

// TaskStore is the read-only persistence surface required by the local API.
type TaskStore interface {
	HealthCheck() error
	ListTaskRecords() ([]persistence.TaskRecord, error)
	GetTaskRecord(id string) (persistence.TaskRecord, error)
}

// TaskController is the supervisor-owned task surface exposed by the API.
// Admission, cancellation and event streaming are deliberately separated from
// HTTP transport so the Go supervisor owns all task lifecycle decisions.
type TaskController interface {
	AdmitTask(id, input string) (persistence.TaskRecord, error)
	CancelTask(id string) (persistence.TaskRecord, error)
	SubscribeTaskEvents(id string, after int64) (<-chan persistence.TaskEvent, func(), error)
}

// RuntimeStatus reports the actual Python runtime probe result.
type RuntimeStatus struct {
	Available bool   `json:"available"`
	Detail    string `json:"detail"`
}

type HealthResponse struct {
	Version     string        `json:"version"`
	Supervisor  bool          `json:"supervisor"`
	Runtime     RuntimeStatus `json:"runtime"`
	Persistence bool          `json:"persistence"`
	Ready       bool          `json:"ready"`
}

type Server struct {
	auth       *localauth.Manager
	store      TaskStore
	runtime    func() RuntimeStatus
	supervisor func() bool
	controller TaskController
	mcp        MCPController
	httpSrv    *http.Server
	listener   net.Listener
	serveErr   chan error
	serveDone  chan error
}

// DefaultPort is the fixed loopback port used by the supervisor and desktop
// client. There is deliberately no host configuration: all-interface binding
// is not supported.
func DefaultPort() int { return defaultAPIPort }

// ValidateAddress accepts only an explicit IPv4 loopback address.
func ValidateAddress(addr string) error {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return errors.New("local API address must be host:port")
	}
	if host != "127.0.0.1" {
		return errors.New("local API must bind to 127.0.0.1")
	}
	value, err := strconv.Atoi(port)
	if err != nil || value < 0 || value > 65535 {
		return errors.New("local API port must be between 0 and 65535")
	}
	return nil
}

// Listen opens a listener only on the IPv4 loopback interface.
func Listen(addr string) (net.Listener, error) {
	if err := ValidateAddress(addr); err != nil {
		return nil, err
	}
	return net.Listen("tcp", addr)
}

// NewServer creates an API server. A nil store or auth manager is rejected.
// Nil probes report their component as unavailable. A nil controller disables
// task mutation and streaming while leaving authenticated reads intact.
func NewServer(
	auth *localauth.Manager,
	store TaskStore,
	runtime func() RuntimeStatus,
	supervisor func() bool,
	controller TaskController,
) (*Server, error) {
	if auth == nil {
		return nil, errors.New("local API requires an authentication manager")
	}
	if store == nil {
		return nil, errors.New("local API requires a persistence store")
	}
	if runtime == nil {
		runtime = func() RuntimeStatus {
			return RuntimeStatus{Available: false, Detail: "runtime probe unavailable"}
		}
	}
	if supervisor == nil {
		supervisor = func() bool { return false }
	}
	return &Server{
		auth:       auth,
		store:      store,
		runtime:    runtime,
		supervisor: supervisor,
		controller: controller,
		serveErr:   make(chan error, 1),
		serveDone:  make(chan error, 1),
	}, nil
}

// Start binds to 127.0.0.1 on port and begins serving in the background.
// WriteTimeout is disabled because event streams are long-lived; each request
// path applies its own bounds.
func (s *Server) Start(port int) error {
	if port < 0 || port > 65535 {
		return errors.New("local API port must be between 0 and 65535")
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	listener, err := Listen(addr)
	if err != nil {
		return err
	}
	s.httpSrv = &http.Server{
		Addr:              addr,
		Handler:           s,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}
	s.listener = listener
	go func() {
		err := s.httpSrv.Serve(listener)
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			select {
			case s.serveErr <- err:
			default:
			}
		}
		if errors.Is(err, http.ErrServerClosed) {
			err = nil
		}
		select {
		case s.serveDone <- err:
		default:
		}
	}()
	return nil
}

// Addr returns the bound loopback address.
func (s *Server) Addr() string {
	if s.listener == nil {
		return ""
	}
	return s.listener.Addr().String()
}

// ServeErr reports an unexpected serve failure, if any.
func (s *Server) ServeErr() error {
	select {
	case err := <-s.serveErr:
		return err
	default:
		return nil
	}
}

// Done reports when the HTTP server has stopped. It yields nil after a clean
// shutdown and a non-nil error after an unexpected serve failure.
func (s *Server) Done() <-chan error { return s.serveDone }

// Shutdown stops accepting requests and closes the listener.
func (s *Server) Shutdown() error {
	if s.httpSrv == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	return s.httpSrv.Shutdown(ctx)
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.ContentLength > maxRequestBody {
		writeError(w, http.StatusRequestEntityTooLarge, "request_too_large")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBody)

	header := r.Header.Get("Authorization")
	if len(header) < 8 || !strings.EqualFold(header[:7], "Bearer ") {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}
	// Reload protected state so rotation or revocation performed by a trusted
	// CLI process takes effect immediately in the running supervisor.
	if err := s.auth.Load(); err != nil || !s.auth.Verify(strings.TrimSpace(header[7:])) {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	path := r.URL.Path
	switch {
	case path == "/v1/health":
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		if rejectQueryAndBody(w, r) {
			return
		}
		s.handleHealth(w)

	case path == "/v1/tasks":
		switch r.Method {
		case http.MethodGet:
			if rejectQueryAndBody(w, r) {
				return
			}
			s.handleTasks(w)
		case http.MethodPost:
			s.handleTaskCreate(w, r)
		default:
			methodNotAllowed(w, http.MethodGet, http.MethodPost)
		}

	case strings.HasPrefix(path, "/v1/tasks/"):
		rest := strings.TrimPrefix(path, "/v1/tasks/")
		switch {
		case strings.HasSuffix(rest, "/events"):
			if r.Method != http.MethodGet {
				methodNotAllowed(w, http.MethodGet)
				return
			}
			id := strings.TrimSuffix(rest, "/events")
			if !validTaskPathID(id) {
				writeError(w, http.StatusNotFound, "not_found")
				return
			}
			s.handleTaskEvents(w, r, id)

		case strings.HasSuffix(rest, "/cancel"):
			if r.Method != http.MethodPost {
				methodNotAllowed(w, http.MethodPost)
				return
			}
			id := strings.TrimSuffix(rest, "/cancel")
			if !validTaskPathID(id) {
				writeError(w, http.StatusNotFound, "not_found")
				return
			}
			if r.URL.RawQuery != "" || r.ContentLength > 0 || len(r.TransferEncoding) > 0 {
				writeError(w, http.StatusBadRequest, "bad_request")
				return
			}
			s.handleTaskCancel(w, id)

		case !strings.Contains(rest, "/"):
			if r.Method != http.MethodGet {
				methodNotAllowed(w, http.MethodGet)
				return
			}
			if !validTaskPathID(rest) {
				writeError(w, http.StatusNotFound, "not_found")
				return
			}
			if rejectQueryAndBody(w, r) {
				return
			}
			s.handleTask(w, rest)

		default:
			writeError(w, http.StatusNotFound, "not_found")
		}

	case path == "/v1/mcp/status":
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		if rejectQueryAndBody(w, r) {
			return
		}
		s.handleMCPStatus(w)

	case path == "/v1/mcp/servers":
		s.handleMCPServers(w, r)

	case strings.HasPrefix(path, "/v1/mcp/servers/"):
		s.handleMCPServerPath(w, r, strings.TrimPrefix(path, "/v1/mcp/servers/"))

	case path == "/v1/mcp/invoke":
		s.handleMCPInvoke(w, r)

	case path == "/v1/mcp/invocations":
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		if rejectQueryAndBody(w, r) {
			return
		}
		s.handleMCPInvocations(w)

	case path == "/v1/mcp/approvals":
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		if rejectQueryAndBody(w, r) {
			return
		}
		s.handleMCPApprovals(w)

	case strings.HasPrefix(path, "/v1/mcp/approvals/"):
		s.handleMCPApprovalDecision(w, r, strings.TrimPrefix(path, "/v1/mcp/approvals/"))

	default:
		writeError(w, http.StatusNotFound, "not_found")
	}
}

func validTaskPathID(id string) bool {
	return id != "" && !strings.Contains(id, "/") && persistence.ValidTaskID(id)
}

func rejectQueryAndBody(w http.ResponseWriter, r *http.Request) bool {
	if r.URL.RawQuery != "" || r.ContentLength > 0 || len(r.TransferEncoding) > 0 {
		writeError(w, http.StatusBadRequest, "bad_request")
		return true
	}
	return false
}

func methodNotAllowed(w http.ResponseWriter, methods ...string) {
	w.Header().Set("Allow", strings.Join(methods, ", "))
	writeError(w, http.StatusMethodNotAllowed, "method_not_allowed")
}

type taskSubmissionRequest struct {
	ID    string `json:"id"`
	Input string `json:"input"`
}

func (s *Server) handleTaskCreate(w http.ResponseWriter, r *http.Request) {
	if s.controller == nil {
		writeError(w, http.StatusServiceUnavailable, "task_control_unavailable")
		return
	}
	if r.URL.RawQuery != "" {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	var request taskSubmissionRequest
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	if !persistence.ValidTaskID(request.ID) || strings.TrimSpace(request.Input) == "" || len(request.Input) > maxTaskInput {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	record, err := s.controller.AdmitTask(request.ID, request.Input)
	if errors.Is(err, persistence.ErrDuplicateTask) {
		writeError(w, http.StatusConflict, "task_already_exists")
		return
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "CRANE local API: task admission failed")
		writeError(w, http.StatusServiceUnavailable, "task_admission_unavailable")
		return
	}
	writeData(w, http.StatusCreated, struct {
		Task persistence.TaskRecord `json:"task"`
	}{Task: record})
}

func (s *Server) handleTaskCancel(w http.ResponseWriter, id string) {
	if s.controller == nil {
		writeError(w, http.StatusServiceUnavailable, "task_control_unavailable")
		return
	}
	record, err := s.controller.CancelTask(id)
	if errors.Is(err, persistence.ErrTaskNotFound) {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "CRANE local API: task cancellation failed")
		writeError(w, http.StatusServiceUnavailable, "task_cancel_unavailable")
		return
	}
	writeData(w, http.StatusOK, struct {
		Task persistence.TaskRecord `json:"task"`
	}{Task: record})
}

func (s *Server) handleTaskEvents(w http.ResponseWriter, r *http.Request, id string) {
	if s.controller == nil {
		writeError(w, http.StatusServiceUnavailable, "task_control_unavailable")
		return
	}
	after, ok := eventCursor(w, r)
	if !ok {
		return
	}
	record, err := s.store.GetTaskRecord(id)
	if errors.Is(err, persistence.ErrTaskNotFound) {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "persistence_unavailable")
		return
	}
	events, unsubscribe, err := s.controller.SubscribeTaskEvents(id, after)
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "event_stream_unavailable")
		return
	}
	defer unsubscribe()

	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "streaming_unavailable")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	writeSSE(w, "snapshot", 0, record)
	flusher.Flush()

	ticker := time.NewTicker(sseKeepAlive)
	defer ticker.Stop()
	for {
		select {
		case event, open := <-events:
			if !open {
				return
			}
			writeSSE(w, event.Type, event.ID, event)
			flusher.Flush()
		case <-ticker.C:
			_, _ = io.WriteString(w, ": keep-alive\n\n")
			flusher.Flush()
		case <-r.Context().Done():
			return
		}
	}
}

func eventCursor(w http.ResponseWriter, r *http.Request) (int64, bool) {
	values := r.URL.Query()
	for key := range values {
		if key != "cursor" || len(values[key]) != 1 {
			writeError(w, http.StatusBadRequest, "bad_request")
			return 0, false
		}
	}
	raw := values.Get("cursor")
	if raw == "" {
		raw = r.Header.Get("Last-Event-ID")
	}
	if raw == "" {
		return 0, true
	}
	value, err := strconv.ParseInt(raw, 10, 64)
	if err != nil || value < 0 {
		writeError(w, http.StatusBadRequest, "bad_request")
		return 0, false
	}
	return value, true
}

func writeSSE(w http.ResponseWriter, event string, id int64, data any) {
	encoded, err := json.Marshal(data)
	if err != nil {
		encoded = []byte(`{}`)
	}
	if id > 0 {
		_, _ = fmt.Fprintf(w, "id: %d\n", id)
	}
	_, _ = fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, encoded)
}

func (s *Server) handleHealth(w http.ResponseWriter) {
	runtime := s.runtime()
	persistenceOK := s.store.HealthCheck() == nil
	supervisorOK := s.supervisor()
	writeData(w, http.StatusOK, HealthResponse{
		Version:     APIVersion,
		Supervisor:  supervisorOK,
		Runtime:     runtime,
		Persistence: persistenceOK,
		Ready:       supervisorOK && runtime.Available && persistenceOK,
	})
}

func (s *Server) handleTasks(w http.ResponseWriter) {
	records, err := s.store.ListTaskRecords()
	if err != nil {
		fmt.Fprintln(os.Stderr, "CRANE local API: task listing failed")
		writeError(w, http.StatusServiceUnavailable, "persistence_unavailable")
		return
	}
	if records == nil {
		records = []persistence.TaskRecord{}
	}
	writeData(w, http.StatusOK, struct {
		Tasks []persistence.TaskRecord `json:"tasks"`
		Count int                      `json:"count"`
	}{Tasks: records, Count: len(records)})
}

func (s *Server) handleTask(w http.ResponseWriter, id string) {
	record, err := s.store.GetTaskRecord(id)
	if errors.Is(err, persistence.ErrTaskNotFound) {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "CRANE local API: task lookup failed")
		writeError(w, http.StatusServiceUnavailable, "persistence_unavailable")
		return
	}
	writeData(w, http.StatusOK, struct {
		Task persistence.TaskRecord `json:"task"`
	}{Task: record})
}

func writeData(w http.ResponseWriter, status int, data any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(struct {
		Data any `json:"data"`
	}{Data: data})
}

type errorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeError(w http.ResponseWriter, status int, code string) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(struct {
		Error errorBody `json:"error"`
	}{Error: errorBody{Code: code, Message: code}})
}
