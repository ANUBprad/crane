package supervisor

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

// Supervisor owns the CRANE runtime lifecycle: task admission, lifecycle
// transitions, local IPC coordination, policy gate, and graceful shutdown.
type Supervisor struct {
	// taskMgr   task.Manager
	// api        *localapi.Server
	// mcpMgr     *manager.Manager
	// policy     *policy.Evaluator
	// cancel     context.CancelFunc
	//            done
}

// New creates a new Supervisor with default configuration.
func New() *Supervisor {
	return &Supervisor{}
}

// Start begins the supervisor lifecycle. It initializes all components,
// begins background execution coordination, and blocks until a shutdown
// signal is received.
func (s *Supervisor) Start() error {
	fmt.Println("CRANE supervisor starting…")

	// Set up signal handling for graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	// Block until a shutdown signal is received
	<-sigChan

	fmt.Println("Shutting down gracefully…")
	return nil
}

// Stop performs graceful shutdown: finish in-flight work, persist state,
// clean up child processes, and release resources.
func (s *Supervisor) Stop() {
	// TODO: persist task state, clean up resources
	fmt.Println("CRANE supervisor stopped")
}

// StopWithContext performs graceful shutdown with a context that can be
// used to signal child goroutines. It does not block; the context is
// available for child goroutines to check cancellation.
func (s *Supervisor) StopWithContext(ctx context.Context) error {
	// TODO: implement context-aware shutdown
	// - signal child goroutines via ctx.Done()
	// - wait for completion
	return nil
}

// Version returns the current supervisor version.
func Version() string {
	return "0.1.0"
}

// HealthCheck performs a basic health check of supervisor components.

// HealthCheck performs a basic health check of supervisor components.
func HealthCheck() error {
	// TODO: implement component health checks
	return nil
}
