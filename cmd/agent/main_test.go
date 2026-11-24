package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"crane/internal/localclient"
	"crane/persistence"
)

func TestRunAPITokenLifecycleAndTaskCreate(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("protected installation tokens are validated on Windows")
	}
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)

	var versionOut bytes.Buffer
	if err := run([]string{"--version"}, &versionOut, &bytes.Buffer{}); err != nil {
		t.Fatalf("--version error = %v", err)
	}
	if !strings.Contains(versionOut.String(), "CRANE version:") {
		t.Fatalf("unexpected version output: %q", versionOut.String())
	}

	var out, errOut bytes.Buffer
	if err := run([]string{"api-token"}, &out, &errOut); err != nil {
		t.Fatalf("api-token error = %v", err)
	}
	original := strings.TrimSpace(out.String())
	decoded, err := base64.RawURLEncoding.DecodeString(original)
	if err != nil || len(decoded) != 32 {
		t.Fatalf("token = %q, decoded length = %d, error = %v", original, len(decoded), err)
	}

	out.Reset()
	if err := run([]string{"api-token"}, &out, &errOut); err != nil {
		t.Fatalf("second api-token error = %v", err)
	}
	if strings.TrimSpace(out.String()) != original {
		t.Fatal("token changed between retrievals")
	}

	out.Reset()
	if err := run([]string{"api-token", "--rotate"}, &out, &errOut); err != nil {
		t.Fatalf("api-token --rotate error = %v", err)
	}
	rotated := strings.TrimSpace(out.String())
	if rotated == original || rotated == "" {
		t.Fatal("rotation did not produce a new token")
	}

	if err := run([]string{"api-token", "--revoke"}, &out, &errOut); err != nil {
		t.Fatalf("api-token --revoke error = %v", err)
	}
	out.Reset()
	errOut.Reset()
	if err := run([]string{"api-token"}, &out, &errOut); err == nil {
		t.Fatal("token retrieval succeeded after revocation")
	}
	if strings.Contains(errOut.String(), original) || strings.Contains(errOut.String(), rotated) {
		t.Fatal("error output leaked a token")
	}
	if out.Len() != 0 {
		t.Fatalf("token retrieval emitted stdout after revocation: %q", out.String())
	}

	out.Reset()
	if err := run([]string{"task", "create", "--id", "e2e-task", "--input", "private input"}, &out, &errOut); err != nil {
		t.Fatalf("task create error = %v", err)
	}
	if !strings.Contains(out.String(), "created task e2e-task") {
		t.Fatalf("unexpected task output: %q", out.String())
	}
	if strings.Contains(out.String(), "private input") {
		t.Fatal("task output exposed private input")
	}
	if _, err := filepath.Abs(root); err != nil {
		t.Fatal(err)
	}
}

func TestStatusAndResultReadPersistedTaskWithoutSupervisor(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)

	var out, errOut bytes.Buffer
	if err := run([]string{"task", "create", "--id", "status-task", "--input", "private task body"}, &out, &errOut); err != nil {
		t.Fatalf("task create error = %v", err)
	}

	out.Reset()
	if err := run([]string{"status", "status-task"}, &out, &errOut); err != nil {
		t.Fatalf("status error = %v; stderr = %q", err, errOut.String())
	}
	if !strings.Contains(out.String(), "Status: queued") {
		t.Fatalf("unexpected status output: %q", out.String())
	}
	if strings.Contains(out.String(), "private task body") {
		t.Fatal("status output exposed task input")
	}

	out.Reset()
	errOut.Reset()
	if err := run([]string{"result", "status-task"}, &out, &errOut); err == nil {
		t.Fatal("result succeeded for a queued task")
	}
	if strings.Contains(out.String()+errOut.String(), "private task body") {
		t.Fatal("result output exposed task input")
	}
}

func TestLifecycleCommandsRejectUnsafeDataRemoval(t *testing.T) {
	var out, errOut bytes.Buffer
	if err := run([]string{"uninstall", "--remove-data"}, &out, &errOut); err == nil {
		t.Fatal("uninstall accepted --remove-data without confirmation")
	}
	if err := run([]string{"uninstall", "--remove-data", "--confirm", "wrong"}, &out, &errOut); err == nil {
		t.Fatal("uninstall accepted an invalid confirmation token")
	}
	if err := run([]string{"run"}, &out, &errOut); err == nil {
		t.Fatal("run accepted empty task text")
	}
}

type recordingTaskSubmitter struct {
	workspace string
	input     string
}

func (r *recordingTaskSubmitter) CreateTaskWithContext(_ context.Context, _, input, _ string, _ []localclient.ConversationMessage, workspace string) (persistence.TaskRecord, error) {
	r.input = input
	r.workspace = workspace
	return persistence.TaskRecord{ID: "task-workspace", Status: "queued"}, nil
}

func TestRunSubmitsAuthorizedWorkspaceForActionTasks(t *testing.T) {
	workspace := t.TempDir()
	objective := "Create a Python project with tests and repair any failures"
	parsed, err := parseRunArgs([]string{objective, "--workspace", workspace})
	if err != nil {
		t.Fatalf("parseRunArgs() error = %v", err)
	}
	if parsed.input != objective {
		t.Fatalf("input = %q, want %q", parsed.input, objective)
	}
	if parsed.workspace != workspace {
		t.Fatalf("workspace = %q, want %q", parsed.workspace, workspace)
	}

	submitter := &recordingTaskSubmitter{}
	if _, err := submitTask(context.Background(), submitter, "task-workspace", parsed.input, parsed.workspace); err != nil {
		t.Fatalf("submitTask() error = %v", err)
	}
	if submitter.workspace != workspace {
		t.Fatalf("submitted workspace = %q, want %q", submitter.workspace, workspace)
	}
	if submitter.input != objective {
		t.Fatalf("submitted input = %q, want %q", submitter.input, objective)
	}
}

func TestDoctorReportsOllamaConfigurationWithoutTaskData(t *testing.T) {
	if _, err := exec.LookPath("python"); err != nil {
		t.Skip("python is not available on PATH")
	}
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("CRANE_PYTHON", "python")
	t.Setenv("CRANE_PYTHON_HOME", root)
	t.Setenv("CRANE_MODEL_PROVIDER", "ollama")
	t.Setenv("CRANE_MODEL", "doctor-model:latest")
	t.Setenv("OLLAMA_HOST", "http://127.0.0.1:1")
	t.Setenv("CRANE_OLLAMA_CONNECT_TIMEOUT_SECONDS", "0.1")

	var out, errOut bytes.Buffer
	if err := run([]string{"doctor"}, &out, &errOut); err != nil {
		t.Fatalf("doctor error = %v; stderr = %q", err, errOut.String())
	}
	for _, expected := range []string{
		"Provider: ollama",
		"Configured model: doctor-model:latest",
		"Ollama reachable: no",
		"ollama_unavailable",
	} {
		if !strings.Contains(out.String(), expected) {
			t.Fatalf("doctor output missing %q: %q", expected, out.String())
		}
	}
	if strings.Contains(out.String(), "private input") {
		t.Fatalf("doctor output exposed task data: %q", out.String())
	}
}

func TestWindowsPowerShellLifecycleHostDetection(t *testing.T) {
	for _, executable := range []string{
		`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`,
		`C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe`,
		"powershell.exe",
		"powershell",
	} {
		if !isWindowsPowerShellHost(executable) {
			t.Fatalf("expected Windows PowerShell host: %q", executable)
		}
	}
	for _, executable := range []string{
		`C:\Program Files\PowerShell\7\pwsh.exe`,
		"pwsh",
		"pwsh.exe",
	} {
		if isWindowsPowerShellHost(executable) {
			t.Fatalf("did not expect Windows PowerShell host: %q", executable)
		}
	}
}

func TestLifecycleEnvironmentDropsPwshModuleStateForWindowsPowerShell(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)
	t.Setenv("PSModulePath", `C:\Users\test\Documents\PowerShell\Modules;C:\Program Files\PowerShell\Modules`)
	t.Setenv("PSHOME", `C:\Program Files\PowerShell\7`)
	t.Setenv("POWERSHELL_DISTRIBUTION_CHANNEL", "MSI:Windows 10.0")

	environment := lifecycleEnvironment(`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`)
	values := make(map[string]string, len(environment))
	for _, entry := range environment {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) == 2 {
			values[strings.ToUpper(parts[0])] = parts[1]
		}
	}
	for _, dropped := range []string{"PSMODULEPATH", "PSHOME", "POWERSHELL_DISTRIBUTION_CHANNEL"} {
		if _, ok := values[dropped]; ok {
			t.Fatalf("expected %s to be removed for Windows PowerShell lifecycle commands", dropped)
		}
	}
	if values["CRANE_INSTALL_ROOT"] == "" || values["CRANE_INSTALL_ROOT"] == "stale-root" {
		t.Fatalf("expected CRANE_INSTALL_ROOT to be set to the discovered install root, got %q", values["CRANE_INSTALL_ROOT"])
	}
}

func TestLifecycleEnvironmentPreservesPwshModuleStateForPwsh(t *testing.T) {
	root := t.TempDir()
	t.Setenv("LOCALAPPDATA", root)
	const modulePath = `C:\Users\test\Documents\PowerShell\Modules;C:\Program Files\PowerShell\Modules`
	const psHome = `C:\Program Files\PowerShell\7`
	t.Setenv("PSModulePath", modulePath)
	t.Setenv("PSHOME", psHome)

	environment := lifecycleEnvironment(`C:\Program Files\PowerShell\7\pwsh.exe`)
	values := make(map[string]string, len(environment))
	for _, entry := range environment {
		parts := strings.SplitN(entry, "=", 2)
		if len(parts) == 2 {
			values[strings.ToUpper(parts[0])] = parts[1]
		}
	}
	if values["PSMODULEPATH"] != modulePath {
		t.Fatalf("expected PSModulePath to be preserved for pwsh, got %q", values["PSMODULEPATH"])
	}
	if values["PSHOME"] != psHome {
		t.Fatalf("expected PSHOME to be preserved for pwsh, got %q", values["PSHOME"])
	}
}

func TestWindowsPowerShellLifecycleEnvironmentRestoresBuiltInCmdlets(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows PowerShell is available only on Windows")
	}
	executable, err := powershellExecutable()
	if err != nil {
		t.Fatal(err)
	}
	if !isWindowsPowerShellHost(executable) {
		t.Skip("Windows PowerShell is not the lifecycle host on this machine")
	}

	t.Setenv("PSModulePath", `C:\NoSuchPowerShell7\Modules`)
	t.Setenv("PSHOME", `C:\NoSuchPowerShell7`)
	command := exec.Command(executable, "-NoProfile", "-NonInteractive", "-Command", `(Get-Command Get-FileHash).Source`)
	command.Env = lifecycleEnvironment(executable)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("Get-FileHash failed in Windows PowerShell lifecycle environment: %v; output = %s", err, output)
	}
}
