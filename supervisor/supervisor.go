package supervisor

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"crane/internal/runtimeenv"
	"crane/mcpruntime"
	"crane/persistence"
	"crane/python_protocol"
)

const (
	// Python's first import of the LangGraph stack can take several seconds on
	// a cold Windows machine or under build load. Keep the probe bounded, but
	// do not report a healthy runtime as unavailable during that cold start.
	runtimeProbeTimeout = 15 * time.Second
	maxConcurrentTasks  = 2
	// A locally installed CPU-only chat model can take several minutes to load
	// and answer its first request. Keep the bound, but allow real inference.
	taskExecutionTimeout  = 180 * time.Second
	cancelAckTimeout      = 5 * time.Second
	processCleanupTimeout = 3 * time.Second
	maxTaskInputBytes     = 64 << 10
	maxEventSubscribers   = 32
	eventBufferSize       = 128
)

var (
	// ErrTaskNotAdmittable is returned before a task record is created.
	ErrTaskNotAdmittable = errors.New("task is not admittable")
	// ErrTaskNotActive reports a cancellation for a task with no live runtime.
	ErrTaskNotActive = errors.New("task has no active runtime")
)

type conversationContext struct {
	ConversationID string
	Messages       []python_protocol.ConversationMessage
}

type runningTask struct {
	id     string
	cancel context.CancelFunc
	stdin  io.WriteCloser
	stderr *boundedBuffer
	write  sync.Mutex
	done   chan struct{}
	once   sync.Once
}

func (r *runningTask) send(message any) error {
	encoded, err := json.Marshal(message)
	if err != nil {
		return err
	}
	encoded = append(encoded, '\n')
	r.write.Lock()
	defer r.write.Unlock()
	_, err = r.stdin.Write(encoded)
	return err
}

type eventSubscription struct {
	ch   chan persistence.TaskEvent
	once sync.Once
}

func (s *eventSubscription) close() {
	s.once.Do(func() { close(s.ch) })
}

type eventHub struct {
	mu    sync.Mutex
	store *persistence.Store
	subs  map[string]map[*eventSubscription]struct{}
}

func newEventHub(store *persistence.Store) *eventHub {
	return &eventHub{store: store, subs: make(map[string]map[*eventSubscription]struct{})}
}

func (h *eventHub) subscribe(id string, after int64) (<-chan persistence.TaskEvent, func(), error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.subs[id]) >= maxEventSubscribers {
		return nil, nil, errors.New("event subscriber limit reached")
	}
	replay, err := h.store.GetTaskEventsAfter(id, after, eventBufferSize)
	if err != nil {
		return nil, nil, err
	}
	sub := &eventSubscription{ch: make(chan persistence.TaskEvent, eventBufferSize)}
	for _, event := range replay {
		sub.ch <- event
	}
	if h.subs[id] == nil {
		h.subs[id] = make(map[*eventSubscription]struct{})
	}
	h.subs[id][sub] = struct{}{}
	return sub.ch, func() {
		h.mu.Lock()
		defer h.mu.Unlock()
		if subscribers := h.subs[id]; subscribers != nil {
			delete(subscribers, sub)
			if len(subscribers) == 0 {
				delete(h.subs, id)
			}
		}
		sub.close()
	}, nil
}

func (h *eventHub) publish(event persistence.TaskEvent) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for sub := range h.subs[event.TaskID] {
		select {
		case sub.ch <- event:
		default:
			delete(h.subs[event.TaskID], sub)
			sub.close()
		}
	}
}

// Supervisor owns process supervision, task admission and durable lifecycle.
type Supervisor struct {
	pythonRuntimePath string
	pythonArgs        []string
	shutdownTimeout   time.Duration
	store             *persistence.Store
	started           atomic.Bool
	ctx               context.Context
	cancel            context.CancelFunc
	wg                sync.WaitGroup
	limits            chan struct{}
	activeMu          sync.Mutex
	active            map[string]*runningTask
	conversations     map[string]conversationContext
	events            *eventHub
}

// New creates a new Supervisor with the default per-user persistence store.
func New() *Supervisor {
	store, err := persistence.NewStore(persistence.DBPath())
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: failed to initialize SQLite store: %v\n", persistence.RedactMCPValue(err.Error()))
		store = nil
	}
	return NewWithStore(store)
}

// NewWithStore creates a Supervisor around an existing persistence store.
func NewWithStore(store *persistence.Store) *Supervisor {
	ctx, cancel := context.WithCancel(context.Background())
	return &Supervisor{
		store:           store,
		ctx:             ctx,
		cancel:          cancel,
		shutdownTimeout: 10 * time.Second,
		limits:          make(chan struct{}, maxConcurrentTasks),
		active:          make(map[string]*runningTask),
		conversations:   make(map[string]conversationContext),
		events:          newEventHub(store),
	}
}

// Start validates dependencies, reconciles interrupted work and marks the
// supervisor ready. Queued work begins only after startup reconciliation.
func (s *Supervisor) Start() error {
	if s == nil || s.store == nil {
		return errors.New("supervisor persistence store is not initialized")
	}
	if err := s.store.HealthCheck(); err != nil {
		return fmt.Errorf("supervisor store health check: %w", err)
	}
	if s.ctx == nil {
		s.ctx, s.cancel = context.WithCancel(context.Background())
	}
	s.started.Store(true)
	if err := s.recoverInterruptedTasks(); err != nil {
		s.started.Store(false)
		return err
	}
	return nil
}

// IsRunning reports supervisor lifecycle state, not mere API liveness.
func (s *Supervisor) IsRunning() bool {
	return s != nil && s.started.Load()
}

// Store returns the supervisor-owned persistence store.
func (s *Supervisor) Store() *persistence.Store {
	if s == nil {
		return nil
	}
	return s.store
}

// SetPythonRuntime overrides the Python executable used for probes and tasks.
func (s *Supervisor) SetPythonRuntime(path string) {
	if s != nil {
		s.pythonRuntimePath = path
	}
}

// RuntimeAvailable probes the Python agent runtime through its health command.
func (s *Supervisor) RuntimeAvailable() (bool, string) {
	if !s.IsRunning() {
		return false, "supervisor is not running"
	}
	candidates := []string{s.pythonRuntimePath}
	if s.pythonRuntimePath == "" {
		candidates = runtimeenv.PythonCandidates()
	}
	for _, runtimePath := range candidates {
		if runtimePath == "" {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), runtimeProbeTimeout)
		cmd := exec.CommandContext(ctx, runtimePath, "-m", "crane.python_runtime", "--health")
		runtimeenv.Configure(cmd)
		output, err := cmd.Output()
		cancel()
		if err != nil {
			continue
		}
		var response struct {
			Protocol      string `json:"protocol"`
			Status        string `json:"status"`
			Version       string `json:"version"`
			PythonVersion string `json:"python_version"`
		}
		if json.Unmarshal(output, &response) != nil {
			return false, "python runtime returned an invalid health response"
		}
		if response.Status != "healthy" || response.Version != python_protocol.ProtocolVersion ||
			response.Protocol != "crane-agent/"+python_protocol.ProtocolVersion {
			return false, "python runtime health check did not pass"
		}
		detail := "Python"
		if response.PythonVersion != "" {
			detail += " " + response.PythonVersion
		}
		return true, detail
	}
	return false, "python runtime is unavailable"
}

// HealthCheck verifies supervisor state and store responsiveness.
func (s *Supervisor) HealthCheck() error {
	if !s.IsRunning() {
		return errors.New("supervisor is not running")
	}
	return s.store.HealthCheck()
}

// AdmitTask durably accepts one task and then schedules bounded execution.
func (s *Supervisor) AdmitTask(id, input string) (persistence.TaskRecord, error) {
	return s.AdmitTaskWithContext(id, input, "", nil)
}

// AdmitTaskWithContext durably accepts one task with optional conversation context.
func (s *Supervisor) AdmitTaskWithContext(id, input string, conversationID string, conversationMessages []python_protocol.ConversationMessage) (persistence.TaskRecord, error) {
	if !s.IsRunning() {
		return persistence.TaskRecord{}, errors.New("supervisor is not running")
	}
	if !persistence.ValidTaskID(id) {
		return persistence.TaskRecord{}, ErrTaskNotAdmittable
	}
	if strings.TrimSpace(input) == "" || len(input) > maxTaskInputBytes {
		return persistence.TaskRecord{}, ErrTaskNotAdmittable
	}
	if conversationID != "" && !persistence.ValidConversationID(conversationID) {
		return persistence.TaskRecord{}, ErrTaskNotAdmittable
	}
	if len(conversationMessages) > 200 {
		return persistence.TaskRecord{}, ErrTaskNotAdmittable
	}
	if _, err := s.store.CreateTask(id, "queued", input); err != nil {
		return persistence.TaskRecord{}, err
	}
	if conversationID != "" || len(conversationMessages) > 0 {
		s.activeMu.Lock()
		s.conversations[id] = conversationContext{ConversationID: conversationID, Messages: append([]python_protocol.ConversationMessage(nil), conversationMessages...)}
		s.activeMu.Unlock()
	}
	s.emitEvent(id, "task_admitted", map[string]any{"status": "queued"})
	s.enqueue(id, input, nil, false)
	return s.store.GetTaskRecord(id)
}

// CancelTask requests supervisor-owned cancellation and waits for a bounded
// acknowledgement. An unconfirmed outcome is recorded as failed, not cancelled.
func (s *Supervisor) CancelTask(id string) (persistence.TaskRecord, error) {
	if !s.IsRunning() {
		return persistence.TaskRecord{}, errors.New("supervisor is not running")
	}
	if !persistence.ValidTaskID(id) {
		return persistence.TaskRecord{}, persistence.ErrTaskNotFound
	}
	record, err := s.store.GetTaskRecord(id)
	if err != nil {
		return persistence.TaskRecord{}, err
	}
	if isTerminal(record.Status) {
		return record, nil
	}
	changed, err := s.store.TransitionTask(id, "cancelling", "queued", "starting", "running", "waiting_approval")
	if err != nil {
		return persistence.TaskRecord{}, err
	}
	if !changed {
		return s.store.GetTaskRecord(id)
	}
	s.emitEvent(id, "cancellation_requested", map[string]any{"status": "cancelling"})

	s.activeMu.Lock()
	active := s.active[id]
	s.activeMu.Unlock()
	if active == nil {
		if err := s.finalize(id, "cancelled", "", "task cancelled before execution", nil); err != nil {
			return persistence.TaskRecord{}, err
		}
		return s.store.GetTaskRecord(id)
	}
	if err := active.send(python_protocol.TaskCancellation{
		Message: python_protocol.Message{Version: python_protocol.ProtocolVersion, Type: python_protocol.TypeTaskCancellation},
		TaskID:  id,
		Reason:  "supervisor_request",
	}); err != nil {
		active.cancel()
	}
	select {
	case <-active.done:
	case <-time.After(cancelAckTimeout):
		_ = active.finish(s, "failed", `{"code":"cancellation_unconfirmed"}`, "cancellation outcome could not be confirmed", nil)
		active.cancel()
		<-active.done
	}
	return s.store.GetTaskRecord(id)
}

// SubscribeTaskEvents replays persisted events after a cursor and delivers
// live events. Disconnecting never cancels the underlying task.
func (s *Supervisor) SubscribeTaskEvents(id string, after int64) (<-chan persistence.TaskEvent, func(), error) {
	if !s.IsRunning() {
		return nil, nil, errors.New("supervisor is not running")
	}
	if !persistence.ValidTaskID(id) {
		return nil, nil, persistence.ErrTaskNotFound
	}
	if _, err := s.store.GetTaskRecord(id); err != nil {
		return nil, nil, err
	}
	return s.events.subscribe(id, after)
}

func (s *Supervisor) emitEvent(taskID, eventType string, payload any) {
	event, err := s.store.AppendTaskEvent(taskID, eventType, payload)
	if err != nil {
		return
	}
	s.events.publish(persistence.TaskEvent{
		ID: event, TaskID: taskID, Timestamp: time.Now().UTC().Format(time.RFC3339Nano),
		Type: eventType, Payload: mustJSON(payload),
	})
}

func mustJSON(value any) json.RawMessage {
	encoded, err := json.Marshal(value)
	if err != nil {
		return json.RawMessage(`{}`)
	}
	return encoded
}

func (s *Supervisor) enqueue(id, input string, checkpoint []byte, resume bool) {
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		s.executeTask(id, input, checkpoint, resume)
	}()
}

func (s *Supervisor) executeTask(id, input string, checkpoint []byte, resume bool) {
	select {
	case s.limits <- struct{}{}:
	case <-s.ctx.Done():
		return
	}
	defer func() { <-s.limits }()

	record, err := s.store.GetTaskRecord(id)
	if err != nil || isTerminal(record.Status) {
		return
	}
	if record.Status == "cancelling" || record.Status == "cancelled" {
		_ = s.finalize(id, "cancelled", "", "task cancelled before execution", nil)
		return
	}
	changed, err := s.store.TransitionTask(id, "starting", "queued", "starting")
	if err != nil {
		return
	}
	if !changed && record.Status != "starting" && record.Status != "running" {
		return
	}
	s.emitEvent(id, "task_starting", map[string]any{"status": "starting"})

	taskCtx, cancel := context.WithTimeout(s.ctx, taskExecutionTimeout)
	defer cancel()
	cmd := s.pythonCommand(taskCtx)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		_ = s.finalize(id, "failed", "", "unable to create Python runtime input", nil)
		return
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		_ = s.finalize(id, "failed", "", "unable to create Python runtime output", nil)
		return
	}
	stderr := &boundedBuffer{limit: 16 << 10}
	cmd.Stderr = stderr
	if err := cmd.Start(); err != nil {
		_ = stdin.Close()
		_ = s.finalize(id, "failed", "", "unable to start Python runtime", nil)
		return
	}
	task := &runningTask{id: id, cancel: cancel, stdin: stdin, stderr: stderr, done: make(chan struct{})}
	s.activeMu.Lock()
	s.active[id] = task
	s.activeMu.Unlock()
	defer func() {
		s.activeMu.Lock()
		if s.active[id] == task {
			delete(s.active, id)
		}
		s.activeMu.Unlock()
	}()

	servers, grants, err := s.mcpSubmissionContext(id)
	if err != nil {
		cancel()
		_ = task.finish(s, "failed", "", "unable to load MCP execution context", nil)
		return
	}
	s.activeMu.Lock()
	convCtx := s.conversations[id]
	s.activeMu.Unlock()
	submission := python_protocol.TaskSubmission{
		Message:              python_protocol.Message{Version: python_protocol.ProtocolVersion, Type: python_protocol.TypeTaskSubmission},
		TaskID:               id,
		Input:                input,
		Resume:               resume,
		Checkpoint:           checkpoint,
		TimeoutMs:            taskExecutionTimeout.Milliseconds(),
		MCPServers:           servers,
		GrantedApprovals:     grants,
		ConversationID:       convCtx.ConversationID,
		ConversationMessages: convCtx.Messages,
	}
	if err := task.send(submission); err != nil {
		cancel()
		_ = task.finish(s, "failed", "", "unable to submit task to Python runtime", nil)
		return
	}
	// A cancellation can arrive between admission and child registration. Once
	// the child exists, forward that decision so its later result cannot revive
	// a task the supervisor already cancelled.
	if record, err := s.store.GetTaskRecord(id); err == nil &&
		(record.Status == "cancelling" || isTerminal(record.Status)) {
		cancellation := python_protocol.TaskCancellation{
			Message: python_protocol.Message{Version: python_protocol.ProtocolVersion, Type: python_protocol.TypeTaskCancellation},
			TaskID:  id,
			Reason:  "supervisor_request",
		}
		if err := task.send(cancellation); err != nil {
			cancel()
		}
	}

	reader := python_protocol.NewFrameReader(stdout)
	for {
		frame, readErr := python_protocol.ReadFrame(reader)
		if readErr != nil {
			break
		}
		if s.handleProtocolFrame(id, task, frame) {
			break
		}
	}
	cancel()
	// Wait must not run until stdout reads have finished; Cmd.Wait closes the
	// parent's pipe copies and can otherwise drop terminal protocol frames.
	waitDone := make(chan error, 1)
	go func() { waitDone <- cmd.Wait() }()
	select {
	case <-waitDone:
	case <-time.After(processCleanupTimeout):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		<-waitDone
	}
	_ = stdin.Close()
	if stderrText := strings.TrimSpace(stderr.String()); stderrText != "" {
		fmt.Fprintln(os.Stderr, "CRANE: task runtime stderr: "+fmt.Sprint(persistence.RedactMCPValue(stderrText)))
	}
	task.finish(s, "failed", `{"code":"runtime_exited"}`, "Python runtime exited before a terminal result", nil)
}

// mcpSubmissionContext supplies the task child with enabled server configs and
// exact approval grants for that task context. Environment values travel only on
// this private process boundary; they are never written to task events or API
// responses.
func (s *Supervisor) mcpSubmissionContext(taskID string) ([]python_protocol.MCPServerConfigMessage, []string, error) {
	records, err := s.store.ListMCPServers()
	if err != nil {
		return nil, nil, err
	}
	servers := make([]python_protocol.MCPServerConfigMessage, 0, len(records))
	for _, record := range records {
		if !record.Enabled || len(servers) >= 32 {
			continue
		}
		servers = append(servers, python_protocol.MCPServerConfigMessage{
			ServerID:                 record.ID,
			Name:                     record.Name,
			Transport:                record.Transport,
			Command:                  record.Command,
			Args:                     append([]string{}, record.Args...),
			Env:                      record.Env,
			URL:                      record.URL,
			Enabled:                  record.Enabled,
			ConnectTimeoutSeconds:    record.ConnectTimeoutSeconds,
			InvocationTimeoutSeconds: record.InvocationTimeoutSeconds,
			AllowRemoteEndpoint:      record.AllowRemoteEndpoint,
		})
	}
	grants, err := s.store.GrantedApprovalFingerprints(taskID)
	if err != nil {
		return nil, nil, err
	}
	if len(grants) > 128 {
		grants = grants[:128]
	}
	return servers, grants, nil
}

func (s *Supervisor) pythonCommand(ctx context.Context) *exec.Cmd {
	runtimePath := s.pythonRuntimePath
	if runtimePath == "" {
		candidates := runtimeenv.PythonCandidates()
		runtimePath = candidates[0]
	}
	args := append([]string{}, s.pythonArgs...)
	if len(args) == 0 {
		args = []string{"-m", "crane.python_runtime", "--protocol"}
	}
	cmd := exec.CommandContext(ctx, runtimePath, args...)
	runtimeenv.Configure(cmd)
	return cmd
}

func (s *Supervisor) handleProtocolFrame(id string, task *runningTask, frame []byte) bool {
	base, err := python_protocol.Unmarshal(frame)
	if err != nil {
		s.emitEvent(id, "protocol_error", map[string]any{"code": "invalid_message"})
		return true
	}
	switch base.Type {
	case python_protocol.TypeRuntimeIdentity:
		identity, err := python_protocol.DecodeRuntimeIdentity(frame)
		if err == nil {
			ref := fmt.Sprintf("python/%s/pid/%d", identity.RuntimeVersion, identity.PID)
			_ = s.store.SetTaskWorkflowRef(id, ref)
		}
	case python_protocol.TypeTaskState:
		state, err := python_protocol.DecodeTaskState(frame)
		if err != nil {
			s.emitEvent(id, "protocol_error", map[string]any{"code": "invalid_task_state"})
			return true
		}
		version := 0
		if err := s.store.SaveTaskState(id, frame); err == nil {
			if latest, ok := s.store.LatestCheckpointVersion(id); ok {
				version = latest
			}
		}
		s.emitEvent(id, "checkpoint", map[string]any{
			"state": state.State, "step": state.CurrentStep, "version": version,
		})
		if state.State == "running" {
			_, _ = s.store.TransitionTask(id, "running", "starting", "running")
		}
	case python_protocol.TypeToolResult:
		result, err := python_protocol.DecodeToolResult(frame)
		if err != nil {
			s.emitEvent(id, "protocol_error", map[string]any{"code": "invalid_tool_result"})
			return false
		}
		if err := mcpruntime.PersistToolResult(s.store, *result); err != nil {
			s.emitEvent(id, "mcp_result_persistence_error", map[string]any{"code": "tool_result_not_saved"})
			return false
		}
		s.emitEvent(id, "mcp_tool_result", map[string]any{
			"invocation_id":   result.InvocationID,
			"server_id":       result.ServerID,
			"tool_name":       result.ToolName,
			"status":          result.Status,
			"policy_decision": result.PolicyDecision,
		})
	case python_protocol.TypeTaskEvent:
		event, err := python_protocol.DecodeTaskEvent(frame)
		if err != nil {
			return false
		}
		s.emitEvent(id, event.EventType, json.RawMessage(event.Payload))
	case python_protocol.TypeTaskResult:
		result, err := python_protocol.DecodeTaskResult(frame)
		if err != nil {
			s.emitEvent(id, "protocol_error", map[string]any{"code": "invalid_task_result"})
			return true
		}
		if len(result.Checkpoint) > 0 {
			_ = s.store.SaveTaskState(id, result.Checkpoint)
		}
		resultJSON := "{}"
		if len(result.Result) > 0 {
			resultJSON = string(result.Result)
		}
		errorText := result.FailureReason
		if result.Status == "cancelled" && errorText == "" {
			errorText = "task cancelled by supervisor request"
		}
		if result.Status == "failed" && task.stderr != nil {
			if stderrText := strings.TrimSpace(task.stderr.String()); stderrText != "" {
				fmt.Fprintln(os.Stderr, "CRANE: task runtime stderr: "+fmt.Sprint(persistence.RedactMCPValue(stderrText)))
			}
		}
		_ = task.finish(s, result.Status, resultJSON, errorText, result.Checkpoint)
		return true
	case python_protocol.TypeTaskError, python_protocol.TypeErrorResponse:
		code := "python_runtime_error"
		message := "Python runtime reported an execution error"
		if base.Type == python_protocol.TypeTaskError {
			if decoded, err := python_protocol.DecodeTaskError(frame); err == nil {
				code, message = decoded.Code, decoded.Detail
			}
		} else if decoded, err := python_protocol.DecodeErrorResponse(frame); err == nil {
			code, message = decoded.Code, decoded.ErrMessage
		}
		_ = task.finish(s, "failed", fmt.Sprintf(`{"code":%q}`, code), message, nil)
		return true
	}
	return false
}

func (r *runningTask) finish(s *Supervisor, status, result, taskError string, checkpoint []byte) error {
	var finishErr error
	r.once.Do(func() {
		// A pre-execution cancellation may already have made this task terminal.
		// Never let a late runtime result overwrite that supervisor decision.
		if record, err := s.store.GetTaskRecord(r.id); err == nil && isTerminal(record.Status) {
			close(r.done)
			return
		}
		finishErr = s.finalize(r.id, status, result, taskError, checkpoint)
		close(r.done)
	})
	return finishErr
}

func (s *Supervisor) finalize(id, status, result, taskError string, checkpoint []byte) error {
	if len(checkpoint) > 0 && json.Valid(checkpoint) {
		_ = s.store.SaveTaskState(id, checkpoint)
	}
	if err := s.store.CompleteTask(id, status, result, taskError); err != nil {
		return err
	}
	payload := map[string]any{"status": status}
	if status == "failed" && taskError != "" {
		payload["error"] = taskError
	}
	s.emitEvent(id, "task_finished", payload)
	return nil
}

func isTerminal(status string) bool {
	switch status {
	case "succeeded", "failed", "cancelled":
		return true
	default:
		return false
	}
}

func (s *Supervisor) recoverInterruptedTasks() error {
	records, err := s.store.ListTasksByStatus("queued", "starting", "running", "waiting_approval", "cancelling")
	if err != nil {
		return err
	}
	for _, record := range records {
		task, err := s.store.GetTask(record.ID)
		if err != nil {
			continue
		}
		input := task["input"]
		switch record.Status {
		case "queued":
			s.emitEvent(record.ID, "recovery_queued", map[string]any{"status": "queued"})
			s.enqueue(record.ID, input, nil, false)
		case "starting", "running":
			checkpoint, ok := s.store.GetLatestTaskState(record.ID)
			if ok && safeToResume(checkpoint) {
				s.emitEvent(record.ID, "recovery_resumed", map[string]any{"checkpoint": true})
				_, _ = s.store.TransitionTask(record.ID, "starting", "queued", "starting", "running")
				s.enqueue(record.ID, input, checkpoint, true)
				continue
			}
			s.emitEvent(record.ID, "recovery_skipped", map[string]any{"reason": "ambiguous_in_flight_work"})
			_ = s.finalize(record.ID, "failed", `{"code":"ambiguous_recovery"}`,
				"interrupted task was not resumed because an in-flight operation may have side effects", nil)
		case "cancelling":
			s.emitEvent(record.ID, "recovery_skipped", map[string]any{"reason": "unconfirmed_cancellation"})
			_ = s.finalize(record.ID, "failed", `{"code":"cancellation_unconfirmed"}`,
				"cancellation outcome could not be confirmed after restart", nil)
		case "waiting_approval":
			s.emitEvent(record.ID, "recovery_skipped", map[string]any{"reason": "approval_required"})
		}
	}
	return nil
}

func safeToResume(checkpoint []byte) bool {
	var state struct {
		State              string          `json:"state"`
		CurrentStep        string          `json:"current_step"`
		LastToolSideEffect json.RawMessage `json:"last_tool_side_effect"`
		SelectedTool       json.RawMessage `json:"selected_tool"`
		Cancelled          bool            `json:"cancelled"`
	}
	if err := json.Unmarshal(checkpoint, &state); err != nil || state.Cancelled {
		return false
	}
	if hasJSONValue(state.LastToolSideEffect) || hasJSONValue(state.SelectedTool) {
		return false
	}
	switch state.CurrentStep {
	case "", "initialization", "normalizing", "planning", "queued", "starting", "running", "completed", "succeeded":
		return true
	default:
		return false
	}
}

func hasJSONValue(raw json.RawMessage) bool {
	value := strings.TrimSpace(string(raw))
	return value != "" && value != "null" && value != "{}" && value != "[]"
}

// RequestShutdown stops supervisor-owned work with bounded cleanup.
func (s *Supervisor) RequestShutdown() error {
	if s == nil {
		return nil
	}
	if s.cancel != nil {
		s.cancel()
	}
	done := make(chan struct{})
	go func() {
		s.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(s.shutdownTimeout):
		return errors.New("supervisor shutdown timed out waiting for tasks")
	}
	return nil
}

// Close releases supervisor-owned persistence resources.
func (s *Supervisor) Close() error {
	if s == nil || s.store == nil {
		return nil
	}
	return s.store.Close()
}

// HealthCheck performs a basic health check of the default supervision store.
func HealthCheck() error {
	store, err := persistence.NewStore(persistence.DBPath())
	if err != nil {
		return err
	}
	defer store.Close()
	return store.HealthCheck()
}

// Version returns the current supervisor version.
func Version() string { return "0.1.0" }

// Stop marks the supervisor unavailable and cancels supervisor-owned work.
func (s *Supervisor) Stop() {
	if s == nil {
		return
	}
	s.started.Store(false)
	if err := s.RequestShutdown(); err != nil {
		fmt.Fprintln(os.Stderr, "CRANE supervisor shutdown warning:", persistence.RedactMCPValue(err.Error()))
	}
}

type boundedBuffer struct {
	mu    sync.Mutex
	buf   bytes.Buffer
	limit int
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	remaining := b.limit - b.buf.Len()
	if remaining <= 0 {
		return len(p), nil
	}
	if len(p) > remaining {
		p = p[:remaining]
	}
	_, _ = b.buf.Write(p)
	return len(p), nil
}

func (b *boundedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}
