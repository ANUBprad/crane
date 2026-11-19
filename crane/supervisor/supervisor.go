package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"crane/crane/python_protocol"
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
	// cancel     context.CancelFunc
	//            done
}

// New creates a new Supervisor with default configuration.
func New() *Supervisor {
	return &Supervisor{
		// pythonRuntimePath: "",
		// pythonArgs:        []string{},
		// shutdownTimeout:   10 * time.Second,
	}
}

// WithPythonRuntime sets the path to the Python runtime executable and
// optional arguments. This is typically used for development with an
// installed Python interpreter.
func (s *Supervisor) WithPythonRuntime(path string, args ...string) *Supervisor {
	s.pythonRuntimePath = path
	s.pythonArgs = args
	return s
}

// WithShutdownTimeout sets the maximum time to wait for graceful shutdown.
func (s *Supervisor) WithShutdownTimeout(timeout time.Duration) *Supervisor {
	s.shutdownTimeout = timeout
	return s
}

// Start begins the supervisor lifecycle. It initializes all components,
// begins background execution coordination, and blocks until a shutdown
// signal is received.
func (s *Supervisor) Start() error {
	fmt.Println("CRANE supervisor starting…")

	// If a Python runtime is configured, launch it and perform handshake
	if s.pythonRuntimePath != "" {
		if err := s.launchPythonRuntime(); err != nil {
			return fmt.Errorf("failed to launch Python runtime: %w", err)
		}
	}

	// Set up signal handling for graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	// Block until a shutdown signal is received
	<-sigChan

	// Graceful shutdown
	fmt.Println("Shutting down gracefully…")
	s.Stop()
	return nil
}

// Stop performs graceful shutdown: finish in-flight work, persist state,
// clean up child processes, and release resources.
func (s *Supervisor) Stop() {
	// TODO: persist task state, clean up resources
	// In a full implementation, this would notify the Python runtime
	// to shut down its MCP connections gracefully
	fmt.Println("CRANE supervisor stopped")
}

// launchPythonRuntime launches the Python runtime child process and
// performs the startup handshake using the versioned JSON protocol.
func (s *Supervisor) launchPythonRuntime() error {
	// Resolve the Python runtime executable
	runtimePath := s.pythonRuntimePath
	if runtimePath == "" {
		// Development default: look for the built agent binary
		runtimePath = "agent"
	}

	// Build the command
	cmdArgs := s.pythonArgs

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
	cmd.Stdin = strings.NewReader("")
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	// Start the process
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("failed to start Python runtime: %w", err)
	}

	fmt.Printf("Python runtime started with PID %d\n", cmd.Process.Pid)

	// Perform startup handshake with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Send a health check message
	healthMsg := fmt.Sprintf(`{"version":"%s","type":"health_check"}`, python_protocol.ProtocolVersion)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		cmd.Process.Kill()
		return fmt.Errorf("failed to get stdin pipe: %w", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		stdin.Close()
		cmd.Process.Kill()
		return fmt.Errorf("failed to get stdout pipe: %w", err)
	}

	// Send health check message
	if _, err := fmt.Fprint(stdin, healthMsg+"\n"); err != nil {
		stdin.Close()
		cmd.Process.Kill()
		return fmt.Errorf("failed to send health check: %w", err)
	}
	stdin.Close()

	// Read health check response with timeout
	var healthResp struct {
		Protocol string `json:"protocol"`
		Status   string `json:"status"`
		Version  string `json:"version"`
		Python   string `json:"python_version"`
	}

	// Read from stdout with context timeout
	done := make(chan error, 1)
	go func() {
		if err := json.NewDecoder(stdout).Decode(&healthResp); err != nil {
			done <- fmt.Errorf("failed to decode health response: %w", err)
			return
		}
		done <- nil
	}()

	select {
	case <-ctx.Done():
		cmd.Process.Kill()
		return fmt.Errorf("Python runtime health check timed out after 5s")
	case err := <-done:
		if err != nil {
			return err
		}
		fmt.Printf("Python runtime handshake successful: protocol=%s, status=%s, version=%s, python=%s\n",
			healthResp.Protocol, healthResp.Status, healthResp.Version, healthResp.Python)
	}

	// Wait for the process to finish (it won't unless killed)
	cmd.Wait()

	return nil
}

// handleProtocolMessage processes incoming protocol messages from the Python runtime.
// This is called from the Go supervisor when communicating via stdin/stdout.
func (s *Supervisor) handleProtocolMessage(rawMsg json.RawMessage) error {
	var msg python_protocol.Message
	if err := json.Unmarshal(rawMsg, &msg); err != nil {
		return fmt.Errorf("failed to parse protocol message: %w", err)
	}

	switch msg.Type {
	case python_protocol.TypeHealthCheck:
		// Health check response already handled during startup
		return nil

	case python_protocol.TypeRegisterServer:
		var registerMsg python_protocol.RegisterServer
		if err := json.Unmarshal(rawMsg, &registerMsg); err != nil {
			return fmt.Errorf("failed to parse register_server message: %w", err)
		}
		// TODO: Implement server registration via Go/Python protocol
		// For now, acknowledge the registration
		fmt.Printf("MCP server registration requested: %s (transport: %s)\n",
			registerMsg.ServerName, registerMsg.Transport)
		return nil

	case python_protocol.TypeRemoveServer:
		var removeMsg python_protocol.RemoveServer
		if err := json.Unmarshal(rawMsg, &removeMsg); err != nil {
			return fmt.Errorf("failed to parse remove_server message: %w", err)
		}
		// TODO: Implement server removal via Go/Python protocol
		fmt.Printf("MCP server removal requested: %s\n", removeMsg.ServerName)
		return nil

	case python_protocol.TypeListServers:
		// List all registered MCP servers
		// TODO: Query the MCP manager and return server list
		// For now, return empty list
		servers := []python_protocol.Message{}
		data, _ := json.Marshal(map[string]interface{}{
			"servers": servers,
		})
		fmt.Println(string(data))
		return nil

	case python_protocol.TypeDiscoverTools:
		var discoverMsg python_protocol.DiscoverTools
		if err := json.Unmarshal(rawMsg, &discoverMsg); err != nil {
			return fmt.Errorf("failed to parse discover_tools message: %w", err)
		}
		// TODO: Implement tool discovery via Go/Python protocol
		// For now, acknowledge the request
		fmt.Printf("Tool discovery requested from server: %s\n", discoverMsg.ServerName)
		return nil

	case python_protocol.TypeInvokeTool:
		var invokeMsg python_protocol.InvokeTool
		if err := json.Unmarshal(rawMsg, &invokeMsg); err != nil {
			return fmt.Errorf("failed to parse invoke_tool message: %w", err)
		}
		// TODO: Implement tool invocation via Go/Python protocol
		// For now, acknowledge the invocation request
		fmt.Printf("Tool invocation requested: %s on server %s\n", invokeMsg.ToolName, invokeMsg.ServerName)
		return nil

	case python_protocol.TypeToolResult:
		var toolResult python_protocol.ToolResult
		if err := json.Unmarshal(rawMsg, &toolResult); err != nil {
			return fmt.Errorf("failed to parse tool_result message: %w", err)
		}
		// TODO: Handle tool result from Python runtime
		fmt.Printf("Tool result from %s: success=%v, error=%s\n",
			toolResult.ServerName, toolResult.Success, toolResult.Error)
		return nil

	default:
		return fmt.Errorf("unknown protocol message type: %s", msg.Type)
	}
}

// RequestShutdown sends a shutdown request to the Python runtime and waits
// for graceful shutdown within the configured timeout.
func (s *Supervisor) RequestShutdown() error {
	if s.pythonRuntimePath == "" {
		return nil // No Python runtime to shut down
	}

	fmt.Println("Python runtime shutdown requested")
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