// Состояние приложения: текущий пользователь, справочники, лента событий в реальном времени.
import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { auth, get } from "./api";

export interface Me { id: number; name: string; real_name: string; role_name: string; is_superadmin: boolean; permissions: string[] }
export interface WatchList { id: number; name: string; color: string; active: boolean; comment: string; face_threshold: number | null; acknowledge: boolean; notify: boolean; ignore_events: boolean; predefined: boolean; access_policy: "none" | "allow" | "deny"; access_schedule: Record<string, [string, string][]> }
export interface CameraGroup { id: number; name: string; comment: string; active: boolean; deduplicate: boolean; deduplicate_delay: number; face_threshold: number | null; predefined: boolean; access_control: boolean; liveness_check: boolean; detect_people: boolean }
export interface FeedMessage { type: string; data: any }
type FeedHandler = (message: FeedMessage) => void;

interface App {
  me: Me;
  can: (...perms: string[]) => boolean;
  lists: WatchList[];
  listById: Map<number, WatchList>;
  groups: CameraGroup[];
  groupById: Map<number, CameraGroup>;
  reloadRefs: () => void;
  feedOnline: boolean;
  subscribe: (handler: FeedHandler) => () => void;
  pending: number; // непринятые события
  refreshPending: () => void;
}

const Ctx = createContext<App>(null as any);
export const useApp = () => useContext(Ctx);

/** Подписка на ленту; обработчик может меняться между отрисовками. */
export function useFeed(handler: FeedHandler) {
  const { subscribe } = useApp();
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => subscribe((m) => ref.current(m)), [subscribe]);
}

export function AppProvider({ me, children }: { me: Me; children: ReactNode }) {
  const [lists, setLists] = useState<WatchList[]>([]);
  const [groups, setGroups] = useState<CameraGroup[]>([]);
  const [feedOnline, setFeedOnline] = useState(false);
  const [pending, setPending] = useState(0);
  const handlers = useRef(new Set<FeedHandler>());

  const can = useCallback((...perms: string[]) => me.is_superadmin || perms.every((p) => me.permissions.includes(p)), [me]);

  const reloadRefs = useCallback(() => {
    if (can("watchlist:view")) get("/watch-lists/").then((r) => setLists(r.results), () => {});
    if (can("cameragroup:view")) get("/camera-groups/").then((r) => setGroups(r.results), () => {});
  }, [can]);
  useEffect(reloadRefs, [reloadRefs]);

  const refreshPending = useCallback(() => {
    if (can("faceevent:view")) get("/events/faces/", { acknowledged: false, limit: 1 }).then((r) => setPending(r.count), () => {});
  }, [can]);
  useEffect(refreshPending, [refreshPending]);

  const subscribe = useCallback((handler: FeedHandler) => {
    handlers.current.add(handler);
    return () => { handlers.current.delete(handler); };
  }, []);

  // Соединение с лентой: авторизация первым сообщением, переподключение с нарастающей паузой.
  useEffect(() => {
    if (!can("faceevent:view")) return;
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let attempts = 0;
    let closed = false;
    let pendingTimer: number | undefined;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${proto}://${location.host}/ws/events/`);
      socket.onopen = () => socket?.send(JSON.stringify({ token: auth.token }));
      socket.onmessage = (e) => {
        const message: FeedMessage = JSON.parse(e.data);
        if (message.type === "ready") { attempts = 0; setFeedOnline(true); return; }
        handlers.current.forEach((h) => h(message));
        window.clearTimeout(pendingTimer);
        pendingTimer = window.setTimeout(refreshPending, 400);
      };
      socket.onclose = () => {
        setFeedOnline(false);
        if (!closed) timer = window.setTimeout(connect, Math.min(15000, 1000 * 2 ** attempts++));
      };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); window.clearTimeout(pendingTimer); socket?.close(); };
  }, [can, refreshPending]);

  const value = useMemo<App>(() => ({
    me, can, lists, groups, reloadRefs, feedOnline, subscribe, pending, refreshPending,
    listById: new Map(lists.map((l) => [l.id, l])),
    groupById: new Map(groups.map((g) => [g.id, g])),
  }), [me, can, lists, groups, reloadRefs, feedOnline, subscribe, pending, refreshPending]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function ListChips({ ids }: { ids: number[] }) {
  const { listById } = useApp();
  if (!ids.length) return null;
  return (
    <span className="row wrap" style={{ gap: "2px 10px" }}>
      {ids.map((id) => {
        const list = listById.get(id);
        return <span key={id} className="chip"><i style={{ background: list?.color ?? "var(--muted)" }} />{list?.name ?? `Список ${id}`}</span>;
      })}
    </span>
  );
}

/** Короткий звуковой сигнал для списков со звуковым оповещением. */
export function beep() {
  try {
    const AudioCtx = window.AudioContext || (window as any).webkitAudioContext;
    const audio = new AudioCtx();
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.15, audio.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.5);
    osc.connect(gain).connect(audio.destination);
    osc.start();
    osc.stop(audio.currentTime + 0.5);
    osc.onended = () => audio.close();
  } catch { /* звук недоступен */ }
}
