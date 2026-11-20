package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"crane/localapi"
	"crane/localauth"
	"crane/mcpruntime"
	"crane/persistence"
	"crane/supervisor"
)

// version string — injected at build time via -ldflags
var version = "dev"

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
		fmt.Fprintln(stdout, "CRANE version:", version)
		return nil
	case "serve":
		return runServe(args[1:], stdout, stderr)
	case "api-token":
		return runAPIToken(args[1:], stdout, stderr)
	case "task":
		return runTask(args[1:], stdout, stderr)
	case "doctor":
		return runDoctor(args[1:], stdout, stderr)
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func runServe(args []string, stdout, stderr io.Writer) error {
	if len(args) != 0 {
		return errors.New("serve accepts no arguments")
	}

	sup := supervisor.New()
	if err := sup.Start(); err != nil {
		return err
	}
	defer sup.Close()
	defer sup.Stop()

	mcp := mcpruntime.New(sup.Store(), mcpruntime.Config{})
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

type doctorReport struct {
	Status   string `json:"status"`
	Provider string `json:"provider"`
	Ollama   struct {
		Installed             bool   `json:"installed"`
		BaseURL               string `json:"base_url"`
		Reachable             bool   `json:"reachable"`
		ConfiguredModel       string `json:"configured_model"`
		ModelAvailable        bool   `json:"model_available"`
		AvailableModelCount   int    `json:"available_model_count"`
		ProviderInitializable bool   `json:"provider_initializable"`
	} `json:"ollama"`
	Issues []struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"issues"`
}

func runDoctor(args []string, stdout, stderr io.Writer) error {
	if len(args) != 0 {
		return errors.New("doctor accepts no arguments")
	}
	report, err := collectDoctorReport()
	if err != nil {
		return err
	}
	yesNo := func(value bool) string {
		if value {
			return "yes"
		}
		return "no"
	}
	fmt.Fprintln(stdout, "CRANE doctor")
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
	return nil
}

func collectDoctorReport() (doctorReport, error) {
	candidates := []string{}
	if configured := strings.TrimSpace(os.Getenv("CRANE_PYTHON")); configured != "" {
		candidates = append(candidates, configured)
	} else {
		candidates = append(candidates, "python", "py")
	}
	for _, runtimePath := range candidates {
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		cmd := exec.CommandContext(ctx, runtimePath, "-m", "crane.python_runtime", "--doctor")
		if home := os.Getenv("CRANE_PYTHON_HOME"); home != "" {
			cmd.Dir = home
		}
		output, err := cmd.Output()
		cancel()
		if err != nil {
			continue
		}
		var report doctorReport
		if json.Unmarshal(output, &report) != nil {
			continue
		}
		if report.Provider == "" {
			continue
		}
		return report, nil
	}
	return doctorReport{}, errors.New("could not collect Python runtime diagnostics")
}
