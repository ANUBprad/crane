package localauth

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func newTestManager(t *testing.T) *Manager {
	t.Helper()
	if runtime.GOOS != "windows" {
		t.Skip("DPAPI-protected token storage is validated on Windows")
	}
	manager, err := NewManager(filepath.Join(t.TempDir(), "auth", "token.json"))
	if err != nil {
		t.Fatalf("NewManager() error = %v", err)
	}
	return manager
}

func TestTokenLifecycleAndRestart(t *testing.T) {
	manager := newTestManager(t)
	if err := manager.LoadOrCreate(); err != nil {
		t.Fatalf("LoadOrCreate() error = %v", err)
	}
	token, err := manager.CurrentToken()
	if err != nil {
		t.Fatalf("CurrentToken() error = %v", err)
	}
	if !manager.Verify(token) {
		t.Fatal("created token did not verify")
	}
	if manager.Verify("") || manager.Verify("invalid-token") {
		t.Fatal("missing or invalid token was accepted")
	}

	restarted, err := NewManager(manager.Path())
	if err != nil {
		t.Fatalf("NewManager() error = %v", err)
	}
	if err := restarted.LoadOrCreate(); err != nil {
		t.Fatalf("restart LoadOrCreate() error = %v", err)
	}
	restored, err := restarted.CurrentToken()
	if err != nil {
		t.Fatalf("restart CurrentToken() error = %v", err)
	}
	if restored != token {
		t.Fatal("token changed after application restart")
	}
	if !restarted.Verify(restored) {
		t.Fatal("restored token did not verify")
	}
}

func TestTokenRotationAndRevocationSurviveRestart(t *testing.T) {
	manager := newTestManager(t)
	if err := manager.LoadOrCreate(); err != nil {
		t.Fatal(err)
	}
	original, err := manager.CurrentToken()
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.Rotate(); err != nil {
		t.Fatalf("Rotate() error = %v", err)
	}
	rotated, err := manager.CurrentToken()
	if err != nil {
		t.Fatal(err)
	}
	if rotated == original {
		t.Fatal("rotation did not change the token")
	}
	if manager.Verify(original) {
		t.Fatal("rotated-out token still verifies")
	}
	if !manager.Verify(rotated) {
		t.Fatal("rotated token did not verify")
	}

	restarted, err := NewManager(manager.Path())
	if err != nil {
		t.Fatal(err)
	}
	if err := restarted.Load(); err != nil {
		t.Fatalf("Load() after rotation error = %v", err)
	}
	if restarted.Verify(original) {
		t.Fatal("rotated-out token verifies after restart")
	}
	if !restarted.Verify(rotated) {
		t.Fatal("rotated token does not verify after restart")
	}

	if err := restarted.Revoke(); err != nil {
		t.Fatalf("Revoke() error = %v", err)
	}
	if restarted.Verify(rotated) {
		t.Fatal("revoked token still verifies")
	}
	if _, err := restarted.CurrentToken(); !errors.Is(err, ErrNoActiveToken) {
		t.Fatalf("CurrentToken() after revoke error = %v", err)
	}

	afterRevoke, err := NewManager(manager.Path())
	if err != nil {
		t.Fatal(err)
	}
	if err := afterRevoke.LoadOrCreate(); err != nil {
		t.Fatalf("LoadOrCreate() after revoke error = %v", err)
	}
	if afterRevoke.Verify(rotated) {
		t.Fatal("revoked token was regenerated after restart")
	}
	if _, err := afterRevoke.CurrentToken(); !errors.Is(err, ErrNoActiveToken) {
		t.Fatalf("unexpected active token after revoke: %v", err)
	}
}

func TestCorruptTokenStateIsNotReplaced(t *testing.T) {
	manager := newTestManager(t)
	if err := os.MkdirAll(filepath.Dir(manager.Path()), 0700); err != nil {
		t.Fatal(err)
	}
	corrupt := []byte("{not json")
	if err := os.WriteFile(manager.Path(), corrupt, 0600); err != nil {
		t.Fatal(err)
	}
	err := manager.LoadOrCreate()
	if err == nil {
		t.Fatal("expected corrupt token state to fail")
	}
	if strings.Contains(err.Error(), "token") && strings.Contains(err.Error(), "decode") {
		// Error text is useful, but it must not contain a credential value.
		if strings.Contains(err.Error(), string(corrupt)) {
			t.Fatal("error leaked token state contents")
		}
	}
	got, readErr := os.ReadFile(manager.Path())
	if readErr != nil {
		t.Fatal(readErr)
	}
	if string(got) != string(corrupt) {
		t.Fatal("corrupt token state was silently replaced")
	}
}
