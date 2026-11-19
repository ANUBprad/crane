package python_protocol

import (
	"bufio"
	"encoding/json"
	"fmt"
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
	TypeTaskState        = "task_state"
)

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
	ErrMessage  string `json:"message"`
	Code        string `json:"code"`
	RetryAfter  *int   `json:"retry_after,omitempty"`
	Retryable   bool   `json:"retryable"`
}

// SuccessResponse is a protocol success message.
type SuccessResponse struct {
	Message
	Result json.RawMessage `json:"result,omitempty"`
}

// RegisterServer is a protocol message for registering an MCP server.
type RegisterServer struct {
	Message
	ServerName   string `json:"server_name"`
	Transport    string `json:"transport"` // "stdio" or "streamable_http"
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
	ServerName string `json:"server_name"`
	ToolName   string `json:"tool_name"`
	Arguments  json.RawMessage `json:"arguments,omitempty"`
}

// ToolResult is a protocol message returning tool invocation results.
type ToolResult struct {
	Message
	ServerName string `json:"server_name"`
	ToolName   string `json:"tool_name"`
	Success    bool      `json:"success"`
	Result     json.RawMessage `json:"result,omitempty"`
	Error      string    `json:"error,omitempty"`
}

// TaskState is a protocol message for persisting workflow checkpoints.
// The Go supervisor stores this in SQLite and can resume the workflow after restart.
type TaskState struct {
	Message
	TaskId       string `json:"task_id"`
	State        string `json:"state"`       // TaskState enum value
	CurrentStep  string `json:"current_step"`
	GraphSteps   int    `json:"graph_steps"`
	WallClockMs  int64  `json:"wall_clock_ms"`
	Result       json.RawMessage `json:"result,omitempty"`
	ResultSummary string `json:"result_summary,omitempty"`
	FailureReason string `json:"failure_reason,omitempty"`
	Cancelled    bool   `json:"cancelled"`
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
		TypeDiscoverTools, TypeInvokeTool, TypeToolResult, TypeTaskState:
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
		Code:      code,
		Retryable: retryable,
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