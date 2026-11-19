package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
)

// version string — injected at build time via -ldflags
var version = "dev"

func main() {
	versionFlag := flag.Bool("version", false, "print version and exit")
	flag.Parse()

	if *versionFlag {
		fmt.Println("CRANE version:", version)
		os.Exit(0)
	}

	// Supervisor lifecycle
	// - Start: initialize components, begin task processing
	// - Running: handle signals, keep process alive
	// - Graceful shutdown: finish in-flight work, exit cleanly

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)

	// Initialize the supervisor
	supervisor := newSupervisor()

	// Start the supervisor
	if err := supervisor.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to start supervisor: %v\n", err)
		os.Exit(1)
	}

	// Wait for shutdown signal
	<-sigChan

	// Graceful shutdown
	fmt.Println("\nShutting down gracefully...")
	supervisor.Stop()

	fmt.Println("CRANE supervisor stopped.")
}

func newSupervisor() *supervisor {
	return &supervisor{
		// TODO: initialize Go supervisor components
		// - task admission / lifecycle coordination
		// - local API/IPC endpoint
		// - MCP manager integration point
		// - policy gate
	}
}

type supervisor struct {
	// Supervisor state and components
}

func (s *supervisor) Start() error {
	// TODO: supervisor startup logic
	// - Initialize task lifecycle
	// - Configure local API/IPC
	// - Load MCP server configurations
	// - Begin background execution coordination
	fmt.Println("CRANE supervisor starting...")
	return nil
}

func (s *supervisor) Stop() {
	// TODO: graceful shutdown
	// - Finish in-flight work
	// - Persist task state
	// - Clean up child processes
	// - Release resources
	fmt.Println("CRANE supervisor stopping...")
}
