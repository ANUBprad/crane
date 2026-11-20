/// <reference types="vite/client" />

type CraneBridgeResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: { message: string; kind: string } };

type CraneAppInfo = {
  name: string;
  version: string;
  platform: string;
};

type CraneRuntimeStatus = {
  available: boolean;
  detail: string;
};

type CraneHealth = {
  version: string;
  supervisor: boolean;
  runtime: CraneRuntimeStatus;
  persistence: boolean;
  ready: boolean;
};

type CraneTask = {
  id: string;
  created_at: string;
  updated_at: string;
  started_at?: string;
  completed_at?: string;
  status: string;
  result?: unknown;
  error?: string;
  workflow_ref?: string;
  has_checkpoint: boolean;
  schema_version?: string;
};

type CraneTaskList = {
  tasks: CraneTask[];
  count: number;
};

type CraneTaskEvent = {
  id: string;
  event: string;
  data: unknown;
};

type CraneEventSubscription = {
  streamId: string;
  unsubscribe: () => void;
};

interface Window {
  craneDesktop?: {
    getAppInfo: () => Promise<CraneAppInfo>;
    getHealth: () => Promise<CraneBridgeResult<CraneHealth>>;
    getTasks: () => Promise<CraneBridgeResult<CraneTaskList>>;
    getTask: (id: string) => Promise<CraneBridgeResult<CraneTask>>;
    submitTask: (id: string, input: string) => Promise<CraneBridgeResult<CraneTask>>;
    cancelTask: (id: string) => Promise<CraneBridgeResult<CraneTask>>;
    subscribeTaskEvents: (
      id: string,
      cursor: number,
      onEvent: (event: CraneTaskEvent) => void,
      onError?: (error: { message: string; kind: string }) => void,
    ) => Promise<CraneBridgeResult<CraneEventSubscription>>;
  };
}
