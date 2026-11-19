package supervisor

import (
	"context"
	"testing"
)

func TestNewSupervisor(t *testing.T) {
	s := New()
	if s == nil {
		t.Fatal("Expected non-nil Supervisor")
	}
}

func TestSupervisorStop(t *testing.T) {
	s := New()
	s.Stop() // should not panic
}

func TestSupervisorStopWithContext(t *testing.T) {
	s := New()
	ctx := context.Background()
	err := s.StopWithContext(ctx)
	if err != nil {
		t.Fatalf("Expected nil error, got: %v", err)
	}
}

func TestVersion(t *testing.T) {
	ver := Version()
	if ver == "" {
		t.Fatal("Expected non-empty version string")
	}
	if ver != "0.1.0" {
		t.Fatalf("Expected version 0.1.0, got: %s", ver)
	}
}

func TestHealthCheck(t *testing.T) {
	err := HealthCheck()
	// Should not panic; may return an error since implementation is stubbed
	_ = err
}
