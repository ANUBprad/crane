package persistence

import (
	"testing"
)

func TestConversationLifecycle(t *testing.T) {
	store := newTestStore(t)

	record, err := store.CreateConversation("conv-1", "")
	if err != nil {
		t.Fatalf("CreateConversation() error = %v", err)
	}
	if record.ID != "conv-1" {
		t.Fatalf("conversation ID = %q", record.ID)
	}

	msg, err := store.AppendConversationMessage("conv-1", "user", "Hello CRANE", "")
	if err != nil {
		t.Fatalf("AppendConversationMessage() error = %v", err)
	}
	if msg.Role != "user" {
		t.Fatalf("role = %q", msg.Role)
	}

	got, err := store.GetConversation("conv-1")
	if err != nil {
		t.Fatalf("GetConversation() error = %v", err)
	}
	if got.MessageCount != 1 {
		t.Fatalf("message count = %d, want 1", got.MessageCount)
	}
	if got.Title == "" {
		t.Fatal("expected title to be set from first user message")
	}

	msgs, err := store.GetConversationMessages("conv-1", 50)
	if err != nil {
		t.Fatalf("GetConversationMessages() error = %v", err)
	}
	if len(msgs) != 1 || msgs[0].Content != "Hello CRANE" {
		t.Fatalf("messages = %+v", msgs)
	}

	list, err := store.ListConversations(10)
	if err != nil {
		t.Fatalf("ListConversations() error = %v", err)
	}
	if len(list) != 1 || list[0].ID != "conv-1" {
		t.Fatalf("list = %+v", list)
	}
}

func TestConversationInvalidID(t *testing.T) {
	store := newTestStore(t)
	if _, err := store.GetConversation("../bad"); err == nil {
		t.Fatal("expected invalid ID error")
	}
}
