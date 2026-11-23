package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"crane/internal/localclient"
	"crane/persistence"
)

func TestTaskStreamRendererDeduplicatesAndSeparatesTextChannels(t *testing.T) {
	var stdout, stderr bytes.Buffer
	renderer := newTaskStreamRenderer(&stdout, &stderr, outputText)
	progress := localclient.TaskEvent{
		TaskID: "task-render", Seq: 1, Type: "step_started",
		Payload: json.RawMessage(`{"step_id":"build"}`),
	}
	renderer.HandleEvent(progress)
	renderer.HandleEvent(progress) // replay duplicate
	renderer.HandleEvent(localclient.TaskEvent{
		TaskID: "task-render", Type: "assistant.delta",
		Payload: json.RawMessage(`{"delta":"hello "}`),
	})
	renderer.HandleEvent(localclient.TaskEvent{
		TaskID: "task-render", Type: "assistant.delta",
		Payload: json.RawMessage(`{"delta":"world"}`),
	})
	if got := stdout.String(); got != "hello world" {
		t.Fatalf("stdout = %q, want streamed assistant text only", got)
	}
	if got := stderr.String(); strings.Count(got, "running step: build") != 1 {
		t.Fatalf("stderr should contain one progress event, got %q", got)
	}
	if renderer.LastSeq() != 1 {
		t.Fatalf("last sequence = %d, want 1", renderer.LastSeq())
	}
	if strings.Contains(stdout.String()+stderr.String(), "\x1b[") {
		t.Fatal("non-TTY renderer emitted terminal control sequences")
	}
}

func TestTaskStreamRendererDropsOutOfOrderEventsAtResumeCursor(t *testing.T) {
	var stdout, stderr bytes.Buffer
	renderer := newTaskStreamRenderer(&stdout, &stderr, outputText)
	renderer.lastSeq = 4
	renderer.HandleEvent(localclient.TaskEvent{
		TaskID: "task-render", Seq: 3, Type: "step_started",
		Payload: json.RawMessage(`{"step_id":"old"}`),
	})
	renderer.HandleEvent(localclient.TaskEvent{
		TaskID: "task-render", Seq: 5, Type: "step_started",
		Payload: json.RawMessage(`{"step_id":"new"}`),
	})
	if strings.Contains(stderr.String(), "old") || !strings.Contains(stderr.String(), "new") {
		t.Fatalf("unexpected rendering after resume cursor: %q", stderr.String())
	}
	if renderer.LastSeq() != 5 {
		t.Fatalf("last sequence = %d, want 5", renderer.LastSeq())
	}
}

func TestTaskStreamRendererUsesDurableSnapshotAfterReconnect(t *testing.T) {
	var stdout, stderr bytes.Buffer
	renderer := newTaskStreamRenderer(&stdout, &stderr, outputText)
	renderer.HandleEvent(localclient.TaskEvent{
		TaskID: "task-render", Seq: 7, Type: "assistant.snapshot",
		Payload: json.RawMessage(`{"text":"recovered final answer","partial":false}`),
	})
	if got := stdout.String(); got != "recovered final answer" {
		t.Fatalf("snapshot output = %q", got)
	}
	if got := renderer.FinalAnswer(persistence.TaskRecord{ID: "task-render"}); got != "recovered final answer" {
		t.Fatalf("FinalAnswer() = %q", got)
	}
}

func TestTaskStreamRendererJSONLIsValidAndDeduplicated(t *testing.T) {
	var stdout, stderr bytes.Buffer
	renderer := newTaskStreamRenderer(&stdout, &stderr, outputJSON)
	event := localclient.TaskEvent{
		TaskID: "task-json", Seq: 1, EventVersion: "1", Type: "step_started",
		Timestamp: "2026-11-23T00:00:00Z",
		Payload: json.RawMessage(`{"step_id":"generate"}`),
	}
	renderer.HandleEvent(event)
	renderer.HandleEvent(event)
	renderer.HandleEvent(localclient.TaskEvent{
		TaskID: "task-json", Seq: 2, EventVersion: "1", Type: "task_finished",
		Payload: json.RawMessage(`{"status":"succeeded"}`),
	})
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("JSONL line count = %d, want 2: %q", len(lines), stdout.String())
	}
	for _, line := range lines {
		var item map[string]any
		if err := json.Unmarshal([]byte(line), &item); err != nil {
			t.Fatalf("invalid JSONL line %q: %v", line, err)
		}
	}
	if stderr.Len() != 0 {
		t.Fatalf("JSON mode wrote diagnostics to stderr unexpectedly: %q", stderr.String())
	}
	if strings.Contains(stdout.String(), "\x1b[") {
		t.Fatal("JSONL output contains ANSI escape sequences")
	}
	if !renderer.TerminalSeen() {
		t.Fatal("terminal task event was not recorded")
	}
}

func TestParseOutputModeRejectsUnknownMode(t *testing.T) {
	if mode, err := parseOutputMode("json"); err != nil || mode != outputJSON {
		t.Fatalf("parseOutputMode(json) = %q, %v", mode, err)
	}
	if _, err := parseOutputMode("xml"); err == nil {
		t.Fatal("parseOutputMode accepted unsupported output mode")
	}
}
