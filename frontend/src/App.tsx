import { FormEvent, useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { api, auth, get, post } from "./api";
import { AppProvider, Me, useApp } from "./ctx";
import AnalyticsPage from "./pages/Analytics";
import CardsPage from "./pages/Cards";
import ComparePage from "./pages/Compare";
import EpisodesPage from "./pages/Episodes";
import EventsPage from "./pages/Events";
import MonitorPage from "./pages/Monitor";
import PeoplePage from "./pages/People";
import ReportsPage from "./pages/Reports";
import SettingsPage from "./pages/Settings";
import VideosPage from "./pages/Videos";
import WatchListsPage from "./pages/WatchLists";
import { ErrorBox, Field, Icon, Logo, Spinner } from "./ui";
import { UpdateBanner } from "./update";

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    const sync = () => {
      // Запрос идёт и без токена: в режиме без входа сервер сам впускает как администратора.
      setChecking(true);
      get<Me>("/users/me/").then(setMe, () => setMe(null)).finally(() => setChecking(false));
    };
    sync();
    window.addEventListener("skud-auth", sync);
    return () => window.removeEventListener("skud-auth", sync);
  }, []);

  if (checking) return <div className="login"><Spinner /></div>;
  if (!me) return <Login />;
  return (
    <AppProvider me={me} key={me.id}>
      <Shell />
    </AppProvider>
  );
}

function Login() {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const result = await api("POST", "/auth/login/", { json: { name, password } });
      auth.set(result.token);
    } catch (err: any) {
      setError(err.message);
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="panel" onSubmit={submit} style={{ padding: 24 }}>
        <div className="row" style={{ color: "var(--accent)", gap: 10 }}>
          <Logo size={28} />
          <h1 style={{ color: "var(--text)" }}>СКУД</h1>
        </div>
        <div className="muted">Распознавание лиц и контроль доступа</div>
        <Field label="Логин">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} autoComplete="username" autoFocus required />
        </Field>
        <Field label="Пароль">
          <input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </Field>
        <ErrorBox>{error}</ErrorBox>
        <button className="btn primary" disabled={busy}>{busy ? "Вход…" : "Войти"}</button>
      </form>
    </div>
  );
}

const NAV: { to: string; label: string; icon: string; perm?: string }[] = [
  { to: "/events", label: "События", icon: "events", perm: "faceevent:view" },
  { to: "/episodes", label: "Эпизоды", icon: "episodes", perm: "humanepisode:view" },
  { to: "/people", label: "Люди", icon: "people", perm: "faceevent:view" },
  { to: "/cards", label: "Картотека", icon: "cards", perm: "humancard:view" },
  { to: "/watch-lists", label: "Списки наблюдения", icon: "lists", perm: "watchlist:view" },
  { to: "/videos", label: "Источники видео", icon: "video", perm: "videoarchive:view" },
  { to: "/monitor", label: "Онлайн", icon: "monitor", perm: "camera:view" },
  { to: "/analytics", label: "Аналитика", icon: "chart", perm: "counter:view" },
  { to: "/reports", label: "Отчёты", icon: "report", perm: "report:view" },
  { to: "/compare", label: "Сравнение", icon: "compare" },
  { to: "/settings", label: "Настройки", icon: "settings" },
];

function Shell() {
  const { me, can, feedOnline, pending } = useApp();
  const location = useLocation();
  const [theme, setTheme] = useState(document.documentElement.dataset.theme ?? "dark");
  const items = NAV.filter((n) => !n.perm || can(n.perm));
  const current = items.find((n) => location.pathname.startsWith(n.to));

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("skud.theme", next); } catch { /* хранилище недоступно */ }
    setTheme(next);
  };
  const logout = () => post("/auth/logout/").catch(() => {}).finally(() => auth.set(null));

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><Logo /><span>СКУД</span></div>
        {items.map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `nav${isActive ? " active" : ""}`}>
            <Icon name={n.icon} />
            <span>{n.label}</span>
            {n.to === "/events" && pending > 0 && <span className="count" title="Непринятые события">{pending > 999 ? "999+" : pending}</span>}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          <div style={{ padding: "0 8px", minWidth: 0 }}>
            <div className="ellipsis" style={{ fontWeight: 500 }}>{me.real_name || me.name}</div>
            <div className="small muted ellipsis">{me.role_name}</div>
          </div>
          <div className="row">
            <button className="btn ghost sm" onClick={toggleTheme} title={theme === "dark" ? "Светлая тема" : "Тёмная тема"}>
              <Icon name={theme === "dark" ? "sun" : "moon"} size={16} />
            </button>
            {auth.token && <button className="btn ghost sm" onClick={logout}><Icon name="out" size={16} />Выйти</button>}
          </div>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <h1 className="grow ellipsis">{current?.label ?? "СКУД"}</h1>
          {can("faceevent:view") && (
            <span className={`live${feedOnline ? " on" : ""}`} title="Соединение с лентой событий">
              <i />{feedOnline ? "лента на связи" : "нет связи с лентой"}
            </span>
          )}
        </header>
        <div className="content">
          <UpdateBanner />
          <Routes>
            <Route path="/events" element={<EventsPage />} />
            <Route path="/episodes" element={<EpisodesPage />} />
            <Route path="/people" element={<PeoplePage />} />
            <Route path="/cards" element={<CardsPage />} />
            <Route path="/watch-lists" element={<WatchListsPage />} />
            <Route path="/videos" element={<VideosPage />} />
            <Route path="/monitor" element={<MonitorPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/compare" element={<ComparePage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to={items[0]?.to ?? "/settings"} replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
