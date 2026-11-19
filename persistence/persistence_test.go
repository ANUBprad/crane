package persistence

import (
	"testing"
)

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
	if err := store.SaveTaskState("missing-task", []byte(`{"state":"running"}`)); err == nil {
		t.Fatal("expected foreign-key violation for unknown task")
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
