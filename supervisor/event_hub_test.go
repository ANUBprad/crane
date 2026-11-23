package supervisor

import (
	"path/filepath"
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
