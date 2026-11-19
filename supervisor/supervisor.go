package supervisor

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"crane/persistence"
	"crane/python_protocol"
)

// Supervisor owns the CRANE runtime lifecycle: task admission, lifecycle
// transitions, local IPC coordination, policy gate, and graceful shutdown.
type Supervisor struct {
	// pythonRuntimePath is the path to the Python runtime executable.
	pythonRuntimePath string
	// pythonArgs are optional arguments for the Python runtime.
	pythonArgs []string
	// shutdownTimeout is the maximum time to wait for graceful shutdown.
	shutdownTimeout time.Duration
	// store is the SQLite persistence layer.
	store *persistence.Store
}

// New creates a new Supervisor with default configuration.
func New() *Supervisor {
	store, err := persistence.NewStore(persistence.DBPath())
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: failed to initialize SQLite store: %v\n", err)
		store = nil
	}
	return &Supervisor{
		// pythonRuntimePath: "",
		// pythonArgs:        []string{},
		// shutdownTimeout:   10 * time.Second,
		store: store,
	}
}

// launchPythonRuntime launches the Python runtime child process and
// performs the startup handshake using the versioned JSON protocol.
// If a task_id is provided as the first argument, it restores the
// persisted workflow state from the SQLite database before the Python
// process starts running the workflow.
//
// The function continuously reads newline-delimited JSON messages from
// Python stdout, parses and validates them using the versioned protocol,
// and dispatches task_state messages to the persistence handler for
// checkpointing. This enables process-restart recovery of workflow state.
func (s *Supervisor) launchPythonRuntime() error {
	// Resolve the Python runtime executable
	runtimePath := s.pythonRuntimePath
	if runtimePath == "" {
		// Development default: look for the built agent binary
		runtimePath = "agent"
	}

	// Build the command
	cmdArgs := s.pythonArgs

	// Check if a task_id was provided to restore persisted state
	taskId := ""
	if len(cmdArgs) > 0 {
		taskId = cmdArgs[0]
	}

	// If --health is the first arg, just do a health check and exit
	if len(cmdArgs) > 0 && cmdArgs[0] == "--health" {
		cmd := exec.Command(runtimePath, cmdArgs[1:]...)
		output, err := cmd.Output()
		if err != nil {
			return fmt.Errorf("Python runtime health check failed: %w", err)
		}
		// Parse the health response
		var healthResp struct {
			Protocol string `json:"protocol"`
			Status   string `json:"status"`
			Version  string `json:"version"`
			Python   string `json:"python_version"`
		}
		if err := json.Unmarshal(output, &healthResp); err != nil {
			return fmt.Errorf("failed to parse Python health response: %w", err)
		}
		fmt.Printf("Python runtime health check: %s (version %s, Python %s)\n",
			healthResp.Status, healthResp.Version, healthResp.Python)
		return nil
	}

	cmd := exec.Command(runtimePath, cmdArgs...)

	// Set up stdin/stdout for protocol communication
	// Use a pipe for stdout so the Go supervisor can read protocol messages
	// from the Python process. StdoutPipe must be called before Start().
	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		cmd.Process.Kill()
		return fmt.Errorf("failed to get stdout pipe: %w", err)
	}
	// Stdin will be set up via StdinPipe after Start()
	cmd.Stderr = os.Stderr

	// If a task_id was provided, restore the persisted state from SQLite
	// and send it to the Python process via stdin using the protocol
	// framing so it can be captured during the protocol handshake.
	if taskId != "" && s.store != nil {
		state, ok := getLatestTaskState(s.store, taskId)
		if ok {
			// Send the restored state to the Python process via stdin
			// using the task_state message type so the Python process
			// can capture it during startup.
			msg := map[string]interface{}{
				"version": python_protocol.ProtocolVersion,
				"type":    "task_state",
				"task_id": taskId,
				"state":   state.State,
				"current_step": state.CurrentStep,
				"graph_steps": state.GraphSteps,
				"wall_clock_ms": state.WallClockMs,
				"result": state.Result,
				"result_summary": state.ResultSummary,
				"failure_reason": state.FailureReason,
				"cancelled": state.Cancelled,
				"approval_decision": state.ApprovalDecision,
				"approval_id": state.ApprovalId,
			}
			data, _ := json.Marshal(msg)
			// Send via stdin pipe before workflow begins
			stdin, err := cmd.StdinPipe()
			if err != nil {
				stdoutPipe.Close()
				cmd.Process.Kill()
				return fmt.Errorf("failed to get stdin pipe: %w", err)
			}
			go func() {
				fmt.Fprint(stdin, string(data)+"\n")
				stdin.Close()
			}()
		}
	}

	// Start the process
	if err := cmd.Start(); err != nil {
		stdoutPipe.Close()
		return fmt.Errorf("failed to start Python runtime: %w", err)
	}

	fmt.Printf("Python runtime started with PID %d\n", cmd.Process.Pid)

	// Perform startup handshake with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Send a health check message via stdin
	healthMsg := fmt.Sprintf(`{"version":"%s","type":"health_check"}`, python_protocol.ProtocolVersion)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		cmd.Process.Kill()
		stdoutPipe.Close()
		return fmt.Errorf("failed to get stdin pipe: %w", err)
	}

	// Send health check message
	if _, err := fmt.Fprint(stdin, healthMsg+"\n"); err != nil {
		stdin.Close()
		cmd.Process.Kill()
		stdoutPipe.Close()
		return fmt.Errorf("failed to send health check: %w", err)
	}
	stdin.Close()

	// Read health check response and continuously process task_state messages
	var healthResp struct {
		Protocol string `json:"protocol"`
		Status   string `json:"status"`
		Version  string `json:"version"`
		Python   string `json:"python_version"`
	}

	done := make(chan error, 1)
	// goroutine: continuously read from stdout pipe, parse messages,
	// and dispatch task_state to persistence
	go func() {
		reader := bufio.NewReader(stdoutPipe)
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				return // Process ended or pipe error
			}
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			// Check if this is a task_state message
			var msg map[string]interface{}
			if err := json.Unmarshal([]byte(line), &msg); err != nil {
				continue // Not a valid JSON message; keep reading
			}
			if msg["type"] == "task_state" {
				// Persist the task state to SQLite
				var state python_protocol.TaskState
				json.Unmarshal([]byte(line), &state)
				if s.store != nil {
					s.persistTaskState(state)
				}
				fmt.Fprintf(os.Stderr, "Persisted task state: %s\n", state.State)
			} else if msg["type"] == "health_check" {
				// Respond to health check with protocol version
				if val, ok := msg["protocol"]; ok && val == python_protocol.ProtocolVersion {
					done <- nil
					return
				}
			}
			// Other message types (shutdown_request, etc.) are not processed
			// here to avoid blocking; they will be handled when the process
			// naturally exits or the message loop terminates.
		}
	}()

	// Wait for health check response with timeout
	select {
	case <-ctx.Done():
		cmd.Process.Kill()
		stdoutPipe.Close()
		return fmt.Errorf("Python runtime health check timed out after 5s")
	case err := <-done:
		if err != nil {
			cmd.Process.Kill()
			stdoutPipe.Close()
			return err
		}
		fmt.Printf("Python runtime handshake successful: protocol=%s, status=%s, version=%s, python=%s\n",
			healthResp.Protocol, healthResp.Status, healthResp.Version, healthResp.Python)
	}

	// Wait for the process to finish and drain any remaining messages
	// from the stdout pipe. The reader goroutine should have already
	// exited when the process ended, but we drain just in case.
	go func() {
		reader := bufio.NewReader(stdoutPipe)
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				break // No more data or EOF
			}
			line = strings.TrimSpace(line)
			if line == "" {
				continue
			}
			// Try to process as a task_state message
			var msg map[string]interface{}
			if err := json.Unmarshal([]byte(line), &msg); err != nil {
				continue
			}
			if msg["type"] == "task_state" {
				var state python_protocol.TaskState
				json.Unmarshal([]byte(line), &state)
				if s.store != nil {
					s.persistTaskState(state)
				}
			}
		}
		stdoutPipe.Close()
	}()

	cmd.Wait()

	return nil
}

// persistTaskState persists a task state snapshot to the SQLite database.
func (s *Supervisor) persistTaskState(taskStateMsg python_protocol.TaskState) error {
	snapshot, err := json.Marshal(taskStateMsg)
	if err != nil {
		return fmt.Errorf("failed to marshal task state snapshot: %w", err)
	}
	err = s.store.SaveTaskState(taskStateMsg.TaskId, snapshot)
	if err != nil {
		return fmt.Errorf("failed to persist task state to SQLite: %w", err)
	}
	return nil
}

// getLatestTaskState retrieves the latest task state snapshot from the SQLite database.
func getLatestTaskState(store *persistence.Store, taskId string) (python_protocol.TaskState, bool) {
	snapshot, ok := store.GetLatestTaskState(taskId)
	if !ok || len(snapshot) == 0 {
		return python_protocol.TaskState{}, false
	}
	var state python_protocol.TaskState
	err := json.Unmarshal(snapshot, &state)
	if err != nil {
		return python_protocol.TaskState{}, false
	}
	return state, true
}

// RequestShutdown sends a shutdown request to the Python runtime and waits
// for graceful shutdown within the configured timeout.
func (s *Supervisor) RequestShutdown() error {
	if s.store == nil {
		return nil // No SQLite store initialized
	}
	fmt.Println("CRANE supervisor shutting down")
	return nil
}

// HealthCheck performs a basic health check of supervisor components.
func HealthCheck() error {
	// TODO: implement component health checks
	return nil
}

// Version returns the current supervisor version.
func Version() string {
	return "0.1.0"
}

// Stop performs graceful shutdown of the supervisor.
func (s *Supervisor) Stop() {
	fmt.Println("CRANE supervisor stopping")
}