package localclient

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"runtime"
	"testing"

	"crane/localauth"
)

func TestCreateTaskWithContextSendsWorkspace(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("DPAPI-protected token storage is validated on Windows")
	}

	auth, err := localauth.NewManager(filepath.Join(t.TempDir(), "auth", "token.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := auth.LoadOrCreate(); err != nil {
		t.Fatal(err)
	}
	token, err := auth.CurrentToken()
	if err != nil {
		t.Fatal(err)
	}

	const workspace = `C:\ authorized\ workspace`
	var payload map[string]any
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/v1/tasks" {
			t.Errorf("request = %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer "+token {
			t.Error("request did not carry the installation token")
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode payload: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"data":{"task":{"id":"task-workspace","status":"queued"}}}`)
	}))
	defer server.Close()

	client := &Client{baseURL: server.URL, auth: auth, http: server.Client()}
	task, err := client.CreateTaskWithContext(context.Background(), "task-workspace", "Create an app with tests", "", nil, workspace)
	if err != nil {
		t.Fatalf("CreateTaskWithContext() error = %v", err)
	}
	if task.ID != "task-workspace" {
		t.Fatalf("task ID = %q", task.ID)
	}
	if payload["workspace"] != workspace {
		t.Fatalf("payload workspace = %v, want %q", payload["workspace"], workspace)
	}
}
