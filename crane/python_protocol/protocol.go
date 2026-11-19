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
	TypeHealthCheck     = "health_check"
	TypeShutdownRequest = "shutdown_request"
	TypeRuntimeIdentity = "runtime_identity"
	TypeErrorResponse   = "error_response"
	TypeSuccessResponse = "success_response"
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
		TypeErrorResponse, TypeSuccessResponse:
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

// ReadShutdownRequest reads and validates a shutdown_request message from stdin.
func ReadShutdownRequest() (*ShutdownRequest, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeShutdownRequest {
		return nil, fmt.Errorf("expected shutdown_request, got %s", msg.Type)
	}
	return &ShutdownRequest{Message: msg}, nil
}

// ReadRuntimeIdentity reads and validates a runtime_identity message from stdin.
func ReadRuntimeIdentity() (*RuntimeIdentity, error) {
	msg, err := ParseAndValidate()
	if err != nil {
		return nil, err
	}
	if msg.Type != TypeRuntimeIdentity {
		return nil, fmt.Errorf("expected runtime_identity, got %s", msg.Type)
	}
	return &RuntimeIdentity{
		Message:        msg,
		RuntimeVersion: msg.Version,
		PythonVersion:  os.Getenv("PYTHON_VERSION"),
		PID:            os.Getpid(),
	}, nil
}
