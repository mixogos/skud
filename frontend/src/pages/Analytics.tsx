import { PointerEvent, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { del, fmtDate, fmtSmart, get, Page, parseDate, patch, post, useLoad } from "../api";
import { useApp } from "../ctx";
import { Check, Empty, ErrorBox, Field, Icon, Modal, Seg, useAction, useBlob } from "../ui";

interface Camera { id: number; name: string; state: string; width: number; height: number }
interface Counter {
  id: number; name: string; camera_id: number; comment: string; active: boolean; count_interval: number; roi: number[];
  last_count: number | null; last_date: string | null;
}
interface CounterRecord { id: number; counter_id: number; created_date: string; faces_count: number; faces_bbox: number[][]; has_image: boolean }
interface ChartPoint { date: string; average: number; maximum: number; records: number }
interface Line { id: number; name: string; camera_id: number; comment: string; active: boolean; points: number[][]; count_ab: number; count_ba: number }
interface Crossing { id: number; line_id: number; created_date: string; direction: "AB" | "BA"; face_event_id: number | null; matched_card_id: number | null; matched_card_name: string | null }

const clamp = (v: number) => Math.min(1, Math.max(0, v));
const round = (v: number) => Math.round(v * 10000) / 10000;
const startOfToday = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.toISOString(); };

function usePolling(reload: () => void, ms: number) {
  useEffect(() => {
    const timer = setInterval(reload, ms);
    return () => clearInterval(timer);
  }, [reload, ms]);
}

export default function AnalyticsPage() {
  const { can } = useApp();
  const cameras = useLoad<Page<Camera>>(() => get("/cameras/", { external_detector: false }), []);
  const list = cameras.data?.results ?? [];
  return (
    <div className="col" style={{ gap: 32 }}>
      <ErrorBox>{cameras.error}</ErrorBox>
      {cameras.data && list.length === 0 && (
        <Empty>Аналитика работает на камерах с видеопотоком. Сначала добавьте камеру в разделе <Link to="/videos">«Источники видео»</Link>.</Empty>
      )}
      {can("counter:view") && <Counters cameras={list} />}
      {can("line:view") && <Lines cameras={list} />}
    </div>
  );
}

// --- Кадр камеры с рисованием поверх ---
/** Показывает свежий кадр камеры; перетаскивание мышью сообщает начало и конец в долях кадра. */
function Stage({ camera, onDrag, children }: { camera: Camera | undefined; onDrag?: (from: number[], to: number[]) => void; children?: ReactNode }) {
  const [tick, setTick] = useState(0);
  const online = camera?.state === "ONLINE";
  useEffect(() => {
    if (!online) return;
    const timer = setInterval(() => setTick((t) => t + 1), 3000);
    return () => clearInterval(timer);
  }, [online]);
  const fresh = useBlob(online ? `/cameras/${camera!.id}/snapshot/?t=${tick}` : null);
  const [shown, setShown] = useState<string | null>(null);
  useEffect(() => { if (fresh) setShown(fresh); }, [fresh]);
  useEffect(() => setShown(null), [camera?.id]);

  const start = useRef<number[] | null>(null);
  const point = (e: PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return [round(clamp((e.clientX - r.left) / r.width)), round(clamp((e.clientY - r.top) / r.height))];
  };
  const ratio = camera && camera.width > 0 ? `${camera.width}/${camera.height}` : "16/9";
  return (
    <div
      className="stage" style={{ aspectRatio: ratio, cursor: onDrag ? "crosshair" : undefined }}
      onPointerDown={onDrag && ((e) => { e.currentTarget.setPointerCapture(e.pointerId); start.current = point(e); })}
      onPointerMove={onDrag && ((e) => { if (start.current) onDrag(start.current, point(e)); })}
      onPointerUp={() => { start.current = null; }}
    >
      {shown
        ? <img src={shown} alt={`Кадр с камеры «${camera?.name}»`} draggable={false} />
        : <div className="stage-empty small muted">{camera ? "Камера не на связи — кадра нет. Область можно задать и без него." : "Выберите камеру"}</div>}
      {children}
    </div>
  );
}

function CameraSelect({ cameras, value, onChange }: { cameras: Camera[]; value: number; onChange: (id: number) => void }) {
  return (
    <Field label="Камера">
      <select className="input" value={value} onChange={(e) => onChange(Number(e.target.value))}>
        {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}{c.state === "ONLINE" ? "" : " — не на связи"}</option>)}
      </select>
    </Field>
  );
}

// --- Счётчики ---
function Counters({ cameras }: { cameras: Camera[] }) {
  const { can } = useApp();
  const list = useLoad<Page<Counter>>(() => get("/counters/"), []);
  usePolling(list.reload, 5000);
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const cameraName = (id: number) => cameras.find((c) => c.id === id)?.name ?? `№ ${id}`;
  const current = typeof editing === "number" ? list.data?.results.find((c) => c.id === editing) ?? null : null;
  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Счётчики</h2>
          <div className="small muted">Через равные промежутки времени считают, сколько лиц в кадре камеры или в выбранной области.</div>
        </div>
        {can("counter:add") && cameras.length > 0 && <button className="btn" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новый счётчик</button>}
      </div>
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0
        ? <div className="muted small">Счётчиков нет.</div>
        : <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Название</th><th>Камера</th><th>Область</th><th>Интервал</th><th>Сейчас в кадре</th><th>Последний подсчёт</th></tr></thead>
              <tbody>
                {list.data.results.map((c) => (
                  <tr key={c.id} style={{ cursor: "pointer" }} onClick={() => setEditing(c.id)}>
                    <td><strong>{c.name}</strong>{!c.active && <> <span className="badge">выключен</span></>}</td>
                    <td>{cameraName(c.camera_id)}</td>
                    <td>{c.roi.length ? "часть кадра" : "весь кадр"}</td>
                    <td className="mono">{c.count_interval} с</td>
                    <td className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{c.last_count ?? "—"}</td>
                    <td className="mono small">{c.last_date ? fmtSmart(c.last_date) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>)}
      {editing && (editing === "new" || current) && <CounterModal counter={current} cameras={cameras} onClose={() => setEditing(null)} onSaved={list.reload} />}
    </section>
  );
}

const PERIODS: [string, string, number][] = [["1", "Час", 1], ["24", "Сутки", 24], ["168", "Неделя", 168]];

function CounterChart({ counterId }: { counterId: number }) {
  const [period, setPeriod] = useState("1");
  const hours = Number(period);
  const chart = useLoad<ChartPoint[]>(
    () => get(`/counters/${counterId}/chart/`, { created_date_gte: new Date(Date.now() - hours * 3600e3).toISOString(), points: 60 }),
    [counterId, period],
  );
  usePolling(chart.reload, 10000);
  const points = chart.data ?? [];
  const from = Date.now() - hours * 3600e3;
  const step = (hours * 3600e3) / 60;
  const top = Math.max(1, ...points.map((p) => p.maximum));
  const label = (ms: number) => new Intl.DateTimeFormat("ru-RU", hours > 24 ? { day: "2-digit", month: "2-digit" } : { hour: "2-digit", minute: "2-digit" }).format(new Date(ms));
  return (
    <div className="col" style={{ gap: 8 }}>
      <div className="spread">
        <strong>Наибольшее число лиц в кадре</strong>
        <Seg value={period} onChange={setPeriod} options={PERIODS.map(([v, l]) => [v, l] as [string, string])} />
      </div>
      <ErrorBox>{chart.error}</ErrorBox>
      {points.length === 0
        ? <div className="small muted">За этот период подсчётов не было.</div>
        : <div className="chart">
            <div className="chart-axis mono small muted"><span>{top}</span><span>0</span></div>
            <div className="chart-bars">
              {Array.from({ length: 60 }, (_, i) => {
                const p = points.find((x) => Math.min(59, Math.floor((parseDate(x.date).getTime() - from + step / 2) / step)) === i);
                return (
                  <div key={i} className="chart-col" title={p ? `${fmtDate(p.date)}: до ${p.maximum}, в среднем ${p.average}` : "нет данных"}>
                    {p && <i style={{ height: `${Math.max(p.maximum ? 4 : 1, (p.maximum / top) * 100)}%`, opacity: p.maximum ? 1 : 0.35 }} />}
                  </div>
                );
              })}
            </div>
          </div>}
      {points.length > 0 && <div className="spread mono small muted" style={{ paddingLeft: 28 }}><span>{label(from)}</span><span>{label(Date.now())}</span></div>}
    </div>
  );
}

function RecordShot({ record }: { record: CounterRecord }) {
  const src = useBlob(`/counter-records/${record.id}/image/`);
  return (
    <figure className="shot">
      <div className="stage">
        {src && <img src={src} alt={`Кадр подсчёта: лиц ${record.faces_count}`} />}
        {src && record.faces_bbox.map((b, i) => (
          <div key={i} className="box" style={{ left: `${b[0] * 100}%`, top: `${b[1] * 100}%`, width: `${(b[2] - b[0]) * 100}%`, height: `${(b[3] - b[1]) * 100}%` }} />
        ))}
      </div>
      <figcaption className="spread small"><span className="mono muted">{fmtSmart(record.created_date)}</span><strong className="mono">{record.faces_count}</strong></figcaption>
    </figure>
  );
}

function CounterModal({ counter, cameras, onClose, onSaved }: { counter: Counter | null; cameras: Camera[]; onClose: () => void; onSaved: () => void }) {
  const { can } = useApp();
  const run = useAction();
  const [form, setForm] = useState({
    name: counter?.name ?? "", camera_id: counter?.camera_id ?? cameras[0]?.id ?? 0, comment: counter?.comment ?? "",
    active: counter?.active ?? true, count_interval: counter?.count_interval ?? 10, roi: counter?.roi ?? [],
  });
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const editable = counter ? can("counter:change") : can("counter:add");
  const camera = cameras.find((c) => c.id === form.camera_id);
  const recent = useLoad<Page<CounterRecord>>(
    () => (counter ? get("/counter-records/", { counter: counter.id, count_gte: 1, limit: 6 }) : Promise.resolve({ count: 0, results: [] })),
    [counter?.id],
  );
  const [x1, y1, x2, y2] = form.roi.length === 4 ? form.roi : [0, 0, 1, 1];

  const save = async () => {
    if (await run(() => (counter ? patch(`/counters/${counter.id}/`, form) : post("/counters/", form)), "Счётчик сохранён")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!counter || !confirm(`Удалить счётчик «${counter.name}» вместе с накопленными записями?`)) return;
    if (await run(() => del(`/counters/${counter.id}/`), "Счётчик удалён")) { onSaved(); onClose(); }
  };

  return (
    <Modal wide title={counter ? counter.name : "Новый счётчик"} onClose={onClose} footer={<>
      {counter && can("counter:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim() || !form.camera_id} onClick={save}>{counter ? "Сохранить" : "Создать"}</button>}
    </>}>
      <Stage camera={camera} onDrag={editable ? (a, b) => {
        const roi = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])];
        if (roi[2] - roi[0] >= 0.02 && roi[3] - roi[1] >= 0.02) set({ roi });
      } : undefined}>
        {form.roi.length === 4 && <div className="roi" style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%`, width: `${(x2 - x1) * 100}%`, height: `${(y2 - y1) * 100}%` }} />}
      </Stage>
      <div className="spread small muted">
        <span>{form.roi.length === 4 ? "Считаются лица, центр которых попадает в выделенную область." : "Считаются лица во всём кадре. Чтобы ограничить область, выделите её мышью на кадре."}</span>
        {form.roi.length === 4 && editable && <button className="btn ghost sm" onClick={() => set({ roi: [] })}>Весь кадр</button>}
      </div>
      <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
        <Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!counter} /></Field>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div className="grow"><CameraSelect cameras={cameras} value={form.camera_id} onChange={(id) => set({ camera_id: id })} /></div>
          <div style={{ width: 200 }}>
            <Field label="Считать каждые, секунд">
              <input className="input" type="number" min={1} max={3600} value={form.count_interval} onChange={(e) => set({ count_interval: Number(e.target.value) })} />
            </Field>
          </div>
        </div>
        <Field label="Комментарий"><input className="input" value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Счётчик включён" checked={form.active} onChange={(v) => set({ active: v })} />
      </fieldset>
      {counter && <CounterChart counterId={counter.id} />}
      {counter && recent.data && recent.data.results.length > 0 && (
        <div className="col" style={{ gap: 8 }}>
          <strong>Последние подсчёты с людьми в кадре</strong>
          <div className="shots">{recent.data.results.map((r) => (r.has_image ? <RecordShot key={r.id} record={r} /> : null))}</div>
        </div>
      )}
    </Modal>
  );
}

// --- Линии ---
function Lines({ cameras }: { cameras: Camera[] }) {
  const { can } = useApp();
  const since = useMemo(startOfToday, []);
  const list = useLoad<Page<Line>>(() => get("/lines/", { since }), [since]);
  usePolling(list.reload, 5000);
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const cameraName = (id: number) => cameras.find((c) => c.id === id)?.name ?? `№ ${id}`;
  const current = typeof editing === "number" ? list.data?.results.find((l) => l.id === editing) ?? null : null;
  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Пересечение линий</h2>
          <div className="small muted">Линия в кадре камеры считает проходы людей в обе стороны — например, вошедших и вышедших.</div>
        </div>
        {can("line:add") && cameras.length > 0 && <button className="btn" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новая линия</button>}
      </div>
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0
        ? <div className="muted small">Линий нет.</div>
        : <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Название</th><th>Камера</th><th>Сегодня A → B</th><th>Сегодня B → A</th><th>Разница</th></tr></thead>
              <tbody>
                {list.data.results.map((l) => (
                  <tr key={l.id} style={{ cursor: "pointer" }} onClick={() => setEditing(l.id)}>
                    <td><strong>{l.name}</strong>{!l.active && <> <span className="badge">выключена</span></>}</td>
                    <td>{cameraName(l.camera_id)}</td>
                    <td className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{l.count_ab}</td>
                    <td className="mono" style={{ fontSize: 16, fontWeight: 600 }}>{l.count_ba}</td>
                    <td className="mono">{l.count_ab - l.count_ba > 0 ? "+" : ""}{l.count_ab - l.count_ba}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>)}
      {editing && (editing === "new" || current) && <LineModal line={current} cameras={cameras} onClose={() => setEditing(null)} onSaved={list.reload} />}
    </section>
  );
}

/** Линия и подписи сторон: A — слева, если смотреть от первой точки ко второй, B — справа. */
function LineOverlay({ points }: { points: number[][] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState([1, 1]);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setSize([el.clientWidth || 1, el.clientHeight || 1]));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const [[x1, y1], [x2, y2]] = points;
  // Нормаль в пикселях: в её сторону лежит B.
  const dx = (x2 - x1) * size[0], dy = (y2 - y1) * size[1];
  const length = Math.hypot(dx, dy) || 1;
  const nx = (-dy / length) * 22, ny = (dx / length) * 22;
  const mx = ((x1 + x2) / 2) * size[0], my = ((y1 + y2) / 2) * size[1];
  return (
    <div ref={ref} className="line-layer">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none">
        <line x1={x1 * 100} y1={y1 * 100} x2={x2 * 100} y2={y2 * 100} />
      </svg>
      <i className="dot" style={{ left: `${x1 * 100}%`, top: `${y1 * 100}%` }} />
      <i className="dot" style={{ left: `${x2 * 100}%`, top: `${y2 * 100}%` }} />
      <b className="side" style={{ left: mx - nx, top: my - ny }}>A</b>
      <b className="side" style={{ left: mx + nx, top: my + ny }}>B</b>
    </div>
  );
}

function LineModal({ line, cameras, onClose, onSaved }: { line: Line | null; cameras: Camera[]; onClose: () => void; onSaved: () => void }) {
  const { can } = useApp();
  const run = useAction();
  const [form, setForm] = useState({
    name: line?.name ?? "", camera_id: line?.camera_id ?? cameras[0]?.id ?? 0, comment: line?.comment ?? "",
    active: line?.active ?? true, points: line?.points ?? [[0.5, 0.15], [0.5, 0.85]],
  });
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const editable = line ? can("line:change") : can("line:add");
  const camera = cameras.find((c) => c.id === form.camera_id);
  const crossings = useLoad<Page<Crossing>>(
    () => (line ? get("/line-crossings/", { line: line.id, limit: 12 }) : Promise.resolve({ count: 0, results: [] })),
    [line?.id],
  );
  usePolling(crossings.reload, 5000);

  const save = async () => {
    if (await run(() => (line ? patch(`/lines/${line.id}/`, form) : post("/lines/", form)), "Линия сохранена")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!line || !confirm(`Удалить линию «${line.name}» вместе с записями о пересечениях?`)) return;
    if (await run(() => del(`/lines/${line.id}/`), "Линия удалена")) { onSaved(); onClose(); }
  };

  return (
    <Modal wide title={line ? line.name : "Новая линия"} onClose={onClose} footer={<>
      {line && can("line:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim() || !form.camera_id} onClick={save}>{line ? "Сохранить" : "Создать"}</button>}
    </>}>
      <Stage camera={camera} onDrag={editable ? (a, b) => { if (Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) >= 0.02) set({ points: [a, b] }); } : undefined}>
        <LineOverlay points={form.points} />
      </Stage>
      <div className="small muted">Чтобы провести линию заново, протяните её мышью по кадру. Пересечением считается переход центра лица с одной стороны на другую.</div>
      <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0 }}>
        <Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!line} /></Field>
        <CameraSelect cameras={cameras} value={form.camera_id} onChange={(id) => set({ camera_id: id })} />
        <Field label="Комментарий" hint="Например: A — улица, B — холл"><input className="input" value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Линия включена" checked={form.active} onChange={(v) => set({ active: v })} />
      </fieldset>
      {line && crossings.data && (
        <div className="col" style={{ gap: 8 }}>
          <strong>Последние пересечения <span className="muted mono small">всего {crossings.data.count}</span></strong>
          {crossings.data.results.length === 0
            ? <div className="small muted">Пересечений пока не было.</div>
            : <div className="table-wrap">
                <table className="table">
                  <thead><tr><th>Время</th><th>Направление</th><th>Кто</th></tr></thead>
                  <tbody>
                    {crossings.data.results.map((c) => (
                      <tr key={c.id}>
                        <td className="mono small">{fmtSmart(c.created_date)}</td>
                        <td><span className="badge">{c.direction === "AB" ? "A → B" : "B → A"}</span></td>
                        <td>
                          {c.matched_card_name ?? (c.face_event_id ? "Нет в картотеке" : <span className="muted" title="Лицо не удалось разобрать, либо событие не создано из-за защиты от повторов">человек не определён</span>)}
                          {c.face_event_id && <> · <Link to={`/events?id=${c.face_event_id}`} onClick={onClose}>событие № {c.face_event_id}</Link></>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>}
        </div>
      )}
    </Modal>
  );
}
