// Клиент HTTP API. Токен хранится в localStorage и передаётся в заголовке Authorization.
import { useCallback, useEffect, useRef, useState } from "react";

const TOKEN_KEY = "skud.token";

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: any) {
    super(message);
  }
}

export const auth = {
  get token(): string | null {
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  },
  set(token: string | null) {
    try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* хранилище недоступно */ }
    blobCache.forEach((p) => p.then((url) => URL.revokeObjectURL(url)).catch(() => {}));
    blobCache.clear();
    window.dispatchEvent(new Event("skud-auth"));
  },
};

type Query = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

function buildUrl(path: string, query?: Query): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, String(v)));
    else params.append(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

function describe(detail: any, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (typeof d === "string" ? d : `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`)).join("; ");
  }
  if (detail && typeof detail.message === "string") return detail.message;
  return `Ошибка сервера (${status})`;
}

interface Options { query?: Query; json?: any; form?: FormData; raw?: boolean }

export async function api<T = any>(method: string, path: string, options: Options = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (auth.token) headers.Authorization = `Token ${auth.token}`;
  let body: BodyInit | undefined;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.json);
  } else if (options.form) body = options.form;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), { method, headers, body });
  } catch {
    throw new ApiError(0, "Нет связи с сервером");
  }
  if (response.status === 401 && path !== "/auth/login/") {
    if (auth.token) auth.set(null);
    throw new ApiError(401, "Сессия завершена — войдите снова");
  }
  if (!response.ok) {
    let detail: any;
    try { detail = (await response.json()).detail; } catch { /* не JSON */ }
    throw new ApiError(response.status, describe(detail, response.status), detail);
  }
  if (options.raw) return response as any;
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const get = <T = any>(path: string, query?: Query) => api<T>("GET", path, { query });
export const post = <T = any>(path: string, json?: any) => api<T>("POST", path, { json: json ?? {} });
export const patch = <T = any>(path: string, json: any) => api<T>("PATCH", path, { json });
export const del = (path: string) => api<void>("DELETE", path);
export const postForm = <T = any>(path: string, form: FormData) => api<T>("POST", path, { form });

// Картинки защищены токеном, поэтому загружаются запросом и показываются через blob-ссылку.
const blobCache = new Map<string, Promise<string>>();
export function blobUrl(path: string): Promise<string> {
  let cached = blobCache.get(path);
  if (!cached) {
    cached = api<Response>("GET", path, { raw: true }).then((r) => r.blob()).then((b) => URL.createObjectURL(b));
    cached.catch(() => blobCache.delete(path));
    if (blobCache.size > 600) blobCache.delete(blobCache.keys().next().value as string);
    blobCache.set(path, cached);
  }
  return cached;
}

export interface Loaded<T> { data: T | undefined; error: string | null; loading: boolean; reload: () => void; setData: (v: T) => void }

/** Загружает данные и перезагружает их при изменении зависимостей. */
export function useLoad<T>(loader: () => Promise<T>, deps: any[]): Loaded<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  useEffect(() => {
    const current = ++seq.current;
    setLoading(true);
    loader().then(
      (value) => { if (current === seq.current) { setData(value); setError(null); setLoading(false); } },
      (e) => { if (current === seq.current) { setError(e.message ?? String(e)); setLoading(false); } },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload, setData };
}

export interface Page<T> { count: number; results: T[] }

/** Время с сервера приходит в UTC без указания пояса. */
export function parseDate(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : value + "Z");
}
const dateTime = new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const timeOnly = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
export function fmtDate(value?: string | null): string {
  return value ? dateTime.format(parseDate(value)) : "—";
}
export function fmtSmart(value: string): string {
  const d = parseDate(value);
  return d.toDateString() === new Date().toDateString() ? timeOnly.format(d) : dateTime.format(d);
}
export const pct = (value?: number | null) => (value == null ? "—" : `${(value * 100).toFixed(1)}%`);
export function fmtDuration(seconds: number): string {
  const s = Math.round(seconds);
  return s >= 3600 ? `${Math.floor(s / 3600)} ч ${Math.floor((s % 3600) / 60)} мин` : s >= 60 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${s} с`;
}
export function fmtSize(bytes: number): string {
  return bytes >= 1 << 30 ? `${(bytes / (1 << 30)).toFixed(1)} ГБ` : bytes >= 1 << 20 ? `${(bytes / (1 << 20)).toFixed(1)} МБ` : `${Math.ceil(bytes / 1024)} КБ`;
}
