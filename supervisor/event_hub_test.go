package supervisor

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"crane/persistence"
)

func TestEventHubReplayAndPublishBoundaryDoesNotLoseEvents(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "events.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.CreateTask("hub-replay-task", "queued", "test"); err != nil {
		t.Fatal(err)
	}

	firstSeq, err := store.AppendTaskEvent("hub-replay-task", "step_started", map[string]any{"step_id": "one"})
	if err != nil {
		t.Fatal(err)
	}
	first := persistence.TaskEvent{
		ID: firstSeq, Seq: firstSeq, TaskID: "hub-replay-task", Type: "step_started",
		Payload: []byte(`{"step_id":"one"}`),
	}
	hub := newEventHub(store)

	// Race publication with the replay/register transition. If publication
	// wins, the durable row is replayed; if subscription holds the hub lock,
	// the same sequence can arrive via replay and live delivery. Consumers
	// deduplicate by sequence, but the event must never be lost.
	publishStart := make(chan struct{})
	publishDone := make(chan struct{})
	go func() {
		<-publishStart
		hub.publish(first)
		close(publishDone)
	}()
	close(publishStart)

	events, cancel, err := hub.subscribe("hub-replay-task", 0)
	if err != nil {
		t.Fatal(err)
	}
	defer cancel()
	select {
	case <-publishDone:
	case <-time.After(2 * time.Second):
		t.Fatal("publisher remained blocked after subscription")
	}

	secondSeq, err := store.AppendTaskEvent("hub-replay-task", "step_completed", map[string]any{"step_id": "one"})
	if err != nil {
		t.Fatal(err)
	}
	hub.publish(persistence.TaskEvent{
		ID: secondSeq, Seq: secondSeq, TaskID: "hub-replay-task", Type: "step_completed",
		Payload: []byte(`{"step_id":"one"}`),
	})

	seenFirst, seenSecond := false, false
	lastSeq := int64(0)
	deadline := time.After(2 * time.Second)
	for !seenSecond {
		select {
		case event := <-events:
			if event.Seq < lastSeq {
				t.Fatalf("event sequence went backwards: %d after %d", event.Seq, lastSeq)
			}
			lastSeq = event.Seq
			switch event.Seq {
			case firstSeq:
				seenFirst = true
			case secondSeq:
				seenSecond = true
			default:
				t.Fatalf("unexpected sequence %d", event.Seq)
			}
		case <-deadline:
			t.Fatal("timed out waiting for replayed and live events")
		}
	}
	if !seenFirst || !seenSecond {
		t.Fatalf("missing event: first=%v second=%v", seenFirst, seenSecond)
	}
}

func TestSupervisorPersistedEventsAreDeliveredInSequence(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "ordered.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.CreateTask("ordered-task", "queued", "test"); err != nil {
		t.Fatal(err)
	}
	s := NewWithStore(store)
	events, cancel, err := s.events.subscribe("ordered-task", 0)
	if err != nil {
		t.Fatal(err)
	}
	defer cancel()

	const emitters = 4
	const perEmitter = 25
	total := emitters * perEmitter
	var wg sync.WaitGroup
	for emitter := 0; emitter < emitters; emitter++ {
		wg.Add(1)
		go func(emitter int) {
			defer wg.Done()
			for i := 0; i < perEmitter; i++ {
				s.emitEvent("ordered-task", "step_started", map[string]any{
					"emitter": emitter, "index": i,
				})
			}
		}(emitter)
	}
	wg.Wait()

	seen := make([]int64, 0, total)
	deadline := time.After(5 * time.Second)
	for len(seen) < total {
		select {
		case event := <-events:
			seen = append(seen, event.Seq)
		case <-deadline:
			t.Fatalf("received %d of %d ordered events", len(seen), total)
		}
	}
	for index, seq := range seen {
		if want := int64(index + 1); seq != want {
			t.Fatalf("event %d seq = %d, want %d (full sequence: %v)", index, seq, want, seen)
		}
	}
}

func TestApproveTaskRejectsMismatchedPendingScope(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "scope.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.CreateTask("scope-task", "waiting_approval", "test"); err != nil {
		t.Fatal(err)
	}
	checkpoint := []byte(`{"execution_status":"waiting_for_approval","generation":{"pending_approval":{"scope":"generation:apply:scope-task:abc123","reason":"Approve exact changes"}}}`)
	if err := store.SaveTaskState("scope-task", checkpoint); err != nil {
		t.Fatal(err)
	}
	s := NewWithStore(store)
	s.started.Store(true)
	if _, err := s.ApproveTask("scope-task", true, "user_approved"); err == nil || !strings.Contains(err.Error(), "approval_scope_mismatch") {
		t.Fatalf("ApproveTask() error = %v, want approval_scope_mismatch", err)
	}
	record, err := store.GetTaskRecord("scope-task")
	if err != nil {
		t.Fatal(err)
	}
	if record.Status != "waiting_approval" {
		t.Fatalf("status = %q, want waiting_approval", record.Status)
	}
	after, ok := store.GetLatestTaskState("scope-task")
	if !ok || !strings.Contains(string(after), "generation:apply:scope-task:abc123") || strings.Contains(string(after), "user_approved") {
		t.Fatalf("checkpoint changed after rejected approval: %s", after)
	}
}

func TestApprovalRejectionEmitsTerminalEvent(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "approval.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.CreateTask("approval-reject-task", "waiting_approval", "test"); err != nil {
		t.Fatal(err)
	}
	s := NewWithStore(store)
	s.started.Store(true)
	events, cancel, err := s.events.subscribe("approval-reject-task", 0)
	if err != nil {
		t.Fatal(err)
	}
	defer cancel()

	if _, err := s.ApproveTask("approval-reject-task", false, "generation:apply:test"); err != nil {
		t.Fatal(err)
	}

	seenRejection := false
	seenTerminal := false
	deadline := time.After(5 * time.Second)
	for !seenTerminal {
		select {
		case event := <-events:
			switch event.Type {
			case "approval_rejected":
				seenRejection = true
			case "task_finished":
				seenTerminal = true
				if !strings.Contains(string(event.Payload), `"status":"failed"`) {
					t.Fatalf("terminal event payload = %s", event.Payload)
				}
			}
		case <-deadline:
			t.Fatalf("missing terminal rejection event: rejection=%v terminal=%v", seenRejection, seenTerminal)
		}
	}
	if !seenRejection {
		t.Fatal("approval_rejected event was not emitted")
	}
}

func TestApprovalRejectionClearsPendingCheckpoint(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "approval-checkpoint.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	const id = "approval-clear-task"
	if _, err := store.CreateTask(id, "waiting_approval", "test"); err != nil {
		t.Fatal(err)
	}
	checkpoint := []byte(`{
		"state":"failed",
		"execution_status":"waiting_for_approval",
		"generation":{"enabled":true,"status":"planned","pending_approval":{"scope":"generation:apply:approval-clear-task:abc123","reason":"Apply generated files"}},
		"pending_approval_scope":"legacy:approval-clear-task:abc123",
		"pending_approval_reason":"legacy reason",
		"plan":[{"step_id":"generate_files","status":"waiting_approval"}],
		"resumable":true
	}`)
	if err := store.SaveTaskState(id, checkpoint); err != nil {
		t.Fatal(err)
	}
	s := NewWithStore(store)
	s.started.Store(true)

	record, err := s.ApproveTask(id, false, "generation:apply:approval-clear-task:abc123")
	if err != nil {
		t.Fatal(err)
	}
	if record.Status != "failed" {
		t.Fatalf("status = %q, want failed", record.Status)
	}
	if record.PendingApprovalScope != "" || record.PendingApprovalReason != "" {
		t.Fatalf("record still exposes approval scope %q and reason %q", record.PendingApprovalScope, record.PendingApprovalReason)
	}

	after, ok := store.GetLatestTaskState(id)
	if !ok {
		t.Fatal("terminal checkpoint was not persisted")
	}
	var decoded map[string]any
	if err := json.Unmarshal(after, &decoded); err != nil {
		t.Fatalf("terminal checkpoint is invalid JSON: %v", err)
	}
	if got, _ := decoded["execution_status"].(string); got != "failed" {
		t.Fatalf("checkpoint execution_status = %q, want failed", got)
	}
	if got, _ := decoded["state"].(string); got != "failed" {
		t.Fatalf("checkpoint state = %q, want failed", got)
	}
	generation, _ := decoded["generation"].(map[string]any)
	if _, exists := generation["pending_approval"]; exists {
		t.Fatalf("terminal checkpoint retained generation.pending_approval: %s", after)
	}
	if _, exists := decoded["pending_approval_scope"]; exists {
		t.Fatalf("terminal checkpoint retained pending_approval_scope: %s", after)
	}
	steps, _ := decoded["plan"].([]any)
	if len(steps) != 1 {
		t.Fatalf("terminal checkpoint plan length = %d, want 1", len(steps))
	}
	step, _ := steps[0].(map[string]any)
	if got, _ := step["status"].(string); got != "failed" {
		t.Fatalf("waiting plan step status = %q, want failed", got)
	}
	if _, err := s.ApproveTask(id, true, "generation:apply:approval-clear-task:abc123"); err == nil {
		t.Fatal("approval after rejection unexpectedly succeeded")
	}
}
