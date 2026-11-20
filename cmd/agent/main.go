package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"crane/internal/localclient"
	"crane/internal/runtimeenv"
	"crane/localapi"
	"crane/localauth"
	"crane/mcpruntime"
	"crane/persistence"
	"crane/supervisor"
)

// Build metadata injected with -ldflags by scripts/build-release.ps1.
var (
	version     = "dev"
	buildCommit = "unknown"
	buildDate   = "unknown"
	goVersion   = runtime.Version()
)

func main() {
	if err := run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		fmt.Fprintf(os.Stderr, "CRANE: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		return runServe(nil, stdout, stderr)
	}
	switch args[0] {
	case "--version", "-version", "version":
		fmt.Fprintf(stdout, "CRANE version: %s (commit %s, built %s, %s)\n", version, buildCommit, buildDate, goVersion)
		return nil
	case "--help", "-h", "help":
		printUsage(stdout)
		return nil
	case "serve":
		return runServe(args[1:], stdout, stderr)
	case "api-token":
		return runAPIToken(args[1:], stdout, stderr)
	case "task":
		return runTask(args[1:], stdout, stderr)
	case "run":
		return runTaskPrompt(args[1:], stdout, stderr)
	case "status":
		return runTaskStatus(args[1:], stdout, stderr)
	case "result":
		return runTaskResult(args[1:], stdout, stderr)
	case "cancel":
		return runTaskCancel(args[1:], stdout, stderr)
	case "doctor":
		return runDoctor(args[1:], stdout, stderr)
	case "update":
		return runUpdate(args[1:], stdout, stderr)
	case "uninstall":
		return runUninstall(args[1:], stdout, stderr)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func printUsage(stdout io.Writer) {
	fmt.Fprintln(stdout, "CRANE — offline-first MCP execution runtime")
	fmt.Fprintln(stdout, "")
	fmt.Fprintln(stdout, "Usage:")
	fmt.Fprintln(stdout, "  agent                      Start the supervisor and local API")
	fmt.Fprintln(stdout, "  agent run \"task\"            Submit a task and print its ID")
	fmt.Fprintln(stdout, "  agent status <task-id>     Report persisted task status")
	fmt.Fprintln(stdout, "  agent result <task-id>     Print a completed task result")
	fmt.Fprintln(stdout, "  agent cancel <task-id>     Request task cancellation")
	fmt.Fprintln(stdout, "  agent doctor               Report runtime and installation health")
	fmt.Fprintln(stdout, "  agent update [flags]       Verify and install a newer release")
	fmt.Fprintln(stdout, "  agent uninstall [flags]    Remove CRANE binaries and PATH entry")
}

func runServe(args []string, stdout, stderr io.Writer) error {
	if len(args) != 0 {
		return errors.New("serve accepts no arguments")
	}
	if err := runtimeenv.EnsureDataDirectories(); err != nil {
		return fmt.Errorf("initialize user data directories: %w", err)
	}

	sup := supervisor.New()
	details := runtimeenv.Discover()
	if details.Python != "" {
		sup.SetPythonRuntime(details.Python)
	}
	if err := sup.Start(); err != nil {
		return err
	}
	defer sup.Close()
	defer sup.Stop()

	mcp := mcpruntime.New(sup.Store(), mcpruntime.Config{
		PythonPath: details.Python,
		PythonHome: details.Home,
	})
	if err := mcp.Start(); err != nil {
		return fmt.Errorf("start MCP controller: %w", err)
	}
	defer mcp.Close()

	auth, err := localauth.NewManager(localauth.DefaultPath())
	if err != nil {
		return err
	}
	if err := auth.LoadOrCreate(); err != nil {
		return fmt.Errorf("load installation token: %w", err)
	}

	api, err := localapi.NewServer(auth, sup.Store(), func() localapi.RuntimeStatus {
		available, detail := sup.RuntimeAvailable()
		return localapi.RuntimeStatus{Available: available, Detail: detail}
	}, sup.IsRunning, sup)
	if err != nil {
		return err
	}
	api.SetMCPController(mcp)
	if err := api.Start(localapi.DefaultPort()); err != nil {
		return fmt.Errorf("start local API: %w", err)
	}
	defer api.Shutdown()

	fmt.Fprintf(stdout, "CRANE supervisor listening on %s\n", api.Addr())
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	defer signal.Stop(signals)

	select {
	case <-signals:
		fmt.Fprintln(stdout, "CRANE supervisor shutting down")
		return nil
	case err := <-api.Done():
		if err != nil {
			return fmt.Errorf("local API stopped: %w", err)
		}
		return nil
	}
}

// runAPIToken is a trusted integration boundary. The token is printed only on
// stdout so the Electron main process can read it without placing it in an
// argument, URL, browser storage or renderer API.
func runAPIToken(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("api-token", flag.ContinueOnError)
	flags.SetOutput(stderr)
	rotate := flags.Bool("rotate", false, "rotate the installation token and print the replacement")
	revoke := flags.Bool("revoke", false, "permanently revoke the active token")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("api-token accepts no positional arguments")
	}
	if *rotate && *revoke {
		return errors.New("api-token: --rotate and --revoke are mutually exclusive")
	}

	manager, err := localauth.NewManager(localauth.DefaultPath())
	if err != nil {
		return err
	}
	if *revoke {
		if err := manager.Load(); err != nil {
			return fmt.Errorf("load installation token: %w", err)
		}
		if err := manager.Revoke(); err != nil {
			return fmt.Errorf("revoke installation token: %w", err)
		}
		fmt.Fprintln(stderr, "CRANE installation token revoked")
		return nil
	}

	if err := manager.LoadOrCreate(); err != nil {
		return fmt.Errorf("load installation token: %w", err)
	}
	if *rotate {
		if err := manager.Rotate(); err != nil {
			return fmt.Errorf("rotate installation token: %w", err)
		}
	}
	token, err := manager.CurrentToken()
	if err != nil {
		return err
	}
	fmt.Fprintln(stdout, token)
	return nil
}

func runTask(args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		return errors.New("task requires a subcommand")
	}
	switch args[0] {
	case "create":
		return runTaskCreate(args[1:], stdout, stderr)
	default:
		return fmt.Errorf("unknown task subcommand %q", args[0])
	}
}

func runTaskCreate(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("task create", flag.ContinueOnError)
	flags.SetOutput(stderr)
	id := flags.String("id", "", "stable task identifier")
	input := flags.String("input", "", "task description")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("task create accepts no positional arguments")
	}
	if !persistence.ValidTaskID(*id) {
		return errors.New("task create requires a valid --id")
	}

	if err := runtimeenv.EnsureDataDirectories(); err != nil {
		return err
	}
	store, err := persistence.NewStore(persistence.DBPath())
	if err != nil {
		return err
	}
	defer store.Close()
	created, err := store.CreateTask(*id, "queued", *input)
	if err != nil {
		return fmt.Errorf("create task: %w", err)
	}
	fmt.Fprintf(stdout, "created task %s\n", created)
	return nil
}

func runTaskPrompt(args []string, stdout, stderr io.Writer) error {
	input := strings.TrimSpace(strings.Join(args, " "))
	if input == "" {
		return errors.New("run requires task text")
	}
	if err := runtimeenv.EnsureDataDirectories(); err != nil {
		return err
	}
	id, err := newTaskID()
	if err != nil {
		return err
	}
	client, err := localclient.New()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if _, err := client.Health(ctx); err != nil {
		executable, execErr := os.Executable()
		if execErr != nil {
			return execErr
		}
		if startErr := startDetachedServe(executable); startErr != nil {
			return startErr
		}
		if err := waitForSupervisor(ctx, client); err != nil {
			return err
		}
	}
	created, err := client.CreateTask(ctx, id, input)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "task %s %s\n", created.ID, created.Status)
	return nil
}

func newTaskID() (string, error) {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return fmt.Sprintf("task-%s-%s", time.Now().UTC().Format("20060102T150405.000000000"), hex.EncodeToString(buf)), nil
}

func waitForSupervisor(ctx context.Context, client *localclient.Client) error {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		health, err := client.Health(ctx)
		if err == nil && health.Supervisor {
			return nil
		}
		select {
		case <-ctx.Done():
			return errors.New("supervisor did not become ready; run `agent doctor` for details")
		case <-ticker.C:
		}
	}
}

func runTaskStatus(args []string, stdout, stderr io.Writer) error {
	id, err := singleTaskID("status", args)
	if err != nil {
		return err
	}
	record, err := readTaskRecord(id)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "ID: %s\nStatus: %s\n", record.ID, record.Status)
	if record.Error != "" {
		fmt.Fprintf(stdout, "Error: %s\n", record.Error)
	}
	return nil
}

func runTaskResult(args []string, stdout, stderr io.Writer) error {
	id, err := singleTaskID("result", args)
	if err != nil {
		return err
	}
	record, err := readTaskRecord(id)
	if err != nil {
		return err
	}
	if record.Status == "failed" && record.Error != "" {
		fmt.Fprintf(stderr, "Task %s failed: %s\n", id, record.Error)
		return nil
	}
	result := strings.TrimSpace(record.Result)
	if result == "" {
		return fmt.Errorf("task %s has no result (status %s)", id, record.Status)
	}
	fmt.Fprintln(stdout, result)
	return nil
}

func runTaskCancel(args []string, stdout, stderr io.Writer) error {
	id, err := singleTaskID("cancel", args)
	if err != nil {
		return err
	}
	client, err := localclient.New()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	record, err := client.CancelTask(ctx, id)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "task %s %s\n", record.ID, record.Status)
	return nil
}

func singleTaskID(command string, args []string) (string, error) {
	if len(args) != 1 {
		return "", fmt.Errorf("%s requires exactly one task ID", command)
	}
	id := strings.TrimSpace(args[0])
	if !persistence.ValidTaskID(id) {
		return "", errors.New("invalid task ID")
	}
	return id, nil
}

func readTaskRecord(id string) (persistence.TaskRecord, error) {
	client, err := localclient.New()
	if err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		record, apiErr := client.GetTask(ctx, id)
		cancel()
		if apiErr == nil {
			return record, nil
		}
	}
	if err := runtimeenv.EnsureDataDirectories(); err != nil {
		return persistence.TaskRecord{}, err
	}
	store, err := persistence.NewStore(persistence.DBPath())
	if err != nil {
		return persistence.TaskRecord{}, err
	}
	defer store.Close()
	return store.GetTaskRecord(id)
}

type doctorReport struct {
	Status       string        `json:"status"`
	Installation installDoctor `json:"installation"`
	Runtime      runtimeDoctor `json:"runtime"`
	SQLite       sqliteDoctor  `json:"sqlite"`
	MCP          mcpDoctor     `json:"mcp"`
	Provider     string        `json:"provider"`
	Ollama       ollamaDoctor  `json:"ollama"`
	Issues       []doctorIssue `json:"issues"`
}

type installDoctor struct {
	Version       string `json:"version"`
	BuildCommit   string `json:"build_commit"`
	InstallRoot   string `json:"install_root"`
	ActiveVersion string `json:"active_version"`
	Packaged      bool   `json:"packaged"`
}

type runtimeDoctor struct {
	Available     bool            `json:"available"`
	PythonVersion string          `json:"python_version"`
	Executable    string          `json:"executable"`
	SQLiteModule  bool            `json:"sqlite_module"`
	Modules       map[string]bool `json:"modules"`
}

type sqliteDoctor struct {
	Available bool   `json:"available"`
	Path      string `json:"path"`
}

type mcpDoctor struct {
	ModulesAvailable bool            `json:"available"`
	Supervisor       bool            `json:"supervisor"`
	Servers          int             `json:"servers"`
	Connected        int             `json:"connected"`
	Modules          map[string]bool `json:"modules"`
}

type ollamaDoctor struct {
	Installed             bool   `json:"installed"`
	BaseURL               string `json:"base_url"`
	Reachable             bool   `json:"reachable"`
	ConfiguredModel       string `json:"configured_model"`
	ModelAvailable        bool   `json:"model_available"`
	AvailableModelCount   int    `json:"available_model_count"`
	ProviderInitializable bool   `json:"provider_initializable"`
}

type doctorIssue struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func runDoctor(args []string, stdout, stderr io.Writer) error {
	if len(args) != 0 {
		return errors.New("doctor accepts no arguments")
	}
	report := collectDoctorReport()
	yesNo := func(value bool) string {
		if value {
			return "yes"
		}
		return "no"
	}
	fmt.Fprintln(stdout, "CRANE doctor")
	fmt.Fprintf(stdout, "Overall: %s\n", report.Status)
	fmt.Fprintf(stdout, "Installation version: %s\n", report.Installation.Version)
	fmt.Fprintf(stdout, "Active release: %s\n", yesNo(report.Installation.Packaged))
	if report.Installation.ActiveVersion != "" {
		fmt.Fprintf(stdout, "Installed release: %s\n", report.Installation.ActiveVersion)
	}
	fmt.Fprintf(stdout, "Python runtime available: %s\n", yesNo(report.Runtime.Available))
	if report.Runtime.Executable != "" {
		fmt.Fprintf(stdout, "Python executable: %s\n", report.Runtime.Executable)
	}
	if report.Runtime.PythonVersion != "" {
		fmt.Fprintf(stdout, "Python version: %s\n", report.Runtime.PythonVersion)
	}
	fmt.Fprintf(stdout, "SQLite available: %s\n", yesNo(report.SQLite.Available))
	fmt.Fprintf(stdout, "SQLite path: %s\n", report.SQLite.Path)
	fmt.Fprintf(stdout, "MCP modules available: %s\n", yesNo(report.MCP.ModulesAvailable))
	fmt.Fprintf(stdout, "MCP supervisor reachable: %s\n", yesNo(report.MCP.Supervisor))
	if report.MCP.Supervisor {
		fmt.Fprintf(stdout, "MCP servers: %d (connected %d)\n", report.MCP.Servers, report.MCP.Connected)
	}
	fmt.Fprintf(stdout, "Provider: %s\n", report.Provider)
	fmt.Fprintf(stdout, "Ollama installed: %s\n", yesNo(report.Ollama.Installed))
	fmt.Fprintf(stdout, "Ollama endpoint: %s\n", report.Ollama.BaseURL)
	fmt.Fprintf(stdout, "Ollama reachable: %s\n", yesNo(report.Ollama.Reachable))
	fmt.Fprintf(stdout, "Configured model: %s\n", report.Ollama.ConfiguredModel)
	fmt.Fprintf(stdout, "Configured model available: %s\n", yesNo(report.Ollama.ModelAvailable))
	fmt.Fprintf(stdout, "LangChain Ollama provider initializes: %s\n", yesNo(report.Ollama.ProviderInitializable))
	if report.Ollama.AvailableModelCount > 0 {
		fmt.Fprintf(stdout, "Local models reported: %d\n", report.Ollama.AvailableModelCount)
	}
	if len(report.Issues) > 0 {
		fmt.Fprintln(stdout, "Issues and notes:")
		for _, issue := range report.Issues {
			fmt.Fprintf(stdout, "  - [%s] %s\n", issue.Code, issue.Message)
		}
	}
	if !report.Runtime.Available || !report.SQLite.Available || !report.MCP.ModulesAvailable {
		return errors.New("required CRANE runtime components are unavailable")
	}
	return nil
}

func collectDoctorReport() doctorReport {
	details := runtimeenv.Discover()
	report := doctorReport{
		Status: "degraded",
		Installation: installDoctor{
			Version:     version,
			BuildCommit: buildCommit,
			InstallRoot: details.InstallRoot,
			Packaged:    details.InstalledRoot,
		},
		Runtime: runtimeDoctor{Modules: map[string]bool{}},
		MCP:     mcpDoctor{Modules: map[string]bool{}},
	}
	if active, ok := runtimeenv.ActiveVersion(details.InstallRoot); ok {
		report.Installation.ActiveVersion = active
	} else if details.InstalledRoot {
		report.addIssue("install_pointer_missing", "the active release pointer is missing or invalid")
	}

	report.SQLite.Path = persistence.DBPath()
	store, err := persistence.NewStore(report.SQLite.Path)
	if err != nil {
		report.addIssue("sqlite_unavailable", "the SQLite data store could not be opened")
	} else {
		report.SQLite.Available = store.HealthCheck() == nil
		_ = store.Close()
		if !report.SQLite.Available {
			report.addIssue("sqlite_unavailable", "the SQLite data store did not pass its health check")
		}
	}

	pythonReport, err := collectPythonDoctorReport()
	if err != nil {
		report.addIssue("python_runtime_unavailable", "the packaged Python runtime did not return diagnostics")
	} else {
		report.Provider = pythonReport.Provider
		report.Ollama = pythonReport.Ollama
		report.Runtime = pythonReport.Runtime
		report.MCP.Modules = pythonReport.MCP.Modules
		report.MCP.ModulesAvailable = pythonReport.MCP.ModulesAvailable
		for _, issue := range pythonReport.Issues {
			report.Issues = append(report.Issues, issue)
		}
		if !pythonReport.Runtime.Available {
			report.addIssue("python_runtime_unavailable", "Python reported unavailable required modules")
		}
	}

	if client, err := localclient.New(); err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		status, mcpErr := client.MCPStatus(ctx)
		cancel()
		if mcpErr == nil {
			report.MCP.Supervisor = true
			report.MCP.Servers = len(status.Servers)
			for _, server := range status.Servers {
				if server.Connected {
					report.MCP.Connected++
				}
			}
		} else {
			report.addIssue("mcp_supervisor_unavailable", "the MCP controller is not reachable; start the supervisor to validate live MCP health")
		}
	}

	fatal := !report.Runtime.Available || !report.SQLite.Available || !report.MCP.ModulesAvailable ||
		(details.InstalledRoot && report.Installation.ActiveVersion == "")
	if !fatal && len(report.Issues) == 0 {
		report.Status = "ok"
	}
	return report
}

type pythonDoctorReport struct {
	Runtime  runtimeDoctor `json:"runtime"`
	MCP      mcpDoctor     `json:"mcp"`
	Provider string        `json:"provider"`
	Ollama   ollamaDoctor  `json:"ollama"`
	Issues   []doctorIssue `json:"issues"`
}

func collectPythonDoctorReport() (pythonDoctorReport, error) {
	candidates := runtimeenv.PythonCandidates()
	for _, runtimePath := range candidates {
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		cmd := exec.CommandContext(ctx, runtimePath, "-m", "crane.python_runtime", "--doctor")
		runtimeenv.Configure(cmd)
		output, err := cmd.Output()
		cancel()
		if err != nil {
			continue
		}
		var report pythonDoctorReport
		if json.Unmarshal(output, &report) != nil {
			continue
		}
		if report.Provider == "" {
			continue
		}
		if report.Runtime.Executable == "" {
			report.Runtime.Executable = runtimePath
		}
		return report, nil
	}
	return pythonDoctorReport{}, errors.New("could not collect Python runtime diagnostics")
}

func (r *doctorReport) addIssue(code, message string) {
	r.Issues = append(r.Issues, doctorIssue{Code: code, Message: message})
}

func runUpdate(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("update", flag.ContinueOnError)
	flags.SetOutput(stderr)
	source := flags.String("source", os.Getenv("CRANE_RELEASE_SOURCE"), "release source URL or directory")
	targetVersion := flags.String("version", "", "specific version to install")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("update accepts no positional arguments")
	}
	if strings.TrimSpace(*source) == "" {
		return errors.New("update requires -source or CRANE_RELEASE_SOURCE")
	}
	extra := []string{"-Action", "Update", "-ReleaseSource", *source}
	if *targetVersion != "" {
		extra = append(extra, "-Version", *targetVersion)
	}
	return runLifecycleScript("install.ps1", extra, stdout, stderr)
}

func runUninstall(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("uninstall", flag.ContinueOnError)
	flags.SetOutput(stderr)
	removeData := flags.Bool("remove-data", false, "also remove CRANE user data")
	confirmation := flags.String("confirm", "", "required confirmation token for --remove-data")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("uninstall accepts no positional arguments")
	}
	if *removeData && *confirmation != "REMOVE-CRANE-DATA" {
		return errors.New("--remove-data requires --confirm REMOVE-CRANE-DATA")
	}
	extra := []string{}
	if *removeData {
		extra = append(extra, "-RemoveData", "-Confirm", "REMOVE-CRANE-DATA")
	}
	return runLifecycleScript("uninstall.ps1", extra, stdout, stderr)
}

func lifecycleScript(name string) (string, error) {
	details := runtimeenv.Discover()
	if !details.InstalledRoot {
		return "", errors.New("lifecycle commands require a packaged CRANE installation")
	}
	path := filepath.Join(details.InstallRoot, "bin", name)
	if _, err := os.Stat(path); err != nil {
		return "", fmt.Errorf("%s is missing from the installation", name)
	}
	return path, nil
}

func runLifecycleScript(name string, extra []string, stdout, stderr io.Writer) error {
	executable, err := powershellExecutable()
	if err != nil {
		return err
	}
	script, err := lifecycleScript(name)
	if err != nil {
		return err
	}
	temporaryDirectory, err := os.MkdirTemp("", "crane-lifecycle-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temporaryDirectory)

	contents, err := os.ReadFile(script)
	if err != nil {
		return err
	}
	temporaryScript := filepath.Join(temporaryDirectory, filepath.Base(script))
	if err := os.WriteFile(temporaryScript, contents, 0700); err != nil {
		return err
	}

	params := []string{
		"-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive",
		"-File", temporaryScript,
	}
	params = append(params, extra...)
	cmd := exec.Command(executable, params...)
	cmd.Stdout = stdout
	cmd.Stderr = stderr
	cmd.Stdin = nil

	details := runtimeenv.Discover()
	prefix := "CRANE_INSTALL_ROOT="
	environment := make([]string, 0, len(os.Environ())+1)
	for _, entry := range os.Environ() {
		if strings.EqualFold(strings.SplitN(entry, "=", 2)[0], "CRANE_INSTALL_ROOT") {
			continue
		}
		environment = append(environment, entry)
	}
	cmd.Env = append(environment, prefix+details.InstallRoot)
	return cmd.Run()
}

func powershellExecutable() (string, error) {
	for _, name := range []string{"powershell.exe", "pwsh"} {
		if path, err := exec.LookPath(name); err == nil {
			return path, nil
		}
	}
	return "", errors.New("PowerShell is required for lifecycle commands")
}
