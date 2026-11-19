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

// dataRoot returns the per-user CRANE data directory.
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

// DBPath returns the path to the CRANE database file.
func DBPath() string {
	return filepath.Join(dataRoot(), "crane.db")
}

// Store is the concrete SQLite implementation of the persistence layer.
type Store struct {
	db *sql.DB
}

// NewStore creates and initializes a new SQLite store.
func NewStore(path string) (*Store, error) {
	if err := os.MkdirAll(dataRoot(), 0755); err != nil {
		return nil, fmt.Errorf("failed to create CRANE data directory: %w", err)
	}

	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, fmt.Errorf("failed to open SQLite database: %w", err)
	}

	db.SetMaxOpenConns(1)
	db.SetMaxIdleConns(0)
	db.SetConnMaxLifetime(0)

	_, _ = db.Exec("PRAGMA journal_mode=WAL")
	_, _ = db.Exec("PRAGMA busy_timeout=5000")
	_, _ = db.Exec("PRAGMA foreign_keys=ON")

	createTables := `
	CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL);
	CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, created_at TEXT, started_at TEXT, completed_at TEXT, status TEXT NOT NULL, input TEXT, result TEXT, error TEXT, workflow_ref TEXT, checkpoint TEXT, policy_state TEXT, artifact_ref TEXT, runtime TEXT, schema_version TEXT NOT NULL DEFAULT '001');
	CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, timestamp TEXT NOT NULL, type TEXT NOT NULL, payload TEXT, FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE);
	CREATE TABLE IF NOT EXISTS checkpoints (task_id TEXT NOT NULL, version INTEGER NOT NULL, timestamp TEXT NOT NULL, snapshot TEXT, PRIMARY KEY (task_id, version), FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE);
	CREATE TABLE IF NOT EXISTS approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, action TEXT NOT NULL, granted BOOLEAN NOT NULL, granted_at TEXT, grantee TEXT, FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE);
	`
	_, dbErr := db.Exec(createTables)
	if dbErr != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %w", dbErr)
	}

	var count int
	_ = db.QueryRow("SELECT COUNT(*) FROM schema_migrations").Scan(&count)
	if count == 0 {
		_, _ = db.Exec("INSERT INTO schema_migrations (version, description, applied_at) VALUES (1, 'initial', datetime('now'))")
	}

	if pErr := db.Ping(); pErr != nil {
		db.Close()
		return nil, fmt.Errorf("database ping failed: %w", pErr)
	}

	return &Store{db: db}, nil
}

// HealthCheck validates the database is accessible.
func (s *Store) HealthCheck() error {
	if s.db == nil {
		return errors.New("store not initialized")
	}
	return s.db.Ping()
}

// Close closes the database connection.
func (s *Store) Close() error {
	if s.db == nil {
		return errors.New("store not initialized")
	}
	return s.db.Close()
}

// CreateTask inserts a new task and returns its ID.
func (s *Store) CreateTask(id, status, input string) (string, error) {
	now := time.Now().UTC().Format("2006-01-02T15:04:05Z")
	result, err := s.db.Exec("INSERT INTO tasks (id, created_at, status, input) VALUES (?, ?, ?, ?)", id, now, status, input)
	if err != nil {
		return "", fmt.Errorf("failed to create task: %w", err)
	}
	lt, _ := result.LastInsertId()
	return fmt.Sprintf("%d", lt), nil
}

// GetTask retrieves a task by ID.
func (s *Store) GetTask(id string) (map[string]string, error) {
	row := s.db.QueryRow("SELECT id, created_at, status, input, result, error FROM tasks WHERE id = ?")
	var rId, rCreatedAt, rStatus, rInput, rResult, rError string
	err := row.Scan(&rId, &rCreatedAt, &rStatus, &rInput, &rResult, &rError)
	if err != nil {
		return nil, fmt.Errorf("task %q not found: %w", id, err)
	}
	return map[string]string{
		"id":       rId,
		"created_at": rCreatedAt,
		"status":   rStatus,
		"input":    rInput,
		"result":   rResult,
		"error":    rError,
	}, nil
}

// ListTasks returns all tasks.
func (s *Store) ListTasks() ([][]string, error) {
	rows, err := s.db.Query("SELECT id, created_at, status, input FROM tasks")
	if err != nil {
		return nil, fmt.Errorf("failed to list tasks: %w", err)
	}
	defer rows.Close()

	var results [][]string
	for rows.Next() {
		var id, createdAt, status, input string
		if err := rows.Scan(&id, &createdAt, &status, &input); err != nil {
			return nil, fmt.Errorf("failed to scan task: %w", err)
		}
		results = append(results, []string{id, createdAt, status, input})
	}
	return results, nil
}

// UpdateTaskStatus updates a task's status.
func (s *Store) UpdateTaskStatus(id, status string) error {
	now := time.Now().UTC().Format("2006-01-02T15:04:05Z")
	_, err := s.db.Exec("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", status, now, id)
	return err
}

// MigrationVersion returns the current migration version.
func (s *Store) MigrationVersion() (int, error) {
	var version int
	_ = s.db.QueryRow("SELECT MAX(version) FROM schema_migrations").Scan(&version)
	return version, nil
}

// SetMigrationVersion records a migration version.
func (s *Store) SetMigrationVersion(version int) error {
	_, err := s.db.Exec("INSERT OR REPLACE INTO schema_migrations (version, description, applied_at) VALUES (?, 'migration', datetime('now'))", version)
	return err
}