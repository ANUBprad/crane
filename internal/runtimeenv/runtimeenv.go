// Package runtimeenv resolves installed CRANE runtime paths without relying on
// the current working directory or a globally installed Python.
package runtimeenv

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// Details describes the Python runtime selected for this process.
type Details struct {
	InstallRoot   string
	Version       string
	Python        string
	Home          string
	AppDir        string
	Packaged      bool
	InstalledRoot bool
}

const (
	installRootEnv = "CRANE_INSTALL_ROOT"
	pythonEnv      = "CRANE_PYTHON"
	pythonHomeEnv  = "CRANE_PYTHON_HOME"
)

// InstallRoot returns the per-user application root, preferring the explicit
// environment override used by the launcher and lifecycle scripts.
func InstallRoot() string {
	if value := strings.TrimSpace(os.Getenv(installRootEnv)); value != "" {
		cleaned := filepath.Clean(value)
		if abs, err := filepath.Abs(cleaned); err == nil {
			if info, statErr := os.Stat(abs); statErr == nil && info.IsDir() {
				return abs
			}
			// If the relative path was resolved against a different cwd than the
			// caller intended, prefer a known-good install root from the executable.
			if executable, exeErr := os.Executable(); exeErr == nil {
				if root, ok := rootFromExecutable(executable); ok {
					return root
				}
			}
			return abs
		}
		return cleaned
	}
	if executable, err := os.Executable(); err == nil {
		if root, ok := rootFromExecutable(executable); ok {
			return root
		}
	}
	if runtime.GOOS == "windows" {
		if local := strings.TrimSpace(os.Getenv("LOCALAPPDATA")); local != "" {
			return filepath.Join(local, "Programs", "CRANE")
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".crane", "programs")
	}
	return filepath.Join(home, ".crane", "programs")
}

// DataRoot returns the per-user data root that lifecycle operations create.
func DataRoot() string {
	if runtime.GOOS == "windows" {
		if local := strings.TrimSpace(os.Getenv("LOCALAPPDATA")); local != "" {
			return filepath.Join(local, "CRANE")
		}
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ".crane"
	}
	return filepath.Join(home, ".crane")
}

// EnsureDataDirectories creates the stable per-user directories owned by CRANE.
func EnsureDataDirectories() error {
	root := DataRoot()
	for _, name := range []string{"config", "data", "auth", "logs", "mcp", "tmp", "cache"} {
		if err := os.MkdirAll(filepath.Join(root, name), 0700); err != nil {
			return err
		}
	}
	return nil
}

// Discover resolves the runtime path from the installation, then applies
// explicit environment overrides used by source-tree development and tests.
func Discover() Details {
	root := InstallRoot()
	details := Details{InstallRoot: root}
	if version, ok := versionFromExecutable(); ok {
		details.Version = version
	} else if version, ok := ActiveVersion(root); ok {
		details.Version = version
	}
	if details.Version != "" {
		base := filepath.Join(root, "versions", details.Version)
		if _, err := os.Stat(filepath.Join(base, "agent.exe")); err == nil {
			details.InstalledRoot = true
			details.Packaged = true
			details.AppDir = filepath.Join(base, "runtime", "app")
			details.Python = filepath.Join(base, "runtime", "python", "python.exe")
			if runtime.GOOS != "windows" {
				details.Python = filepath.Join(base, "runtime", "python", "bin", "python")
			}
			if _, err := os.Stat(details.Python); err != nil {
				details.Python = ""
			}
			details.Home = details.AppDir
		}
	}
	if configured := strings.TrimSpace(os.Getenv(pythonEnv)); configured != "" {
		details.Python = configured
	}
	if configured := strings.TrimSpace(os.Getenv(pythonHomeEnv)); configured != "" {
		details.Home = configured
	}
	return details
}

// PythonCandidates returns runtime paths in preference order. A packaged
// runtime is never silently bypassed unless an explicit override is present.
func PythonCandidates() []string {
	if configured := strings.TrimSpace(os.Getenv(pythonEnv)); configured != "" {
		return []string{configured}
	}
	details := Discover()
	if details.Python != "" {
		return []string{details.Python}
	}
	if runtime.GOOS == "windows" {
		return []string{"python", "py"}
	}
	return []string{"python3", "python"}
}

// Configure sets the private process boundary for a Python child process.
func Configure(cmd *exec.Cmd) {
	details := Discover()
	environment := os.Environ()
	if details.Python != "" {
		environment = replaceEnv(environment, pythonEnv, details.Python)
	}
	if details.Home != "" {
		environment = replaceEnv(environment, pythonHomeEnv, details.Home)
		cmd.Dir = details.Home
	}
	if details.AppDir != "" && details.Packaged {
		environment = replaceEnv(environment, "PYTHONPATH", prependPath(os.Getenv("PYTHONPATH"), details.AppDir))
	}
	cmd.Env = environment
}

// ActiveVersion reads the pointer written by the installer.
func ActiveVersion(root string) (string, bool) {
	raw, err := os.ReadFile(filepath.Join(root, "current.txt"))
	if err != nil {
		return "", false
	}
	version := strings.TrimSpace(string(raw))
	if !validVersion(version) {
		return "", false
	}
	return version, true
}

// PreviousVersion reads the recovery pointer used by rollback.
func PreviousVersion(root string) (string, bool) {
	raw, err := os.ReadFile(filepath.Join(root, "previous.txt"))
	if err != nil {
		return "", false
	}
	version := strings.TrimSpace(string(raw))
	if !validVersion(version) {
		return "", false
	}
	return version, true
}

func validVersion(version string) bool {
	if version == "" || len(version) > 100 || strings.ContainsAny(version, `/\:`) {
		return false
	}
	for _, r := range version {
		if r == '.' || r == '-' || r == '_' || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			continue
		}
		return false
	}
	return true
}

func versionFromExecutable() (string, bool) {
	executable, err := os.Executable()
	if err != nil {
		return "", false
	}
	parent := filepath.Clean(filepath.Dir(executable))
	if strings.EqualFold(filepath.Base(filepath.Dir(parent)), "versions") {
		version := filepath.Base(parent)
		if validVersion(version) {
			return version, true
		}
	}
	if strings.EqualFold(filepath.Base(parent), "versions") {
		version := filepath.Base(filepath.Dir(parent))
		if validVersion(version) {
			return version, true
		}
	}
	return "", false
}

func rootFromExecutable(executable string) (string, bool) {
	parent := filepath.Clean(filepath.Dir(executable))
	base := strings.ToLower(filepath.Base(parent))
	if strings.EqualFold(filepath.Base(filepath.Dir(parent)), "versions") {
		return filepath.Dir(filepath.Dir(parent)), true
	}
	switch base {
	case "versions":
		return filepath.Dir(parent), true
	case "bin":
		root := filepath.Dir(parent)
		if _, err := os.Stat(filepath.Join(root, "current.txt")); err == nil {
			return root, true
		}
	}
	return "", false
}

func replaceEnv(environment []string, key, value string) []string {
	result := make([]string, 0, len(environment)+1)
	for _, entry := range environment {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) == 2 && strings.EqualFold(parts[0], key) {
			continue
		}
		result = append(result, entry)
	}
	return append(result, key+"="+value)
}

func prependPath(existing, value string) string {
	existing = strings.TrimSpace(existing)
	if existing == "" {
		return value
	}
	return value + string(os.PathListSeparator) + existing
}
