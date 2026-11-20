// Package localapi implements the authenticated loopback API owned by the Go
// supervisor. It serves only real supervisor, runtime and persistence state.
package localapi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
	defaultAPIPort  = 8787
	shutdownTimeout = 5 * time.Second
)

type TaskStore interface {
	HealthCheck() error
	ListTaskRecords() ([]persistence.TaskRecord, error)
	GetTaskRecord(id string) (persistence.TaskRecord, error)
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
// A nil supervisor probe reports the supervisor as unavailable; the API never
// reports readiness from its own HTTP liveness alone.
func NewServer(
	auth *localauth.Manager,
	store TaskStore,
	runtime func() RuntimeStatus,
	supervisor func() bool,
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
		serveErr:   make(chan error, 1),
		serveDone:  make(chan error, 1),
	}, nil
}

// Start binds to 127.0.0.1 on port and begins serving in the background.
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
		WriteTimeout:      10 * time.Second,
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
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("X-Content-Type-Options", "nosniff")

	r.Body = http.MaxBytesReader(w, r.Body, maxRequestBody)
	if r.ContentLength > maxRequestBody {
		writeError(w, http.StatusRequestEntityTooLarge, "request_too_large")
		return
	}
	if r.URL.RawQuery != "" || r.ContentLength > 0 || len(r.TransferEncoding) > 0 {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}

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

	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeError(w, http.StatusMethodNotAllowed, "method_not_allowed")
		return
	}

	switch path := r.URL.Path; {
	case path == "/v1/health":
		s.handleHealth(w)
	case path == "/v1/tasks":
		s.handleTasks(w)
	case strings.HasPrefix(path, "/v1/tasks/"):
		id := strings.TrimPrefix(path, "/v1/tasks/")
		if !persistence.ValidTaskID(id) {
			writeError(w, http.StatusNotFound, "not_found")
			return
		}
		s.handleTask(w, id)
	default:
		writeError(w, http.StatusNotFound, "not_found")
	}
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
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(struct {
		Error errorBody `json:"error"`
	}{Error: errorBody{Code: code, Message: code}})
}
