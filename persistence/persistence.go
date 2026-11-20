package persistence

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

func dataRoot() string {
	if runtime.GOOS == "windows" {
		if p := os.Getenv("LOCALAPPDATA"); p != "" {
			return filepath.Join(p, "CRANE")
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".crane"
	}
	return filepath.Join(home, ".crane")
}

// DBPath returns the default per-user CRANE database path.
func DBPath() string {
	return filepath.Join(dataRoot(), "crane.db")
}

// ErrTaskNotFound is returned when a task ID has no persisted record.
var ErrTaskNotFound = errors.New("task not found")

// ErrDuplicateTask is returned when admissions reuse a stable task identifier.
var ErrDuplicateTask = errors.New("task already exists")

const (
	maxTaskIDLength  = 200
	maxEventsPerTask = 1000
)

// mcpSchema is applied as migration 2 to existing and fresh databases.
const mcpSchema = `
CREATE TABLE IF NOT EXISTS mcp_servers (
	id TEXT PRIMARY KEY,
	name TEXT NOT NULL,
	transport TEXT NOT NULL,
	command TEXT NOT NULL DEFAULT '',
	args TEXT NOT NULL DEFAULT '[]',
	env TEXT NOT NULL DEFAULT '{}',
	url TEXT NOT NULL DEFAULT '',
	enabled INTEGER NOT NULL DEFAULT 1,
	connect_timeout_seconds REAL NOT NULL DEFAULT 10,
	invocation_timeout_seconds REAL NOT NULL DEFAULT 30,
	allow_remote_endpoint INTEGER NOT NULL DEFAULT 0,
	status TEXT NOT NULL DEFAULT 'registered',
	last_error TEXT NOT NULL DEFAULT '',
	created_at TEXT NOT NULL,
	updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_tools (
	server_id TEXT NOT NULL,
	tool_name TEXT NOT NULL,
	description TEXT NOT NULL DEFAULT '',
	input_schema TEXT NOT NULL DEFAULT '{}',
	annotations TEXT NOT NULL DEFAULT '{}',
	policy_class TEXT NOT NULL DEFAULT 'approval_required',
	discovered_at TEXT NOT NULL,
	PRIMARY KEY (server_id, tool_name),
	FOREIGN KEY (server_id) REFERENCES mcp_servers(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS mcp_invocations (
	id TEXT PRIMARY KEY,
	task_id TEXT NOT NULL DEFAULT '',
	server_id TEXT NOT NULL,
	server_name TEXT NOT NULL DEFAULT '',
	tool_name TEXT NOT NULL,
	arguments TEXT NOT NULL DEFAULT '{}',
	status TEXT NOT NULL,
	result TEXT NOT NULL DEFAULT '{}',
	error TEXT NOT NULL DEFAULT '',
	policy_decision TEXT NOT NULL DEFAULT '',
	duration_ms INTEGER NOT NULL DEFAULT 0,
	created_at TEXT NOT NULL,
	completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcp_invocations_task ON mcp_invocations(task_id, created_at DESC);
CREATE TABLE IF NOT EXISTS mcp_approval_requests (
	id TEXT PRIMARY KEY,
	task_id TEXT NOT NULL DEFAULT '',
	server_id TEXT NOT NULL,
	tool_name TEXT NOT NULL,
	arguments TEXT NOT NULL DEFAULT '{}',
	fingerprint TEXT NOT NULL UNIQUE,
	status TEXT NOT NULL DEFAULT 'pending',
	requested_at TEXT NOT NULL,
	expires_at TEXT NOT NULL,
	decided_at TEXT NOT NULL DEFAULT '',
	decided_by TEXT NOT NULL DEFAULT '',
	decision TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcp_approvals_status ON mcp_approval_requests(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_mcp_approvals_task ON mcp_approval_requests(task_id, status);
`

// ValidTaskID restricts task IDs to a safe, portable character set shared by
// the CLI, persistence boundary and local API paths.
func ValidTaskID(id string) bool {
	if id == "" || len(id) > maxTaskIDLength {
		return false
	}
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
		case r == '-', r == '_', r == '.', r == ':':
		default:
			return false
		}
	}
	return true
}

// Store is the SQLite persistence implementation.
type Store struct {
	db *sql.DB
}

// TaskRecord is the API-safe view of a persisted task. Raw task input is
// deliberately omitted.
type TaskRecord struct {
	ID            string `json:"id"`
	CreatedAt     string `json:"created_at"`
	UpdatedAt     string `json:"updated_at"`
	StartedAt     string `json:"started_at,omitempty"`
	CompletedAt   string `json:"completed_at,omitempty"`
	Status        string `json:"status"`
	Result        string `json:"result,omitempty"`
	Error         string `json:"error,omitempty"`
	WorkflowRef   string `json:"workflow_ref,omitempty"`
	HasCheckpoint bool   `json:"has_checkpoint"`
	SchemaVersion string `json:"schema_version,omitempty"`
}

// TaskEvent is an API-safe, persisted lifecycle or workflow event.
type TaskEvent struct {
	ID        int64           `json:"id"`
	TaskID    string          `json:"task_id"`
	Timestamp string          `json:"timestamp"`
	Type      string          `json:"type"`
	Payload   json.RawMessage `json:"payload"`
}

// NewStore opens the database at path and initializes the schema.
func NewStore(path string) (*Store, error) {
	if path == "" {
		return nil, errors.New("database path must not be empty")
	}
	if path != ":memory:" && path != "file::memory:?cache=shared" {
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			return nil, fmt.Errorf("create database directory: %w", err)
		}
	}

	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("open SQLite database: %w", err)
	}
	// Keep one connection alive: SQLite PRAGMAs such as foreign_keys and
	// busy_timeout are connection-scoped.
	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(1)
	db.SetConnMaxLifetime(0)

	fail := func(err error) (*Store, error) {
		_ = db.Close()
		return nil, err
	}
	for _, pragma := range []string{
		"PRAGMA busy_timeout=5000",
		"PRAGMA foreign_keys=ON",
	} {
		if _, err := db.Exec(pragma); err != nil {
			return fail(fmt.Errorf("configure SQLite (%s): %w", pragma, err))
		}
	}
	if path != ":memory:" && path != "file::memory:?cache=shared" {
		if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
			return fail(fmt.Errorf("enable SQLite WAL: %w", err))
		}
	}

	const schema = `
	CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY,
		description TEXT NOT NULL,
		applied_at TEXT NOT NULL
	);
	CREATE TABLE IF NOT EXISTS tasks (
		id TEXT PRIMARY KEY,
		created_at TEXT,
		started_at TEXT,
		completed_at TEXT,
		status TEXT NOT NULL,
		input TEXT,
		result TEXT,
		error TEXT,
		workflow_ref TEXT,
		checkpoint TEXT,
		policy_state TEXT,
		artifact_ref TEXT,
		runtime TEXT,
		schema_version TEXT NOT NULL DEFAULT '001'
	);
	CREATE TABLE IF NOT EXISTS task_events (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		task_id TEXT NOT NULL,
		timestamp TEXT NOT NULL,
		type TEXT NOT NULL,
		payload TEXT,
		FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
	);
	CREATE TABLE IF NOT EXISTS checkpoints (
		task_id TEXT NOT NULL,
		version INTEGER NOT NULL,
		timestamp TEXT NOT NULL,
		snapshot TEXT,
		PRIMARY KEY (task_id, version),
		FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
	);
	CREATE TABLE IF NOT EXISTS approvals (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		task_id TEXT NOT NULL,
		action TEXT NOT NULL,
		granted BOOLEAN NOT NULL,
		granted_at TEXT,
		grantee TEXT,
		FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
	);
	CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
	CREATE INDEX IF NOT EXISTS idx_checkpoints_task_version ON checkpoints(task_id, version DESC);
	`
	if _, err := db.Exec(schema); err != nil {
		return fail(fmt.Errorf("initialize SQLite schema: %w", err))
	}
	if _, err := db.Exec(
		"INSERT OR IGNORE INTO schema_migrations (version, description, applied_at) VALUES (1, 'initial', ?)",
		time.Now().UTC().Format(time.RFC3339Nano),
	); err != nil {
		return fail(fmt.Errorf("record initial schema migration: %w", err))
	}
	version, err := (&Store{db: db}).MigrationVersion()
	if err != nil {
		return fail(err)
	}
	if version < 2 {
		if _, err := db.Exec(mcpSchema); err != nil {
			return fail(fmt.Errorf("apply MCP schema migration: %w", err))
		}
		if _, err := db.Exec(
			"INSERT OR REPLACE INTO schema_migrations (version, description, applied_at) VALUES (2, 'mcp lifecycle and approvals', ?)",
			time.Now().UTC().Format(time.RFC3339Nano),
		); err != nil {
			return fail(fmt.Errorf("record MCP schema migration: %w", err))
		}
	}
	if err := db.Ping(); err != nil {
		return fail(fmt.Errorf("SQLite ping: %w", err))
	}
	return &Store{db: db}, nil
}

func (s *Store) HealthCheck() error {
	if s == nil || s.db == nil {
		return errors.New("store not initialized")
	}
	return s.db.Ping()
}

func (s *Store) Close() error {
	if s == nil || s.db == nil {
		return errors.New("store not initialized")
	}
	return s.db.Close()
}

// CreateTask creates a task with its caller-supplied stable ID.
func (s *Store) CreateTask(id, status, input string) (string, error) {
	if id == "" {
		return "", errors.New("task ID must not be empty")
	}
	if status == "" {
		status = "queued"
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.Exec(
		"INSERT INTO tasks (id, created_at, status, input) VALUES (?, ?, ?, ?)",
		id, now, status, input,
	)
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "unique constraint") {
			return "", ErrDuplicateTask
		}
		return "", fmt.Errorf("create task %q: %w", id, err)
	}
	return id, nil
}

// GetTask retrieves a task by ID, normalizing nullable fields to empty strings.
func (s *Store) GetTask(id string) (map[string]string, error) {
	var createdAt, status, input, result, taskErr sql.NullString
	err := s.db.QueryRow(
		"SELECT created_at, status, input, result, error FROM tasks WHERE id = ?", id,
	).Scan(&createdAt, &status, &input, &result, &taskErr)
	if err != nil {
		return nil, fmt.Errorf("get task %q: %w", id, err)
	}
	return map[string]string{
		"id": id, "created_at": createdAt.String, "status": status.String,
		"input": input.String, "result": result.String, "error": taskErr.String,
	}, nil
}

// ListTasks returns task ID, creation time, status and input for each task.
func (s *Store) ListTasks() ([][]string, error) {
	rows, err := s.db.Query("SELECT id, COALESCE(created_at, ''), status, COALESCE(input, '') FROM tasks ORDER BY created_at DESC, id")
	if err != nil {
		return nil, fmt.Errorf("list tasks: %w", err)
	}
	defer rows.Close()
	var tasks [][]string
	for rows.Next() {
		var id, createdAt, status, input string
		if err := rows.Scan(&id, &createdAt, &status, &input); err != nil {
			return nil, fmt.Errorf("scan task: %w", err)
		}
		tasks = append(tasks, []string{id, createdAt, status, input})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate tasks: %w", err)
	}
	return tasks, nil
}

// GetTaskRecord retrieves the API-safe detail view of one persisted task.
func (s *Store) GetTaskRecord(id string) (TaskRecord, error) {
	var record TaskRecord
	var checkpointAt string
	err := s.db.QueryRow(`
		SELECT t.id,
		       COALESCE(t.created_at, ''),
		       COALESCE(t.started_at, ''),
		       COALESCE(t.completed_at, ''),
		       COALESCE(t.status, ''),
		       COALESCE(t.result, ''),
		       COALESCE(t.error, ''),
		       COALESCE(t.workflow_ref, ''),
		       COALESCE(t.schema_version, ''),
		       COALESCE((SELECT MAX(timestamp) FROM checkpoints WHERE task_id = t.id), '')
		FROM tasks t WHERE t.id = ?`, id,
	).Scan(
		&record.ID, &record.CreatedAt, &record.StartedAt, &record.CompletedAt,
		&record.Status, &record.Result, &record.Error, &record.WorkflowRef,
		&record.SchemaVersion, &checkpointAt,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return TaskRecord{}, ErrTaskNotFound
	}
	if err != nil {
		return TaskRecord{}, fmt.Errorf("get task record %q: %w", id, err)
	}
	record.HasCheckpoint = checkpointAt != ""
	record.UpdatedAt = latestTimestamp(record.CreatedAt, record.StartedAt, record.CompletedAt, checkpointAt)
	return record, nil
}

// ListTaskRecords returns API-safe task summaries, newest first.
func (s *Store) ListTaskRecords() ([]TaskRecord, error) {
	rows, err := s.db.Query(`
		SELECT t.id,
		       COALESCE(t.created_at, ''),
		       COALESCE(t.started_at, ''),
		       COALESCE(t.completed_at, ''),
		       COALESCE(t.status, ''),
		       COALESCE(t.workflow_ref, ''),
		       COALESCE(t.schema_version, ''),
		       COALESCE((SELECT MAX(timestamp) FROM checkpoints WHERE task_id = t.id), '')
		FROM tasks t
		ORDER BY COALESCE(t.created_at, '') DESC, t.id`)
	if err != nil {
		return nil, fmt.Errorf("list task records: %w", err)
	}
	defer rows.Close()

	records := make([]TaskRecord, 0)
	for rows.Next() {
		var record TaskRecord
		var checkpointAt string
		if err := rows.Scan(
			&record.ID, &record.CreatedAt, &record.StartedAt, &record.CompletedAt,
			&record.Status, &record.WorkflowRef, &record.SchemaVersion, &checkpointAt,
		); err != nil {
			return nil, fmt.Errorf("scan task record: %w", err)
		}
		record.HasCheckpoint = checkpointAt != ""
		record.UpdatedAt = latestTimestamp(record.CreatedAt, record.StartedAt, record.CompletedAt, checkpointAt)
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate task records: %w", err)
	}
	return records, nil
}

func latestTimestamp(values ...string) string {
	latest := ""
	for _, value := range values {
		if value > latest {
			latest = value
		}
	}
	return latest
}

// UpdateTaskStatus updates a task and sets lifecycle timestamps where applicable.
func (s *Store) UpdateTaskStatus(id, status string) error {
	now := time.Now().UTC().Format(time.RFC3339Nano)
	var query string
	switch status {
	case "starting":
		query = "UPDATE tasks SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?"
	case "succeeded", "failed", "cancelled":
		query = "UPDATE tasks SET status = ?, completed_at = ? WHERE id = ?"
	case "queued", "running", "waiting_approval", "cancelling":
		query = "UPDATE tasks SET status = ? WHERE id = ?"
		if _, err := s.db.Exec(query, status, id); err != nil {
			return fmt.Errorf("update task %q status: %w", id, err)
		}
		return s.requireTask(id)
	default:
		return fmt.Errorf("unsupported task status %q", status)
	}
	if _, err := s.db.Exec(query, status, now, id); err != nil {
		return fmt.Errorf("update task %q status: %w", id, err)
	}
	return s.requireTask(id)
}

func (s *Store) requireTask(id string) error {
	var found int
	if err := s.db.QueryRow("SELECT 1 FROM tasks WHERE id = ?", id).Scan(&found); err != nil {
		return fmt.Errorf("task %q not found: %w", id, err)
	}
	return nil
}

func (s *Store) MigrationVersion() (int, error) {
	var version sql.NullInt64
	if err := s.db.QueryRow("SELECT MAX(version) FROM schema_migrations").Scan(&version); err != nil {
		return 0, fmt.Errorf("read schema migration version: %w", err)
	}
	if !version.Valid {
		return 0, nil
	}
	return int(version.Int64), nil
}

func (s *Store) SetMigrationVersion(version int) error {
	if version < 1 {
		return fmt.Errorf("migration version must be positive")
	}
	_, err := s.db.Exec(
		"INSERT OR REPLACE INTO schema_migrations (version, description, applied_at) VALUES (?, 'migration', ?)",
		version, time.Now().UTC().Format(time.RFC3339Nano),
	)
	return err
}

// SaveTaskState appends a monotonically versioned checkpoint atomically.
func (s *Store) SaveTaskState(taskID string, snapshot []byte) error {
	if taskID == "" {
		return errors.New("task ID must not be empty")
	}
	if len(snapshot) == 0 {
		return errors.New("checkpoint snapshot must not be empty")
	}
	tx, err := s.db.Begin()
	if err != nil {
		return fmt.Errorf("begin checkpoint transaction: %w", err)
	}
	defer tx.Rollback()
	// The first workflow checkpoint can arrive before any separate task-create
	// request. Create a minimal task row in the same transaction so the
	// checkpoint's foreign key is valid and the state is never discarded.
	now := time.Now().UTC().Format(time.RFC3339Nano)
	if _, err := tx.Exec(
		"INSERT OR IGNORE INTO tasks (id, created_at, status, input) VALUES (?, ?, 'queued', '')",
		taskID, now,
	); err != nil {
		return fmt.Errorf("ensure task row for checkpoint %q: %w", taskID, err)
	}
	var version int
	if err := tx.QueryRow(
		"SELECT COALESCE(MAX(version), 0) + 1 FROM checkpoints WHERE task_id = ?", taskID,
	).Scan(&version); err != nil {
		return fmt.Errorf("allocate checkpoint version: %w", err)
	}
	_, err = tx.Exec(
		"INSERT INTO checkpoints (task_id, version, timestamp, snapshot) VALUES (?, ?, ?, ?)",
		taskID, version, time.Now().UTC().Format(time.RFC3339Nano), string(snapshot),
	)
	if err != nil {
		return fmt.Errorf("save checkpoint for task %q: %w", taskID, err)
	}
	return tx.Commit()
}

// GetLatestTaskState retrieves the newest checkpoint for a task.
func (s *Store) GetLatestTaskState(taskID string) ([]byte, bool) {
	var snapshot sql.NullString
	err := s.db.QueryRow(
		"SELECT snapshot FROM checkpoints WHERE task_id = ? ORDER BY version DESC LIMIT 1", taskID,
	).Scan(&snapshot)
	if err != nil || !snapshot.Valid {
		return nil, false
	}
	return []byte(snapshot.String), true
}

// LatestCheckpointVersion returns the newest durable checkpoint version.
func (s *Store) LatestCheckpointVersion(taskID string) (int, bool) {
	var version sql.NullInt64
	err := s.db.QueryRow(
		"SELECT MAX(version) FROM checkpoints WHERE task_id = ?", taskID,
	).Scan(&version)
	if err != nil || !version.Valid {
		return 0, false
	}
	return int(version.Int64), true
}

// AppendTaskEvent durably records one safe lifecycle event and bounds the
// retained event history for each task.
func (s *Store) AppendTaskEvent(taskID, eventType string, payload any) (int64, error) {
	if !ValidTaskID(taskID) {
		return 0, errors.New("invalid task identifier")
	}
	if eventType == "" || strings.ContainsAny(eventType, "\r\n") {
		return 0, errors.New("invalid event type")
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		return 0, fmt.Errorf("encode task event: %w", err)
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	result, err := s.db.Exec(
		"INSERT INTO task_events (task_id, timestamp, type, payload) VALUES (?, ?, ?, ?)",
		taskID, now, eventType, string(encoded),
	)
	if err != nil {
		return 0, fmt.Errorf("append task event: %w", err)
	}
	id, err := result.LastInsertId()
	if err != nil {
		return 0, fmt.Errorf("read task event identifier: %w", err)
	}
	if _, err := s.db.Exec(`
		DELETE FROM task_events
		WHERE task_id = ? AND id NOT IN (
			SELECT id FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?
		)`, taskID, taskID, maxEventsPerTask); err != nil {
		return 0, fmt.Errorf("bound task event history: %w", err)
	}
	return id, nil
}

// GetTaskEventsAfter returns persisted events newer than a cursor.
func (s *Store) GetTaskEventsAfter(taskID string, after int64, limit int) ([]TaskEvent, error) {
	if limit <= 0 || limit > maxEventsPerTask {
		limit = maxEventsPerTask
	}
	rows, err := s.db.Query(`
		SELECT id, task_id, timestamp, type, COALESCE(payload, '{}')
		FROM task_events WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?`,
		taskID, after, limit,
	)
	if err != nil {
		return nil, fmt.Errorf("list task events: %w", err)
	}
	defer rows.Close()
	events := make([]TaskEvent, 0)
	for rows.Next() {
		var event TaskEvent
		var payload string
		if err := rows.Scan(&event.ID, &event.TaskID, &event.Timestamp, &event.Type, &payload); err != nil {
			return nil, fmt.Errorf("scan task event: %w", err)
		}
		event.Payload = json.RawMessage(payload)
		events = append(events, event)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate task events: %w", err)
	}
	return events, nil
}

// TransitionTask atomically changes status when its current status is allowed.
func (s *Store) TransitionTask(id, to string, from ...string) (bool, error) {
	if len(from) == 0 {
		return false, errors.New("transition requires at least one source status")
	}
	placeholders := make([]string, len(from))
	args := make([]any, 0, len(from)+2)
	args = append(args, to, id)
	for i, status := range from {
		placeholders[i] = "?"
		args = append(args, status)
	}
	query := fmt.Sprintf(
		"UPDATE tasks SET status = ? WHERE id = ? AND status IN (%s)",
		strings.Join(placeholders, ","),
	)
	result, err := s.db.Exec(query, args...)
	if err != nil {
		return false, fmt.Errorf("transition task %q: %w", id, err)
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, fmt.Errorf("read transition result: %w", err)
	}
	return affected > 0, nil
}

// CompleteTask persists a terminal status, result and error atomically.
func (s *Store) CompleteTask(id, status, result, taskError string) error {
	switch status {
	case "succeeded", "failed", "cancelled":
	default:
		return fmt.Errorf("unsupported terminal status %q", status)
	}
	_, err := s.db.Exec(
		"UPDATE tasks SET status = ?, result = ?, error = ?, completed_at = ? WHERE id = ?",
		status, nullableString(result), nullableString(taskError),
		time.Now().UTC().Format(time.RFC3339Nano), id,
	)
	if err != nil {
		return fmt.Errorf("complete task %q: %w", id, err)
	}
	return s.requireTask(id)
}

func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

// SetTaskWorkflowRef records the Python runtime identity that executed a task.
func (s *Store) SetTaskWorkflowRef(id, ref string) error {
	_, err := s.db.Exec("UPDATE tasks SET workflow_ref = ? WHERE id = ?", ref, id)
	if err != nil {
		return fmt.Errorf("set workflow reference for task %q: %w", id, err)
	}
	return s.requireTask(id)
}

// ListTasksByStatus returns API-safe records in any of the supplied statuses.
func (s *Store) ListTasksByStatus(statuses ...string) ([]TaskRecord, error) {
	if len(statuses) == 0 {
		return []TaskRecord{}, nil
	}
	placeholders := make([]string, len(statuses))
	args := make([]any, len(statuses))
	for i, status := range statuses {
		placeholders[i] = "?"
		args[i] = status
	}
	rows, err := s.db.Query(fmt.Sprintf(`
		SELECT id, COALESCE(created_at, ''), COALESCE(status, ''), COALESCE(input, '')
		FROM tasks WHERE status IN (%s) ORDER BY created_at ASC, id ASC`, strings.Join(placeholders, ",")), args...)
	if err != nil {
		return nil, fmt.Errorf("list tasks by status: %w", err)
	}
	defer rows.Close()
	records := make([]TaskRecord, 0)
	for rows.Next() {
		var record TaskRecord
		var input string
		if err := rows.Scan(&record.ID, &record.CreatedAt, &record.Status, &input); err != nil {
			return nil, fmt.Errorf("scan recoverable task: %w", err)
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate recoverable tasks: %w", err)
	}
	return records, nil
}
