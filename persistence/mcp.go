package persistence

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"
)

var (
	// ErrMCPServerNotFound is returned when a server has no persisted record.
	ErrMCPServerNotFound = errors.New("MCP server not found")
	// ErrApprovalNotFound is returned when an approval identifier is unknown.
	ErrApprovalNotFound = errors.New("approval request not found")
	// ErrApprovalAlreadyDecided protects a recorded decision from being changed.
	ErrApprovalAlreadyDecided = errors.New("approval request was already decided")
	// ErrApprovalExpired prevents an expired approval from being granted.
	ErrApprovalExpired = errors.New("approval request has expired")

	mcpServerIDPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`)
	mcpEnvKeyPattern    = regexp.MustCompile(`^CRANE_MCP_[A-Z0-9_]{1,127}$`)
	mcpSecretKeyPattern = regexp.MustCompile(`(?i)(TOKEN|SECRET|PASSWORD|CREDENTIAL|API_?KEY|PRIVATE)`)
	mcpDangerousArg     = regexp.MustCompile(`(?i)^(-c|-m|--eval|--exec|-e|--command)$`)
	mcpSecretInText     = regexp.MustCompile(`(?i)\b(bearer\s+[A-Za-z0-9._~+/=-]+|(token|secret|password|api[_-]?key|credential)\s*[:=]\s*("[^"]*"|'[^']*'|[^\s,;&}\)]+))`)
)

// MCPServerRecord is the persisted server configuration. Env values are kept
// private and are deliberately excluded from JSON serialization.
type MCPServerRecord struct {
	ID                       string            `json:"server_id"`
	Name                     string            `json:"name"`
	Transport                string            `json:"transport"`
	Command                  string            `json:"command,omitempty"`
	Args                     []string          `json:"args,omitempty"`
	Env                      map[string]string `json:"-"`
	EnvKeys                  []string          `json:"env_keys,omitempty"`
	URL                      string            `json:"url,omitempty"`
	Enabled                  bool              `json:"enabled"`
	ConnectTimeoutSeconds    float64           `json:"connect_timeout_seconds"`
	InvocationTimeoutSeconds float64           `json:"invocation_timeout_seconds"`
	AllowRemoteEndpoint      bool              `json:"allow_remote_endpoint,omitempty"`
	Status                   string            `json:"status"`
	LastError                string            `json:"last_error,omitempty"`
	CreatedAt                string            `json:"created_at,omitempty"`
	UpdatedAt                string            `json:"updated_at,omitempty"`
}

// MCPToolRecord is an API-safe discovered tool summary.
type MCPToolRecord struct {
	ServerID     string          `json:"server_id"`
	ToolName     string          `json:"tool_name"`
	Description  string          `json:"description,omitempty"`
	InputSchema  json.RawMessage `json:"input_schema"`
	Annotations  json.RawMessage `json:"annotations,omitempty"`
	PolicyClass  string          `json:"policy_class"`
	DiscoveredAt string          `json:"discovered_at,omitempty"`
}

// MCPInvocationRecord is an API-safe durable tool invocation record.
type MCPInvocationRecord struct {
	ID             string          `json:"invocation_id"`
	TaskID         string          `json:"task_id,omitempty"`
	ServerID       string          `json:"server_id"`
	ServerName     string          `json:"server_name,omitempty"`
	ToolName       string          `json:"tool_name"`
	Arguments      json.RawMessage `json:"arguments"`
	Status         string          `json:"status"`
	Result         json.RawMessage `json:"result,omitempty"`
	Error          string          `json:"error,omitempty"`
	PolicyDecision string          `json:"policy_decision,omitempty"`
	DurationMS     int64           `json:"duration_ms,omitempty"`
	CreatedAt      string          `json:"created_at,omitempty"`
	CompletedAt    string          `json:"completed_at,omitempty"`
}

// ApprovalRequestRecord is the durable, exact-action approval record.
type ApprovalRequestRecord struct {
	ID          string          `json:"id"`
	TaskID      string          `json:"task_id,omitempty"`
	ServerID    string          `json:"server_id"`
	ToolName    string          `json:"tool_name"`
	Arguments   json.RawMessage `json:"arguments"`
	Fingerprint string          `json:"fingerprint"`
	Status      string          `json:"status"`
	RequestedAt string          `json:"requested_at,omitempty"`
	ExpiresAt   string          `json:"expires_at,omitempty"`
	DecidedAt   string          `json:"decided_at,omitempty"`
	DecidedBy   string          `json:"decided_by,omitempty"`
	Decision    string          `json:"decision,omitempty"`
}

type dbScanner interface {
	Scan(dest ...any) error
}

// RedactMCPValue removes likely secrets from data that could reach persisted
// records, API responses, events or diagnostics.
func RedactMCPValue(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			if mcpSecretKeyPattern.MatchString(key) {
				result[key] = "[redacted]"
				continue
			}
			result[key] = RedactMCPValue(item)
		}
		return result
	case map[string]string:
		result := make(map[string]string, len(typed))
		for key, item := range typed {
			if mcpSecretKeyPattern.MatchString(key) {
				result[key] = "[redacted]"
			} else {
				result[key] = RedactMCPValue(item).(string)
			}
		}
		return result
	case []any:
		result := make([]any, 0, len(typed))
		for _, item := range typed {
			result = append(result, RedactMCPValue(item))
		}
		return result
	case []string:
		result := make([]string, 0, len(typed))
		for _, item := range typed {
			result = append(result, RedactMCPValue(item).(string))
		}
		return result
	case string:
		return mcpSecretInText.ReplaceAllString(typed, "[redacted]")
	default:
		return value
	}
}

// RedactedJSON returns compact JSON with likely secrets removed.
func RedactedJSON(value any) json.RawMessage {
	encoded, err := json.Marshal(RedactMCPValue(value))
	if err != nil {
		return json.RawMessage(`{}`)
	}
	return encoded
}

// ApprovalFingerprint produces the exact-action digest shared with Python.
func ApprovalFingerprint(serverID, toolName string, arguments any) string {
	payload := map[string]any{
		"server_id": serverID,
		"tool_name": toolName,
		"arguments": RedactMCPValue(arguments),
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return ""
	}
	sum := sha256Sum(encoded)
	return hex.EncodeToString(sum[:])
}

// ValidateMCPServerRecord enforces the same narrow configuration policy used
// by the Python MCP runtime before any record reaches SQLite.
func ValidateMCPServerRecord(record MCPServerRecord) error {
	if !mcpServerIDPattern.MatchString(record.ID) {
		return errors.New("MCP server id is invalid")
	}
	if record.Name == "" || len(record.Name) > 128 || strings.ContainsAny(record.Name, "\r\n") {
		return errors.New("MCP server name is invalid")
	}
	if record.ConnectTimeoutSeconds < 0.1 || record.ConnectTimeoutSeconds > 30 {
		return errors.New("MCP connect timeout is out of range")
	}
	if record.InvocationTimeoutSeconds < 0.1 || record.InvocationTimeoutSeconds > 300 {
		return errors.New("MCP invocation timeout is out of range")
	}
	if len(record.Args) > 32 || len(record.Env) > 32 {
		return errors.New("MCP server arguments or environment are too large")
	}
	for key, value := range record.Env {
		if !mcpEnvKeyPattern.MatchString(key) || mcpSecretKeyPattern.MatchString(key) {
			return errors.New("MCP environment variable name is not allowed")
		}
		if len(value) > 1024 {
			return errors.New("MCP environment variable value is too long")
		}
	}
	switch record.Transport {
	case "stdio":
		allowed := map[string]bool{}
		configured := strings.TrimSpace(getenvCRANE("CRANE_MCP_ALLOWED_COMMANDS"))
		if configured == "" {
			configured = "python,pythonw,node"
		}
		for _, item := range strings.Split(configured, ",") {
			if item = strings.TrimSpace(item); item != "" {
				allowed[item] = true
			}
		}
		if !allowed[record.Command] {
			return errors.New("MCP stdio executable is not allowed")
		}
		for _, arg := range record.Args {
			if arg == "" || len(arg) > 1024 || strings.ContainsAny(arg, "\x00\r\n") || mcpDangerousArg.MatchString(arg) {
				return errors.New("MCP stdio argument is not allowed")
			}
		}
		if record.URL != "" {
			return errors.New("MCP stdio server must not define a URL")
		}
	case "streamable_http":
		parsed, err := url.Parse(record.URL)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Hostname() == "" {
			return errors.New("MCP HTTP endpoint is invalid")
		}
		if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
			return errors.New("MCP HTTP endpoint must not contain credentials, query or fragment")
		}
		if parsed.Path != "" && parsed.Path != "/" && parsed.Path != "/mcp" {
			return errors.New("MCP HTTP endpoint path is not allowed")
		}
		if !record.AllowRemoteEndpoint && !isLoopbackHost(parsed.Hostname()) {
			return errors.New("MCP endpoint is not local")
		}
		if record.Command != "" || len(record.Args) > 0 || len(record.Env) > 0 {
			return errors.New("MCP HTTP server must not define process settings")
		}
	default:
		return errors.New("MCP transport is not supported")
	}
	return nil
}

func normalizeMCPServer(record MCPServerRecord) MCPServerRecord {
	if record.Args == nil {
		record.Args = []string{}
	}
	if record.Env == nil {
		record.Env = map[string]string{}
	}
	if record.ConnectTimeoutSeconds == 0 {
		record.ConnectTimeoutSeconds = 10
	}
	if record.InvocationTimeoutSeconds == 0 {
		record.InvocationTimeoutSeconds = 30
	}
	if record.Status == "" {
		record.Status = "registered"
	}
	record.EnvKeys = make([]string, 0, len(record.Env))
	for key := range record.Env {
		record.EnvKeys = append(record.EnvKeys, key)
	}
	sortStrings(record.EnvKeys)
	return record
}

func scanMCPServer(scanner dbScanner) (MCPServerRecord, error) {
	var record MCPServerRecord
	var argsRaw, envRaw string
	var enabled, remote int
	if err := scanner.Scan(
		&record.ID, &record.Name, &record.Transport, &record.Command, &argsRaw, &envRaw,
		&record.URL, &enabled, &record.ConnectTimeoutSeconds, &record.InvocationTimeoutSeconds,
		&remote, &record.Status, &record.LastError, &record.CreatedAt, &record.UpdatedAt,
	); err != nil {
		return MCPServerRecord{}, err
	}
	record.Enabled = enabled != 0
	record.AllowRemoteEndpoint = remote != 0
	if err := json.Unmarshal([]byte(argsRaw), &record.Args); err != nil {
		return MCPServerRecord{}, fmt.Errorf("decode MCP arguments: %w", err)
	}
	if err := json.Unmarshal([]byte(envRaw), &record.Env); err != nil {
		return MCPServerRecord{}, fmt.Errorf("decode MCP environment: %w", err)
	}
	return normalizeMCPServer(record), nil
}

// UpsertMCPServer creates or replaces one validated MCP server configuration.
func (s *Store) UpsertMCPServer(record MCPServerRecord) error {
	record = normalizeMCPServer(record)
	if err := ValidateMCPServerRecord(record); err != nil {
		return err
	}
	args, err := json.Marshal(record.Args)
	if err != nil {
		return err
	}
	env, err := json.Marshal(record.Env)
	if err != nil {
		return err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err = s.db.Exec(`
		INSERT INTO mcp_servers
			(id, name, transport, command, args, env, url, enabled,
			 connect_timeout_seconds, invocation_timeout_seconds,
			 allow_remote_endpoint, status, last_error, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			name=excluded.name, transport=excluded.transport, command=excluded.command,
			args=excluded.args, env=excluded.env, url=excluded.url, enabled=excluded.enabled,
			connect_timeout_seconds=excluded.connect_timeout_seconds,
			invocation_timeout_seconds=excluded.invocation_timeout_seconds,
			allow_remote_endpoint=excluded.allow_remote_endpoint,
			status=excluded.status, last_error=excluded.last_error, updated_at=excluded.updated_at`,
		record.ID, record.Name, record.Transport, record.Command, string(args), string(env),
		record.URL, boolToInt(record.Enabled), record.ConnectTimeoutSeconds,
		record.InvocationTimeoutSeconds, boolToInt(record.AllowRemoteEndpoint),
		record.Status, record.LastError, now, now,
	)
	if err != nil {
		return fmt.Errorf("save MCP server %q: %w", record.ID, err)
	}
	return nil
}

// GetMCPServer returns one server record including private env values for the
// Go-to-Python process boundary. JSON serialization still omits those values.
func (s *Store) GetMCPServer(id string) (MCPServerRecord, error) {
	record, err := scanMCPServer(s.db.QueryRow(`
		SELECT id, name, transport, command, args, env, url, enabled,
		       connect_timeout_seconds, invocation_timeout_seconds,
		       allow_remote_endpoint, status, last_error, created_at, updated_at
		FROM mcp_servers WHERE id = ?`, id))
	if errors.Is(err, sql.ErrNoRows) {
		return MCPServerRecord{}, ErrMCPServerNotFound
	}
	if err != nil {
		return MCPServerRecord{}, fmt.Errorf("get MCP server %q: %w", id, err)
	}
	return record, nil
}

// ListMCPServers returns all API-safe server summaries.
func (s *Store) ListMCPServers() ([]MCPServerRecord, error) {
	rows, err := s.db.Query(`
		SELECT id, name, transport, command, args, env, url, enabled,
		       connect_timeout_seconds, invocation_timeout_seconds,
		       allow_remote_endpoint, status, last_error, created_at, updated_at
		FROM mcp_servers ORDER BY id`)
	if err != nil {
		return nil, fmt.Errorf("list MCP servers: %w", err)
	}
	defer rows.Close()
	records := make([]MCPServerRecord, 0)
	for rows.Next() {
		record, err := scanMCPServer(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate MCP servers: %w", err)
	}
	return records, nil
}

// SetMCPServerEnabled changes only the enabled state of a server.
func (s *Store) SetMCPServerEnabled(id string, enabled bool) error {
	result, err := s.db.Exec(
		"UPDATE mcp_servers SET enabled = ?, updated_at = ? WHERE id = ?",
		boolToInt(enabled), time.Now().UTC().Format(time.RFC3339Nano), id,
	)
	if err != nil {
		return fmt.Errorf("set MCP server %q enabled: %w", id, err)
	}
	affected, err := result.RowsAffected()
	if err != nil || affected == 0 {
		return ErrMCPServerNotFound
	}
	return nil
}

// UpdateMCPServerStatus records connection state and the last safe error.
func (s *Store) UpdateMCPServerStatus(id, status, lastError string) error {
	result, err := s.db.Exec(
		"UPDATE mcp_servers SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
		status, lastError, time.Now().UTC().Format(time.RFC3339Nano), id,
	)
	if err != nil {
		return fmt.Errorf("update MCP server %q status: %w", id, err)
	}
	affected, err := result.RowsAffected()
	if err != nil || affected == 0 {
		return ErrMCPServerNotFound
	}
	return nil
}

// DeleteMCPServer removes a server and its discovered tools.
func (s *Store) DeleteMCPServer(id string) error {
	result, err := s.db.Exec("DELETE FROM mcp_servers WHERE id = ?", id)
	if err != nil {
		return fmt.Errorf("delete MCP server %q: %w", id, err)
	}
	affected, err := result.RowsAffected()
	if err != nil || affected == 0 {
		return ErrMCPServerNotFound
	}
	return nil
}

// ReplaceMCPTools atomically replaces the discovered tool catalog for a server.
func (s *Store) ReplaceMCPTools(serverID string, tools []MCPToolRecord) error {
	if _, err := s.GetMCPServer(serverID); err != nil {
		return err
	}
	tx, err := s.db.Begin()
	if err != nil {
		return fmt.Errorf("begin MCP tool replacement: %w", err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec("DELETE FROM mcp_tools WHERE server_id = ?", serverID); err != nil {
		return fmt.Errorf("clear MCP tools: %w", err)
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	for _, tool := range tools {
		if !mcpServerIDPattern.MatchString(tool.ToolName) {
			return errors.New("MCP tool name is invalid")
		}
		schema := string(tool.InputSchema)
		if !json.Valid([]byte(schema)) {
			return errors.New("MCP tool schema is invalid")
		}
		annotations := string(tool.Annotations)
		if annotations == "" || !json.Valid([]byte(annotations)) {
			annotations = "{}"
		}
		switch tool.PolicyClass {
		case "allowed", "approval_required", "blocked":
		default:
			return errors.New("MCP tool policy class is invalid")
		}
		description := RedactMCPValue(tool.Description).(string)
		if _, err := tx.Exec(`
			INSERT INTO mcp_tools (server_id, tool_name, description, input_schema, annotations, policy_class, discovered_at)
			VALUES (?, ?, ?, ?, ?, ?, ?)`,
			serverID, tool.ToolName, description, schema, annotations, tool.PolicyClass, now,
		); err != nil {
			return fmt.Errorf("save MCP tool %q: %w", tool.ToolName, err)
		}
	}
	return tx.Commit()
}

// ListMCPTools returns discovered tools, optionally filtered by server.
func (s *Store) ListMCPTools(serverID string) ([]MCPToolRecord, error) {
	query := `
		SELECT server_id, tool_name, description, input_schema, annotations, policy_class, discovered_at
		FROM mcp_tools`
	args := []any{}
	if serverID != "" {
		query += " WHERE server_id = ?"
		args = append(args, serverID)
	}
	query += " ORDER BY server_id, tool_name"
	rows, err := s.db.Query(query, args...)
	if err != nil {
		return nil, fmt.Errorf("list MCP tools: %w", err)
	}
	defer rows.Close()
	records := make([]MCPToolRecord, 0)
	for rows.Next() {
		var record MCPToolRecord
		var schema, annotations string
		if err := rows.Scan(
			&record.ServerID, &record.ToolName, &record.Description, &schema,
			&annotations, &record.PolicyClass, &record.DiscoveredAt,
		); err != nil {
			return nil, fmt.Errorf("scan MCP tool: %w", err)
		}
		record.InputSchema = json.RawMessage(schema)
		record.Annotations = json.RawMessage(annotations)
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate MCP tools: %w", err)
	}
	return records, nil
}

// RecordMCPInvocation upserts one safe tool invocation record.
func (s *Store) RecordMCPInvocation(record MCPInvocationRecord) error {
	if !mcpServerIDPattern.MatchString(record.ID) || !mcpServerIDPattern.MatchString(record.ServerID) ||
		!mcpServerIDPattern.MatchString(record.ToolName) {
		return errors.New("MCP invocation identifiers are invalid")
	}
	switch record.Status {
	case "running", "succeeded", "failed", "blocked":
	default:
		return errors.New("MCP invocation status is invalid")
	}
	arguments := RedactedJSON(jsonRawToValue(record.Arguments))
	result := RedactedJSON(jsonRawToValue(record.Result))
	errorText := RedactMCPValue(record.Error).(string)
	now := time.Now().UTC().Format(time.RFC3339Nano)
	if record.CreatedAt == "" {
		record.CreatedAt = now
	}
	if record.CompletedAt == "" {
		record.CompletedAt = now
	}
	_, err := s.db.Exec(`
		INSERT INTO mcp_invocations
			(id, task_id, server_id, server_name, tool_name, arguments, status,
			 result, error, policy_decision, duration_ms, created_at, completed_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO UPDATE SET
			status=excluded.status, result=excluded.result, error=excluded.error,
			policy_decision=excluded.policy_decision, duration_ms=excluded.duration_ms,
			completed_at=excluded.completed_at`,
		record.ID, record.TaskID, record.ServerID, record.ServerName, record.ToolName,
		string(arguments), record.Status, string(result), errorText,
		record.PolicyDecision, record.DurationMS, record.CreatedAt, record.CompletedAt,
	)
	if err != nil {
		return fmt.Errorf("record MCP invocation %q: %w", record.ID, err)
	}
	return nil
}

// ListMCPInvocations returns the newest safe invocation records.
func (s *Store) ListMCPInvocations(limit int) ([]MCPInvocationRecord, error) {
	if limit <= 0 || limit > 200 {
		limit = 200
	}
	rows, err := s.db.Query(`
		SELECT id, task_id, server_id, server_name, tool_name, arguments, status,
		       result, error, policy_decision, duration_ms, created_at, completed_at
		FROM mcp_invocations ORDER BY created_at DESC, id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, fmt.Errorf("list MCP invocations: %w", err)
	}
	defer rows.Close()
	records := make([]MCPInvocationRecord, 0)
	for rows.Next() {
		var record MCPInvocationRecord
		var arguments, result string
		if err := rows.Scan(
			&record.ID, &record.TaskID, &record.ServerID, &record.ServerName,
			&record.ToolName, &arguments, &record.Status, &result, &record.Error,
			&record.PolicyDecision, &record.DurationMS, &record.CreatedAt, &record.CompletedAt,
		); err != nil {
			return nil, fmt.Errorf("scan MCP invocation: %w", err)
		}
		record.Arguments = json.RawMessage(arguments)
		record.Result = json.RawMessage(result)
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate MCP invocations: %w", err)
	}
	return records, nil
}

// CreateApprovalRequest stores a pending exact-action approval.
func (s *Store) CreateApprovalRequest(request ApprovalRequestRecord) error {
	if !mcpServerIDPattern.MatchString(request.ID) || !mcpServerIDPattern.MatchString(request.ServerID) ||
		!mcpServerIDPattern.MatchString(request.ToolName) || !validFingerprint(request.Fingerprint) {
		return errors.New("approval request identifiers are invalid")
	}
	if request.TaskID != "" && !ValidTaskID(request.TaskID) {
		return errors.New("approval request task identifier is invalid")
	}
	if !json.Valid(request.Arguments) {
		return errors.New("approval request arguments are invalid")
	}
	status := request.Status
	if status == "" {
		status = "pending"
	}
	if status != "pending" {
		return errors.New("new approval requests must be pending")
	}
	now := time.Now().UTC()
	requestedAt := request.RequestedAt
	if requestedAt == "" {
		requestedAt = now.Format(time.RFC3339Nano)
	}
	expiresAt := request.ExpiresAt
	if expiresAt == "" {
		expiresAt = now.Add(5 * time.Minute).Format(time.RFC3339Nano)
	}
	arguments := RedactedJSON(jsonRawToValue(request.Arguments))
	_, err := s.db.Exec(`
		INSERT INTO mcp_approval_requests
			(id, task_id, server_id, tool_name, arguments, fingerprint, status,
			 requested_at, expires_at, decided_at, decided_by, decision)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', '', '')`,
		request.ID, request.TaskID, request.ServerID, request.ToolName,
		string(arguments), request.Fingerprint, status, requestedAt, expiresAt,
	)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "unique constraint") {
			return nil // The exact action is already tracked.
		}
		return fmt.Errorf("create approval request: %w", err)
	}
	return nil
}

func scanApproval(scanner dbScanner) (ApprovalRequestRecord, error) {
	var record ApprovalRequestRecord
	var arguments string
	if err := scanner.Scan(
		&record.ID, &record.TaskID, &record.ServerID, &record.ToolName, &arguments,
		&record.Fingerprint, &record.Status, &record.RequestedAt, &record.ExpiresAt,
		&record.DecidedAt, &record.DecidedBy, &record.Decision,
	); err != nil {
		return ApprovalRequestRecord{}, err
	}
	record.Arguments = json.RawMessage(arguments)
	return record, nil
}

func approvalColumns() string {
	return `id, task_id, server_id, tool_name, arguments, fingerprint, status,
	        requested_at, expires_at, decided_at, decided_by, decision`
}

// GetApprovalRequest returns one durable approval record.
func (s *Store) GetApprovalRequest(id string) (ApprovalRequestRecord, error) {
	record, err := scanApproval(s.db.QueryRow(
		"SELECT "+approvalColumns()+" FROM mcp_approval_requests WHERE id = ?", id))
	if errors.Is(err, sql.ErrNoRows) {
		return ApprovalRequestRecord{}, ErrApprovalNotFound
	}
	if err != nil {
		return ApprovalRequestRecord{}, fmt.Errorf("get approval request %q: %w", id, err)
	}
	return record, nil
}

// GetApprovalByFingerprint returns the approval record for one exact action.
func (s *Store) GetApprovalByFingerprint(fingerprint string) (ApprovalRequestRecord, error) {
	if !validFingerprint(fingerprint) {
		return ApprovalRequestRecord{}, errors.New("approval fingerprint is invalid")
	}
	record, err := scanApproval(s.db.QueryRow(
		"SELECT "+approvalColumns()+" FROM mcp_approval_requests WHERE fingerprint = ?", fingerprint))
	if errors.Is(err, sql.ErrNoRows) {
		return ApprovalRequestRecord{}, ErrApprovalNotFound
	}
	if err != nil {
		return ApprovalRequestRecord{}, fmt.Errorf("get approval by fingerprint: %w", err)
	}
	return record, nil
}

// DecideApprovalRequest records a granted or denied decision exactly once.
func (s *Store) DecideApprovalRequest(id, decision, actor string) (ApprovalRequestRecord, error) {
	switch decision {
	case "granted", "denied":
	default:
		return ApprovalRequestRecord{}, errors.New("approval decision must be granted or denied")
	}
	if actor == "" || len(actor) > 128 {
		return ApprovalRequestRecord{}, errors.New("approval actor is invalid")
	}
	record, err := s.GetApprovalRequest(id)
	if err != nil {
		return ApprovalRequestRecord{}, err
	}
	if record.Status != "pending" {
		return ApprovalRequestRecord{}, ErrApprovalAlreadyDecided
	}
	if _, expired := parseTimestamp(record.ExpiresAt); expired {
		if _, err := s.db.Exec(
			"UPDATE mcp_approval_requests SET status = 'expired' WHERE id = ? AND status = 'pending'", id,
		); err != nil {
			return ApprovalRequestRecord{}, fmt.Errorf("expire approval request: %w", err)
		}
		return ApprovalRequestRecord{}, ErrApprovalExpired
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	result, err := s.db.Exec(`
		UPDATE mcp_approval_requests
		SET status = ?, decision = ?, decided_by = ?, decided_at = ?
		WHERE id = ? AND status = 'pending'`, decision, decision, actor, now, id)
	if err != nil {
		return ApprovalRequestRecord{}, fmt.Errorf("decide approval request: %w", err)
	}
	affected, err := result.RowsAffected()
	if err != nil || affected == 0 {
		return ApprovalRequestRecord{}, ErrApprovalAlreadyDecided
	}
	return s.GetApprovalRequest(id)
}

// GrantedApprovalFingerprints returns active grants for task execution.
func (s *Store) GrantedApprovalFingerprints() ([]string, error) {
	rows, err := s.db.Query(`
		SELECT fingerprint FROM mcp_approval_requests
		WHERE status = 'granted' AND expires_at > ? ORDER BY decided_at, id`,
		time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil {
		return nil, fmt.Errorf("list granted approvals: %w", err)
	}
	defer rows.Close()
	fingerprints := make([]string, 0)
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			return nil, fmt.Errorf("scan granted approval: %w", err)
		}
		fingerprints = append(fingerprints, value)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate granted approvals: %w", err)
	}
	return fingerprints, nil
}

// ListApprovalRequests returns the newest durable approval records.
func (s *Store) ListApprovalRequests() ([]ApprovalRequestRecord, error) {
	rows, err := s.db.Query(
		"SELECT " + approvalColumns() + " FROM mcp_approval_requests ORDER BY requested_at DESC, id DESC LIMIT 200")
	if err != nil {
		return nil, fmt.Errorf("list approval requests: %w", err)
	}
	defer rows.Close()
	records := make([]ApprovalRequestRecord, 0)
	for rows.Next() {
		record, err := scanApproval(rows)
		if err != nil {
			return nil, err
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate approval requests: %w", err)
	}
	return records, nil
}

func validFingerprint(value string) bool {
	if len(value) != 64 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func parseTimestamp(value string) (time.Time, bool) {
	parsed, err := time.Parse(time.RFC3339Nano, value)
	return parsed, err == nil && !parsed.After(time.Now().UTC())
}

func boolToInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func jsonRawToValue(raw json.RawMessage) any {
	if len(raw) == 0 || !json.Valid(raw) {
		return map[string]any{}
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return map[string]any{}
	}
	return value
}

func sha256Sum(data []byte) [32]byte { return sha256.Sum256(data) }

func getenvCRANE(key string) string { return os.Getenv(key) }

func sortStrings(values []string) { sort.Strings(values) }

func isLoopbackHost(host string) bool {
	host = strings.Trim(host, "[]")
	if strings.EqualFold(host, "localhost") {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}
