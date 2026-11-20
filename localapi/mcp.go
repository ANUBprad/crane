package localapi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"sort"
	"strings"

	"crane/mcpruntime"
	"crane/persistence"
)

// MCPController is the Go-owned MCP surface exposed through the authenticated
// loopback API. Environment values stay behind this interface and are never
// serialized into API responses.
type MCPController interface {
	AddServer(persistence.MCPServerRecord) error
	RemoveServer(string) error
	SetEnabled(string, bool) error
	Connect(string) error
	Disconnect(string) error
	Discover(string) error
	ListServers() ([]mcpruntime.ServerStatus, error)
	ListTools(string) ([]persistence.MCPToolRecord, error)
	ListInvocations(int) ([]persistence.MCPInvocationRecord, error)
	Invoke(string, string, map[string]any) (mcpruntime.InvokeResult, error)
	ListApprovals() ([]persistence.ApprovalRequestRecord, error)
	DecideApproval(string, string, string) (persistence.ApprovalRequestRecord, error)
}

// SetMCPController attaches the supervisor-owned MCP controller to the API.
// A nil controller leaves MCP routes unavailable.
func (s *Server) SetMCPController(controller MCPController) {
	s.mcp = controller
}

var mcpPathID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)

func validMCPPathID(id string) bool { return mcpPathID.MatchString(id) }

type mcpServerRequest struct {
	ServerID                 string            `json:"server_id"`
	Name                     string            `json:"name"`
	Transport                string            `json:"transport"`
	Command                  string            `json:"command,omitempty"`
	Args                     []string          `json:"args,omitempty"`
	Env                      map[string]string `json:"env,omitempty"`
	URL                      string            `json:"url,omitempty"`
	Enabled                  *bool             `json:"enabled,omitempty"`
	ConnectTimeoutSeconds    float64           `json:"connect_timeout_seconds,omitempty"`
	InvocationTimeoutSeconds float64           `json:"invocation_timeout_seconds,omitempty"`
	AllowRemoteEndpoint      bool              `json:"allow_remote_endpoint,omitempty"`
}

type mcpInvokeRequest struct {
	ServerID  string         `json:"server_id"`
	ToolName  string         `json:"tool_name"`
	Arguments map[string]any `json:"arguments,omitempty"`
}

type mcpDecisionRequest struct {
	Decision string `json:"decision"`
}

func decodeMCPRequest(r *http.Request, target any) bool {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return false
	}
	var trailing any
	return decoder.Decode(&trailing) == io.EOF
}

func (s *Server) requireMCPController(w http.ResponseWriter) bool {
	if s.mcp == nil {
		writeError(w, http.StatusServiceUnavailable, "mcp_unavailable")
		return false
	}
	return true
}

func writeMCPError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, persistence.ErrMCPServerNotFound), errors.Is(err, persistence.ErrApprovalNotFound):
		writeError(w, http.StatusNotFound, "not_found")
	case errors.Is(err, persistence.ErrApprovalAlreadyDecided), errors.Is(err, persistence.ErrApprovalExpired):
		writeError(w, http.StatusConflict, "conflict")
	case errors.Is(err, mcpruntime.ErrControllerNotStarted):
		writeError(w, http.StatusServiceUnavailable, "mcp_unavailable")
	default:
		writeError(w, http.StatusBadRequest, "mcp_request_failed")
	}
}

func (s *Server) handleMCPStatus(w http.ResponseWriter) {
	if !s.requireMCPController(w) {
		return
	}
	servers, err := s.mcp.ListServers()
	if err != nil {
		writeMCPError(w, err)
		return
	}
	invocations, err := s.mcp.ListInvocations(20)
	if err != nil {
		writeMCPError(w, err)
		return
	}
	approvals, err := s.mcp.ListApprovals()
	if err != nil {
		writeMCPError(w, err)
		return
	}
	writeData(w, http.StatusOK, struct {
		Servers     []mcpruntime.ServerStatus           `json:"servers"`
		Invocations []persistence.MCPInvocationRecord   `json:"invocations"`
		Approvals   []persistence.ApprovalRequestRecord `json:"approvals"`
	}{Servers: servers, Invocations: invocations, Approvals: approvals})
}

func (s *Server) handleMCPServers(w http.ResponseWriter, r *http.Request) {
	if !s.requireMCPController(w) {
		return
	}
	switch r.Method {
	case http.MethodGet:
		if rejectQueryAndBody(w, r) {
			return
		}
		servers, err := s.mcp.ListServers()
		if err != nil {
			writeMCPError(w, err)
			return
		}
		if servers == nil {
			servers = []mcpruntime.ServerStatus{}
		}
		writeData(w, http.StatusOK, struct {
			Servers []mcpruntime.ServerStatus `json:"servers"`
			Count   int                       `json:"count"`
		}{Servers: servers, Count: len(servers)})
	case http.MethodPost:
		s.handleMCPServerCreate(w, r)
	default:
		methodNotAllowed(w, http.MethodGet, http.MethodPost)
	}
}

func (s *Server) handleMCPServerCreate(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	var request mcpServerRequest
	if !decodeMCPRequest(r, &request) {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	enabled := true
	if request.Enabled != nil {
		enabled = *request.Enabled
	}
	record := persistence.MCPServerRecord{
		ID:                       request.ServerID,
		Name:                     request.Name,
		Transport:                request.Transport,
		Command:                  request.Command,
		Args:                     request.Args,
		Env:                      request.Env,
		URL:                      request.URL,
		Enabled:                  enabled,
		ConnectTimeoutSeconds:    request.ConnectTimeoutSeconds,
		InvocationTimeoutSeconds: request.InvocationTimeoutSeconds,
		AllowRemoteEndpoint:      request.AllowRemoteEndpoint,
	}
	if err := s.mcp.AddServer(record); err != nil {
		writeMCPError(w, err)
		return
	}
	keys := make([]string, 0, len(request.Env))
	for key := range request.Env {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	writeData(w, http.StatusCreated, struct {
		ServerID string   `json:"server_id"`
		EnvKeys  []string `json:"env_keys"`
	}{ServerID: record.ID, EnvKeys: keys})
}

func (s *Server) handleMCPServerPath(w http.ResponseWriter, r *http.Request, rest string) {
	if !s.requireMCPController(w) {
		return
	}
	switch {
	case strings.HasSuffix(rest, "/connect"):
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		s.handleMCPServerAction(w, r, strings.TrimSuffix(rest, "/connect"), "connect")
	case strings.HasSuffix(rest, "/disconnect"):
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		s.handleMCPServerAction(w, r, strings.TrimSuffix(rest, "/disconnect"), "disconnect")
	case strings.HasSuffix(rest, "/discover"):
		if r.Method != http.MethodPost {
			methodNotAllowed(w, http.MethodPost)
			return
		}
		s.handleMCPServerAction(w, r, strings.TrimSuffix(rest, "/discover"), "discover")
	case strings.HasSuffix(rest, "/tools"):
		if r.Method != http.MethodGet {
			methodNotAllowed(w, http.MethodGet)
			return
		}
		if rejectQueryAndBody(w, r) {
			return
		}
		id := strings.TrimSuffix(rest, "/tools")
		if !validMCPPathID(id) {
			writeError(w, http.StatusNotFound, "not_found")
			return
		}
		tools, err := s.mcp.ListTools(id)
		if err != nil {
			writeMCPError(w, err)
			return
		}
		if tools == nil {
			tools = []persistence.MCPToolRecord{}
		}
		writeData(w, http.StatusOK, struct {
			Tools []persistence.MCPToolRecord `json:"tools"`
			Count int                         `json:"count"`
		}{Tools: tools, Count: len(tools)})
	case !strings.Contains(rest, "/"):
		switch r.Method {
		case http.MethodPatch:
			s.handleMCPServerEnable(w, r, rest)
		case http.MethodDelete:
			if rejectQueryAndBody(w, r) {
				return
			}
			if !validMCPPathID(rest) {
				writeError(w, http.StatusNotFound, "not_found")
				return
			}
			if err := s.mcp.RemoveServer(rest); err != nil {
				writeMCPError(w, err)
				return
			}
			writeData(w, http.StatusOK, struct {
				ServerID string `json:"server_id"`
				Removed  bool   `json:"removed"`
			}{ServerID: rest, Removed: true})
		default:
			methodNotAllowed(w, http.MethodPatch, http.MethodDelete)
		}
	default:
		writeError(w, http.StatusNotFound, "not_found")
	}
}

func (s *Server) handleMCPServerAction(w http.ResponseWriter, r *http.Request, id, action string) {
	if rejectQueryAndBody(w, r) || !validMCPPathID(id) {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	var err error
	switch action {
	case "connect":
		err = s.mcp.Connect(id)
	case "disconnect":
		err = s.mcp.Disconnect(id)
	case "discover":
		err = s.mcp.Discover(id)
	}
	if err != nil {
		writeMCPError(w, err)
		return
	}
	writeData(w, http.StatusOK, struct {
		ServerID string `json:"server_id"`
		Action   string `json:"action"`
	}{ServerID: id, Action: action})
}

func (s *Server) handleMCPServerEnable(w http.ResponseWriter, r *http.Request, id string) {
	if r.URL.RawQuery != "" || !validMCPPathID(id) {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	var request struct {
		Enabled bool `json:"enabled"`
	}
	if !decodeMCPRequest(r, &request) {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	if err := s.mcp.SetEnabled(id, request.Enabled); err != nil {
		writeMCPError(w, err)
		return
	}
	writeData(w, http.StatusOK, struct {
		ServerID string `json:"server_id"`
		Enabled  bool   `json:"enabled"`
	}{ServerID: id, Enabled: request.Enabled})
}

func (s *Server) handleMCPInvoke(w http.ResponseWriter, r *http.Request) {
	if !s.requireMCPController(w) {
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	if r.URL.RawQuery != "" {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	var request mcpInvokeRequest
	if !decodeMCPRequest(r, &request) {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	if !validMCPPathID(request.ServerID) || !validMCPPathID(request.ToolName) || request.Arguments == nil {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	result, err := s.mcp.Invoke(request.ServerID, request.ToolName, request.Arguments)
	if err != nil {
		writeMCPError(w, err)
		return
	}
	writeData(w, http.StatusOK, result)
}

func (s *Server) handleMCPInvocations(w http.ResponseWriter) {
	if !s.requireMCPController(w) {
		return
	}
	invocations, err := s.mcp.ListInvocations(50)
	if err != nil {
		writeMCPError(w, err)
		return
	}
	if invocations == nil {
		invocations = []persistence.MCPInvocationRecord{}
	}
	writeData(w, http.StatusOK, struct {
		Invocations []persistence.MCPInvocationRecord `json:"invocations"`
		Count       int                               `json:"count"`
	}{Invocations: invocations, Count: len(invocations)})
}

func (s *Server) handleMCPApprovals(w http.ResponseWriter) {
	if !s.requireMCPController(w) {
		return
	}
	approvals, err := s.mcp.ListApprovals()
	if err != nil {
		writeMCPError(w, err)
		return
	}
	if approvals == nil {
		approvals = []persistence.ApprovalRequestRecord{}
	}
	writeData(w, http.StatusOK, struct {
		Approvals []persistence.ApprovalRequestRecord `json:"approvals"`
		Count     int                                 `json:"count"`
	}{Approvals: approvals, Count: len(approvals)})
}

func (s *Server) handleMCPApprovalDecision(w http.ResponseWriter, r *http.Request, rest string) {
	if !s.requireMCPController(w) {
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w, http.MethodPost)
		return
	}
	id := strings.TrimSuffix(rest, "/decide")
	if !validMCPPathID(id) || !strings.HasSuffix(rest, "/decide") {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	var request mcpDecisionRequest
	if !decodeMCPRequest(r, &request) {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	if request.Decision != "granted" && request.Decision != "denied" {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	approval, err := s.mcp.DecideApproval(id, request.Decision, "local-user")
	if err != nil {
		writeMCPError(w, err)
		return
	}
	writeData(w, http.StatusOK, struct {
		Approval persistence.ApprovalRequestRecord `json:"approval"`
	}{Approval: approval})
}
