package runtimeenv

import (
	"os"
	"path/filepath"
	"testing"
)

func TestActiveVersionRejectsUnsafePointers(t *testing.T) {
	root := t.TempDir()
	if version, ok := ActiveVersion(root); ok || version != "" {
		t.Fatalf("missing pointer = %q, %v", version, ok)
	}
	for _, value := range []string{"", "../escape", "1.2.3\\evil", "with space"} {
		if err := os.WriteFile(filepath.Join(root, "current.txt"), []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
		if version, ok := ActiveVersion(root); ok || version != "" {
			t.Fatalf("pointer %q accepted as %q", value, version)
		}
	}
	if err := os.WriteFile(filepath.Join(root, "current.txt"), []byte("0.1.0\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if version, ok := ActiveVersion(root); !ok || version != "0.1.0" {
		t.Fatalf("valid pointer = %q, %v", version, ok)
	}
}

func TestEnsureDataDirectories(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)
	if err := EnsureDataDirectories(); err != nil {
		t.Fatalf("EnsureDataDirectories() error = %v", err)
	}
	for _, name := range []string{"config", "data", "auth", "logs", "mcp", "tmp", "cache"} {
		if info, err := os.Stat(filepath.Join(root, "CRANE", name)); err != nil || !info.IsDir() {
			t.Fatalf("directory %s missing: %v", name, err)
		}
	}
}
