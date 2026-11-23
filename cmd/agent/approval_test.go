package main

import (
	"context"
	"testing"

	"crane/persistence"
)

type fakeApprovalClient struct {
	id       string
	approved bool
	scope    string
}

func (f *fakeApprovalClient) ApproveTask(_ context.Context, id string, approved bool, scope string) (persistence.TaskRecord, error) {
	f.id = id
	f.approved = approved
	f.scope = scope
	return persistence.TaskRecord{ID: id}, nil
}

func TestSubmitApprovalDecisionUsesExactPendingScope(t *testing.T) {
	client := &fakeApprovalClient{}
	record := persistence.TaskRecord{
		ID:                   "task-approval-scope",
		PendingApprovalScope: "generation:validate:task-approval-scope:abc123",
	}
	if _, err := submitApprovalDecision(context.Background(), client, record, true); err != nil {
		t.Fatal(err)
	}
	if client.id != record.ID || !client.approved {
		t.Fatalf("approval decision = %#v", client)
	}
	if client.scope != record.PendingApprovalScope {
		t.Fatalf("scope = %q, want %q", client.scope, record.PendingApprovalScope)
	}
	if client.scope == "user_approved" {
		t.Fatal("generic approval scope must not be sent")
	}
}

func TestTaskApprovalSummaryPrefersPendingReason(t *testing.T) {
	record := persistence.TaskRecord{
		Result:                `{"summary":"stale result"}`,
		PendingApprovalReason: "Approve exact generated files",
	}
	if got := taskApprovalSummary(record); got != "Approve exact generated files" {
		t.Fatalf("summary = %q", got)
	}
}
