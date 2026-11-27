import type {
  ConditionReportCreateInput,
  MobileTripSyncPayload,
  SyncQueueItem,
  UserPinInput,
  UserPinItem,
} from "../types";
import { getPrivateOfflineBackend } from "./packStore";

export function generateClientUuid(): string {
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return globalThis.crypto.randomUUID();
  }
  // Deterministic-shape RFC 4122 v4 fallback
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export async function listSyncQueue(ownerId: string): Promise<SyncQueueItem[]> {
  const items = await getPrivateOfflineBackend(ownerId).getAll<SyncQueueItem>("sync_queue");
  return items.sort((a, b) => a.created_at.localeCompare(b.created_at));
}

export function pinsFromSyncQueue(items: readonly SyncQueueItem[]): UserPinItem[] {
  return items
    .filter((item) => item.kind === "pin")
    .map((item) => ({ ...item.payload, id: item.id, created_at: item.created_at }))
    .reverse();
}

async function enqueuePrivateItem(
  ownerId: string,
  item: SyncQueueItem
): Promise<SyncQueueItem> {
  await getPrivateOfflineBackend(ownerId).put("sync_queue", item.id, item);
  return item;
}

export async function enqueueOfflineTrip(
  ownerId: string,
  payload: MobileTripSyncPayload
): Promise<SyncQueueItem> {
  const clientUuid =
    payload.client_uuid ?? payload.client_session_id ?? generateClientUuid();
  return enqueuePrivateItem(ownerId, {
    id: `trip:${clientUuid}`,
    kind: "trip",
    created_at: new Date().toISOString(),
    label: `Trip: ${payload.name}`,
    payload: {
      ...payload,
      client_uuid: clientUuid,
      client_session_id: payload.client_session_id ?? clientUuid,
    },
    last_error: null,
  });
}

export async function enqueueOfflineReport(
  ownerId: string,
  payload: Omit<ConditionReportCreateInput, "photo">
): Promise<SyncQueueItem> {
  const clientUuid = payload.client_uuid ?? generateClientUuid();
  return enqueuePrivateItem(ownerId, {
    id: `report:${clientUuid}`,
    kind: "report",
    created_at: new Date().toISOString(),
    label: `Report: ${payload.report_type.replace(/_/g, " ")}`,
    payload: { ...payload, client_uuid: clientUuid },
    last_error: null,
  });
}

export async function enqueueOfflinePin(
  ownerId: string,
  payload: UserPinInput
): Promise<SyncQueueItem> {
  const clientUuid = payload.client_uuid ?? generateClientUuid();
  return enqueuePrivateItem(ownerId, {
    id: `offline-pin-${clientUuid}`,
    kind: "pin",
    created_at: new Date().toISOString(),
    label: `Pin: ${payload.name}`,
    payload: { ...payload, client_uuid: clientUuid },
    last_error: null,
  });
}

export async function deleteQueuedPin(ownerId: string, id: string): Promise<void> {
  // A pending pin is the queue entry itself, so deleting it also cancels sync.
  if (id.startsWith("offline-pin-")) {
    await getPrivateOfflineBackend(ownerId).delete("sync_queue", id);
  }
}

export interface SyncQueueHandlers {
  syncTrip: (payload: MobileTripSyncPayload) => Promise<void>;
  syncReport: (
    payload: Omit<ConditionReportCreateInput, "photo">
  ) => Promise<void>;
  syncPin: (payload: UserPinInput) => Promise<void>;
}

export interface SyncFlushResult {
  synced: number;
  failed: number;
  remaining: number;
  authRequired: boolean;
}

/**
 * Flush queued offline trips, condition reports, and personal pins sequentially.
 * Retains failed items cleanly for retry; marks `authRequired` if HTTP 401 occurs.
 */
export async function flushSyncQueue(
  ownerId: string,
  signal: AbortSignal,
  handlers: SyncQueueHandlers
): Promise<SyncFlushResult> {
  const backend = getPrivateOfflineBackend(ownerId);
  const items = await listSyncQueue(ownerId);
  let synced = 0;
  let failed = 0;
  let authRequired = false;

  for (const item of items) {
    if (signal.aborted) break;
    // A pin may have been deleted while an earlier item was syncing.
    const pending = await backend.get<SyncQueueItem>("sync_queue", item.id);
    if (signal.aborted) break;
    if (!pending) continue;
    try {
      if (pending.kind === "trip") {
        await handlers.syncTrip(pending.payload);
      } else if (pending.kind === "report") {
        await handlers.syncReport(pending.payload);
      } else if (pending.kind === "pin") {
        await handlers.syncPin(pending.payload);
      }
      await backend.delete("sync_queue", item.id);
      synced += 1;
    } catch (err) {
      if (signal.aborted) break;
      failed += 1;
      const status =
        err && typeof err === "object" && "status" in err
          ? Number((err as { status: unknown }).status)
          : null;
      const message = err instanceof Error ? err.message : "Sync failed";
      authRequired = status === 401 || status === 403 ||
        (status === 409 && message === "The signed-in account has changed");
      await backend.put("sync_queue", item.id, {
        ...pending,
        last_error: message,
      });
      if (authRequired) break;
    }
  }

  const remaining = (await listSyncQueue(ownerId)).length;
  return { synced, failed, remaining, authRequired };
}
