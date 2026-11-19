// Общие элементы интерфейса.
import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { blobUrl } from "./api";

// --- Значки (одна толщина линии, 18 px) ---
const paths: Record<string, string> = {
  events: "M4 6h16M4 12h16M4 18h10",
  episodes: "M5 5h6v6H5zM13 5h6v6h-6zM5 13h6v6H5zM13 13h6v6h-6z",
  cards: "M4 5h16v14H4zM9 11a2 2 0 100-4 2 2 0 000 4zM6 16c.6-1.8 1.7-2.5 3-2.5s2.4.7 3 2.5M14 9h4M14 13h4",
  lists: "M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01",
  video: "M4 6h11v12H4zM15 10l5-3v10l-5-3",
  report: "M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h7",
  copy: "M9 9h11v11H9zM5 15V4h11",
  compare: "M8 4v16M16 4v16M3 9l5-5 5 5M21 15l-5 5-5-5",
  settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19 12l2-1-1-3-2 .4-1.5-1.5.4-2-3-1-1 2h-2l-1-2-3 1 .4 2L5.8 8.4 4 8l-1 3 2 1v2l-2 1 1 3 2-.4 1.5 1.5-.4 2 3 1 1-2h2l1 2 3-1-.4-2 1.5-1.5 2 .4 1-3-2-1z",
  close: "M6 6l12 12M18 6L6 18",
  plus: "M12 5v14M5 12h14",
  search: "M11 18a7 7 0 100-14 7 7 0 000 14zM20 20l-4-4",
  check: "M5 12l5 5 9-10",
  user: "M12 12a4 4 0 100-8 4 4 0 000 8zM5 20c1-3.5 3.8-5 7-5s6 1.5 7 5",
  sun: "M12 16a4 4 0 100-8 4 4 0 000 8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5",
  moon: "M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z",
  out: "M10 5H5v14h5M15 8l4 4-4 4M19 12H9",
  trash: "M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13",
  upload: "M12 16V5M7 9l5-5 5 5M5 19h14",
  pause: "M8 5v14M16 5v14",
  play: "M7 5l12 7-12 7z",
  chart: "M4 20V10M10 20V4M16 20v-7M21 20H3",
  monitor: "M3 5h18v12H3zM8 21h8M12 17v4",
  shield: "M12 3l7 3v5c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z",
  people: "M16 17v-1a4 4 0 00-4-4H6a4 4 0 00-4 4v1M9 8a3 3 0 100-6 3 3 0 000 6M22 17v-1a4 4 0 00-3-3.85M16 2.3a4 4 0 010 7.6",
  download: "M12 4v11M8 11l4 4 4-4M5 19h14",
};
export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={paths[name] ?? ""} />
    </svg>
  );
}

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <path d="M8 12V8h4M24 12V8h-4M8 20v4h4M24 20v4h-4" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="16" cy="16" r="3.4" fill="currentColor" />
    </svg>
  );
}

// --- Простые блоки ---
export const Spinner = () => <div className="spinner" role="status" aria-label="Загрузка" />;
export const ErrorBox = ({ children }: { children: ReactNode }) => (children ? <div className="error" role="alert">{children}</div> : null);
export const Empty = ({ children }: { children: ReactNode }) => <div className="empty">{children}</div>;

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

export function Check({ label, checked, onChange, disabled }: { label: ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

export function Seg<T extends string>({ value, onChange, options }: { value: T; onChange: (v: T) => void; options: [T, string][] }) {
  return (
    <div className="seg" role="group">
      {options.map(([key, label]) => (
        <button key={key} type="button" className={key === value ? "on" : ""} aria-pressed={key === value} onClick={() => onChange(key)}>{label}</button>
      ))}
    </div>
  );
}

export function Modal({ title, onClose, children, footer, wide }: { title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true">
        <div className="modal-head">
          <h2 className="ellipsis">{title}</h2>
          <button className="btn ghost sm" onClick={onClose} aria-label="Закрыть"><Icon name="close" size={16} /></button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function Pager({ count, limit, offset, onChange }: { count: number; limit: number; offset: number; onChange: (offset: number) => void }) {
  if (count <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.ceil(count / limit);
  return (
    <div className="row" style={{ justifyContent: "center", marginTop: 16 }}>
      <button className="btn sm" disabled={page <= 1} onClick={() => onChange(offset - limit)}>Назад</button>
      <span className="mono small muted">{page} / {pages}</span>
      <button className="btn sm" disabled={page >= pages} onClick={() => onChange(offset + limit)}>Вперёд</button>
    </div>
  );
}

/** Изображение, защищённое токеном. */
export function Face({ src, size = 64, alt = "", className = "" }: { src: string | null; size?: number | string; alt?: string; className?: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setUrl(null);
    if (src) blobUrl(src).then((u) => alive && setUrl(u), () => {});
    return () => { alive = false; };
  }, [src]);
  const style = typeof size === "number" ? { width: size, height: size } : { width: size };
  if (!url) return <div className={`face ph ${className}`} style={style}><Icon name="user" size={typeof size === "number" ? Math.max(16, size / 3) : 28} /></div>;
  return <img className={`face ${className}`} style={style} src={url} alt={alt} />;
}

export function useBlob(src: string | null): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setUrl(null);
    if (src) blobUrl(src).then((u) => alive && setUrl(u), () => {});
    return () => { alive = false; };
  }, [src]);
  return url;
}

// --- Всплывающие сообщения ---
interface Toast { id: number; text: string; bad: boolean }
const ToastCtx = createContext<(text: string, bad?: boolean) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((text: string, bad = false) => {
    const id = Date.now() + Math.random();
    setItems((list) => [...list.slice(-3), { id, text, bad }]);
    setTimeout(() => setItems((list) => list.filter((t) => t.id !== id)), bad ? 7000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => <div key={t.id} className={`toast${t.bad ? " bad" : ""}`}>{t.text}</div>)}
      </div>
    </ToastCtx.Provider>
  );
}

/** Выполняет действие, показывая ошибку во всплывающем сообщении. Возвращает true при успехе. */
export function useAction() {
  const toast = useToast();
  return useCallback(async (action: () => Promise<any>, success?: string): Promise<boolean> => {
    try {
      await action();
      if (success) toast(success);
      return true;
    } catch (e: any) {
      toast(e.message ?? String(e), true);
      return false;
    }
  }, [toast]);
}
