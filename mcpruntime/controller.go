// Package mcpruntime owns the long-lived Python MCP daemon and exposes a
// Go-owned controller for configuration, discovery, approval and invocation.
package mcpruntime

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sync"
	"sync/atomic"
	"time"

	"crane/internal/runtimeenv"
	"crane/persistence"
	"crane/python_protocol"
)

const (
	defaultStartTimeout    = 90 * time.Second
	defaultCommandTimeout  = 120 * time.Second
	defaultShutdownTimeout = 5 * time.Second
	maxStderrBytes         = 16 << 10
)

var (
	// ErrControllerNotStarted reports MCP operations before daemon startup.
	ErrControllerNotStarted = errors.New("MCP controller is not running")
	// ErrServerNotFound reports an unknown MCP server identifier.
	ErrServerNotFound = errors.New("MCP server not found")
	// ErrToolNotFound reports a tool that was not discovered on a server.
	ErrToolNotFound = errors.New("MCP tool not found")
)

// CommandError is a safe, actionable MCP daemon failure.
type CommandError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *CommandError) Error() string {
	if e == nil {
		return "MCP command failed"
	}
	return e.Code + ": " + e.Message
}

// Config selects the Python runtime used for the MCP daemon.
type Config struct {
	PythonPath     string
	PythonHome     string
	PythonArgs     []string
	StartTimeout   time.Duration
	CommandTimeout time.Duration
}

// ToolResultHook receives persisted tool_result frames for supervisor events.
type ToolResultHook func(python_protocol.ToolResult)

// ServerStatus combines persisted configuration with live daemon state.
type ServerStatus struct {
	persistence.MCPServerRecord
	Connected bool `json:"connected"`
	ToolCount int  `json:"tool_count"`
}

// InvokeResult is the API-safe result of one MCP invocation request.
type InvokeResult struct {
	Invocation        map[string]any `json:"invocation"`
	ApprovalRequestID string         `json:"approval_request_id,omitempty"`
}

type discoveredTool struct {
	ServerID    string          `json:"server_id"`
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	InputSchema json.RawMessage `json:"input_schema"`
	Annotations json.RawMessage `json:"annotations,omitempty"`
	PolicyClass string          `json:"policy_class"`
}

type daemonServerState struct {
	Config    persistence.MCPServerRecord `json:"config"`
	Connected bool                        `json:"connected"`
	ToolCount int                         `json:"tool_count"`
	LastError string                      `json:"last_error,omitempty"`
}

type pendingResponse struct {
	response *python_protocol.MCPResponse
	err      error
}

// Controller owns one Python MCP daemon process and all durable MCP records.
type Controller struct {
	store  *persistence.Store
	cfg    Config
	ctx    context.Context
	cancel context.CancelFunc

	mu       sync.Mutex
	started  bool
	cmd      *exec.Cmd
	stdin    io.WriteCloser
	reader   *bufio.Reader
	stderr   *boundedBuffer
	done     chan struct{}
	closeOne sync.Once
	counter  atomic.Uint64

	pendingMu sync.Mutex
	pending   map[string]chan pendingResponse
	writeMu   sync.Mutex

	hookMu sync.RWMutex
	hook   ToolResultHook
}

// New creates a controller. Start must be called before MCP operations.
func New(store *persistence.Store, cfg Config) *Controller {
	if cfg.StartTimeout <= 0 {
		cfg.StartTimeout = defaultStartTimeout
	}
	if cfg.CommandTimeout <= 0 {
		cfg.CommandTimeout = defaultCommandTimeout
	}
	ctx, cancel := context.WithCancel(context.Background())
	return &Controller{
		store:   store,
		cfg:     cfg,
		ctx:     ctx,
		cancel:  cancel,
		pending: make(map[string]chan pendingResponse),
	}
}

// SetToolResultHook registers a supervisor callback for persisted tool results.
func (c *Controller) SetToolResultHook(hook ToolResultHook) {
	c.hookMu.Lock()
	defer c.hookMu.Unlock()
	c.hook = hook
}

// Start launches the Python MCP daemon and recovers enabled server sessions.
func (c *Controller) Start() error {
	if c == nil || c.store == nil {
		return errors.New("MCP controller requires a persistence store")
	}
	if err := c.store.HealthCheck(); err != nil {
		return fmt.Errorf("MCP controller store health check: %w", err)
	}
	c.mu.Lock()
	if c.started {
		c.mu.Unlock()
		return nil
	}
	c.mu.Unlock()

	pythonPath := c.cfg.PythonPath
	if pythonPath == "" {
		pythonPath = runtimeenv.PythonCandidates()[0]
	}
	args := append([]string{}, c.cfg.PythonArgs...)
	if len(args) == 0 {
		args = []string{"-m", "crane.python_runtime", "--mcp-protocol"}
	}
	cmd := exec.CommandContext(c.ctx, pythonPath, args...)
	runtimeenv.Configure(cmd)
	if c.cfg.PythonHome != "" {
		cmd.Dir = c.cfg.PythonHome
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("create MCP daemon stdin: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return fmt.Errorf("create MCP daemon stdout: %w", err)
	}
	stderrBuf := &boundedBuffer{limit: maxStderrBytes}
	cmd.Stderr = stderrBuf
	if err := cmd.Start(); err != nil {
		_ = stdin.Close()
		return fmt.Errorf("start MCP daemon: %w", err)
	}

	done := make(chan struct{})
	identity := make(chan struct{}, 1)
	c.mu.Lock()
	c.cmd, c.stdin, c.reader, c.stderr, c.done = cmd, stdin, bufio.NewReaderSize(stdout, python_protocol.MaxMessageBytes+1), stderrBuf, done
	c.started = true
	c.mu.Unlock()
	go c.readLoop(identity)

	select {
	case <-identity:
	case <-done:
		stderr := stderrBuf.String()
		c.Close()
		if stderr != "" {
			return fmt.Errorf("MCP daemon exited before its identity frame: %s", stderr)
		}
		return errors.New("MCP daemon exited before its identity frame")
	case <-time.After(c.cfg.StartTimeout):
		stderr := stderrBuf.String()
		c.Close()
		if stderr != "" {
			return fmt.Errorf("MCP daemon startup timed out: %s", stderr)
		}
		return errors.New("MCP daemon startup timed out")
	}

	if err := c.recoverServers(); err != nil {
		fmt.Fprintln(os.Stderr, "CRANE MCP: recovery reported an error")
	}
	return nil
}

func (c *Controller) recoverServers() error {
	servers, err := c.store.ListMCPServers()
	if err != nil {
		return err
	}
	enabled := make([]persistence.MCPServerRecord, 0, len(servers))
	for _, server := range servers {
		if server.Enabled {
			enabled = append(enabled, server)
		}
	}
	if err := c.Configure(enabled); err != nil {
		return err
	}
	var firstErr error
	for _, server := range enabled {
		if err := c.Connect(server.ID); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (c *Controller) readLoop(identity chan<- struct{}) {
	defer c.markDone()
	for {
		c.mu.Lock()
		reader := c.reader
		c.mu.Unlock()
		if reader == nil {
			return
		}
		frame, err := python_protocol.ReadFrame(reader)
		if err != nil {
			return
		}
		base, err := python_protocol.Unmarshal(frame)
		if err != nil {
			continue
		}
		switch base.Type {
		case python_protocol.TypeRuntimeIdentity:
			if _, err := python_protocol.DecodeRuntimeIdentity(frame); err == nil {
				select {
				case identity <- struct{}{}:
				default:
				}
			}
		case python_protocol.TypeMCPResponse:
			response, err := python_protocol.DecodeMCPResponse(frame)
			if err != nil {
				continue
			}
			c.deliver(response.CommandID, pendingResponse{response: response})
		case python_protocol.TypeToolResult:
			result, err := python_protocol.DecodeToolResult(frame)
			if err != nil {
				continue
			}
			if err := PersistToolResult(c.store, *result); err != nil {
				fmt.Fprintln(os.Stderr, "CRANE MCP: failed to persist a tool result")
				continue
			}
			c.hookMu.RLock()
			hook := c.hook
			c.hookMu.RUnlock()
			if hook != nil {
				hook(*result)
			}
		case python_protocol.TypeTaskEvent, python_protocol.TypeErrorResponse:
			// Events are owned by the task supervisor; malformed errors are
			// never echoed because they may contain caller data.
		}
	}
}

func (c *Controller) markDone() {
	c.pendingMu.Lock()
	for id, ch := range c.pending {
		select {
		case ch <- pendingResponse{err: errors.New("MCP daemon exited")}:
		default:
		}
		delete(c.pending, id)
	}
	c.pendingMu.Unlock()
	c.mu.Lock()
	stderr := ""
	if c.stderr != nil {
		stderr = c.stderr.String()
	}
	if c.done != nil {
		close(c.done)
		c.done = nil
	}
	started := c.started
	c.mu.Unlock()
	if started && stderr != "" {
		fmt.Fprintln(os.Stderr, "CRANE MCP: daemon exited: "+stderr)
	}
}

func (c *Controller) deliver(commandID string, response pendingResponse) {
	c.pendingMu.Lock()
	ch := c.pending[commandID]
	delete(c.pending, commandID)
	c.pendingMu.Unlock()
	if ch != nil {
		select {
		case ch <- response:
		default:
		}
	}
}

func (c *Controller) sendCommand(operation string, payload any) (*python_protocol.MCPResponse, error) {
	c.mu.Lock()
	started, stdin, done := c.started, c.stdin, c.done
	c.mu.Unlock()
	if !started || stdin == nil {
		return nil, ErrControllerNotStarted
	}
	encodedPayload := json.RawMessage(`{}`)
	if payload != nil {
		raw, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("encode MCP command payload: %w", err)
		}
		encodedPayload = raw
	}
	commandID := fmt.Sprintf("cmd-%d-%d", time.Now().UnixNano(), c.counter.Add(1))
	command := python_protocol.MCPCommand{
		Message:   python_protocol.Message{Version: python_protocol.ProtocolVersion, Type: python_protocol.TypeMCPCommand},
		CommandID: commandID,
		Operation: operation,
		Payload:   encodedPayload,
	}
	encoded, err := json.Marshal(command)
	if err != nil {
		return nil, fmt.Errorf("encode MCP command: %w", err)
	}
	responses := make(chan pendingResponse, 1)
	c.pendingMu.Lock()
	c.pending[commandID] = responses
	c.pendingMu.Unlock()
	defer func() {
		c.pendingMu.Lock()
		delete(c.pending, commandID)
		c.pendingMu.Unlock()
	}()

	c.writeMu.Lock()
	_, err = stdin.Write(append(encoded, '\n'))
	c.writeMu.Unlock()
	if err != nil {
		return nil, fmt.Errorf("write MCP command: %w", err)
	}

	timer := time.NewTimer(c.cfg.CommandTimeout)
	defer timer.Stop()
	select {
	case delivered := <-responses:
		if delivered.err != nil {
			return nil, delivered.err
		}
		if delivered.response == nil {
			return nil, errors.New("MCP daemon returned an empty response")
		}
		if !delivered.response.Success {
			if delivered.response.Error != nil {
				return nil, &CommandError{Code: delivered.response.Error.Code, Message: delivered.response.Error.Message}
			}
			return nil, &CommandError{Code: "mcp_command_failed", Message: "MCP daemon rejected the command"}
		}
		return delivered.response, nil
	case <-timer.C:
		return nil, &CommandError{Code: "mcp_command_timeout", Message: "MCP command timed out"}
	case <-c.ctx.Done():
		return nil, errors.New("MCP controller is shutting down")
	case <-done:
		return nil, errors.New("MCP daemon exited before answering")
	}
}

func decodeResult(response *python_protocol.MCPResponse, target any) error {
	raw := response.Result
	if len(raw) == 0 {
		raw = json.RawMessage(`{}`)
	}
	if err := json.Unmarshal(raw, target); err != nil {
		return fmt.Errorf("decode MCP command result: %w", err)
	}
	return nil
}

func serverMessage(record persistence.MCPServerRecord) python_protocol.MCPServerConfigMessage {
	return python_protocol.MCPServerConfigMessage{
		ServerID:                 record.ID,
		Name:                     record.Name,
		Transport:                record.Transport,
		Command:                  record.Command,
		Args:                     append([]string{}, record.Args...),
		Env:                      record.Env,
		URL:                      record.URL,
		Enabled:                  record.Enabled,
		ConnectTimeoutSeconds:    record.ConnectTimeoutSeconds,
		InvocationTimeoutSeconds: record.InvocationTimeoutSeconds,
		AllowRemoteEndpoint:      record.AllowRemoteEndpoint,
	}
}

// Configure replaces the daemon's server catalog and active approval grants.
func (c *Controller) Configure(servers []persistence.MCPServerRecord) error {
	grants, err := c.store.GrantedApprovalFingerprints()
	if err != nil {
		return err
	}
	configs := make([]python_protocol.MCPServerConfigMessage, 0, len(servers))
	for _, server := range servers {
		configs = append(configs, serverMessage(server))
	}
	_, err = c.sendCommand("configure", map[string]any{
		"servers":           configs,
		"granted_approvals": grants,
		"task_id":           "mcp-daemon",
	})
	return err
}

// AddServer validates and persists a server before registering it with Python.
func (c *Controller) AddServer(record persistence.MCPServerRecord) error {
	if err := c.store.UpsertMCPServer(record); err != nil {
		return err
	}
	_, err := c.sendCommand("add_server", map[string]any{"config": serverMessage(record)})
	return err
}

// RemoveServer disconnects and deletes one server.
func (c *Controller) RemoveServer(serverID string) error {
	if _, err := c.store.GetMCPServer(serverID); err != nil {
		return err
	}
	if _, err := c.sendCommand("remove_server", map[string]any{"server_id": serverID}); err != nil {
		return err
	}
	return c.store.DeleteMCPServer(serverID)
}

// SetEnabled changes the persisted enabled state and reconfigures the daemon.
func (c *Controller) SetEnabled(serverID string, enabled bool) error {
	if err := c.store.SetMCPServerEnabled(serverID, enabled); err != nil {
		return err
	}
	if !enabled {
		_, _ = c.sendCommand("disconnect", map[string]any{"server_id": serverID})
	}
	servers, err := c.store.ListMCPServers()
	if err != nil {
		return err
	}
	return c.Configure(servers)
}

// Connect opens one server and replaces its persisted tool catalog.
func (c *Controller) Connect(serverID string) error {
	if _, err := c.store.GetMCPServer(serverID); err != nil {
		return err
	}
	response, err := c.sendCommand("connect", map[string]any{"server_id": serverID})
	if err != nil {
		_ = c.store.UpdateMCPServerStatus(serverID, "error", err.Error())
		return err
	}
	var result struct {
		Connected []string         `json:"connected"`
		Tools     []discoveredTool `json:"tools"`
	}
	if err := decodeResult(response, &result); err != nil {
		return err
	}
	if err := c.persistTools(serverID, result.Tools); err != nil {
		return err
	}
	return c.store.UpdateMCPServerStatus(serverID, "connected", "")
}

func (c *Controller) persistTools(serverID string, tools []discoveredTool) error {
	records := make([]persistence.MCPToolRecord, 0, len(tools))
	for _, tool := range tools {
		if tool.ServerID != serverID || tool.Name == "" {
			continue
		}
		records = append(records, persistence.MCPToolRecord{
			ServerID:    tool.ServerID,
			ToolName:    tool.Name,
			Description: tool.Description,
			InputSchema: tool.InputSchema,
			Annotations: tool.Annotations,
			PolicyClass: tool.PolicyClass,
		})
	}
	return c.store.ReplaceMCPTools(serverID, records)
}

// Disconnect closes one live server session.
func (c *Controller) Disconnect(serverID string) error {
	if _, err := c.store.GetMCPServer(serverID); err != nil {
		return err
	}
	if _, err := c.sendCommand("disconnect", map[string]any{"server_id": serverID}); err != nil {
		return err
	}
	return c.store.UpdateMCPServerStatus(serverID, "disconnected", "")
}

// Discover refreshes one server's persisted tool catalog.
func (c *Controller) Discover(serverID string) error {
	if _, err := c.store.GetMCPServer(serverID); err != nil {
		return err
	}
	response, err := c.sendCommand("discover_tools", map[string]any{"server_id": serverID})
	if err != nil {
		return err
	}
	var result struct {
		Tools []discoveredTool `json:"tools"`
	}
	if err := decodeResult(response, &result); err != nil {
		return err
	}
	if err := c.persistTools(serverID, result.Tools); err != nil {
		return err
	}
	return c.store.UpdateMCPServerStatus(serverID, "connected", "")
}

// ListServers returns live state merged with persisted server records.
func (c *Controller) ListServers() ([]ServerStatus, error) {
	response, err := c.sendCommand("list_servers", map[string]any{})
	if err != nil {
		return nil, err
	}
	var result struct {
		Servers []daemonServerState `json:"servers"`
	}
	if err := decodeResult(response, &result); err != nil {
		return nil, err
	}
	persisted, err := c.store.ListMCPServers()
	if err != nil {
		return nil, err
	}
	byID := make(map[string]persistence.MCPServerRecord, len(persisted))
	for _, record := range persisted {
		byID[record.ID] = record
	}
	statuses := make([]ServerStatus, 0, len(result.Servers))
	for _, state := range result.Servers {
		record := state.Config
		if stored, ok := byID[record.ID]; ok {
			record.Status = stored.Status
		}
		if state.LastError != "" {
			record.LastError = state.LastError
		}
		statuses = append(statuses, ServerStatus{
			MCPServerRecord: record,
			Connected:       state.Connected,
			ToolCount:       state.ToolCount,
		})
	}
	return statuses, nil
}

// ListTools returns API-safe persisted tool schemas.
func (c *Controller) ListTools(serverID string) ([]persistence.MCPToolRecord, error) {
	return c.store.ListMCPTools(serverID)
}

// ListInvocations returns the newest persisted invocation records.
func (c *Controller) ListInvocations(limit int) ([]persistence.MCPInvocationRecord, error) {
	return c.store.ListMCPInvocations(limit)
}

// Invoke runs one policy-checked MCP tool invocation. Consequential tools are
// blocked until an exact approval grant exists in durable storage.
func (c *Controller) Invoke(serverID, toolName string, arguments map[string]any) (InvokeResult, error) {
	if arguments == nil {
		arguments = map[string]any{}
	}
	if _, err := c.store.GetMCPServer(serverID); err != nil {
		return InvokeResult{}, err
	}
	if err := c.Discover(serverID); err != nil {
		return InvokeResult{}, err
	}
	tools, err := c.store.ListMCPTools(serverID)
	if err != nil {
		return InvokeResult{}, err
	}
	policy := "approval_required"
	for _, tool := range tools {
		if tool.ToolName == toolName {
			policy = tool.PolicyClass
			break
		}
	}
	if policy == "blocked" {
		return InvokeResult{}, &CommandError{Code: "mcp_tool_blocked", Message: "This tool is blocked by policy"}
	}
	fingerprint := persistence.ApprovalFingerprint(serverID, toolName, arguments)
	grants, err := c.store.GrantedApprovalFingerprints()
	if err != nil {
		return InvokeResult{}, err
	}
	approved := hasFingerprint(grants, fingerprint)
	approvalID := ""
	if policy != "allowed" && !approved {
		request := persistence.ApprovalRequestRecord{
			ID:          fmt.Sprintf("approval-%d", time.Now().UnixNano()),
			ServerID:    serverID,
			ToolName:    toolName,
			Arguments:   persistence.RedactedJSON(arguments),
			Fingerprint: fingerprint,
		}
		if err := c.store.CreateApprovalRequest(request); err != nil {
			return InvokeResult{}, err
		}
		approvalID = request.ID
	}
	// Only an existing durable grant may authorize a consequential action.
	if approved {
		grants = append(grants, fingerprint)
	}

	response, err := c.sendCommand("invoke_tool", map[string]any{
		"server_id":         serverID,
		"tool_name":         toolName,
		"arguments":         arguments,
		"task_id":           "mcp-daemon",
		"granted_approvals": grants,
	})
	if err != nil {
		return InvokeResult{}, err
	}
	var result struct {
		Invocation map[string]any `json:"invocation"`
	}
	if err := decodeResult(response, &result); err != nil {
		return InvokeResult{}, err
	}
	return InvokeResult{Invocation: result.Invocation, ApprovalRequestID: approvalID}, nil
}

// RequestApproval records an exact action for later explicit approval.
func (c *Controller) RequestApproval(serverID, toolName string, arguments map[string]any) (persistence.ApprovalRequestRecord, error) {
	if arguments == nil {
		arguments = map[string]any{}
	}
	request := persistence.ApprovalRequestRecord{
		ID:          fmt.Sprintf("approval-%d", time.Now().UnixNano()),
		ServerID:    serverID,
		ToolName:    toolName,
		Arguments:   persistence.RedactedJSON(arguments),
		Fingerprint: persistence.ApprovalFingerprint(serverID, toolName, arguments),
	}
	err := c.store.CreateApprovalRequest(request)
	return request, err
}

// DecideApproval records a durable granted or denied decision.
func (c *Controller) DecideApproval(id, decision, actor string) (persistence.ApprovalRequestRecord, error) {
	return c.store.DecideApprovalRequest(id, decision, actor)
}

// ListApprovals returns durable approval records.
func (c *Controller) ListApprovals() ([]persistence.ApprovalRequestRecord, error) {
	return c.store.ListApprovalRequests()
}

// PersistToolResult stores one tool_result frame from either the daemon or a
// task child process. Values are redacted again at the persistence boundary.
func PersistToolResult(store *persistence.Store, result python_protocol.ToolResult) error {
	if store == nil {
		return errors.New("persistence store is not initialized")
	}
	arguments := result.Arguments
	if len(arguments) == 0 {
		arguments = json.RawMessage(`{}`)
	}
	payload := result.Result
	if len(payload) == 0 {
		payload = json.RawMessage(`{}`)
	}
	status := result.Status
	if status == "" {
		status = "succeeded"
	}
	return store.RecordMCPInvocation(persistence.MCPInvocationRecord{
		ID:             result.InvocationID,
		TaskID:         result.TaskID,
		ServerID:       result.ServerID,
		ServerName:     result.ServerName,
		ToolName:       result.ToolName,
		Arguments:      arguments,
		Status:         status,
		Result:         payload,
		Error:          result.Error,
		PolicyDecision: result.PolicyDecision,
		DurationMS:     result.DurationMS,
		CreatedAt:      time.Now().UTC().Format(time.RFC3339Nano),
		CompletedAt:    time.Now().UTC().Format(time.RFC3339Nano),
	})
}

// Close stops the Python MCP daemon with bounded cleanup.
func (c *Controller) Close() error {
	if c == nil {
		return nil
	}
	var closeErr error
	c.closeOne.Do(func() {
		c.mu.Lock()
		stdin, cmd, done := c.stdin, c.cmd, c.done
		c.started = false
		c.stdin = nil
		c.mu.Unlock()
		if stdin != nil {
			frame := python_protocol.Message{Version: python_protocol.ProtocolVersion, Type: python_protocol.TypeShutdownRequest}
			encoded, err := json.Marshal(frame)
			if err == nil {
				c.writeMu.Lock()
				_, _ = stdin.Write(append(encoded, '\n'))
				c.writeMu.Unlock()
			}
			_ = stdin.Close()
		}
		if cmd == nil || cmd.Process == nil {
			return
		}
		waitDone := make(chan error, 1)
		go func() { waitDone <- cmd.Wait() }()
		select {
		case closeErr = <-waitDone:
		case <-time.After(defaultShutdownTimeout):
			_ = cmd.Process.Kill()
			closeErr = <-waitDone
		}
		if done != nil {
			select {
			case <-done:
			case <-time.After(time.Second):
			}
		}
		c.cancel()
	})
	return closeErr
}

func hasFingerprint(values []string, fingerprint string) bool {
	for _, value := range values {
		if value == fingerprint {
			return true
		}
	}
	return false
}

type boundedBuffer struct {
	mu    sync.Mutex
	buf   []byte
	limit int
}

func (b *boundedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return string(b.buf)
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	remaining := b.limit - len(b.buf)
	if remaining <= 0 {
		return len(p), nil
	}
	if len(p) > remaining {
		p = p[:remaining]
	}
	b.buf = append(b.buf, p...)
	return len(p), nil
}
