package persistence

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

func TestDBPathMigratesLegacyDatabaseOnce(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)
	migrateLegacyOnce = sync.Once{}
	t.Cleanup(func() { migrateLegacyOnce = sync.Once{} })

	legacy := filepath.Join(root, "CRANE", "crane.db")
	if err := os.MkdirAll(filepath.Dir(legacy), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(legacy, []byte("legacy database"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(legacy+"-wal", []byte("legacy wal"), 0600); err != nil {
		t.Fatal(err)
	}

	got := DBPath()
	want := filepath.Join(root, "CRANE", "data", "crane.db")
	if got != want {
		t.Fatalf("DBPath() = %q, want %q", got, want)
	}
	if _, err := os.Stat(legacy); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("legacy database remained after migration: %v", err)
	}
	if _, err := os.Stat(want); err != nil {
		t.Fatalf("migrated database is missing: %v", err)
	}
	if _, err := os.Stat(want + "-wal"); err != nil {
		t.Fatalf("migrated WAL is missing: %v", err)
	}
}

func TestTaskRecordExposesExactPendingApprovalScope(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.CreateTask("approval-scope-task", "waiting_approval", "test"); err != nil {
		t.Fatal(err)
	}
	checkpoint := []byte(`{"execution_status":"waiting_for_approval","generation":{"pending_approval":{"scope":"generation:apply:approval-scope-task:abc123","reason":"Approve exact changes"}}}`)
	if err := store.SaveTaskState("approval-scope-task", checkpoint); err != nil {
		t.Fatal(err)
	}
	record, err := store.GetTaskRecord("approval-scope-task")
	if err != nil {
		t.Fatal(err)
	}
	if record.PendingApprovalScope != "generation:apply:approval-scope-task:abc123" {
		t.Fatalf("PendingApprovalScope = %q", record.PendingApprovalScope)
	}
	if record.PendingApprovalReason != "Approve exact changes" {
		t.Fatalf("PendingApprovalReason = %q", record.PendingApprovalReason)
	}
}

func newTestStore(t *testing.T) *Store {
	t.Helper()
	store, err := NewStore(t.TempDir() + "/nested/crane.db")
	if err != nil {
		t.Fatalf("NewStore() error = %v", err)
	}
	t.Cleanup(func() { _ = store.Close() })
	return store
}

func TestTaskCRUDAndStatusTimestamps(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.CreateTask("task-1", "queued", "do something"); err != nil {
		t.Fatalf("CreateTask() error = %v", err)
	}
	task, err := store.GetTask("task-1")
	if err != nil {
		t.Fatalf("GetTask() error = %v", err)
	}
	if task["id"] != "task-1" || task["status"] != "queued" || task["input"] != "do something" {
		t.Fatalf("unexpected task: %#v", task)
	}
	if err := store.UpdateTaskStatus("task-1", "starting"); err != nil {
		t.Fatalf("UpdateTaskStatus(starting) error = %v", err)
	}
	if err := store.UpdateTaskStatus("task-1", "running"); err != nil {
		t.Fatalf("UpdateTaskStatus(running) error = %v", err)
	}
	if err := store.UpdateTaskStatus("task-1", "succeeded"); err != nil {
		t.Fatalf("UpdateTaskStatus(succeeded) error = %v", err)
	}
	task, err = store.GetTask("task-1")
	if err != nil || task["status"] != "succeeded" {
		t.Fatalf("final task = %#v, error = %v", task, err)
	}
	if err := store.UpdateTaskStatus("missing", "running"); err == nil {
		t.Fatal("expected missing task update to fail")
	}
}

func TestCheckpointVersionsAreMonotonic(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.CreateTask("task-checkpoint", "queued", "checkpoint test"); err != nil {
		t.Fatal(err)
	}
	for _, snapshot := range []string{`{"state":"queued"}`, `{"state":"running"}`, `{"state":"succeeded"}`} {
		if err := store.SaveTaskState("task-checkpoint", []byte(snapshot)); err != nil {
			t.Fatalf("SaveTaskState() error = %v", err)
		}
	}
	got, ok := store.GetLatestTaskState("task-checkpoint")
	if !ok || string(got) != `{"state":"succeeded"}` {
		t.Fatalf("latest checkpoint = %q, ok = %v", got, ok)
	}
	if err := store.SaveTaskState("first-checkpoint-task", []byte(`{"state":"running"}`)); err != nil {
		t.Fatalf("first checkpoint should create its task row: %v", err)
	}
	if _, ok := store.GetLatestTaskState("first-checkpoint-task"); !ok {
		t.Fatal("expected checkpoint to be persisted for a task created by first snapshot")
	}
}

func TestListTasks(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.CreateTask("task-list", "queued", "input"); err != nil {
		t.Fatal(err)
	}
	tasks, err := store.ListTasks()
	if err != nil {
		t.Fatalf("ListTasks() error = %v", err)
	}
	if len(tasks) != 1 || tasks[0][0] != "task-list" {
		t.Fatalf("unexpected tasks: %#v", tasks)
	}
}

func TestTaskRecordsForAPI(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.CreateTask("api-task", "queued", "private input"); err != nil {
		t.Fatal(err)
	}
	if err := store.UpdateTaskStatus("api-task", "running"); err != nil {
		t.Fatal(err)
	}
	if err := store.SaveTaskState("api-task", []byte(`{"state":"running"}`)); err != nil {
		t.Fatal(err)
	}

	records, err := store.ListTaskRecords()
	if err != nil {
		t.Fatalf("ListTaskRecords() error = %v", err)
	}
	if len(records) != 1 || records[0].ID != "api-task" || records[0].Status != "running" {
		t.Fatalf("unexpected records: %#v", records)
	}
	if !records[0].HasCheckpoint || records[0].UpdatedAt == "" {
		t.Fatalf("checkpoint metadata missing: %#v", records[0])
	}

	record, err := store.GetTaskRecord("api-task")
	if err != nil {
		t.Fatalf("GetTaskRecord() error = %v", err)
	}
	if record.ID != "api-task" || record.CreatedAt == "" || record.UpdatedAt == "" {
		t.Fatalf("unexpected task detail: %#v", record)
	}
	if _, err := store.GetTaskRecord("missing"); !errors.Is(err, ErrTaskNotFound) {
		t.Fatalf("GetTaskRecord(missing) error = %v, want ErrTaskNotFound", err)
	}
}

func TestTaskEventsArePerTaskSequenced(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.CreateTask("seq-task", "queued", "input"); err != nil {
		t.Fatal(err)
	}
	seq1, err := store.AppendTaskEvent("seq-task", "a", map[string]any{"n": 1})
	if err != nil {
		t.Fatal(err)
	}
	seq2, err := store.AppendTaskEvent("seq-task", "b", map[string]any{"n": 2})
	if err != nil {
		t.Fatal(err)
	}
	if seq1 != 1 || seq2 != 2 {
		t.Fatalf("expected per-task seq 1,2 got %d,%d", seq1, seq2)
	}
	events, err := store.GetTaskEventsAfter("seq-task", 1, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].Seq != 2 || events[0].Type != "b" {
		t.Fatalf("unexpected events: %#v", events)
	}
	for i := 0; i < maxEventsPerTask+5; i++ {
		if _, err := store.AppendTaskEvent("seq-task", "x", map[string]any{"i": i}); err != nil {
			t.Fatal(err)
		}
	}
	_, err = store.GetTaskEventsAfter("seq-task", 1, 10)
	if !errors.Is(err, ErrEventCursorExpired) {
		t.Fatalf("expected ErrEventCursorExpired, got %v", err)
	}
	if _, err := store.GetTaskEventsAfter("seq-task", 0, 10); !errors.Is(err, ErrEventCursorExpired) {
		t.Fatalf("expected cursor=0 to be expired after history trim, got %v", err)
	}
	if _, err := store.GetTaskEventsAfter("seq-task", int64(maxEventsPerTask+100), 10); !errors.Is(err, ErrEventCursorExpired) {
		t.Fatalf("expected future cursor to be rejected, got %v", err)
	}
}
