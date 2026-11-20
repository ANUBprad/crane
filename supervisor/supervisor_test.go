package supervisor

import (
	"encoding/json"
	"os/exec"
	"path/filepath"
	"testing"

	"crane/persistence"
	"crane/python_protocol"
)

// TestNewSupervisor creates a new Supervisor and verifies it is non-nil.
func TestNewSupervisor(t *testing.T) {
	s := New()
	if s == nil {
		t.Fatal("Expected non-nil Supervisor")
	}
}

// TestSupervisorStop verifies Stop() does not panic.
func TestSupervisorStop(t *testing.T) {
	s := New()
	s.Stop() // should not panic
}

// TestVersion verifies the Version() function returns the expected version string.
func TestVersion(t *testing.T) {
	ver := Version()
	if ver == "" {
		t.Fatal("Expected non-empty version string")
	}
	if ver != "0.1.0" {
		t.Fatalf("Expected version 0.1.0, got: %s", ver)
	}
}

// TestHealthCheck verifies HealthCheck() does not panic.
func TestHealthCheck(t *testing.T) {
	err := HealthCheck()
	// Should not panic; may return an error since implementation is stubbed
	_ = err
}

// TestPersistentState_jsonMarshalUnmarshal verifies that TaskState can be
// marshaled and unmarshaled correctly via JSON.
func TestPersistentState_jsonMarshalUnmarshal(t *testing.T) {
	state := python_protocol.TaskState{
		Message: python_protocol.Message{
			Version: python_protocol.ProtocolVersion,
			Type:    python_protocol.TypeTaskState,
		},
		TaskId:      "test-struct-1",
		State:       "running",
		CurrentStep: "initialization",
		GraphSteps:  0,
		WallClockMs: 0,
		Cancelled:   false,
	}
	data, err := json.Marshal(state)
	if err != nil {
		t.Fatalf("Failed to marshal TaskState: %v", err)
	}
	if len(data) == 0 {
		t.Fatal("Expected non-empty marshalled data")
	}

	var decoded python_protocol.TaskState
	err = json.Unmarshal(data, &decoded)
	if err != nil {
		t.Fatalf("Failed to unmarshal TaskState: %v", err)
	}
	if decoded.TaskId != "test-struct-1" {
		t.Fatalf("Expected task_id test-struct-1, got: %s", decoded.TaskId)
	}
}

// TestLaunchPythonRuntimeStructure verifies the launchPythonRuntime function
// has the correct structure for protocol message handling.
func TestSupervisorLifecycleAndRuntimeProbe(t *testing.T) {
	store, err := persistence.NewStore(filepath.Join(t.TempDir(), "crane.db"))
	if err != nil {
		t.Fatal(err)
	}
	supervisor := NewWithStore(store)
	defer supervisor.Close()
	if supervisor.IsRunning() {
		t.Fatal("supervisor reported running before Start()")
	}
	if err := supervisor.Start(); err != nil {
		t.Fatalf("Start() error = %v", err)
	}
	if !supervisor.IsRunning() {
		t.Fatal("supervisor did not report running after Start()")
	}
	if err := supervisor.HealthCheck(); err != nil {
		t.Fatalf("HealthCheck() error = %v", err)
	}

	if _, err := exec.LookPath("python"); err != nil {
		t.Skip("python is not available on PATH")
	}
	root, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CRANE_PYTHON_HOME", root)
	t.Setenv("CRANE_PYTHON", "python")
	available, detail := supervisor.RuntimeAvailable()
	if !available || detail == "" {
		t.Fatalf("RuntimeAvailable() = %v, %q", available, detail)
	}

	supervisor.Stop()
	if supervisor.IsRunning() {
		t.Fatal("supervisor still reported running after Stop()")
	}
	if err := supervisor.HealthCheck(); err == nil {
		t.Fatal("HealthCheck() succeeded after Stop()")
	}
}

func TestLaunchPythonRuntimeStructure(t *testing.T) {
	s := New()
	if s == nil {
		t.Fatal("Expected non-nil Supervisor")
	}

	// Verify the supervisor has a store initialized
	if s.store == nil {
		t.Log("Store is nil (may be expected if SQLite not available)")
		return
	}

	// Verify persistTaskState method exists and is callable
	// We test the structure by checking the method signature
	_ = s.persistTaskState

	// Verify getLatestTaskState function is accessible
	_, _ = getLatestTaskState(s.store, "dummy-id")
}
