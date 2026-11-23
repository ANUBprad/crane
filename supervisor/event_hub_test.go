package supervisor

import (
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
