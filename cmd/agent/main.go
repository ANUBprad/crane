package main

import (
	"bufio"
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
	"sync"
	"syscall"
	"time"

	"crane/internal/ghauth"
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
	case "create":
		return runCreate(args[1:], stdout, stderr)
	case "chat":
		return runChat(args[1:], stdout, stderr)
	case "auth":
		return runAuth(args[1:], stdout, stderr)
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
	fmt.Fprintln(stdout, "  agent run \"task\"            Submit a task and follow live progress")
	fmt.Fprintln(stdout, "  agent create \"objective\" --workspace <dir>  Generate a project in an authorized workspace")
	fmt.Fprintln(stdout, "  agent chat [flags]         Start or resume a persistent conversation")
	fmt.Fprintln(stdout, "  agent status <task-id>     Report persisted task status")
	fmt.Fprintln(stdout, "  agent result <task-id>     Print a completed task result")
	fmt.Fprintln(stdout, "  agent cancel <task-id>     Request task cancellation")
	fmt.Fprintln(stdout, "  agent auth <login|status|logout>  Manage GitHub authentication")
	fmt.Fprintln(stdout, "  agent doctor               Report runtime and installation health")
	fmt.Fprintln(stdout, "  agent update [flags]       Verify and install a newer release")
	fmt.Fprintln(stdout, "  agent uninstall [flags]    Remove CRANE binaries and PATH entry")
	fmt.Fprintln(stdout, "")
	fmt.Fprintln(stdout, "Chat flags:")
	fmt.Fprintln(stdout, "  --resume <id>              Resume an existing conversation")
	fmt.Fprintln(stdout, "  --list                     List recent conversations")
	fmt.Fprintln(stdout, "  --history <id>             Show conversation history")
	fmt.Fprintln(stdout, "  --limit <n>                Limit list/history entries")
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
	ctx := context.Background()
	healthCtx, healthCancel := context.WithTimeout(ctx, 30*time.Second)
	if _, err := client.Health(healthCtx); err != nil {
		executable, execErr := os.Executable()
		if execErr != nil {
			healthCancel()
			return execErr
		}
		if startErr := startDetachedServe(executable); startErr != nil {
			healthCancel()
			return startErr
		}
		if err := waitForSupervisor(healthCtx, client); err != nil {
			healthCancel()
			return err
		}
	}
	healthCancel()

	submitCtx, submitCancel := context.WithTimeout(ctx, 30*time.Second)
	created, err := client.CreateTask(submitCtx, id, input)
	submitCancel()
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "task %s %s\n", created.ID, created.Status)

	for {
		renderer, cancelStream := streamTaskOutput(ctx, client, created.ID, stdout, stderr)
		record, err := waitForTaskCompletion(ctx, client, created.ID, 10*time.Minute)
		cancelStream()
		time.Sleep(150 * time.Millisecond)
		if err != nil {
			return err
		}
		if taskExecutionStatus(record) == "waiting_for_approval" {
			_, approved, aerr := promptApproval(ctx, client, record, os.Stdin, stdout, stderr)
			if aerr != nil {
				return aerr
			}
			if !approved {
				return errors.New("approval rejected")
			}
			continue
		}
		answer := renderer.FinalAnswer(record)
		if !renderer.SawDelta() {
			fmt.Fprintln(stdout, answer)
		} else if !strings.HasSuffix(answer, "\n") {
			fmt.Fprintln(stdout)
		}
		if record.Status == "failed" {
			if record.Error != "" {
				return fmt.Errorf("%s", record.Error)
			}
			return errors.New("task failed")
		}
		if record.Status == "cancelled" {
			return errors.New("task cancelled")
		}
		return nil
	}
}

func runCreate(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("create", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	workspace := flags.String("workspace", "", "Authorized workspace directory for generated project files")
	planOnly := flags.Bool("plan-only", false, "Plan the generation task without applying file changes")
	if err := flags.Parse(args); err != nil {
		return errors.New("usage: agent create \"<objective>\" --workspace <dir>")
	}
	objective := strings.TrimSpace(strings.Join(flags.Args(), " "))
	if objective == "" {
		return errors.New("usage: agent create \"<objective>\" --workspace <dir>")
	}
	if strings.TrimSpace(*workspace) == "" {
		return errors.New("create requires --workspace <dir>")
	}
	if *planOnly {
		objective = "[PLAN ONLY] " + objective
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
	ctx := context.Background()
	healthCtx, healthCancel := context.WithTimeout(ctx, 30*time.Second)
	if _, err := client.Health(healthCtx); err != nil {
		executable, execErr := os.Executable()
		if execErr != nil {
			healthCancel()
			return execErr
		}
		if startErr := startDetachedServe(executable); startErr != nil {
			healthCancel()
			return startErr
		}
		if err := waitForSupervisor(healthCtx, client); err != nil {
			healthCancel()
			return err
		}
	}
	healthCancel()

	fmt.Fprintf(stderr, "workspace: %s\n", *workspace)
	submitCtx, submitCancel := context.WithTimeout(ctx, 30*time.Second)
	created, err := client.CreateTaskWithContext(submitCtx, id, objective, "", nil, *workspace)
	submitCancel()
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "task %s %s\n", created.ID, created.Status)

	for {
		renderer, cancelStream := streamTaskOutput(ctx, client, created.ID, stdout, stderr)
		record, err := waitForTaskCompletion(ctx, client, created.ID, 15*time.Minute)
		cancelStream()
		time.Sleep(150 * time.Millisecond)
		if err != nil {
			return err
		}
		if taskExecutionStatus(record) == "waiting_for_approval" {
			_, approved, aerr := promptApproval(ctx, client, record, os.Stdin, stdout, stderr)
			if aerr != nil {
				return aerr
			}
			if !approved {
				return errors.New("approval rejected")
			}
			continue
		}
		answer := renderer.FinalAnswer(record)
		if !renderer.SawDelta() {
			fmt.Fprintln(stdout, answer)
		} else if !strings.HasSuffix(answer, "\n") {
			fmt.Fprintln(stdout)
		}
		if record.Status == "failed" {
			if record.Error != "" {
				return fmt.Errorf("%s", record.Error)
			}
			return errors.New("task failed")
		}
		if record.Status == "cancelled" {
			return errors.New("task cancelled")
		}
		return nil
	}
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

func runChat(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("chat", flag.ContinueOnError)
	flags.SetOutput(stderr)
	resumeID := flags.String("resume", "", "resume an existing conversation ID")
	list := flags.Bool("list", false, "list recent conversations")
	historyID := flags.String("history", "", "show history for a conversation ID")
	limit := flags.Int("limit", 20, "maximum records/messages to show")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if *limit <= 0 || *limit > 200 {
		return errors.New("chat --limit must be between 1 and 200")
	}

	if err := runtimeenv.EnsureDataDirectories(); err != nil {
		return err
	}
	client, err := localclient.New()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := ensureSupervisorReady(ctx, client); err != nil {
		return err
	}

	if *list {
		return chatList(ctx, client, *limit, stdout)
	}
	if *historyID != "" {
		if !persistence.ValidConversationID(*historyID) {
			return fmt.Errorf("invalid conversation ID %q", *historyID)
		}
		return chatHistory(ctx, client, *historyID, *limit, stdout)
	}

	conversationID := strings.TrimSpace(*resumeID)
	if conversationID != "" {
		if !persistence.ValidConversationID(conversationID) {
			return fmt.Errorf("invalid conversation ID %q", conversationID)
		}
		if _, err := client.GetConversation(ctx, conversationID); err != nil {
			return fmt.Errorf("conversation %q not found or unavailable: %w", conversationID, err)
		}
	} else {
		conversationID, err = newConversationID()
		if err != nil {
			return err
		}
		if _, err := client.CreateConversation(ctx, conversationID, ""); err != nil {
			return fmt.Errorf("create conversation: %w", err)
		}
	}

	// One-shot mode: remaining args are treated as a single prompt.
	prompt := strings.TrimSpace(strings.Join(flags.Args(), " "))
	if prompt != "" {
		return chatTurn(context.Background(), client, conversationID, prompt, stdout, stderr)
	}

	// Interactive mode.
	if !isTerminal(os.Stdin) {
		// Non-interactive stdin: read all input as one prompt.
		data, err := io.ReadAll(os.Stdin)
		if err != nil {
			return err
		}
		prompt = strings.TrimSpace(string(data))
		if prompt == "" {
			return errors.New("chat requires a prompt, --resume, --list, or --history")
		}
		return chatTurn(context.Background(), client, conversationID, prompt, stdout, stderr)
	}

	fmt.Fprintf(stdout, "CRANE chat (%s)\n", conversationID)
	fmt.Fprintln(stdout, "Type /exit to quit, /history to show messages, /id to print conversation ID.")
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 0, 64*1024), 2*1024*1024)
	for {
		fmt.Fprint(stdout, "crane> ")
		if !scanner.Scan() {
			if err := scanner.Err(); err != nil {
				return err
			}
			fmt.Fprintln(stdout)
			return nil
		}
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		switch line {
		case "/exit", "/quit":
			return nil
		case "/id":
			fmt.Fprintln(stdout, conversationID)
			continue
		case "/history":
			if err := chatHistory(context.Background(), client, conversationID, *limit, stdout); err != nil {
				fmt.Fprintf(stderr, "history error: %v\n", err)
			}
			continue
		}
		if err := chatTurn(context.Background(), client, conversationID, line, stdout, stderr); err != nil {
			fmt.Fprintf(stderr, "CRANE: %v\n", err)
		}
	}
}

func isTerminal(f *os.File) bool {
	info, err := f.Stat()
	if err != nil {
		return false
	}
	return (info.Mode() & os.ModeCharDevice) != 0
}

func newConversationID() (string, error) {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return fmt.Sprintf("conv-%s-%s", time.Now().UTC().Format("20060102T150405.000000000"), hex.EncodeToString(buf)), nil
}

func ensureSupervisorReady(ctx context.Context, client *localclient.Client) error {
	if _, err := client.Health(ctx); err == nil {
		return nil
	}
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	if err := startDetachedServe(executable); err != nil {
		return err
	}
	waitCtx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	return waitForSupervisor(waitCtx, client)
}

func chatList(ctx context.Context, client *localclient.Client, limit int, stdout io.Writer) error {
	conversations, err := client.ListConversations(ctx, limit)
	if err != nil {
		return err
	}
	if len(conversations) == 0 {
		fmt.Fprintln(stdout, "No conversations.")
		return nil
	}
	for _, c := range conversations {
		title := c.Title
		if title == "" {
			title = "(untitled)"
		}
		fmt.Fprintf(stdout, "%s\t%s\tmessages=%d\tupdated=%s\n", c.ID, title, c.MessageCount, c.UpdatedAt)
	}
	return nil
}

func chatHistory(ctx context.Context, client *localclient.Client, conversationID string, limit int, stdout io.Writer) error {
	messages, err := client.GetConversationMessages(ctx, conversationID, limit)
	if err != nil {
		return err
	}
	if len(messages) == 0 {
		fmt.Fprintln(stdout, "No messages.")
		return nil
	}
	for _, m := range messages {
		fmt.Fprintf(stdout, "[%s] %s: %s\n", m.CreatedAt, m.Role, m.Content)
	}
	return nil
}

type taskStreamRenderer struct {
	stdout   io.Writer
	stderr   io.Writer
	tty      bool
	mu       sync.Mutex
	sawDelta bool
	deltaBuf strings.Builder
	lastLine string
}

func newTaskStreamRenderer(stdout, stderr io.Writer) *taskStreamRenderer {
	return &taskStreamRenderer{stdout: stdout, stderr: stderr, tty: isTerminalWriter(stdout)}
}

func isTerminalWriter(w io.Writer) bool {
	f, ok := w.(*os.File)
	if !ok {
		return false
	}
	info, err := f.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

func (r *taskStreamRenderer) HandleEvent(ev localclient.TaskEvent) {
	r.mu.Lock()
	defer r.mu.Unlock()
	switch ev.Type {
	case "assistant.delta":
		var p struct {
			Delta string `json:"delta"`
			Text  string `json:"text"`
		}
		_ = json.Unmarshal(ev.Payload, &p)
		delta := p.Delta
		if delta == "" {
			delta = p.Text
		}
		if delta == "" {
			return
		}
		r.sawDelta = true
		r.deltaBuf.WriteString(delta)
		fmt.Fprint(r.stdout, delta)
	case "assistant.completed":
		// Final assistant message is persisted by task result; avoid duplicate print.
	default:
		line := renderProgressEvent(ev)
		if line == "" || line == r.lastLine {
			return
		}
		r.lastLine = line
		if r.tty {
			// Keep progress separate from streamed assistant text.
			if r.sawDelta {
				fmt.Fprintln(r.stderr, "\n"+line)
			} else {
				fmt.Fprintln(r.stderr, line)
			}
		} else {
			fmt.Fprintf(r.stdout, "event\t%s\t%s\n", ev.Type, line)
		}
	}
}

func (r *taskStreamRenderer) SawDelta() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.sawDelta
}

func (r *taskStreamRenderer) FinalAnswer(record persistence.TaskRecord) string {
	r.mu.Lock()
	sawDelta := r.sawDelta
	deltaText := strings.TrimSpace(r.deltaBuf.String())
	r.mu.Unlock()
	if sawDelta && deltaText != "" {
		return deltaText
	}
	answer := strings.TrimSpace(record.Result)
	var parsed map[string]any
	if err := json.Unmarshal([]byte(record.Result), &parsed); err == nil {
		if mo, _ := parsed["model_output"].(string); strings.TrimSpace(mo) != "" {
			answer = strings.TrimSpace(mo)
		} else if summary, _ := parsed["summary"].(string); strings.TrimSpace(summary) != "" {
			answer = strings.TrimSpace(summary)
		}
	}
	if answer == "" {
		answer = "(no result)"
	}
	return answer
}

func renderProgressEvent(ev localclient.TaskEvent) string {
	var p map[string]any
	_ = json.Unmarshal(ev.Payload, &p)
	get := func(k string) string {
		v, _ := p[k].(string)
		return v
	}
	switch ev.Type {
	case "snapshot":
		return ""
	case "task_admitted":
		return "queued"
	case "task_starting":
		return "starting"
	case "checkpoint":
		step := get("step")
		state := get("state")
		if step != "" {
			return "step: " + step
		}
		if state != "" {
			return "state: " + state
		}
		return "checkpoint"
	case "task_finished":
		status := get("status")
		if status != "" {
			return "finished: " + status
		}
		return "finished"
	case "plan_created", "plan_repaired":
		return "plan ready"
	case "step_selected":
		if id := get("step_id"); id != "" {
			return "selected step: " + id
		}
		return "step selected"
	case "step_started":
		if id := get("step_id"); id != "" {
			return "running step: " + id
		}
		return "step started"
	case "tool_selected":
		if name := get("tool_name"); name != "" {
			return "tool: " + name
		}
		return "tool selected"
	case "mcp_tool_result":
		name := get("tool_name")
		status := get("status")
		if name != "" {
			return "tool result: " + name + " (" + status + ")"
		}
		return "tool result"
	case "approval_required", "waiting_approval":
		return "waiting for approval"
	case "recovery_attempt", "replanned":
		return "recovering/replanning"
	case "provider_result":
		return "model response received"
	case "provider_failed":
		return "model provider failed"
	case "validation_started":
		return "validation started"
	case "validation_completed":
		if status := get("status"); status != "" {
			return "validation: " + status
		}
		return "validation completed"
	case "generation.file.staged", "generation.file.written":
		if path := get("path"); path != "" {
			return ev.Type + ": " + path
		}
		return ev.Type
	case "task_event":
		return ""
	default:
		if strings.HasPrefix(ev.Type, "assistant.") {
			return ""
		}
		// Keep unknown events quiet by default to avoid flooding.
		return ""
	}
}

func streamTaskOutput(ctx context.Context, client *localclient.Client, taskID string, stdout, stderr io.Writer) (*taskStreamRenderer, context.CancelFunc) {
	renderer := newTaskStreamRenderer(stdout, stderr)
	streamCtx, cancel := context.WithCancel(ctx)
	go func() {
		_ = client.StreamTaskEvents(streamCtx, taskID, 0, func(ev localclient.TaskEvent) error {
			renderer.HandleEvent(ev)
			return nil
		})
	}()
	return renderer, cancel
}

func chatTurn(ctx context.Context, client *localclient.Client, conversationID, prompt string, stdout, stderr io.Writer) error {
	if _, err := client.AppendConversationMessage(ctx, conversationID, "user", prompt, ""); err != nil {
		return fmt.Errorf("persist user message: %w", err)
	}
	recent, err := client.GetConversationMessages(ctx, conversationID, 40)
	if err != nil {
		return fmt.Errorf("load conversation context: %w", err)
	}
	contextMessages := make([]localclient.ConversationMessage, 0, len(recent))
	for _, m := range recent {
		contextMessages = append(contextMessages, localclient.ConversationMessage{Role: m.Role, Content: m.Content})
	}

	taskID, err := newTaskID()
	if err != nil {
		return err
	}
	submitCtx, submitCancel := context.WithTimeout(ctx, 30*time.Second)
	_, err = client.CreateTaskWithContext(submitCtx, taskID, prompt, conversationID, contextMessages, "")
	submitCancel()
	if err != nil {
		return fmt.Errorf("submit chat task: %w", err)
	}

	for {
		renderer, cancelStream := streamTaskOutput(ctx, client, taskID, stdout, stderr)
		record, err := waitForTaskCompletion(ctx, client, taskID, 10*time.Minute)
		cancelStream()
		time.Sleep(150 * time.Millisecond)
		if err != nil {
			return err
		}
		if taskExecutionStatus(record) == "waiting_for_approval" {
			rec, approved, aerr := promptApproval(ctx, client, record, os.Stdin, stdout, stderr)
			if aerr != nil {
				return aerr
			}
			if !approved {
				_, _ = client.AppendConversationMessage(ctx, conversationID, "assistant", "Approval rejected.", `{"task_id":"`+taskID+`","status":"failed"}`)
				return errors.New("approval rejected")
			}
			_ = rec
			continue
		}
		if record.Status == "failed" {
			msg := record.Error
			if msg == "" {
				msg = "task failed"
			}
			_, _ = client.AppendConversationMessage(ctx, conversationID, "assistant", "Error: "+msg, `{"task_id":"`+taskID+`","status":"failed"}`)
			return fmt.Errorf("%s", msg)
		}
		if record.Status == "cancelled" {
			_, _ = client.AppendConversationMessage(ctx, conversationID, "assistant", "Task cancelled.", `{"task_id":"`+taskID+`","status":"cancelled"}`)
			return errors.New("task cancelled")
		}
		answer := renderer.FinalAnswer(record)
		if !renderer.SawDelta() {
			fmt.Fprintln(stdout, answer)
		} else if !strings.HasSuffix(answer, "\n") {
			fmt.Fprintln(stdout)
		}
		if _, err := client.AppendConversationMessage(ctx, conversationID, "assistant", answer, `{"task_id":"`+taskID+`","status":"succeeded"}`); err != nil {
			fmt.Fprintf(stderr, "warning: failed to persist assistant message: %v\n", err)
		}
		return nil
	}
}

func taskExecutionStatus(record persistence.TaskRecord) string {
	var payload map[string]any
	if err := json.Unmarshal([]byte(record.Result), &payload); err == nil {
		if s, _ := payload["execution_status"].(string); s != "" {
			return s
		}
	}
	return ""
}

func taskApprovalSummary(record persistence.TaskRecord) string {
	var payload map[string]any
	if err := json.Unmarshal([]byte(record.Result), &payload); err != nil {
		return "Approval required."
	}
	if reason, _ := payload["failure_reason"].(string); strings.TrimSpace(reason) != "" {
		return reason
	}
	if summary, _ := payload["summary"].(string); strings.TrimSpace(summary) != "" {
		return summary
	}
	return "Approval required."
}

func promptApproval(ctx context.Context, client *localclient.Client, record persistence.TaskRecord, stdin io.Reader, stdout, stderr io.Writer) (persistence.TaskRecord, bool, error) {
	if !isTerminal(os.Stdin) {
		return record, false, fmt.Errorf("task %s requires approval: %s", record.ID, taskApprovalSummary(record))
	}
	fmt.Fprintf(stderr, "\nApproval required for task %s\n", record.ID)
	fmt.Fprintf(stderr, "Reason: %s\n", taskApprovalSummary(record))
	fmt.Fprint(stderr, "Approve? [y/N]: ")
	reader := bufio.NewReader(stdin)
	line, err := reader.ReadString('\n')
	if err != nil && !errors.Is(err, io.EOF) {
		return record, false, err
	}
	answer := strings.ToLower(strings.TrimSpace(line))
	approved := answer == "y" || answer == "yes"
	scope := "user_approved"
	rec, err := client.ApproveTask(ctx, record.ID, approved, scope)
	if err != nil {
		return record, false, err
	}
	return rec, approved, nil
}

func waitForTaskCompletion(ctx context.Context, client *localclient.Client, taskID string, timeout time.Duration) (persistence.TaskRecord, error) {
	waitCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		record, err := client.GetTask(waitCtx, taskID)
		if err == nil {
			switch record.Status {
			case "succeeded", "failed", "cancelled":
				return record, nil
			}
		}
		select {
		case <-waitCtx.Done():
			if err != nil {
				return persistence.TaskRecord{}, err
			}
			return persistence.TaskRecord{}, fmt.Errorf("timed out waiting for task %s", taskID)
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
	if record.Result != "" {
		var result map[string]any
		if err := json.Unmarshal([]byte(record.Result), &result); err == nil {
			if es, _ := result["execution_status"].(string); es != "" {
				fmt.Fprintf(stdout, "ExecutionStatus: %s\n", es)
			}
			if summary, _ := result["summary"].(string); summary != "" {
				fmt.Fprintf(stdout, "Summary: %s\n", summary)
			}
		}
	}
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

func runAuth(args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		return errors.New("auth requires a subcommand: login, status, logout")
	}
	switch args[0] {
	case "login":
		return runAuthLogin(args[1:], stdout, stderr)
	case "status":
		return runAuthStatus(args[1:], stdout, stderr)
	case "logout":
		return runAuthLogout(args[1:], stdout, stderr)
	default:
		return fmt.Errorf("unknown auth subcommand %q", args[0])
	}
}

func githubAuthConfig(clientID string) ghauth.Config {
	cfg := ghauth.Config{
		ClientID: strings.TrimSpace(clientID),
		DataDir:  filepath.Join(runtimeenv.DataRoot(), "auth"),
	}
	if cfg.ClientID == "" {
		cfg.ClientID = ghauth.ClientIDFromEnv()
	}
	return cfg
}

func runAuthLogin(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("auth login", flag.ContinueOnError)
	flags.SetOutput(stderr)
	clientID := flags.String("client-id", "", "GitHub OAuth client ID (or set CRANE_GITHUB_CLIENT_ID)")
	scope := flags.String("scope", "read:user", "OAuth scope")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("auth login accepts no positional arguments")
	}
	if err := runtimeenv.EnsureDataDirectories(); err != nil {
		return err
	}
	cfg := githubAuthConfig(*clientID)
	cfg.Scope = strings.TrimSpace(*scope)
	if cfg.Scope == "" {
		cfg.Scope = "read:user"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	dc, err := ghauth.StartDeviceFlow(ctx, cfg)
	if err != nil {
		return err
	}
	fmt.Fprintln(stdout, "GitHub authentication required.")
	fmt.Fprintf(stdout, "Visit: %s\n", dc.VerificationURI)
	fmt.Fprintf(stdout, "Code: %s\n", dc.UserCode)
	fmt.Fprintln(stdout, "Waiting for authorization...")

	token, err := ghauth.PollForToken(ctx, cfg, dc)
	if err != nil {
		return err
	}
	if err := ghauth.SaveToken(cfg, token); err != nil {
		return fmt.Errorf("save GitHub token: %w", err)
	}
	fmt.Fprintln(stdout, "GitHub authentication complete. Token stored securely.")
	return nil
}

func runAuthStatus(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("auth status", flag.ContinueOnError)
	flags.SetOutput(stderr)
	validate := flags.Bool("validate", false, "validate stored token with GitHub API")
	clientID := flags.String("client-id", "", "GitHub OAuth client ID (optional)")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("auth status accepts no positional arguments")
	}
	cfg := githubAuthConfig(*clientID)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	st, err := ghauth.GetStatus(ctx, cfg, *validate)
	if err != nil {
		return err
	}
	if !st.Authenticated {
		fmt.Fprintln(stdout, "GitHub: not authenticated")
		return nil
	}
	fmt.Fprintln(stdout, "GitHub: authenticated")
	if st.Login != "" {
		fmt.Fprintf(stdout, "Login: %s\n", st.Login)
	}
	if st.Scope != "" {
		fmt.Fprintf(stdout, "Scope: %s\n", st.Scope)
	}
	if st.ObtainedAt != "" {
		fmt.Fprintf(stdout, "Obtained: %s\n", st.ObtainedAt)
	}
	if *validate {
		if st.Validated {
			fmt.Fprintln(stdout, "Validation: ok")
		} else if st.ValidationErr != "" {
			fmt.Fprintf(stdout, "Validation: %s\n", st.ValidationErr)
		}
	}
	return nil
}

func runAuthLogout(args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("auth logout", flag.ContinueOnError)
	flags.SetOutput(stderr)
	clientID := flags.String("client-id", "", "GitHub OAuth client ID (optional)")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if flags.NArg() != 0 {
		return errors.New("auth logout accepts no positional arguments")
	}
	cfg := githubAuthConfig(*clientID)
	if err := ghauth.DeleteToken(cfg); err != nil {
		return err
	}
	fmt.Fprintln(stdout, "GitHub token removed.")
	return nil
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
	return startDetachedUninstall(extra, stdout)
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
	cmd.Env = lifecycleEnvironment(executable)
	return cmd.Run()
}

func startDetachedUninstall(extra []string, stdout io.Writer) error {
	executable, err := powershellExecutable()
	if err != nil {
		return err
	}
	script, err := lifecycleScript("uninstall.ps1")
	if err != nil {
		return err
	}
	contents, err := os.ReadFile(script)
	if err != nil {
		return err
	}
	temporaryDirectory, err := os.MkdirTemp("", "crane-uninstall-")
	if err != nil {
		return err
	}
	temporaryScript := filepath.Join(temporaryDirectory, filepath.Base(script))
	if err := os.WriteFile(temporaryScript, contents, 0700); err != nil {
		return err
	}
	logPath := filepath.Join(temporaryDirectory, "uninstall.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		return err
	}

	params := []string{
		"-NoProfile", "-ExecutionPolicy", "Bypass", "-NonInteractive",
		"-File", temporaryScript,
	}
	params = append(params, extra...)
	cmd := exec.Command(executable, params...)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.Stdin = nil
	cmd.Env = lifecycleEnvironment(executable)
	if err := startDetachedCommand(cmd); err != nil {
		_ = logFile.Close()
		return err
	}
	_ = logFile.Close()
	fmt.Fprintf(stdout, "CRANE uninstall started (log: %s).\n", logPath)
	return nil
}

func lifecycleEnvironment(executable string) []string {
	details := runtimeenv.Discover()
	windowsPowerShell := isWindowsPowerShellHost(executable)
	environment := make([]string, 0, len(os.Environ())+1)
	for _, entry := range os.Environ() {
		key := strings.SplitN(entry, "=", 2)[0]
		if strings.EqualFold(key, "CRANE_INSTALL_ROOT") {
			continue
		}
		if windowsPowerShell && shouldDropForWindowsPowerShell(key) {
			continue
		}
		environment = append(environment, entry)
	}
	return append(environment, "CRANE_INSTALL_ROOT="+details.InstallRoot)
}

func isWindowsPowerShellHost(executable string) bool {
	base := strings.ToLower(filepath.Base(strings.TrimSpace(executable)))
	return base == "powershell.exe" || base == "powershell"
}

func shouldDropForWindowsPowerShell(key string) bool {
	switch strings.ToUpper(key) {
	case "PSMODULEPATH", "PSHOME", "POWERSHELL_DISTRIBUTION_CHANNEL":
		return true
	default:
		return false
	}
}

func powershellExecutable() (string, error) {
	for _, name := range []string{"powershell.exe", "pwsh"} {
		if path, err := exec.LookPath(name); err == nil {
			return path, nil
		}
	}
	return "", errors.New("PowerShell is required for lifecycle commands")
}
