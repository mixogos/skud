import { useCallback, useState } from "react";
import { del, fmtSmart, get, Page, patch, post, useLoad } from "../api";
import { useApp, useFeed } from "../ctx";
import { Check, Empty, ErrorBox, Field, Icon, Modal, Seg, useAction, useBlob } from "../ui";
import { CameraSelect, Stage } from "./Analytics";

interface Camera { id: number; name: string; state: string; width: number; height: number }
interface Rule {
  id: number; name: string; camera_id: number; rule_type: RuleType; comment: string; active: boolean;
  zone: number[][]; params: Record<string, number>;
}
interface SecEvent {
  id: number; created_date: string; camera_id: number | null; rule_id: number | null; rule_type: RuleType;
  rule_name: string; severity: "alarm" | "warning"; message: string; zone: number[][]; boxes: number[][];
  frame_width: number; frame_height: number; has_image: boolean; acknowledged: boolean;
}
type RuleType = "intrusion" | "loitering" | "crowd" | "tampering" | "abandoned";

const TYPES: Record<RuleType, { label: string; zone: boolean; hint: string }> = {
  intrusion: { label: "Вторжение в зону", zone: true, hint: "Тревога, когда человек входит в запретную зону." },
  loitering: { label: "Праздношатание", zone: true, hint: "Тревога, когда человек задерживается в зоне дольше заданного времени." },
  crowd: { label: "Скопление людей", zone: true, hint: "Тревога, когда в зоне одновременно больше заданного числа людей." },
  tampering: { label: "Саботаж камеры", zone: false, hint: "Тревога при перекрытии обзора, расфокусировке или резком сдвиге камеры — следится весь кадр, зона не нужна." },
  abandoned: { label: "Оставленные предметы", zone: true, hint: "Тревога, когда в зоне появляется неподвижный предмет без человека рядом и остаётся дольше заданного времени." },
};
const round = (v: number) => Math.round(v * 10000) / 10000;

export default function SecurityPage() {
  const { can } = useApp();
  const cameras = useLoad<Page<Camera>>(() => get("/cameras/", { external_detector: false }), []);
  const list = cameras.data?.results ?? [];
  return (
    <div className="col" style={{ gap: 32 }}>
      <ErrorBox>{cameras.error}</ErrorBox>
      {cameras.data && list.length === 0
        ? <Empty>Охранная аналитика работает на камерах с видеопотоком. Сначала добавьте камеру в разделе «Источники видео».</Empty>
        : <>
            <Alarms cameras={list} />
            {can("securityrule:view") && <Rules cameras={list} />}
          </>}
    </div>
  );
}

// --- Лента тревог ---
const FILTERS: [string, string][] = [["", "Все"], ["intrusion", "Вторжение"], ["loitering", "Праздношатание"], ["crowd", "Скопление"], ["tampering", "Саботаж"], ["abandoned", "Предметы"]];

function Alarms({ cameras }: { cameras: Camera[] }) {
  const { can } = useApp();
  const [type, setType] = useState("");
  const [onlyNew, setOnlyNew] = useState(false);
  const query: Record<string, string | number | boolean> = { limit: 60 };
  if (type) query.rule_type = type;
  if (onlyNew) query.acknowledged = false;
  const list = useLoad<Page<SecEvent>>(() => get("/events/security/", query), [type, onlyNew]);
  const reload = list.reload;
  useFeed(useCallback((m) => { if (m.type === "security_alarm") reload(); }, [reload]));
  const cameraName = (id: number | null) => cameras.find((c) => c.id === id)?.name ?? "—";
  const ack = useAction();
  const acknowledge = async (e: SecEvent) => { if (await ack(() => post(`/events/security/${e.id}/acknowledge/`))) reload(); };
  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Охранные тревоги</h2>
          <div className="small muted">Срабатывания правил охранной аналитики в реальном времени.</div>
        </div>
        <Check label="Только непринятые" checked={onlyNew} onChange={setOnlyNew} />
      </div>
      <Seg value={type} onChange={setType} options={FILTERS} />
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0
        ? <div className="muted small" style={{ marginTop: 12 }}>Тревог нет.</div>
        : <div className="alarm-grid">
            {list.data.results.map((e) => (
              <AlarmCard key={e.id} event={e} camera={cameraName(e.camera_id)}
                onAck={can("securityevent:change") ? () => acknowledge(e) : undefined} />
            ))}
          </div>)}
    </section>
  );
}

function AlarmCard({ event, camera, onAck }: { event: SecEvent; camera: string; onAck?: () => void }) {
  const src = useBlob(event.has_image ? `/events/security/${event.id}/image/` : null);
  return (
    <figure className={`alarm-card ${event.severity}${event.acknowledged ? " ack" : ""}`}>
      <div className="stage">
        {src
          ? <img src={src} alt={event.message} />
          : <div className="stage-empty small muted">без снимка</div>}
        {src && event.zone.length >= 3 && <ZoneOverlay points={event.zone} dim />}
        {src && event.boxes.map((b, i) => (
          <div key={i} className="box alarm-box" style={{ left: `${b[0] * 100}%`, top: `${b[1] * 100}%`, width: `${(b[2] - b[0]) * 100}%`, height: `${(b[3] - b[1]) * 100}%` }} />
        ))}
        <span className={`sev-tag ${event.severity}`}>{TYPES[event.rule_type]?.label ?? event.rule_type}</span>
      </div>
      <figcaption className="col" style={{ gap: 4 }}>
        <div className="spread small"><span className="mono muted">{fmtSmart(event.created_date)}</span><span className="muted">{camera}</span></div>
        <div className="small">{event.message}</div>
        <div className="spread">
          {event.acknowledged ? <span className="badge">принято</span> : <span className="badge warn">новое</span>}
          {onAck && !event.acknowledged && <button className="btn ghost sm" onClick={onAck}><Icon name="check" size={14} />Принять</button>}
        </div>
      </figcaption>
    </figure>
  );
}

/** Многоугольник зоны поверх кадра. */
function ZoneOverlay({ points, dim }: { points: number[][]; dim?: boolean }) {
  const attr = points.map((p) => `${p[0] * 100},${p[1] * 100}`).join(" ");
  return (
    <div className={`zone-layer${dim ? " dim" : ""}`}>
      <svg viewBox="0 0 100 100" preserveAspectRatio="none">
        {points.length >= 3 ? <polygon points={attr} /> : <polyline points={attr} fill="none" />}
      </svg>
      {points.map((p, i) => <i key={i} className="dot" style={{ left: `${p[0] * 100}%`, top: `${p[1] * 100}%` }} />)}
    </div>
  );
}

// --- Правила ---
function Rules({ cameras }: { cameras: Camera[] }) {
  const { can } = useApp();
  const list = useLoad<Page<Rule>>(() => get("/security-rules/"), []);
  const [editing, setEditing] = useState<number | "new" | null>(null);
  const cameraName = (id: number) => cameras.find((c) => c.id === id)?.name ?? `№ ${id}`;
  const current = typeof editing === "number" ? list.data?.results.find((r) => r.id === editing) ?? null : null;
  return (
    <section>
      <div className="toolbar">
        <div className="grow">
          <h2>Правила охранной аналитики</h2>
          <div className="small muted">Вторжение в зону, праздношатание, скопление людей и саботаж камеры.</div>
        </div>
        {can("securityrule:add") && cameras.length > 0 && <button className="btn" onClick={() => setEditing("new")}><Icon name="plus" size={15} />Новое правило</button>}
      </div>
      <ErrorBox>{list.error}</ErrorBox>
      {list.data && (list.data.results.length === 0
        ? <div className="muted small">Правил нет. Добавьте первое, чтобы включить охранные тревоги.</div>
        : <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Название</th><th>Тип</th><th>Камера</th><th>Зона</th><th>Параметры</th></tr></thead>
              <tbody>
                {list.data.results.map((r) => (
                  <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => setEditing(r.id)}>
                    <td><strong>{r.name}</strong>{!r.active && <> <span className="badge">выключено</span></>}</td>
                    <td>{TYPES[r.rule_type]?.label ?? r.rule_type}</td>
                    <td>{cameraName(r.camera_id)}</td>
                    <td>{r.rule_type === "tampering" ? "весь кадр" : r.zone.length >= 3 ? `${r.zone.length} точек` : "—"}</td>
                    <td className="mono small">{paramSummary(r)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>)}
      {editing && (editing === "new" || current) && <RuleModal rule={current} cameras={cameras} onClose={() => setEditing(null)} onSaved={list.reload} />}
    </section>
  );
}

function paramSummary(r: Rule): string {
  if (r.rule_type === "loitering") return `задержка ${r.params.dwell_seconds ?? 10} с`;
  if (r.rule_type === "crowd") return `порог ${r.params.min_count ?? 3} чел.`;
  if (r.rule_type === "tampering") return `чувствительность ${r.params.sensitivity ?? 50}`;
  if (r.rule_type === "abandoned") return `задержка ${r.params.dwell_seconds ?? 30} с`;
  return "—";
}

function RuleModal({ rule, cameras, onClose, onSaved }: { rule: Rule | null; cameras: Camera[]; onClose: () => void; onSaved: () => void }) {
  const { can } = useApp();
  const run = useAction();
  const [form, setForm] = useState({
    name: rule?.name ?? "", camera_id: rule?.camera_id ?? cameras[0]?.id ?? 0, rule_type: rule?.rule_type ?? "intrusion" as RuleType,
    comment: rule?.comment ?? "", active: rule?.active ?? true, zone: rule?.zone ?? [] as number[][],
    params: rule?.params ?? {} as Record<string, number>,
  });
  const set = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const setParam = (k: string, v: number) => setForm((f) => ({ ...f, params: { ...f.params, [k]: v } }));
  const editable = rule ? can("securityrule:change") : can("securityrule:add");
  const camera = cameras.find((c) => c.id === form.camera_id);
  const meta = TYPES[form.rule_type];
  const needsZone = meta.zone;
  const zoneOk = !needsZone || form.zone.length >= 3;

  const save = async () => {
    const body = { ...form, zone: needsZone ? form.zone : [] };
    if (await run(() => (rule ? patch(`/security-rules/${rule.id}/`, body) : post("/security-rules/", body)), "Правило сохранено")) { onSaved(); onClose(); }
  };
  const remove = async () => {
    if (!rule || !confirm(`Удалить правило «${rule.name}»?`)) return;
    if (await run(() => del(`/security-rules/${rule.id}/`), "Правило удалено")) { onSaved(); onClose(); }
  };

  return (
    <Modal wide title={rule ? rule.name : "Новое правило"} onClose={onClose} footer={<>
      {rule && can("securityrule:delete") && <button className="btn danger" style={{ marginRight: "auto" }} onClick={remove}><Icon name="trash" size={15} />Удалить</button>}
      <button className="btn" onClick={onClose}>Отмена</button>
      {editable && <button className="btn primary" disabled={!form.name.trim() || !form.camera_id || !zoneOk} onClick={save}>{rule ? "Сохранить" : "Создать"}</button>}
    </>}>
      <fieldset disabled={!editable} className="col" style={{ border: 0, padding: 0, margin: 0, gap: 14 }}>
        <div className="row" style={{ alignItems: "flex-start" }}>
          <div className="grow"><Field label="Название"><input className="input" value={form.name} onChange={(e) => set({ name: e.target.value })} maxLength={256} autoFocus={!rule} /></Field></div>
          <div style={{ width: 230 }}>
            <Field label="Тип правила">
              <select className="input" value={form.rule_type} disabled={!!rule} onChange={(e) => set({ rule_type: e.target.value as RuleType })}>
                {Object.entries(TYPES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </select>
            </Field>
          </div>
        </div>
        <div className="small muted">{meta.hint}</div>
        <CameraSelect cameras={cameras} value={form.camera_id} onChange={(id) => set({ camera_id: id })} />

        {needsZone && (
          <div className="col" style={{ gap: 8 }}>
            <Stage camera={camera} onPoint={editable ? (p) => set({ zone: [...form.zone, [round(p[0]), round(p[1])]] }) : undefined}>
              {form.zone.length > 0 && <ZoneOverlay points={form.zone} />}
            </Stage>
            <div className="spread small muted">
              <span>{form.zone.length < 3 ? "Отмечайте вершины зоны кликами по кадру — нужно минимум три точки." : `Зона из ${form.zone.length} точек. Кликните, чтобы добавить вершину.`}</span>
              {editable && form.zone.length > 0 && <span className="row" style={{ gap: 8 }}>
                <button className="btn ghost sm" onClick={() => set({ zone: form.zone.slice(0, -1) })}>Убрать точку</button>
                <button className="btn ghost sm" onClick={() => set({ zone: [] })}>Очистить</button>
              </span>}
            </div>
          </div>
        )}

        {form.rule_type === "loitering" && (
          <Field label="Задержка в зоне, секунд" hint="Дольше этого времени — тревога">
            <input className="input" type="number" min={1} max={600} value={form.params.dwell_seconds ?? 10} onChange={(e) => setParam("dwell_seconds", Number(e.target.value))} />
          </Field>
        )}
        {form.rule_type === "crowd" && (
          <div className="row">
            <div className="grow"><Field label="Порог числа людей" hint="Столько и больше в зоне — тревога">
              <input className="input" type="number" min={2} max={100} value={form.params.min_count ?? 3} onChange={(e) => setParam("min_count", Number(e.target.value))} />
            </Field></div>
            <div className="grow"><Field label="Выдержка, секунд" hint="Сколько держится скопление до тревоги">
              <input className="input" type="number" min={0} max={60} value={form.params.hold_seconds ?? 3} onChange={(e) => setParam("hold_seconds", Number(e.target.value))} />
            </Field></div>
          </div>
        )}
        {form.rule_type === "tampering" && (
          <Field label={`Чувствительность: ${form.params.sensitivity ?? 50}`} hint="Выше — строже реагирует на перекрытие, расфокус и сдвиг">
            <input type="range" min={1} max={100} value={form.params.sensitivity ?? 50} onChange={(e) => setParam("sensitivity", Number(e.target.value))} style={{ width: "100%" }} />
          </Field>
        )}
        {form.rule_type === "abandoned" && (
          <div className="row">
            <div className="grow"><Field label="Задержка, секунд" hint="Сколько предмет стоит без присмотра до тревоги">
              <input className="input" type="number" min={5} max={600} value={form.params.dwell_seconds ?? 30} onChange={(e) => setParam("dwell_seconds", Number(e.target.value))} />
            </Field></div>
            <div className="grow"><Field label="Мин. размер, % кадра" hint="Предметы мельче этого не считаются">
              <input className="input" type="number" min={0.1} max={20} step={0.1}
                value={Math.round((form.params.min_area ?? 0.004) * 1000) / 10}
                onChange={(e) => setParam("min_area", Number(e.target.value) / 100)} />
            </Field></div>
          </div>
        )}

        <Field label="Комментарий"><input className="input" value={form.comment} onChange={(e) => set({ comment: e.target.value })} maxLength={2048} /></Field>
        <Check label="Правило включено" checked={form.active} onChange={(v) => set({ active: v })} />
      </fieldset>
    </Modal>
  );
}
