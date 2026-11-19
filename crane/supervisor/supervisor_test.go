package supervisor

import (
	"testing"
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
