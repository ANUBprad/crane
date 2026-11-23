package python_protocol

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

// Protocol version — increment when the message schema changes.
const ProtocolVersion = "0.1.0"

// These constants define the valid protocol message types.
const (
	TypeHealthCheck      = "health_check"
	TypeShutdownRequest  = "shutdown_request"
	TypeRuntimeIdentity  = "runtime_identity"
	TypeErrorResponse    = "error_response"
	TypeSuccessResponse  = "success_response"
	TypeRegisterServer   = "register_server"
	TypeRemoveServer     = "remove_server"
	TypeListServers      = "list_servers"
	TypeDiscoverTools    = "discover_tools"
	TypeInvokeTool       = "invoke_tool"
	TypeToolResult       = "tool_result"
	TypeMCPCommand       = "mcp_command"
	TypeMCPResponse      = "mcp_response"
	TypeTaskState        = "task_state"
	TypeTaskSubmission   = "task_submission"
	TypeTaskCancellation = "task_cancellation"
	TypeTaskEvent        = "task_event"
	TypeTaskResult       = "task_result"
	TypeTaskError        = "task_error"
)

// MaxMessageBytes bounds one newline-delimited protocol frame.
const MaxMessageBytes = 1 << 20

// Message is a base protocol message with version and type fields.
type Message struct {
	Version string `json:"version"`
	Type    string `json:"type"`
}

// HealthCheck is a protocol message for health checks.
type HealthCheck struct {
	Message
}

// ShutdownRequest is a protocol message requesting runtime shutdown.
type ShutdownRequest struct {
	Message
}

// RuntimeIdentity is a protocol message providing runtime identity info.
type RuntimeIdentity struct {
	Message
	RuntimeVersion string `json:"runtime_version"`
	PythonVersion  string `json:"python_version"`
	PID            int    `json:"pid"`
}

// ErrorResponse is a protocol error message.
type ErrorResponse struct {
	Message
	ErrMessage string `json:"message"`
	Code       string `json:"code"`
	RetryAfter *int   `json:"retry_after,omitempty"`
	Retryable  bool   `json:"retryable"`
}

// SuccessResponse is a protocol success message.
type SuccessResponse struct {
	Message
	Result json.RawMessage `json:"result,omitempty"`
}

// RegisterServer is a protocol message for registering an MCP server.
type RegisterServer struct {
	Message
	ServerName   string          `json:"server_name"`
	Transport    string          `json:"transport"` // "stdio" or "streamable_http"
	Parameters   json.RawMessage `json:"parameters,omitempty"`
	Capabilities json.RawMessage `json:"capabilities,omitempty"`
}

// RemoveServer is a protocol message for removing an MCP server.
type RemoveServer struct {
	Message
	ServerName string `json:"server_name"`
}

// ListServers is a protocol message for listing registered servers.
type ListServers struct {
	Message
}

// DiscoverTools is a protocol message for discovering tools from a server.
type DiscoverTools struct {
	Message
	ServerName string `json:"server_name,omitempty"`
}

// InvokeTool is a protocol message for invoking an MCP tool.
type InvokeTool struct {
	Message
	ServerName string          `json:"server_name"`
	ToolName   string          `json:"tool_name"`
	Arguments  json.RawMessage `json:"arguments,omitempty"`
}

// ToolResult is the durable outcome of one MCP tool invocation.
type ToolResult struct {
	Message
	TaskID         string          `json:"task_id"`
	InvocationID   string          `json:"invocation_id"`
	ServerID       string          `json:"server_id"`
	ServerName     string          `json:"server_name"`
	ToolName       string          `json:"tool_name"`
	Arguments      json.RawMessage `json:"arguments,omitempty"`
	Result         json.RawMessage `json:"result,omitempty"`
	Error          string          `json:"error,omitempty"`
	PolicyDecision string          `json:"policy_decision,omitempty"`
	DurationMS     int64           `json:"duration_ms,omitempty"`
	Status         string          `json:"status,omitempty"`
}

// MCPServerConfigMessage is the validated server configuration sent to the
// Python MCP daemon. Environment values are included only on this private
// process boundary and are never written to API responses, logs or events.
type MCPServerConfigMessage struct {
	ServerID                 string            `json:"server_id"`
	Name                     string            `json:"name"`
	Transport                string            `json:"transport"`
	Command                  string            `json:"command,omitempty"`
	Args                     []string          `json:"args,omitempty"`
	Env                      map[string]string `json:"env,omitempty"`
	URL                      string            `json:"url,omitempty"`
	Enabled                  bool              `json:"enabled"`
	ConnectTimeoutSeconds    float64           `json:"connect_timeout_seconds,omitempty"`
	InvocationTimeoutSeconds float64           `json:"invocation_timeout_seconds,omitempty"`
	AllowRemoteEndpoint      bool              `json:"allow_remote_endpoint,omitempty"`
}

// MCPCommand asks the long-lived Python MCP daemon to perform one lifecycle or
// invocation operation.
type MCPCommand struct {
	Message
	CommandID string          `json:"command_id"`
	Operation string          `json:"operation"`
	Payload   json.RawMessage `json:"payload,omitempty"`
}

// MCPErrorDetail carries a safe, actionable error from Python to Go.
type MCPErrorDetail struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// MCPResponse answers one MCPCommand.
type MCPResponse struct {
	Message
	CommandID string          `json:"command_id"`
	Success   bool            `json:"success"`
	Result    json.RawMessage `json:"result,omitempty"`
	Error     *MCPErrorDetail `json:"error,omitempty"`
}

// ToolInvocation records the exact action and decision for an MCP call.
type ToolInvocation struct {
	InvocationID   string          `json:"invocation_id"`
	TaskID         string          `json:"task_id,omitempty"`
	ServerID       string          `json:"server_id"`
	ServerName     string          `json:"server_name,omitempty"`
	ToolName       string          `json:"tool_name"`
	Arguments      json.RawMessage `json:"arguments"`
	Status         string          `json:"status"`
	PolicyDecision string          `json:"policy_decision,omitempty"`
	DurationMS     int64           `json:"duration_ms,omitempty"`
	Error          string          `json:"error,omitempty"`
}

// TaskState is a protocol message for persisting workflow checkpoints.
// The Go supervisor stores this in SQLite and can resume the workflow after restart.
type TaskState struct {
	Message
	TaskId           string          `json:"task_id"`
	State            string          `json:"state"` // TaskState enum value
	CurrentStep      string          `json:"current_step"`
	GraphSteps       int             `json:"graph_steps"`
	WallClockMs      int64           `json:"wall_clock_ms"`
	Result           json.RawMessage `json:"result,omitempty"`
	ResultSummary    string          `json:"result_summary,omitempty"`
	FailureReason    string          `json:"failure_reason,omitempty"`
	Cancelled        bool            `json:"cancelled"`
	ApprovalDecision string          `json:"approval_decision,omitempty"`
	ApprovalId       string          `json:"approval_id,omitempty"`
	Checkpoint       json.RawMessage `json:"checkpoint,omitempty"`
}

// ConversationMessage carries one persisted chat turn for model context.
type ConversationMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// TaskSubmission asks the Python runtime to execute one admitted task.
type TaskSubmission struct {
	Message
	TaskID               string                   `json:"task_id"`
	Input                string                   `json:"input"`
	Resume               bool                     `json:"resume,omitempty"`
	Checkpoint           json.RawMessage          `json:"checkpoint,omitempty"`
	TimeoutMs            int64                    `json:"timeout_ms,omitempty"`
	MCPServers           []MCPServerConfigMessage `json:"mcp_servers,omitempty"`
	GrantedApprovals     []string                 `json:"granted_approvals,omitempty"`
	ConversationID       string                   `json:"conversation_id,omitempty"`
	ConversationMessages []ConversationMessage    `json:"conversation_messages,omitempty"`
}

// TaskCancellation propagates a Go-owned cancellation decision to Python.
type TaskCancellation struct {
	Message
	TaskID string `json:"task_id"`
	Reason string `json:"reason,omitempty"`
}

// TaskEvent is a real workflow event emitted by Python.
type TaskEvent struct {
	Message
	TaskID    string          `json:"task_id"`
	EventType string          `json:"event_type"`
	Payload   json.RawMessage `json:"payload,omitempty"`
}

// TaskResult is the terminal outcome reported by Python. Go never marks a
// task successful before receiving this message with a succeeded status.
type TaskResult struct {
	Message
	TaskID        string          `json:"task_id"`
	Status        string          `json:"status"`
	Result        json.RawMessage `json:"result,omitempty"`
	ResultSummary string          `json:"result_summary,omitempty"`
	FailureReason string          `json:"failure_reason,omitempty"`
	GraphSteps    int             `json:"graph_steps"`
	WallClockMs   int64           `json:"wall_clock_ms"`
	CurrentStep   string          `json:"current_step,omitempty"`
	Checkpoint    json.RawMessage `json:"checkpoint,omitempty"`
}

// TaskError is an explicit Python-side execution failure.
type TaskError struct {
	Message
	TaskID    string `json:"task_id"`
	Code      string `json:"code"`
	Detail    string `json:"message"`
	Step      string `json:"step,omitempty"`
	Retryable bool   `json:"retryable,omitempty"`
}

// Unmarshal parses a raw JSON byte slice into a Message.
// It validates the protocol version and returns an appropriate
// typed message or an error.
func Unmarshal(data []byte) (Message, error) {
	var raw Message
	if err := json.Unmarshal(data, &raw); err != nil {
		return Message{}, fmt.Errorf("failed to parse JSON: %w", err)
	}

	// Protocol version validation
	if raw.Version != ProtocolVersion {
		return Message{}, fmt.Errorf(
			"protocol version mismatch: expected %s, got %s",
			ProtocolVersion, raw.Version,
		)
	}

	return raw, nil
}

// Marshal marshals a Message to JSON bytes.
func MarshalMsg(msg Message) ([]byte, error) {
	// Ensure the version is set
	if msg.Version == "" {
		msg.Version = ProtocolVersion
	}
	return json.Marshal(msg)
}

// ValidateType checks that the message type is one of the known protocol types.
func ValidateType(msgType string) bool {
	switch msgType {
	case TypeHealthCheck, TypeShutdownRequest, TypeRuntimeIdentity,
		TypeErrorResponse, TypeSuccessResponse,
		TypeRegisterServer, TypeRemoveServer, TypeListServers,
		TypeDiscoverTools, TypeInvokeTool, TypeToolResult, TypeMCPCommand,
		TypeMCPResponse, TypeTaskState, TypeTaskSubmission, TypeTaskCancellation,
		TypeTaskEvent, TypeTaskResult, TypeTaskError:
		return true
	default:
		return false
	}
}

// Validate validates a message's structural integrity.
func Validate(msg Message) error {
	if msg.Version != ProtocolVersion {
		return fmt.Errorf("protocol version mismatch: expected %s, got %s", ProtocolVersion, msg.Version)
	}

	if !ValidateType(msg.Type) {
		return fmt.Errorf("unknown message type: %s", msg.Type)
	}

	return nil
}

// PrintError prints a structured error response to stderr and exits with code 1.
// This is intended for use in child processes (Python runtime) when reporting
// failures back to the Go supervisor.
func PrintError(code, message string, retryable bool, retryAfter *int) {
	response := ErrorResponse{
		Message: Message{
			Version: ProtocolVersion,
			Type:    TypeErrorResponse,
		},
		ErrMessage: message,
		Code:       code,
		Retryable:  retryable,
		RetryAfter: retryAfter,
	}
	data, _ := json.Marshal(response)
	fmt.Fprintf(os.Stderr, "%s\n", data)
	os.Exit(1)
}

// PrintSuccess prints a structured success response to stdout.
func PrintSuccess(result any) {
	response := SuccessResponse{
		Message: Message{
			Version: ProtocolVersion,
			Type:    TypeSuccessResponse,
		},
	}
	if result != nil {
		if b, ok := result.(json.RawMessage); ok {
			response.Result = b
		} else {
			response.Result = json.RawMessage(fmt.Sprintf("%v", result))
		}
	}
	data, _ := json.Marshal(response)
	fmt.Println(string(data))
}

// ParseAndValidate reads from stdin, validates the protocol message,
// and returns the typed message. Used by the Go supervisor when communicating
// with the Python runtime via stdin/stdout.
func ParseAndValidate() (Message, error) {
	reader := bufio.NewReader(os.Stdin)
	line, err := reader.ReadString('\n')
	if err != nil {
		return Message{}, fmt.Errorf("failed to read protocol message: %w", err)
	}

	line = strings.TrimSpace(line)
	if line == "" {
		return Message{}, fmt.Errorf("empty protocol message")
	}

	var raw Message
	if err := json.Unmarshal([]byte(line), &raw); err != nil {
		return Message{}, fmt.Errorf("failed to parse JSON: %w", err)
	}

	if raw.Version != ProtocolVersion {
		return Message{}, fmt.Errorf(
			"protocol version mismatch: expected %s, got %s",
			ProtocolVersion, raw.Version,
		)
	}

	if !ValidateType(raw.Type) {
		return Message{}, fmt.Errorf("unsupported protocol type: %s", raw.Type)
	}

	return raw, nil
}

// ReadHealthCheck reads and validates a health_check message from stdin.
func ReadHealthCheck() (*HealthCheck, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeHealthCheck {
		return nil, fmt.Errorf("expected health_check, got %s", msg.Type)
	}
	return &HealthCheck{Message: msg}, nil
}

// ReadRegisterServer reads and validates a register_server message from stdin.
func ReadRegisterServer() (*RegisterServer, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeRegisterServer {
		return nil, fmt.Errorf("expected register_server, got %s", msg.Type)
	}
	return &RegisterServer{Message: msg}, nil
}

// ReadRemoveServer reads and validates a remove_server message from stdin.
func ReadRemoveServer() (*RemoveServer, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeRemoveServer {
		return nil, fmt.Errorf("expected remove_server, got %s", msg.Type)
	}
	return &RemoveServer{Message: msg}, nil
}

// ReadListServers reads and validates a list_servers message from stdin.
func ReadListServers() (*ListServers, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeListServers {
		return nil, fmt.Errorf("expected list_servers, got %s", msg.Type)
	}
	return &ListServers{Message: msg}, nil
}

// ReadDiscoverTools reads and validates a discover_tools message from stdin.
func ReadDiscoverTools() (*DiscoverTools, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeDiscoverTools {
		return nil, fmt.Errorf("expected discover_tools, got %s", msg.Type)
	}
	return &DiscoverTools{Message: msg}, nil
}

// ReadInvokeTool reads and validates an invoke_tool message from stdin.
func ReadInvokeTool() (*InvokeTool, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeInvokeTool {
		return nil, fmt.Errorf("expected invoke_tool, got %s", msg.Type)
	}
	return &InvokeTool{Message: msg}, nil
}

// ReadToolResult reads and validates a tool_result message from stdin.
func ReadToolResult() (*ToolResult, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeToolResult {
		return nil, fmt.Errorf("expected tool_result, got %s", msg.Type)
	}
	return &ToolResult{Message: msg}, nil
}

// ReadTaskState reads and validates a task_state message from stdin.
func ReadTaskState() (*TaskState, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeTaskState {
		return nil, fmt.Errorf("expected task_state, got %s", msg.Type)
	}
	return &TaskState{Message: msg}, nil
}

// NewFrameReader returns a reader sized for the largest legal protocol frame.
func NewFrameReader(input io.Reader) *bufio.Reader {
	return bufio.NewReaderSize(input, MaxMessageBytes+1)
}

// ReadFrame reads one bounded newline-delimited protocol frame. The reader
// must have been created with NewFrameReader or an equivalent buffer size.
func ReadFrame(reader *bufio.Reader) ([]byte, error) {
	line, err := reader.ReadSlice('\n')
	if errors.Is(err, bufio.ErrBufferFull) {
		return nil, fmt.Errorf("protocol message exceeds %d bytes", MaxMessageBytes)
	}
	if err != nil {
		return nil, err
	}
	trimmed := strings.TrimRight(string(line), "\r\n")
	if trimmed == "" {
		return nil, errors.New("empty protocol message")
	}
	if len(trimmed) > MaxMessageBytes {
		return nil, fmt.Errorf("protocol message exceeds %d bytes", MaxMessageBytes)
	}
	return []byte(trimmed), nil
}

// ValidTaskID mirrors the shared task identifier policy at the process boundary.
func ValidTaskID(id string) bool {
	if id == "" || len(id) > 200 {
		return false
	}
	for i, r := range id {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-', r == '_', r == '.', r == ':':
			if i == 0 {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func decodeTyped(data []byte, expected string, target any) error {
	msg, err := Unmarshal(data)
	if err != nil {
		return err
	}
	if msg.Type != expected {
		return fmt.Errorf("expected %s, got %s", expected, msg.Type)
	}
	if err := json.Unmarshal(data, target); err != nil {
		return fmt.Errorf("invalid %s payload: %w", expected, err)
	}
	return nil
}

// DecodeRuntimeIdentity validates a runtime_identity message.
func DecodeRuntimeIdentity(data []byte) (*RuntimeIdentity, error) {
	var msg RuntimeIdentity
	if err := decodeTyped(data, TypeRuntimeIdentity, &msg); err != nil {
		return nil, err
	}
	if msg.RuntimeVersion == "" || msg.PID <= 0 {
		return nil, errors.New("runtime_identity is missing required fields")
	}
	return &msg, nil
}

// DecodeTaskState validates a task_state checkpoint message.
func DecodeTaskState(data []byte) (*TaskState, error) {
	var msg TaskState
	if err := decodeTyped(data, TypeTaskState, &msg); err != nil {
		return nil, err
	}
	if !ValidTaskID(msg.TaskId) || msg.State == "" || len(msg.Checkpoint) == 0 || !json.Valid(msg.Checkpoint) {
		return nil, errors.New("task_state is missing required fields")
	}
	return &msg, nil
}

// DecodeTaskEvent validates a workflow event message.
func DecodeTaskEvent(data []byte) (*TaskEvent, error) {
	var msg TaskEvent
	if err := decodeTyped(data, TypeTaskEvent, &msg); err != nil {
		return nil, err
	}
	if !ValidTaskID(msg.TaskID) || msg.EventType == "" {
		return nil, errors.New("task_event is missing required fields")
	}
	if len(msg.Payload) > 0 && !json.Valid(msg.Payload) {
		return nil, errors.New("task_event payload is not valid JSON")
	}
	return &msg, nil
}

// DecodeTaskResult validates a terminal task_result message.
func DecodeTaskResult(data []byte) (*TaskResult, error) {
	var msg TaskResult
	if err := decodeTyped(data, TypeTaskResult, &msg); err != nil {
		return nil, err
	}
	if !ValidTaskID(msg.TaskID) {
		return nil, errors.New("task_result has an invalid task identifier")
	}
	switch msg.Status {
	case "succeeded", "failed", "cancelled":
	default:
		return nil, fmt.Errorf("task_result has unsupported status %q", msg.Status)
	}
	if msg.Result != nil && !json.Valid(msg.Result) {
		return nil, errors.New("task_result payload is not valid JSON")
	}
	if msg.Checkpoint != nil && !json.Valid(msg.Checkpoint) {
		return nil, errors.New("task_result checkpoint is not valid JSON")
	}
	return &msg, nil
}

// DecodeTaskError validates an explicit task_error message.
func DecodeTaskError(data []byte) (*TaskError, error) {
	var msg TaskError
	if err := decodeTyped(data, TypeTaskError, &msg); err != nil {
		return nil, err
	}
	if !ValidTaskID(msg.TaskID) || msg.Code == "" || msg.Detail == "" {
		return nil, errors.New("task_error is missing required fields")
	}
	return &msg, nil
}

// DecodeErrorResponse validates an error_response message.
func DecodeErrorResponse(data []byte) (*ErrorResponse, error) {
	var msg ErrorResponse
	if err := decodeTyped(data, TypeErrorResponse, &msg); err != nil {
		return nil, err
	}
	if msg.Code == "" || msg.ErrMessage == "" {
		return nil, errors.New("error_response is missing required fields")
	}
	return &msg, nil
}

func validCommandID(value string) bool {
	if value == "" || len(value) > 128 {
		return false
	}
	for i, r := range value {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-' || r == '_' || r == '.' || r == ':' || r == '/':
			if i == 0 {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validMCPIdentifier(value string) bool {
	if value == "" || len(value) > 200 {
		return false
	}
	for i, r := range value {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-' || r == '_' || r == '.':
			if i == 0 {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validFingerprint(value string) bool {
	if len(value) != 64 {
		return false
	}
	for _, r := range value {
		if !(r >= '0' && r <= '9') && !(r >= 'a' && r <= 'f') && !(r >= 'A' && r <= 'F') {
			return false
		}
	}
	return true
}

// DecodeMCPCommand validates one Go-to-Python MCP daemon command.
func DecodeMCPCommand(data []byte) (*MCPCommand, error) {
	var msg MCPCommand
	if err := decodeTyped(data, TypeMCPCommand, &msg); err != nil {
		return nil, err
	}
	if !validCommandID(msg.CommandID) {
		return nil, errors.New("mcp_command has an invalid command identifier")
	}
	switch msg.Operation {
	case "configure", "add_server", "remove_server", "connect", "disconnect",
		"discover_tools", "invoke_tool", "list_servers", "list_tools",
		"list_invocations", "clear_invocations":
	default:
		return nil, fmt.Errorf("mcp_command has unsupported operation %q", msg.Operation)
	}
	if len(msg.Payload) > 0 && !json.Valid(msg.Payload) {
		return nil, errors.New("mcp_command payload is not valid JSON")
	}
	return &msg, nil
}

// DecodeMCPResponse validates one Python-to-Go MCP daemon response.
func DecodeMCPResponse(data []byte) (*MCPResponse, error) {
	var msg MCPResponse
	if err := decodeTyped(data, TypeMCPResponse, &msg); err != nil {
		return nil, err
	}
	if !validCommandID(msg.CommandID) {
		return nil, errors.New("mcp_response has an invalid command identifier")
	}
	if !msg.Success {
		if msg.Error == nil || msg.Error.Code == "" || msg.Error.Message == "" {
			return nil, errors.New("failed mcp_response is missing a safe error")
		}
		return &msg, nil
	}
	if len(msg.Result) > 0 && !json.Valid(msg.Result) {
		return nil, errors.New("mcp_response result is not valid JSON")
	}
	return &msg, nil
}

// DecodeToolResult validates a durable MCP tool invocation frame.
func DecodeToolResult(data []byte) (*ToolResult, error) {
	var msg ToolResult
	if err := decodeTyped(data, TypeToolResult, &msg); err != nil {
		return nil, err
	}
	if !ValidTaskID(msg.TaskID) || !validMCPIdentifier(msg.InvocationID) ||
		!validMCPIdentifier(msg.ServerID) || !validMCPIdentifier(msg.ToolName) {
		return nil, errors.New("tool_result is missing valid identifiers")
	}
	if len(msg.Arguments) > 0 && !json.Valid(msg.Arguments) {
		return nil, errors.New("tool_result arguments are not valid JSON")
	}
	if len(msg.Result) > 0 && !json.Valid(msg.Result) {
		return nil, errors.New("tool_result payload is not valid JSON")
	}
	if msg.DurationMS < 0 {
		return nil, errors.New("tool_result has a negative duration")
	}
	switch msg.Status {
	case "", "running", "succeeded", "failed", "blocked":
	default:
		return nil, fmt.Errorf("tool_result has unsupported status %q", msg.Status)
	}
	return &msg, nil
}

// DecodeTaskSubmission validates a task_submission message including MCP grants.
func DecodeTaskSubmission(data []byte) (*TaskSubmission, error) {
	var msg TaskSubmission
	if err := decodeTyped(data, TypeTaskSubmission, &msg); err != nil {
		return nil, err
	}
	if !ValidTaskID(msg.TaskID) || strings.TrimSpace(msg.Input) == "" || len(msg.Input) > 64<<10 {
		return nil, errors.New("task_submission is missing required fields")
	}
	if len(msg.MCPServers) > 32 || len(msg.GrantedApprovals) > 128 {
		return nil, errors.New("task_submission MCP fields exceed their bounds")
	}
	for _, server := range msg.MCPServers {
		if !validMCPIdentifier(server.ServerID) || server.Name == "" || len(server.Name) > 128 {
			return nil, errors.New("task_submission has an invalid MCP server")
		}
		switch server.Transport {
		case "stdio", "streamable_http":
		default:
			return nil, errors.New("task_submission has an unsupported MCP transport")
		}
	}
	for _, fingerprint := range msg.GrantedApprovals {
		if !validFingerprint(fingerprint) {
			return nil, errors.New("task_submission has an invalid approval fingerprint")
		}
	}
	if msg.ConversationID != "" && !ValidTaskID(msg.ConversationID) {
		return nil, errors.New("task_submission has an invalid conversation identifier")
	}
	if len(msg.ConversationMessages) > 200 {
		return nil, errors.New("task_submission conversation history exceeds bounds")
	}
	for _, m := range msg.ConversationMessages {
		switch strings.ToLower(strings.TrimSpace(m.Role)) {
		case "user", "assistant", "system":
		default:
			return nil, errors.New("task_submission has an invalid conversation message role")
		}
		if len(m.Content) > 16<<10 {
			return nil, errors.New("task_submission conversation message exceeds size limit")
		}
	}
	return &msg, nil
}
