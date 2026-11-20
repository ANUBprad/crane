package persistence

import (
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
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

const maxTaskIDLength = 200

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
	case "queued", "running", "waiting_approval":
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
