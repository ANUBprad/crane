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

func TestDiscoverResolvesRelativeInstallRoot(t *testing.T) {
	base := t.TempDir()
	install := filepath.Join(base, "install")
	version := "1.2.3-test"
	verDir := filepath.Join(install, "versions", version)
	pythonDir := filepath.Join(verDir, "runtime", "python")
	appDir := filepath.Join(verDir, "runtime", "app")
	for _, dir := range []string{pythonDir, appDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	pythonExe := filepath.Join(pythonDir, "python.exe")
	if err := os.WriteFile(pythonExe, []byte("stub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(verDir, "agent.exe"), []byte("stub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(install, "current.txt"), []byte(version), 0o644); err != nil {
		t.Fatal(err)
	}

	other := filepath.Join(base, "other-cwd")
	if err := os.MkdirAll(other, 0o755); err != nil {
		t.Fatal(err)
	}
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(other); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(wd) })

	rel, err := filepath.Rel(other, install)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CRANE_INSTALL_ROOT", rel)
	t.Setenv("CRANE_PYTHON", "")
	t.Setenv("CRANE_PYTHON_HOME", "")

	details := Discover()
	if !filepath.IsAbs(details.InstallRoot) {
		t.Fatalf("InstallRoot should be absolute, got %q", details.InstallRoot)
	}
	if details.Python == "" {
		t.Fatal("Discover() did not resolve packaged python from relative install root")
	}
	if !filepath.IsAbs(details.Python) {
		t.Fatalf("Python path should be absolute, got %q", details.Python)
	}
	if _, err := os.Stat(details.Python); err != nil {
		t.Fatalf("resolved python missing: %v", err)
	}
	if details.Version != version {
		t.Fatalf("version = %q, want %q", details.Version, version)
	}
}
